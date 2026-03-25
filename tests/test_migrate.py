"""Tests for the database migration command (commands/migrate_db.py)."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from paper_firehose.commands.migrate_db import (
    _table_has_column,
    _rebuild_without_column,
    _archive_raw_data,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> dict:
    return {
        "database": {
            "path": str(tmp_path / "papers.db"),
            "all_feeds_path": str(tmp_path / "all_feed_entries.db"),
            "history_path": str(tmp_path / "matched_entries_history.db"),
        }
    }


def _write_config_yaml(tmp_path: Path) -> str:
    """Write a minimal config.yaml and return its path."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    topics_dir = cfg_dir / "topics"
    topics_dir.mkdir()
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(f"""\
database:
  path: {tmp_path / 'papers.db'}
  all_feeds_path: {tmp_path / 'all_feed_entries.db'}
  history_path: {tmp_path / 'matched_entries_history.db'}
feeds: []
priority_journals: []
""")
    return str(cfg_path)


def _create_legacy_all_feeds(path: str) -> None:
    """Create an all_feed_entries.db WITH raw_data column (legacy schema)."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE feed_entries (
            entry_id TEXT PRIMARY KEY,
            feed_name TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT,
            authors TEXT,
            published_date TEXT,
            first_seen TEXT,
            last_seen TEXT,
            raw_data TEXT,
            UNIQUE(feed_name, entry_id)
        )
    """)
    conn.execute("""
        INSERT INTO feed_entries (entry_id, feed_name, title, link, raw_data)
        VALUES ('id1', 'arxiv', 'Paper A', 'http://a', ?)
    """, (json.dumps({"title": "Paper A", "extra": "data"}),))
    conn.execute("""
        INSERT INTO feed_entries (entry_id, feed_name, title, link, raw_data)
        VALUES ('id2', 'arxiv', 'Paper B', 'http://b', ?)
    """, (json.dumps({"title": "Paper B"}),))
    conn.commit()
    conn.close()


def _create_legacy_history(path: str) -> None:
    """Create a matched_entries_history.db WITH raw_data column."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE matched_entries (
            entry_id TEXT PRIMARY KEY,
            feed_name TEXT NOT NULL,
            topics TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT,
            authors TEXT,
            abstract TEXT,
            doi TEXT,
            published_date TEXT,
            matched_date TEXT,
            raw_data TEXT,
            llm_summary TEXT,
            paper_qa_summary TEXT,
            rank_score REAL
        )
    """)
    conn.execute("""
        INSERT INTO matched_entries (entry_id, feed_name, topics, title, link, raw_data)
        VALUES ('h1', 'arxiv', 'perovskites', 'History Paper', 'http://h1', '{"key":"val"}')
    """)
    conn.commit()
    conn.close()


def _create_legacy_current(path: str) -> None:
    """Create a papers.db WITH raw_data column."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE entries (
            id TEXT NOT NULL,
            topic TEXT NOT NULL,
            feed_name TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            summary TEXT,
            authors TEXT,
            abstract TEXT,
            doi TEXT,
            published_date TEXT,
            discovered_date TEXT,
            status TEXT DEFAULT 'new',
            rank_score REAL,
            rank_reasoning TEXT,
            llm_summary TEXT,
            paper_qa_summary TEXT,
            raw_data TEXT,
            PRIMARY KEY (id, topic)
        )
    """)
    conn.execute("""
        INSERT INTO entries (id, topic, feed_name, title, link, raw_data)
        VALUES ('c1', 'perovskites', 'arxiv', 'Current Paper', 'http://c1', '{"cur":"rent"}')
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_table_has_column(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
        assert _table_has_column(conn, "t", "a")
        assert not _table_has_column(conn, "t", "c")
        conn.close()

    def test_rebuild_without_column(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a TEXT PRIMARY KEY, b TEXT, c TEXT)")
        conn.execute("INSERT INTO t VALUES ('1', 'hello', 'world')")
        conn.commit()

        _rebuild_without_column(conn, "t", "c")

        cols = [r[1] for r in conn.execute("PRAGMA table_info(t)")]
        assert "c" not in cols
        assert "a" in cols and "b" in cols

        row = conn.execute("SELECT a, b FROM t").fetchone()
        assert row == ("1", "hello")
        conn.close()

    def test_rebuild_noop_if_column_absent(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (a TEXT, b TEXT)")
        conn.execute("INSERT INTO t VALUES ('x', 'y')")
        conn.commit()
        _rebuild_without_column(conn, "t", "nonexistent")
        row = conn.execute("SELECT * FROM t").fetchone()
        assert row == ("x", "y")
        conn.close()

    def test_archive_raw_data(self, tmp_path):
        db_path = str(tmp_path / "src.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (entry_id TEXT PRIMARY KEY, raw_data TEXT)")
        conn.execute("INSERT INTO t VALUES ('a', 'data_a')")
        conn.execute("INSERT INTO t VALUES ('b', NULL)")
        conn.commit()

        archive_path = str(tmp_path / "archive.db")
        archive_conn = sqlite3.connect(archive_path)
        archive_conn.execute("""
            CREATE TABLE raw_data_archive (
                entry_id TEXT NOT NULL,
                db_source TEXT NOT NULL,
                raw_data TEXT,
                PRIMARY KEY (entry_id, db_source)
            )
        """)
        archive_conn.commit()

        count = _archive_raw_data(conn, archive_conn, "t", "entry_id", "test")
        assert count == 1  # only non-NULL

        rows = archive_conn.execute("SELECT * FROM raw_data_archive").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("a", "test", "data_a")

        conn.close()
        archive_conn.close()


# ---------------------------------------------------------------------------
# Full migration
# ---------------------------------------------------------------------------

class TestMigration:
    def test_full_migration(self, tmp_path):
        config_path = _write_config_yaml(tmp_path)
        cfg = _make_config(tmp_path)

        _create_legacy_all_feeds(cfg["database"]["all_feeds_path"])
        _create_legacy_history(cfg["database"]["history_path"])
        _create_legacy_current(cfg["database"]["path"])

        run(config_path)

        # Verify raw_data removed from all DBs
        for db_path, table in [
            (cfg["database"]["all_feeds_path"], "feed_entries"),
            (cfg["database"]["history_path"], "matched_entries"),
            (cfg["database"]["path"], "entries"),
        ]:
            conn = sqlite3.connect(db_path)
            assert not _table_has_column(conn, table, "raw_data"), f"{table} still has raw_data"
            conn.close()

        # Verify archive created with data
        archive_path = tmp_path / "raw_archive.db"
        assert archive_path.exists()
        conn = sqlite3.connect(str(archive_path))
        rows = conn.execute("SELECT COUNT(*) FROM raw_data_archive").fetchone()[0]
        assert rows == 4  # 2 from all_feeds + 1 from history + 1 from current
        conn.close()

        # Verify backups created
        assert (tmp_path / "all_feed_entries.pre-migration.db").exists()
        assert (tmp_path / "matched_entries_history.pre-migration.db").exists()
        assert (tmp_path / "papers.pre-migration.db").exists()

        # Verify data preserved (minus raw_data)
        conn = sqlite3.connect(cfg["database"]["all_feeds_path"])
        count = conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0]
        assert count == 2
        conn.close()

    def test_skip_archive(self, tmp_path):
        config_path = _write_config_yaml(tmp_path)
        cfg = _make_config(tmp_path)
        _create_legacy_all_feeds(cfg["database"]["all_feeds_path"])

        run(config_path, skip_archive=True)

        assert not (tmp_path / "raw_archive.db").exists()
        conn = sqlite3.connect(cfg["database"]["all_feeds_path"])
        assert not _table_has_column(conn, "feed_entries", "raw_data")
        conn.close()

    def test_dry_run(self, tmp_path):
        config_path = _write_config_yaml(tmp_path)
        cfg = _make_config(tmp_path)
        _create_legacy_all_feeds(cfg["database"]["all_feeds_path"])
        original_size = os.path.getsize(cfg["database"]["all_feeds_path"])

        run(config_path, dry_run=True)

        # DB should be unchanged
        assert os.path.getsize(cfg["database"]["all_feeds_path"]) == original_size
        conn = sqlite3.connect(cfg["database"]["all_feeds_path"])
        assert _table_has_column(conn, "feed_entries", "raw_data")
        conn.close()
        # No backup
        assert not (tmp_path / "all_feed_entries.pre-migration.db").exists()

    def test_idempotent(self, tmp_path):
        """Running migrate twice should not fail."""
        config_path = _write_config_yaml(tmp_path)
        cfg = _make_config(tmp_path)
        _create_legacy_all_feeds(cfg["database"]["all_feeds_path"])
        _create_legacy_history(cfg["database"]["history_path"])
        _create_legacy_current(cfg["database"]["path"])

        run(config_path)
        # Second run: raw_data already gone, should succeed
        run(config_path)

        conn = sqlite3.connect(cfg["database"]["all_feeds_path"])
        assert not _table_has_column(conn, "feed_entries", "raw_data")
        count = conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0]
        assert count == 2
        conn.close()

    def test_pragmas_applied(self, tmp_path):
        config_path = _write_config_yaml(tmp_path)
        cfg = _make_config(tmp_path)
        _create_legacy_all_feeds(cfg["database"]["all_feeds_path"])

        run(config_path, skip_archive=True)

        conn = sqlite3.connect(cfg["database"]["all_feeds_path"])
        auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        assert auto_vacuum == 2  # INCREMENTAL
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        assert page_size == 8192
        conn.close()
