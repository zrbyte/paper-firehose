import sqlite3
from pathlib import Path
import textwrap
import sys
import json

import pytest


# Make the src/ directory importable for command modules
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import abstracts as abstracts_cmd
from paper_firehose.commands import email_list as email_cmd
from paper_firehose.commands import export_recent as export_cmd
from paper_firehose.commands import filter as filter_cmd
from paper_firehose.commands import generate_html as html_cmd
from paper_firehose.commands import pqa_summary as pqa_cmd
from paper_firehose.commands import rank as rank_cmd
import paper_firehose.core.config as core_config
from paper_firehose.processors import abstract_fetcher


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
          abstracts:
            mailto: "testing@example.com"
        email:
          from: "test@example.com"
          to: "recipient@example.com"
          smtp:
            host: "smtp.example.com"
            port: 465
            username: "test@example.com"
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
        abstract_fetch:
          enabled: true
          rank_threshold: 0.0
        paperqa:
          download_rank_threshold: 0.0
          rps: 1.0
          llm: "gpt-4o"
          summary_llm: "gpt-4o-mini"
          prompt: |
            Summarize papers about {ranking_query} as concise JSON.
        output:
          filename: "test_topic_filtered.html"
          filename_ranked: "test_topic_ranked.html"
          filename_summary: "test_topic_summary.html"
          archive: true
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
    from paper_firehose.core import model_manager
    monkeypatch.setattr(model_manager, "ensure_local_model", lambda spec: spec)
    monkeypatch.setattr(rank_cmd, "STRanker", DummyRanker)

    def fake_fill_arxiv_summaries(db_manager, topics=None):
        with db_manager.get_connection("current") as conn:
            conn.execute("UPDATE entries SET abstract = 'Filled abstract'")
        return 1

    def fake_crossref_pass(
        db_manager,
        topic_name,
        threshold,
        *,
        mailto,
        session,
        min_interval,
        max_per_topic,
        max_retries=3,
    ):
        return 0

    def fake_fallback_pass(
        db_manager,
        topic_name,
        threshold,
        *,
        mailto,
        session,
        min_interval,
        max_per_topic,
    ):
        return 0

    # Patch at the point of use (abstracts_cmd) not at definition (abstract_fetcher)
    # because abstracts.py imports these functions at module level
    import paper_firehose.commands.abstracts as abstracts_module
    monkeypatch.setattr(abstracts_module, "fill_arxiv_summaries", fake_fill_arxiv_summaries)
    monkeypatch.setattr(abstracts_module, "crossref_pass", fake_crossref_pass)
    monkeypatch.setattr(abstracts_module, "fallback_pass", fake_fallback_pass)

    def fake_download_pdf(pdf_url, dest_path, *, mailto, session=None, max_retries=3):
        Path(dest_path).write_bytes(b"0" * 12000)
        return True

    # Mock PaperQASession to avoid actual paper-qa calls
    class MockPaperQASession:
        def __init__(self, llm=None, summary_llm=None):
            self.llm = llm
            self.summary_llm = summary_llm

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def summarize_pdf(self, pdf_path, question):
            return json.dumps({"summary": "Graphene summary for experts", "methods": "Graphene methods"})

    monkeypatch.setattr(pqa_cmd, "_download_pdf", fake_download_pdf)
    monkeypatch.setattr(pqa_cmd, "_query_arxiv_api_for_pdf", lambda arxiv_id, *, mailto, session=None: f"https://arxiv.org/pdf/{arxiv_id}.pdf")
    monkeypatch.setattr(pqa_cmd, "PaperQASession", MockPaperQASession)
    monkeypatch.setattr(pqa_cmd, "_resolve_arxiv_id", lambda entry: "2501.12345v1")
    monkeypatch.setattr(pqa_cmd.time, "sleep", lambda *_args, **_kwargs: None)

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

    # Populate abstracts using the patched helpers
    abstracts_cmd.run(config_path_str)

    with sqlite3.connect(db_path) as conn:
        abstracts = conn.execute("SELECT abstract FROM entries WHERE abstract IS NOT NULL").fetchall()
    assert abstracts and abstracts[0][0] == "Filled abstract"

    # Generate paper-qa summaries with deterministic stubs
    pqa_cmd.run(config_path_str, limit=1)

    with sqlite3.connect(db_path) as conn:
        summary_rows = conn.execute("SELECT paper_qa_summary FROM entries WHERE paper_qa_summary IS NOT NULL").fetchall()
    assert summary_rows
    summary_payload = json.loads(summary_rows[0][0])
    assert summary_payload["summary"] == "Graphene summary for experts"

    # Generate all HTML outputs from the populated database
    html_cmd.run(config_path_str)

    html_dir = data_dir / "html"
    filtered_path = html_dir / "test_topic_filtered.html"
    ranked_path = html_dir / "test_topic_ranked.html"

    assert filtered_path.exists()
    assert ranked_path.exists()

    filtered_html = filtered_path.read_text(encoding="utf-8")
    ranked_html = ranked_path.read_text(encoding="utf-8")

    assert "Graphene breakthroughs in materials science" in filtered_html
    assert "Other topic unrelated to filters" not in filtered_html

    # Ranked output should include the assigned score badge from DummyRanker
    assert "Score 0.90" in ranked_html

    # PQA summary HTML should include the paper-qa summary content
    summary_path = html_dir / "test_topic_summary.html"
    assert summary_path.exists(), "PQA summary HTML was not generated"
    summary_html = summary_path.read_text(encoding="utf-8")
    assert "Graphene summary for experts" in summary_html
    assert "Graphene methods" in summary_html

    # Ensure the environment override directed outputs into the temporary directory
    assert filtered_path.is_file() and str(filtered_path).startswith(str(data_dir))

    # History DB should contain the matched entry
    history_path = data_dir / "matched_entries_history.db"
    assert history_path.exists()
    with sqlite3.connect(history_path) as conn:
        history_rows = conn.execute("SELECT title FROM matched_entries").fetchall()
    history_titles = {r[0] for r in history_rows}
    assert "Graphene breakthroughs in materials science" in history_titles

    # Email dry-run should produce a preview HTML file
    email_cmd.run(config_path_str, dry_run=True)

    # Find the email preview file
    email_previews = list(data_dir.glob("email_preview_*.html"))
    assert email_previews, "Email dry-run did not produce a preview file"
    email_html = email_previews[0].read_text(encoding="utf-8")
    assert "Graphene breakthroughs" in email_html
    assert "Score 0.90" in email_html

    # Export-recent should create a smaller history DB
    export_cmd.run(config_path_str, days=365)

    recent_path = data_dir / "matched_entries_history.recent.db"
    assert recent_path.exists(), "export-recent did not create the recent DB"
    with sqlite3.connect(recent_path) as conn:
        recent_rows = conn.execute("SELECT COUNT(*) FROM matched_entries").fetchone()
    assert recent_rows[0] >= 1


@pytest.mark.usefixtures("monkeypatch")
def test_json_output_filter(tmp_path, monkeypatch):
    """Filter command with output_json returns structured dict."""
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
        """
    ).strip() + "\n"

    topic_yaml = textwrap.dedent(
        """
        name: "Test Topic"
        feeds:
          - "local_feed"
        filter:
          pattern: "graphene"
          fields: ["title", "summary"]
        ranking:
          query: "graphene"
        output:
          filename: "test.html"
        """
    ).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))

    result = filter_cmd.run(str(config_path), output_json=True)
    assert result is not None
    assert result["command"] == "filter"
    assert "topics" in result
    assert "total_matched" in result
    assert isinstance(result["total_matched"], int)


@pytest.mark.usefixtures("monkeypatch")
def test_json_output_rank(tmp_path, monkeypatch):
    """Rank command with output_json returns structured dict."""
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
        """
    ).strip() + "\n"

    topic_yaml = textwrap.dedent(
        """
        name: "Test Topic"
        feeds:
          - "local_feed"
        filter:
          pattern: "graphene"
          fields: ["title", "summary"]
        ranking:
          query: "graphene materials"
          model: "dummy-model"
        output:
          filename: "test.html"
        """
    ).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))

    from paper_firehose.core import model_manager
    monkeypatch.setattr(model_manager, "ensure_local_model", lambda spec: spec)
    monkeypatch.setattr(rank_cmd, "STRanker", DummyRanker)

    # Filter first to populate DB
    filter_cmd.run(str(config_path))

    result = rank_cmd.run(str(config_path), output_json=True)
    assert result is not None
    assert result["command"] == "rank"
    assert "topics" in result
    assert "total_ranked" in result
    assert isinstance(result["total_ranked"], int)


@pytest.mark.usefixtures("monkeypatch")
def test_json_output_abstracts(tmp_path, monkeypatch):
    """Abstracts command with output_json returns structured dict."""
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
        """
    ).strip() + "\n"

    topic_yaml = textwrap.dedent(
        """
        name: "Test Topic"
        feeds:
          - "local_feed"
        filter:
          pattern: "graphene"
          fields: ["title", "summary"]
        ranking:
          query: "graphene"
        abstract_fetch:
          enabled: true
          rank_threshold: 0.0
        output:
          filename: "test.html"
        """
    ).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")
    monkeypatch.setattr(core_config, "_copy_tree", lambda src, dest: False)
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))

    import paper_firehose.commands.abstracts as abstracts_module
    monkeypatch.setattr(abstracts_module, "fill_arxiv_summaries", lambda db, topics=None: 0)
    monkeypatch.setattr(
        abstracts_module, "crossref_pass",
        lambda db, t, thr, *, mailto, session, min_interval, max_per_topic, max_retries=3: 0,
    )
    monkeypatch.setattr(
        abstracts_module, "fallback_pass",
        lambda db, t, thr, *, mailto, session, min_interval, max_per_topic: 0,
    )

    # Filter first
    filter_cmd.run(str(config_path))

    result = abstracts_cmd.run(str(config_path), output_json=True)
    assert result is not None
    assert result["command"] == "abstracts"
    assert "arxiv_filled" in result
    assert "topics" in result
