import os
import sqlite3
from pathlib import Path
import textwrap
import sys

import pytest


# Make the src/ directory importable for command modules
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import filter as filter_cmd
from paper_firehose.commands import generate_html as html_cmd
from paper_firehose.commands import rank as rank_cmd
import paper_firehose.core.config as core_config


class DummyRanker:
    """Deterministic ranker for tests."""

    def __init__(self, model_name: str = "unused") -> None:
        self.model_name = model_name

    def available(self) -> bool:
        return True

    def score_entries(self, query, entries, *, use_summary: bool = False):
        # Assign descending scores so ranked HTML has deterministic ordering
        results = []
        base = 0.9
        step = 0.05
        for index, (entry_id, topic_name, _text) in enumerate(entries):
            results.append((entry_id, topic_name, base - index * step))
        return results


@pytest.mark.usefixtures("monkeypatch")
def test_end_to_end_pipeline_generates_html(tmp_path, monkeypatch):
    """Pipeline should run purge, filter, rank, and html using a temp data dir."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)

    feed_path = Path(__file__).parent / "fixtures" / "sample_feed.xml"
    feed_uri = feed_path.resolve().as_uri()

    config_yaml = textwrap.dedent(
        f"""
        database:
          path: "papers.db"
          all_feeds_path: "all_feed_entries.db"
          history_path: "matched_entries_history.db"
        feeds:
          local_feed:
            name: "Local Test Feed"
            url: "{feed_uri}"
            enabled: true
        priority_journals: []
        defaults:
          time_window_days: 365
        """
    ).strip() + "\n"

    topic_yaml = textwrap.dedent(
        """
        name: "Test Topic"
        description: "Local feed pipeline test"
        feeds:
          - "local_feed"
        filter:
          pattern: "graphene"
          fields: ["title", "summary"]
        ranking:
          query: "graphene materials"
          model: "dummy-model"
        output:
          filename: "test_topic_filtered.html"
          filename_ranked: "test_topic_ranked.html"
          filename_summary: "test_topic_summary.html"
        """
    ).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")

    # Prevent ConfigManager from copying template configs that reference remote feeds
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)

    # Ensure runtime data lives under the temporary directory
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))

    # Avoid external model downloads and heavy dependencies during ranking
    monkeypatch.setattr(rank_cmd, "_ensure_local_model", lambda spec: spec)
    monkeypatch.setattr(rank_cmd, "STRanker", DummyRanker)

    config_path_str = str(config_path)

    # Purge recent entries to start with a clean slate
    filter_cmd.purge(config_path_str, days=1, all_data=False)

    # Run filter to populate the databases from the local RSS feed
    filter_cmd.run(config_path_str)

    # Verify that exactly one entry was matched (graphene item only)
    db_path = data_dir / "papers.db"
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT title FROM entries").fetchall()
    titles = {row[0] for row in rows}
    assert "Graphene breakthroughs in materials science" in titles
    assert "Other topic unrelated to filters" not in titles

    # Rank the filtered entries using the deterministic ranker
    rank_cmd.run(config_path_str)

    # Generate all HTML outputs from the populated database
    html_cmd.run(config_path_str)

    html_dir = data_dir / "html"
    filtered_path = html_dir / "test_topic_filtered.html"
    ranked_path = html_dir / "test_topic_ranked.html"
    summary_path = html_dir / "test_topic_summary.html"

    assert filtered_path.exists()
    assert ranked_path.exists()
    assert summary_path.exists()

    filtered_html = filtered_path.read_text(encoding="utf-8")
    ranked_html = ranked_path.read_text(encoding="utf-8")
    summary_html = summary_path.read_text(encoding="utf-8")

    assert "Graphene breakthroughs in materials science" in filtered_html
    assert "Other topic unrelated to filters" not in filtered_html

    # Ranked output should include the assigned score badge from DummyRanker
    assert "Score 0.90" in ranked_html

    # Summary page should still list the entry and note the fallback text
    assert "Graphene breakthroughs in materials science" in summary_html
    assert "No abstract available." in summary_html

    # Ensure the environment override directed outputs into the temporary directory
    assert filtered_path.is_file() and str(filtered_path).startswith(str(data_dir))
