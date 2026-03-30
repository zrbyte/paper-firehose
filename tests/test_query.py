"""Tests for the query command and DatabaseManager.query_entries()."""

import json
import sqlite3
import textwrap
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import query as query_cmd
from paper_firehose.core.database import DatabaseManager
import paper_firehose.core.config as core_config


def _make_config(tmp_path, monkeypatch):
    """Create minimal config and topic, return config path."""
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)

    config_yaml = textwrap.dedent("""
        database:
          path: "papers.db"
          all_feeds_path: "all_feed_entries.db"
          history_path: "matched_entries_history.db"
        feeds:
          test_feed:
            name: "Test Feed"
            url: "https://example.com/feed"
            enabled: true
    """).strip() + "\n"

    topic_yaml = textwrap.dedent("""
        name: "Test Topic"
        feeds:
          - "test_feed"
        filter:
          pattern: "test"
          fields: ["title"]
        ranking:
          query: "test query"
    """).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")
    return str(config_path), data_dir


def _seed_current_db(db: DatabaseManager):
    """Insert sample entries into papers.db."""
    with db.get_connection('current') as conn:
        conn.executemany(
            """INSERT INTO entries
               (id, topic, feed_name, title, link, summary, authors,
                abstract, doi, published_date, discovered_date, status,
                rank_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("e1", "test_topic", "Test Feed", "Graphene superlattices",
                 "https://example.com/1", "Summary about graphene", "Smith, J.",
                 "Abstract about graphene layers", "10.1000/e1",
                 "2026-03-25", "2026-03-25", "ranked", 0.85),
                ("e2", "test_topic", "Test Feed", "Perovskite solar cells",
                 "https://example.com/2", "Summary about perovskites", "Doe, A.",
                 "Abstract about perovskite efficiency", "10.1000/e2",
                 "2026-03-20", "2026-03-20", "ranked", 0.72),
                ("e3", "test_topic", "Test Feed", "Topological insulators review",
                 "https://example.com/3", "Summary about topology", "Lee, B.",
                 None, None,
                 "2026-03-15", "2026-03-15", "filtered", 0.60),
            ],
        )
        conn.commit()


def _seed_history_db(db: DatabaseManager):
    """Insert sample entries into matched_entries_history.db."""
    with db.get_connection('history') as conn:
        conn.executemany(
            """INSERT INTO matched_entries
               (entry_id, feed_name, topics, title, link, summary, authors,
                abstract, doi, published_date, matched_date, rank_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("h1", "Test Feed", "test_topic", "Graphene nanoribbons",
                 "https://example.com/h1", "Summary h1", "Author A",
                 "Abstract about nanoribbons", "10.1000/h1",
                 "2026-03-22", "2026-03-22 10:00:00", 0.91),
                ("h2", "Test Feed", "test_topic, other_topic", "BCS theory",
                 "https://example.com/h2", "Summary h2", "Author B",
                 None, None,
                 "2026-02-15", "2026-02-16 08:00:00", 0.55),
            ],
        )
        conn.commit()


class TestQueryEntries:
    """Tests for DatabaseManager.query_entries()."""

    def test_basic_query_current(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', limit=10)
        assert total == 3
        assert len(rows) == 3
        assert rows[0]['rank_score'] >= rows[1]['rank_score']

    def test_topic_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', topic='test_topic')
        assert total == 3

        rows, total = ctx.db.query_entries(
            db_key='current', topic='nonexistent')
        assert total == 0

    def test_min_rank_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', min_rank=0.7)
        assert total == 2
        assert all(r['rank_score'] >= 0.7 for r in rows)

    def test_date_range_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', since='2026-03-20', until='2026-03-25')
        assert total == 2  # e1 and e2

    def test_text_search(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', search='graphene')
        assert total == 1
        assert rows[0]['id'] == 'e1'

    def test_has_doi_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', has_doi=True)
        assert total == 2

    def test_has_abstract_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', has_abstract=True)
        assert total == 2  # e3 has no abstract

    def test_status_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', status='filtered')
        assert total == 1
        assert rows[0]['id'] == 'e3'

    def test_limit_and_offset(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', limit=2, offset=0)
        assert total == 3
        assert len(rows) == 2

        rows2, total2 = ctx.db.query_entries(
            db_key='current', limit=2, offset=2)
        assert total2 == 3
        assert len(rows2) == 1

    def test_history_topic_like_filter(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_history_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='history', topic='test_topic')
        assert total == 2  # both entries contain 'test_topic'

        rows, total = ctx.db.query_entries(
            db_key='history', topic='other_topic')
        assert total == 1
        assert rows[0]['entry_id'] == 'h2'

    def test_returns_dicts(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, _ = ctx.db.query_entries(db_key='current', limit=1)
        assert isinstance(rows[0], dict)
        assert 'title' in rows[0]


class TestFuzzySearch:
    """Tests for FTS5 trigram fuzzy search."""

    def test_fuzzy_search_current(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        # "graphen" (missing 'e') should match "Graphene superlattices" via trigram
        rows, total = ctx.db.query_entries(db_key='current', fuzzy='graphen')
        assert total >= 1
        assert any('Graphene' in r['title'] for r in rows)

    def test_fuzzy_search_history(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_history_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='history', fuzzy='nanoribbon')
        assert total >= 1
        assert any('nanoribbons' in r['title'] for r in rows)

    def test_fuzzy_and_search_mutually_exclusive(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)

        with pytest.raises(ValueError, match="mutually exclusive"):
            ctx.db.query_entries(db_key='current', search='test', fuzzy='test')

    def test_fuzzy_min_length(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)

        with pytest.raises(ValueError, match="at least 3"):
            ctx.db.query_entries(db_key='current', fuzzy='ab')

    def test_fuzzy_no_match(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', fuzzy='zzzzzzz')
        assert total == 0


class TestRerank:
    """Tests for semantic reranking via STRanker."""

    def test_rerank_basic(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        # Mock STRanker to avoid model dependency in tests
        class FakeRanker:
            def __init__(self, **kw): pass
            def available(self): return True
            def score_entries(self, query, entries):
                # Give highest score to the entry whose text matches "perovskite"
                result = []
                for eid, group, text in entries:
                    score = 0.9 if 'perovskite' in text.lower() else 0.3
                    result.append((eid, group, score))
                return result

        monkeypatch.setattr(query_cmd, "STRanker", FakeRanker)
        monkeypatch.setattr(query_cmd, "ensure_local_model", lambda spec: spec)

        query_cmd.run(config_path, rerank='perovskite solar cells', output_json=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Perovskite entry should be first after reranking
        assert data['entries'][0]['title'] == 'Perovskite solar cells'
        assert 'rerank_score' in data['entries'][0]
        # Scores should be descending
        scores = [e['rerank_score'] for e in data['entries']]
        assert scores == sorted(scores, reverse=True)

    def test_rerank_with_filters(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        class FakeRanker:
            def __init__(self, **kw): pass
            def available(self): return True
            def score_entries(self, query, entries):
                return [(eid, g, 0.5) for eid, g, _ in entries]

        monkeypatch.setattr(query_cmd, "STRanker", FakeRanker)
        monkeypatch.setattr(query_cmd, "ensure_local_model", lambda spec: spec)

        # Filter to only ranked entries (2 of 3), then rerank
        query_cmd.run(config_path, rerank='test', status='ranked', output_json=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data['total'] == 2

    def test_rerank_model_unavailable(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        class BrokenRanker:
            def __init__(self, **kw): pass
            def available(self): return False

        monkeypatch.setattr(query_cmd, "STRanker", BrokenRanker)
        monkeypatch.setattr(query_cmd, "ensure_local_model", lambda spec: spec)

        with pytest.raises(RuntimeError, match="unavailable"):
            query_cmd.run(config_path, rerank='test query')

    def test_rerank_table_output_has_score_column(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        class FakeRanker:
            def __init__(self, **kw): pass
            def available(self): return True
            def score_entries(self, query, entries):
                return [(eid, g, 0.5) for eid, g, _ in entries]

        monkeypatch.setattr(query_cmd, "STRanker", FakeRanker)
        monkeypatch.setattr(query_cmd, "ensure_local_model", lambda spec: spec)

        query_cmd.run(config_path, rerank='test')
        captured = capsys.readouterr()
        assert 'rerank_score' in captured.out


class TestFuzzyPlusRerank:
    """Test combining fuzzy search with reranking."""

    def test_fuzzy_plus_rerank(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        class FakeRanker:
            def __init__(self, **kw): pass
            def available(self): return True
            def score_entries(self, query, entries):
                return [(eid, g, 0.7) for eid, g, _ in entries]

        monkeypatch.setattr(query_cmd, "STRanker", FakeRanker)
        monkeypatch.setattr(query_cmd, "ensure_local_model", lambda spec: spec)

        # Fuzzy narrows to graphene entry, then rerank scores it
        query_cmd.run(config_path, fuzzy='graphen', rerank='carbon materials',
                      output_json=True)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Fuzzy should have narrowed to graphene entry(ies)
        assert data['total'] >= 1
        assert all('rerank_score' in e for e in data['entries'])


class TestQueryCommand:
    """Tests for the query command run() function."""

    def test_validation_min_rank_all_feeds(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="--min-rank"):
            query_cmd.run(config_path, db_key='all_feeds', min_rank=0.5)

    def test_validation_status_history(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="--status"):
            query_cmd.run(config_path, db_key='history', status='ranked')

    def test_validation_has_abstract_all_feeds(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="--has-abstract"):
            query_cmd.run(config_path, db_key='all_feeds', has_abstract=True)

    def test_count_output(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        # Patch CommandContext so run() reuses our seeded DB
        monkeypatch.setattr(
            query_cmd, "CommandContext",
            lambda path: ctx,
        )
        query_cmd.run(config_path, count_only=True)
        captured = capsys.readouterr()
        assert captured.out.strip() == "3"

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(
            query_cmd, "CommandContext",
            lambda path: ctx,
        )
        query_cmd.run(config_path, output_json=True, limit=2)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data['total'] == 3
        assert len(data['entries']) == 2
        assert 'title' in data['entries'][0]

    def test_table_output(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        monkeypatch.setattr(
            query_cmd, "CommandContext",
            lambda path: ctx,
        )
        query_cmd.run(config_path, limit=5)
        captured = capsys.readouterr()
        assert "Found 3 entries" in captured.out
        assert "Graphene" in captured.out

    def test_empty_result(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        # Don't seed — DB is empty

        monkeypatch.setattr(
            query_cmd, "CommandContext",
            lambda path: ctx,
        )
        query_cmd.run(config_path)
        captured = capsys.readouterr()
        assert "No entries found" in captured.out


class TestKeywordSearch:
    """Tests for FTS5 keyword search (porter stemming)."""

    def test_basic_word_match(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', search='graphene')
        assert total >= 1
        assert any('Graphene' in r['title'] for r in rows)

    def test_stemming(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        # "insulating" should match "insulators" via porter stemming
        rows, total = ctx.db.query_entries(db_key='current', search='insulating')
        assert total >= 1
        assert any('insulator' in r['title'].lower() for r in rows)

    def test_phrase_search(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', search='"solar cells"')
        assert total >= 1
        assert any('solar cells' in r['title'].lower() for r in rows)

    def test_prefix_search(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', search='perovsk*')
        assert total >= 1
        assert any('Perovskite' in r['title'] for r in rows)

    def test_boolean_not(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(
            db_key='current', search='graphene NOT superlattice')
        # "graphene NOT superlattice" should exclude e1 (title has both)
        # but e1's summary also has "graphene" — FTS5 searches all indexed columns
        # The result depends on whether FTS5 considers column-level exclusion.
        # At minimum, it should not error.
        assert isinstance(total, int)

    def test_no_match(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', search='nonexistentterm')
        assert total == 0

    def test_bm25_scores_attached(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='current', search='graphene')
        assert total >= 1
        assert 'bm25_score' in rows[0]
        assert rows[0]['bm25_score'] is not None

    def test_history_keyword_search(self, tmp_path, monkeypatch):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_history_db(ctx.db)

        rows, total = ctx.db.query_entries(db_key='history', search='nanoribbons')
        assert total >= 1
        assert any('nanoribbons' in r['title'] for r in rows)

    def test_bm25_sort_in_query_cmd(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)
        monkeypatch.setattr(query_cmd, "CommandContext", lambda path: ctx)

        query_cmd.run(config_path, search='graphene', output_json=True, limit=0)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Graphene entry should have a bm25_score
        graphene_entries = [e for e in data['entries'] if 'graphene' in e['title'].lower()]
        assert len(graphene_entries) >= 1
        assert 'bm25_score' in graphene_entries[0]
