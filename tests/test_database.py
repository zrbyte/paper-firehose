"""Tests for core.database.DatabaseManager.

Exercises the three-database system (all_feeds, history, current) with
realistic data flowing through the same paths as a real pipeline run.
"""

import datetime
import json
import os
import sqlite3
import textwrap
import time
from pathlib import Path

import pytest

from paper_firehose.core.database import DatabaseManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> dict:
    """Return a minimal config dict whose DB paths live under *tmp_path*."""
    return {
        "database": {
            "path": str(tmp_path / "papers.db"),
            "all_feeds_path": str(tmp_path / "all_feed_entries.db"),
            "history_path": str(tmp_path / "matched_entries_history.db"),
        }
    }


def _sample_entry(title="Graphene nanoribbon transport", link="http://arxiv.org/abs/2501.00001",
                   authors=None, published_parsed=None, doi=None):
    """Return a dict shaped like a feedparser entry."""
    entry = {
        "title": title,
        "link": link,
        "summary": f"We study {title.lower()}.",
        "authors": authors or [{"name": "Alice"}, {"name": "Bob"}],
        "id": link,
    }
    if published_parsed:
        entry["published_parsed"] = published_parsed
    else:
        entry["published_parsed"] = time.strptime("2026-03-20", "%Y-%m-%d")
    if doi:
        entry["doi"] = doi
    return entry


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

class TestSchemaInit:
    """DatabaseManager creates all three databases on init."""

    def test_creates_all_three_databases(self, tmp_path):
        cfg = _make_config(tmp_path)
        DatabaseManager(cfg)
        for key in ("path", "all_feeds_path", "history_path"):
            assert Path(cfg["database"][key]).exists()

    def test_all_feeds_schema(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("all_feeds") as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(feed_entries)")}
        assert {"entry_id", "feed_name", "title", "link", "summary", "authors",
                "published_date", "first_seen", "last_seen"} <= cols
        assert "raw_data" not in cols

    def test_history_schema_has_all_columns(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("history") as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(matched_entries)")}
        assert {"entry_id", "feed_name", "topics", "title", "link", "abstract",
                "doi", "llm_summary", "paper_qa_summary", "rank_score"} <= cols

    def test_current_schema_has_composite_pk(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("current") as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
        assert {"id", "topic", "feed_name", "title", "status",
                "rank_score", "paper_qa_summary"} <= cols

    def test_indexes_created(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("all_feeds") as conn:
            indexes = {r[1] for r in conn.execute("PRAGMA index_list(feed_entries)")}
        assert "idx_feed_entries_feed_name" in indexes
        assert "idx_feed_entries_first_seen" in indexes


# ---------------------------------------------------------------------------
# Entry IDs
# ---------------------------------------------------------------------------

class TestComputeEntryId:
    def test_stable_from_link(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        e = {"link": "http://example.com/paper/1"}
        assert db.compute_entry_id(e) == db.compute_entry_id(e)

    def test_strips_query_and_fragment(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        e1 = {"link": "http://example.com/paper/1?ref=rss#sec2"}
        e2 = {"link": "http://example.com/paper/1"}
        assert db.compute_entry_id(e1) == db.compute_entry_id(e2)

    def test_fallback_to_title_date(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        e = {"title": "A paper", "published": "2026-03-20"}
        eid = db.compute_entry_id(e)
        assert len(eid) == 40  # sha1 hex


# ---------------------------------------------------------------------------
# Deduplication (all_feeds DB)
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_new_entry_detected(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        assert db.is_new_entry("Never seen before")

    def test_saved_entry_not_new(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_feed_entry(entry, "Test Feed", eid)
        assert not db.is_new_entry(entry["title"])

    def test_save_feed_entry_preserves_first_seen(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_feed_entry(entry, "Feed A", eid)
        with db.get_connection("all_feeds") as conn:
            first = conn.execute("SELECT first_seen FROM feed_entries WHERE title = ?",
                                 (entry["title"],)).fetchone()["first_seen"]
        # Save again — first_seen should not change
        db.save_feed_entry(entry, "Feed A", eid)
        with db.get_connection("all_feeds") as conn:
            second = conn.execute("SELECT first_seen FROM feed_entries WHERE title = ?",
                                  (entry["title"],)).fetchone()["first_seen"]
        assert first == second


# ---------------------------------------------------------------------------
# Current DB (papers.db)
# ---------------------------------------------------------------------------

class TestCurrentDB:
    def test_save_and_retrieve(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "arXiv cond-mat", "perovskites", eid)

        rows = db.get_current_entries(topic="perovskites")
        assert len(rows) == 1
        assert rows[0]["title"] == entry["title"]
        assert rows[0]["status"] == "filtered"

    def test_filter_by_status(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        assert len(db.get_current_entries(status="filtered")) == 1
        assert len(db.get_current_entries(status="ranked")) == 0

    def test_clear_current_db(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        db.clear_current_db()
        assert len(db.get_current_entries()) == 0

    def test_same_entry_different_topics(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic_a", eid)
        db.save_current_entry(entry, "Feed", "topic_b", eid)
        assert len(db.get_current_entries(topic="topic_a")) == 1
        assert len(db.get_current_entries(topic="topic_b")) == 1


# ---------------------------------------------------------------------------
# Rank updates
# ---------------------------------------------------------------------------

class TestRankUpdates:
    def test_update_entry_rank(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        db.update_entry_rank(eid, "topic", 0.87)

        rows = db.get_current_entries(topic="topic")
        assert abs(rows[0]["rank_score"] - 0.87) < 1e-6
        assert rows[0]["status"] == "ranked"

    def test_update_entry_rank_with_reasoning(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        db.update_entry_rank(eid, "topic", 0.5, reasoning="keyword match")

        rows = db.get_current_entries(topic="topic")
        assert rows[0]["rank_reasoning"] == "keyword match"

    def test_update_history_rank_keeps_highest(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "topic", eid)

        db.update_history_rank(eid, 0.5)
        db.update_history_rank(eid, 0.9)
        db.update_history_rank(eid, 0.3)  # lower — should be ignored

        with db.get_connection("history") as conn:
            row = conn.execute("SELECT rank_score FROM matched_entries WHERE entry_id = ?",
                               (eid,)).fetchone()
        assert abs(row["rank_score"] - 0.9) < 1e-6


# ---------------------------------------------------------------------------
# History DB — topic merging
# ---------------------------------------------------------------------------

class TestHistoryTopicMerge:
    def test_new_entry_gets_single_topic(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "perovskites", eid)

        with db.get_connection("history") as conn:
            row = conn.execute("SELECT topics FROM matched_entries WHERE entry_id = ?",
                               (eid,)).fetchone()
        assert row["topics"] == "perovskites"

    def test_second_topic_merged(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "perovskites", eid)
        db.save_matched_entry(entry, "Feed", "catalysis", eid)

        with db.get_connection("history") as conn:
            row = conn.execute("SELECT topics FROM matched_entries WHERE entry_id = ?",
                               (eid,)).fetchone()
        assert "catalysis" in row["topics"]
        assert "perovskites" in row["topics"]

    def test_duplicate_topic_not_appended(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "topic", eid)
        db.save_matched_entry(entry, "Feed", "topic", eid)

        with db.get_connection("history") as conn:
            row = conn.execute("SELECT topics FROM matched_entries WHERE entry_id = ?",
                               (eid,)).fetchone()
        assert row["topics"] == "topic"


# ---------------------------------------------------------------------------
# Batch abstract updates
# ---------------------------------------------------------------------------

class TestBatchAbstracts:
    def test_update_abstracts_batch(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entries = [_sample_entry(title=f"Paper {i}", link=f"http://example.com/{i}")
                   for i in range(3)]
        eids = []
        for e in entries:
            eid = db.compute_entry_id(e)
            eids.append(eid)
            db.save_current_entry(e, "Feed", "topic", eid)

        updates = [
            ("Abstract for paper 0", "10.1234/a", eids[0], "topic"),
            ("Abstract for paper 1", "10.1234/b", eids[1], "topic"),
        ]
        count = db.update_abstracts_batch(updates)
        assert count == 2

        rows = db.get_current_entries(topic="topic")
        abstracts = {r["title"]: r["abstract"] for r in rows}
        assert abstracts["Paper 0"] == "Abstract for paper 0"
        assert abstracts["Paper 2"] is None  # not updated

    def test_empty_batch_returns_zero(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        assert db.update_abstracts_batch([]) == 0

    def test_update_history_abstracts_batch(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "topic", eid)

        count = db.update_history_abstracts_batch([("Full abstract text", "10.5678/x", eid)])
        assert count == 1

        with db.get_connection("history") as conn:
            row = conn.execute("SELECT abstract, doi FROM matched_entries WHERE entry_id = ?",
                               (eid,)).fetchone()
        assert row["abstract"] == "Full abstract text"
        assert row["doi"] == "10.5678/x"


# ---------------------------------------------------------------------------
# Query builder (get_entries_by_criteria)
# ---------------------------------------------------------------------------

class TestQueryBuilder:
    def _populate(self, db, tmp_path):
        for i, (score, doi) in enumerate([(0.9, "10.1/a"), (0.5, None), (0.2, "10.1/c")]):
            e = _sample_entry(title=f"P{i}", link=f"http://ex.com/{i}")
            eid = db.compute_entry_id(e)
            db.save_current_entry(e, "Feed", "topic", eid)
            db.update_entry_rank(eid, "topic", score)
            if doi:
                with db.get_connection("current", row_factory=False) as conn:
                    conn.execute("UPDATE entries SET doi = ? WHERE id = ?", (doi, eid))

    def test_filter_by_min_rank(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        self._populate(db, tmp_path)
        rows = db.get_entries_by_criteria(topic="topic", min_rank=0.5)
        assert len(rows) == 2  # 0.9 and 0.5

    def test_filter_has_doi(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        self._populate(db, tmp_path)
        rows = db.get_entries_by_criteria(topic="topic", has_doi=True)
        assert len(rows) == 2

    def test_filter_no_doi(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        self._populate(db, tmp_path)
        rows = db.get_entries_by_criteria(topic="topic", has_doi=False)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# iter_targets / iter_history_entries
# ---------------------------------------------------------------------------

class TestIterators:
    def test_iter_targets_yields_all(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        db.update_entry_rank(eid, "topic", 0.8)

        rows = list(db.iter_targets(topic="topic"))
        assert len(rows) == 1
        assert rows[0]["id"] == eid

    def test_iter_targets_respects_min_rank(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        for i, score in enumerate([0.9, 0.3]):
            e = _sample_entry(title=f"P{i}", link=f"http://ex.com/{i}")
            eid = db.compute_entry_id(e)
            db.save_current_entry(e, "Feed", "topic", eid)
            db.update_entry_rank(eid, "topic", score)

        rows = list(db.iter_targets(topic="topic", min_rank=0.5))
        assert len(rows) == 1

    def test_iter_history_entries(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_matched_entry(entry, "Feed", "topic", eid)

        rows = list(db.iter_history_entries([eid]))
        assert len(rows) == 1
        assert rows[0]["entry_id"] == eid

    def test_iter_history_entries_empty_ids(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        assert list(db.iter_history_entries([])) == []


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

class TestPurge:
    def test_purge_removes_todays_entries(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        today = time.strptime(datetime.date.today().isoformat(), "%Y-%m-%d")
        entry = _sample_entry(published_parsed=today)
        eid = db.compute_entry_id(entry)
        db.save_feed_entry(entry, "Feed", eid)
        db.save_matched_entry(entry, "Feed", "topic", eid)
        db.save_current_entry(entry, "Feed", "topic", eid)

        db.purge_old_entries(days=1)

        with db.get_connection("all_feeds") as conn:
            assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 0
        with db.get_connection("history") as conn:
            assert conn.execute("SELECT COUNT(*) FROM matched_entries").fetchone()[0] == 0
        with db.get_connection("current") as conn:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0

    def test_purge_leaves_old_entries(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        old_date = time.strptime("2025-01-01", "%Y-%m-%d")
        entry = _sample_entry(published_parsed=old_date)
        eid = db.compute_entry_id(entry)
        db.save_feed_entry(entry, "Feed", eid)

        db.purge_old_entries(days=1)

        with db.get_connection("all_feeds") as conn:
            assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

class TestBackups:
    def test_backup_creates_files(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        backups = db.backup_important_databases()
        assert "all_feeds" in backups
        assert "history" in backups
        assert Path(backups["all_feeds"]).exists()

    def test_rotate_keeps_one(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        for _ in range(5):
            db.backup_important_databases()
        import glob as globmod
        pattern = str(tmp_path / "all_feed_entries.*.backup.db")
        assert len(globmod.glob(pattern)) <= 1


# ---------------------------------------------------------------------------
# Connection context manager
# ---------------------------------------------------------------------------

class TestConnectionManager:
    def test_autocommit_on_success(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("current", row_factory=False) as conn:
            conn.execute("INSERT INTO entries (id, topic, feed_name, title, link, status) "
                         "VALUES ('x','t','f','Title','http://x','filtered')")
        # Should be persisted
        with db.get_connection("current") as conn:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1

    def test_rollback_on_error(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with pytest.raises(RuntimeError):
            with db.get_connection("current", row_factory=False) as conn:
                conn.execute("INSERT INTO entries (id, topic, feed_name, title, link, status) "
                             "VALUES ('x','t','f','Title','http://x','filtered')")
                raise RuntimeError("boom")
        # Should NOT be persisted
        with db.get_connection("current") as conn:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0

    def test_row_factory_returns_dicts(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry()
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        with db.get_connection("current", row_factory=True) as conn:
            row = conn.execute("SELECT * FROM entries LIMIT 1").fetchone()
        assert row["title"] == entry["title"]


# ---------------------------------------------------------------------------
# Date formatting
# ---------------------------------------------------------------------------

class TestDateFormatting:
    def test_struct_time(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry(published_parsed=time.strptime("2026-03-15", "%Y-%m-%d"))
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        rows = db.get_current_entries()
        assert rows[0]["published_date"] == "2026-03-15"

    def test_string_date_iso(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = {"title": "Test", "link": "http://x", "published": "2026-03-15",
                 "summary": "s"}
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        rows = db.get_current_entries()
        assert rows[0]["published_date"] == "2026-03-15"

    def test_fallback_to_today(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = {"title": "Test", "link": "http://x", "summary": "s"}
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        rows = db.get_current_entries()
        assert rows[0]["published_date"] == datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# Author extraction
# ---------------------------------------------------------------------------

class TestAuthorExtraction:
    def test_authors_list(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = _sample_entry(authors=[{"name": "Alice"}, {"name": "Bob"}])
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        rows = db.get_current_entries()
        assert rows[0]["authors"] == "Alice, Bob"

    def test_author_string_fallback(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entry = {"title": "Test", "link": "http://x", "summary": "s", "author": "Charlie"}
        eid = db.compute_entry_id(entry)
        db.save_current_entry(entry, "Feed", "topic", eid)
        rows = db.get_current_entries()
        assert rows[0]["authors"] == "Charlie"


# ---------------------------------------------------------------------------
# Entry IDs keep identifying query parameters (issue #88)
# ---------------------------------------------------------------------------

class TestEntryIdQueryParams:
    def _sha1(self, s):
        import hashlib
        return hashlib.sha1(s.encode("utf-8")).hexdigest()

    @pytest.mark.parametrize("link", [
        "https://www.science.org/doi/abs/10.1126/science.aeh7895?af=R",
        "https://www.cell.com/joule/fulltext/S2542-4351(26)00430-7?rss=yes",
        "https://example.com/paper/1?utm_source=rss&utm_medium=feed",
        "https://example.com/paper/1?ref=rss#sec2",
    ])
    def test_tracking_only_query_keeps_old_id(self, tmp_path, link):
        """Links whose query is pure tracking hash as before (query dropped)."""
        db = DatabaseManager(_make_config(tmp_path))
        bare = link.split("?")[0]
        assert db.compute_entry_id({"link": link}) == self._sha1(bare)

    @pytest.mark.parametrize("a,b", [
        ("https://opg.optica.org/abstract.cfm?URI=optica-13-12-2495",
         "https://opg.optica.org/abstract.cfm?URI=optica-13-12-2501"),
        ("https://quantum-journal.org/?post_type=paper&p=19280",
         "https://quantum-journal.org/?post_type=paper&p=19281"),
    ])
    def test_identifying_query_gives_distinct_ids(self, tmp_path, a, b):
        db = DatabaseManager(_make_config(tmp_path))
        assert db.compute_entry_id({"link": a}) != db.compute_entry_id({"link": b})

    def test_tracking_params_removed_identifying_params_kept(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with_tracking = {"link": "https://opg.optica.org/abstract.cfm?URI=optica-13-12-2495&af=R&utm_campaign=x"}
        without = {"link": "https://opg.optica.org/abstract.cfm?URI=optica-13-12-2495"}
        assert db.compute_entry_id(with_tracking) == db.compute_entry_id(without)

    def test_entries_differing_only_in_query_stored_separately(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        e1 = _sample_entry(title="Paper one", link="https://opg.optica.org/abstract.cfm?URI=optica-13-12-2495")
        e2 = _sample_entry(title="Paper two", link="https://opg.optica.org/abstract.cfm?URI=optica-13-12-2501")
        for e in (e1, e2):
            eid = db.compute_entry_id(e)
            db.save_current_entry(e, "Optica", "test_topic", eid)
            db.save_matched_entry(e, "Optica", "test_topic", eid)
            db.save_feed_entry(e, "Optica", eid)
        with db.get_connection("current") as conn:
            assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
        with db.get_connection("history") as conn:
            assert conn.execute("SELECT COUNT(*) FROM matched_entries").fetchone()[0] == 2
        with db.get_connection("all_feeds") as conn:
            assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Dedup lookups use an index and batch connections (issue #89)
# ---------------------------------------------------------------------------

class TestDedupPerformance:
    def test_title_index_exists_and_is_used(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        with db.get_connection("all_feeds", row_factory=False) as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='feed_entries'")}
            assert "idx_feed_entries_title" in names
            plan = " ".join(str(r) for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM feed_entries WHERE title = ?", ("x",)))
            assert "idx_feed_entries_title" in plan

    def test_index_added_to_existing_database(self, tmp_path):
        path = tmp_path / "all_feed_entries.db"
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE feed_entries (
            entry_id TEXT PRIMARY KEY, feed_name TEXT NOT NULL, title TEXT NOT NULL,
            link TEXT NOT NULL, summary TEXT, authors TEXT, published_date TEXT,
            first_seen TEXT DEFAULT (datetime('now')), last_seen TEXT DEFAULT (datetime('now')),
            UNIQUE(feed_name, entry_id))""")
        conn.commit()
        conn.close()
        DatabaseManager(_make_config(tmp_path))
        conn = sqlite3.connect(path)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_feed_entries_title" in names

    def test_filter_new_titles(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        seen = _sample_entry(title="Already seen")
        db.save_feed_entry(seen, "Feed", db.compute_entry_id(seen))
        assert db.filter_new_titles(["Already seen", "Brand new", "Brand new"]) == {"Brand new"}
        assert db.filter_new_titles([]) == set()

    def test_save_feed_entries_batch(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        entries = [_sample_entry(title=f"Paper {i}", link=f"http://example.com/{i}") for i in range(5)]
        db.save_feed_entries([(e, "Feed", db.compute_entry_id(e)) for e in entries])
        with db.get_connection("all_feeds") as conn:
            assert conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0] == 5
        assert db.filter_new_titles([e["title"] for e in entries]) == set()

    def test_backup_once_per_day_reuses_todays_backup(self, tmp_path):
        import glob as globmod
        db = DatabaseManager(_make_config(tmp_path))
        first = db.backup_important_databases()
        second = db.backup_important_databases(once_per_day=True)
        assert second == first
        assert len(globmod.glob(str(tmp_path / "all_feed_entries.*.backup.db"))) == 1

    def test_backup_once_per_day_ignores_older_backups(self, tmp_path):
        db = DatabaseManager(_make_config(tmp_path))
        stale = tmp_path / "all_feed_entries.20000101-000000.backup.db"
        stale.write_bytes(b"")
        backups = db.backup_important_databases(once_per_day=True)
        assert Path(backups["all_feeds"]) != stale
        assert Path(backups["all_feeds"]).exists()
        assert not stale.exists()  # rotated away
