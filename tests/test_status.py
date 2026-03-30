"""Tests for the status command."""

import json
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import status as status_cmd
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
    with db.get_connection("current") as conn:
        conn.executemany(
            """INSERT INTO entries
               (id, topic, feed_name, title, link, summary, authors,
                abstract, doi, published_date, discovered_date, status,
                rank_score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("e1", "test_topic", "Test Feed", "Paper A",
                 "https://example.com/1", "Sum A", "Auth A",
                 "Abstract A", "10.1000/e1",
                 "2026-03-30", "2026-03-30", "ranked", 0.85),
                ("e2", "test_topic", "Test Feed", "Paper B",
                 "https://example.com/2", "Sum B", "Auth B",
                 "Abstract B", "10.1000/e2",
                 "2026-03-30", "2026-03-30", "filtered", 0.60),
            ],
        )
        conn.commit()


class TestStatusJson:
    """Test --json output."""

    def test_json_output_empty_dbs(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        status_cmd.run(config_path, output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)

        assert data["config"]["valid"] is True
        assert "test_topic" in data["config"]["topics"]
        assert data["config"]["enabled_feeds"] == 1
        assert data["databases"]["current"]["exists"] is True
        assert data["databases"]["current"]["entry_count"] == 0

    def test_json_output_with_entries(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        status_cmd.run(config_path, output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)

        current = data["databases"]["current"]
        assert current["entry_count"] == 2
        assert current["by_status"]["ranked"] == 1
        assert current["by_status"]["filtered"] == 1
        assert current["latest_discovered_date"] == "2026-03-30"
        assert "test_topic" in current["topics"]

    def test_json_has_db_file_metadata(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        status_cmd.run(config_path, output_json=True)
        data = json.loads(capsys.readouterr().out)

        for db_key in ("current", "history", "all_feeds"):
            db_info = data["databases"][db_key]
            assert "path" in db_info
            assert db_info["exists"] is True
            assert "size_bytes" in db_info
            assert "modified_utc" in db_info


class TestStatusHuman:
    """Test human-readable output."""

    def test_human_output_shows_key_info(self, tmp_path, monkeypatch, capsys):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        status_cmd.run(config_path, output_json=False)
        out = capsys.readouterr().out

        assert "Configuration is valid" in out
        assert "test_topic" in out
        assert "Enabled feeds: 1" in out
        assert "Current run:" in out
        assert "Entries:" in out

    def test_human_output_shows_pipeline_status(
        self, tmp_path, monkeypatch, capsys
    ):
        config_path, _ = _make_config(tmp_path, monkeypatch)
        from paper_firehose.core.command_context import CommandContext
        ctx = CommandContext(config_path)
        _seed_current_db(ctx.db)

        status_cmd.run(config_path, output_json=False)
        out = capsys.readouterr().out

        assert "ranked: 1" in out
        assert "filtered: 1" in out
        assert "Latest discovered:" in out
