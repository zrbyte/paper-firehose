"""Tests for the ``synthesize`` command (the OpenAI client is faked; nothing leaves the machine)."""

import json
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from paper_firehose.cli import cli
from paper_firehose.commands import email_list as email_cmd
from paper_firehose.commands import generate_html as html_cmd
from paper_firehose.commands import synthesize as synthesize_cmd
from paper_firehose.core.config import ConfigManager
from paper_firehose.core.database import DatabaseManager
from paper_firehose.core.exit_codes import ERR_CONFIG, ERR_RUNTIME
from paper_firehose.processors import synthesizer as synth


ANSWER = {
    "overview": "The papers show X.",
    "themes": [{"title": "Theme A", "description": "Papers 1 and 2 agree.", "papers": [1, 2]}],
}


class FakeResponses:
    def __init__(self, output_text=None, exc=None):
        self.output_text = output_text
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(output_text=self.output_text)


def fake_client(answer=ANSWER, exc=None):
    responses = FakeResponses(json.dumps(answer) if answer is not None else "", exc)
    return SimpleNamespace(responses=responses), responses


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A data directory with two topics ('alpha' has synthesis enabled, 'beta' has not) and a helper to add papers."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config_dir = tmp_path / "config"
    (config_dir / "topics").mkdir(parents=True)
    (config_dir / "config.yaml").write_text(textwrap.dedent("""
        database:
          path: "papers.db"
          all_feeds_path: "all_feed_entries.db"
          history_path: "matched_entries_history.db"
        feeds:
          feed_a:
            name: "Feed A"
            url: "https://feed.example/rss"
            enabled: true
        priority_journals: []
        defaults:
          time_window_days: 365
    """).strip() + "\n")

    def topic(name, synthesis=""):
        (config_dir / "topics" / f"{name}.yaml").write_text(textwrap.dedent(f"""
            name: "Topic {name}"
            feeds: ["feed_a"]
            filter:
              pattern: "x"
              fields: ["title"]
            ranking:
              query: "graphene transport"
            output:
              filename: "results_{name}.html"
              filename_ranked: "results_{name}_ranked.html"
        """).strip() + "\n" + textwrap.dedent(synthesis))

    topic("alpha", """
        synthesis:
          enabled: true
          days: 7
          max_papers: 5
    """)
    topic("beta")

    config_path = str(config_dir / "config.yaml")
    db = DatabaseManager(ConfigManager(config_path).load_config())

    def add(entry_id, topics, days_ago=1, score=0.5):
        with db.get_connection("history", row_factory=False) as conn:
            conn.execute(
                "INSERT INTO matched_entries (entry_id, feed_name, topics, title, link, summary, authors, "
                "abstract, published_date, matched_date, rank_score) "
                "VALUES (?,?,?,?,?,?,?,?,?, datetime('now', ?), ?)",
                (entry_id, "Feed A", topics, f"Title {entry_id}", f"https://example.org/{entry_id}",
                 "summary", "A. Author", f"Abstract {entry_id}", "2026-11-19", f"-{days_ago} days", score),
            )

    return SimpleNamespace(config_path=config_path, db=db, add=add, topic=topic, data_dir=data_dir)


def syntheses(db):
    with db.get_connection("history") as conn:
        return conn.execute("SELECT * FROM syntheses ORDER BY id").fetchall()


# ---------------------------------------------------------------------------
# Which topics, which papers
# ---------------------------------------------------------------------------

def test_stores_a_synthesis_for_the_enabled_topic_only(setup):
    for n in range(4):
        setup.add(f"a{n}", "alpha", score=0.9 - n / 10)
        setup.add(f"b{n}", "beta")
    client, responses = fake_client()

    result = synthesize_cmd.run(setup.config_path, client=client)

    assert result["stored"] == 1 and result["failed"] == 0
    assert list(result["topics"]) == ["alpha"]                 # beta did not opt in: not even mentioned
    assert result["topics"]["alpha"]["status"] == "stored"
    assert len(responses.calls) == 1
    assert "Topic: Topic alpha" in responses.calls[0]["input"]
    assert "graphene transport" in responses.calls[0]["instructions"]

    rows = syntheses(setup.db)
    assert len(rows) == 1 and rows[0]["topic"] == "alpha" and rows[0]["paper_count"] == 4
    content = json.loads(rows[0]["content"])
    assert content["overview"] == "The papers show X."
    # The theme's numbers are stored as positions in the stored paper list (best score first)
    assert [p["entry_id"] for p in content["papers"]] == ["a0", "a1", "a2", "a3"]
    assert content["themes"][0]["papers"] == [0, 1]
    assert content["papers"][0]["link"] == "https://example.org/a0"


def test_explicit_topic_needs_no_enabled_flag(setup):
    for n in range(3):
        setup.add(f"b{n}", "beta")
    client, _ = fake_client()
    result = synthesize_cmd.run(setup.config_path, "beta", client=client)
    assert result["topics"]["beta"]["status"] == "stored"


def test_only_the_period_and_the_best_papers_are_sent(setup):
    for n in range(8):
        setup.add(f"new{n}", "alpha", days_ago=1, score=n / 10)
    setup.add("old", "alpha", days_ago=20, score=0.99)
    client, responses = fake_client()

    result = synthesize_cmd.run(setup.config_path, client=client)

    info = result["topics"]["alpha"]
    assert info["papers"] == 5 and info["total"] == 8          # max_papers: 5; the old paper is outside 7 days
    sent = responses.calls[0]["input"]
    assert "Title old" not in sent
    assert "[1] Title new7" in sent and "Title new2" not in sent


def test_command_line_values_override_the_topic_section(setup):
    for n in range(8):
        setup.add(f"p{n}", "alpha", days_ago=10, score=n / 10)
    client, responses = fake_client()

    result = synthesize_cmd.run(setup.config_path, "alpha", days=30, limit=3, min_rank=0.4, client=client)

    info = result["topics"]["alpha"]
    assert info["papers"] == 3 and info["total"] == 4 and info["days"] == 30     # scores 0.4 .. 0.7


def test_too_few_papers_is_skipped_without_a_client(setup, monkeypatch):
    setup.add("a0", "alpha")
    setup.add("a1", "alpha")

    def no_client(*args, **kwargs):
        raise AssertionError("no client must be created when there is nothing to send")
    monkeypatch.setattr(synth, "Synthesizer", no_client)

    result = synthesize_cmd.run(setup.config_path)
    assert result["topics"]["alpha"]["status"] == "skipped"
    assert "only 2 paper" in result["topics"]["alpha"]["reason"]
    assert result["failed"] == 0 and syntheses(setup.db) == []


def test_dry_run_calls_nothing_and_stores_nothing(setup, monkeypatch):
    for n in range(4):
        setup.add(f"a{n}", "alpha")

    def no_client(*args, **kwargs):
        raise AssertionError("a dry run must not create a client")
    monkeypatch.setattr(synth, "Synthesizer", no_client)

    result = synthesize_cmd.run(setup.config_path, dry_run=True)
    info = result["topics"]["alpha"]
    assert info["status"] == "dry-run" and len(info["titles"]) == 4 and info["input_chars"] > 0
    assert syntheses(setup.db) == []


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

def test_missing_key_is_a_runtime_error(setup):
    """Without OPENAI_API_KEY the real client cannot be created: say so, do not exit quietly."""
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        synthesize_cmd.run(setup.config_path)
    assert syntheses(setup.db) == []


def test_a_failed_call_is_counted_and_the_other_topics_go_on(setup):
    setup.topic("gamma", """
        synthesis:
          enabled: true
    """)
    for n in range(3):
        setup.add(f"a{n}", "alpha")
        setup.add(f"g{n}", "gamma")

    class Flaky(FakeResponses):
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if "Topic alpha" in kwargs["input"]:
                raise RuntimeError("boom")
            return SimpleNamespace(output_text=json.dumps(ANSWER))

    responses = Flaky()
    result = synthesize_cmd.run(setup.config_path, client=SimpleNamespace(responses=responses))

    assert result["topics"]["alpha"]["status"] == "failed"
    assert result["topics"]["gamma"]["status"] == "stored"
    assert result["failed"] == 1 and result["stored"] == 1
    assert [row["topic"] for row in syntheses(setup.db)] == ["gamma"]


def test_an_unusable_answer_stores_nothing(setup):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client({"overview": "", "themes": [{"title": "t", "description": "d", "papers": [99]}]})
    result = synthesize_cmd.run(setup.config_path, client=client)
    assert result["topics"]["alpha"]["status"] == "failed" and syntheses(setup.db) == []


@pytest.mark.parametrize("section", [
    "synthesis:\n  enabled: true\n  days: many\n",
    "synthesis:\n  enabled: true\n  days: 0\n",
    "synthesis:\n  enabled: true\n  max_papers: 1\n",
    "synthesis:\n  enabled: true\n  min_rank: high\n",
])
def test_bad_values_in_the_topic_section_fail_that_topic(setup, section):
    setup.topic("alpha", section)
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, responses = fake_client()
    result = synthesize_cmd.run(setup.config_path, client=client)
    assert result["topics"]["alpha"]["status"] == "failed"
    assert responses.calls == []


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

def patch_client(monkeypatch, client):
    real = synth.Synthesizer
    monkeypatch.setattr(synth, "Synthesizer", lambda model=None, **kw: real(model, **{**kw, "client": client}))


def test_cli_reports_and_exits_zero(setup, monkeypatch):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client()
    patch_client(monkeypatch, client)

    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize"])
    assert result.exit_code == 0
    assert "alpha: synthesis of 3 papers stored (1 themes)" in result.output


def test_cli_json_output(setup, monkeypatch):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client()
    patch_client(monkeypatch, client)

    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["command"] == "synthesize" and data["stored"] == 1


def test_cli_dry_run_lists_the_papers(setup):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize", "--dry-run"])
    assert result.exit_code == 0
    assert "would send 3 of 3 papers" in result.output and "[1] Title a" in result.output


def test_cli_says_when_no_topic_is_enabled(setup):
    setup.topic("alpha")                # no synthesis section any more
    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize"])
    assert result.exit_code == 0
    assert "No topic to synthesize" in result.output


def test_cli_missing_key_exits_runtime(setup):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize"])
    assert result.exit_code == ERR_RUNTIME
    assert "OPENAI_API_KEY" in result.output


def test_cli_failed_topic_exits_runtime(setup, monkeypatch):
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client(exc=RuntimeError("boom"))
    patch_client(monkeypatch, client)
    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize"])
    assert result.exit_code == ERR_RUNTIME


def test_cli_bad_config_exits_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
    bad = tmp_path / "bad.yaml"
    bad.write_text("not_a_valid_key: true\n")
    result = CliRunner().invoke(cli, ["--config", str(bad), "synthesize"])
    assert result.exit_code == ERR_CONFIG


@pytest.mark.parametrize("args", [["--days", "0"], ["--limit", "2"], ["--days", "x"]])
def test_cli_refuses_senseless_numbers(setup, args):
    result = CliRunner().invoke(cli, ["--config", setup.config_path, "synthesize", *args])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Configuration and the pages
# ---------------------------------------------------------------------------

def test_the_synthesis_section_is_a_known_key(setup):
    setup.topic("alpha", """
        synthesis:
          enabled: true
          days: 7
          max_papers: 40
          min_rank: 0.3
          model: "some-model"
          reasoning_effort: "low"
          prompt: "Write for {ranking_query}."
    """)
    manager = ConfigManager(setup.config_path)
    assert manager.validate_config()
    assert not [w for w in manager.check_unknown_keys() if "synthesis" in w]


def test_html_command_shows_the_stored_synthesis(setup):
    """synthesize, then html: the block is on the generated pages."""
    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client()
    synthesize_cmd.run(setup.config_path, client=client)

    html_cmd.run(setup.config_path, "alpha")

    html_dir = setup.data_dir / "html"
    pages = sorted(html_dir.glob("results_alpha*.html"))
    assert len(pages) == 2
    for page in pages:
        text = page.read_text()
        assert '<details class="synthesis"' in text
        assert "The papers show X." in text and "Theme A" in text
        assert '<a href="https://example.org/a' in text


def test_email_dry_run_puts_the_synthesis_above_the_topics_papers(setup, tmp_path):
    """synthesize, then email --dry-run: the preview has the block before the topic's entries."""
    import time
    import yaml

    # The digest needs SMTP settings (nothing is sent in a dry run) and reads papers.db
    config = yaml.safe_load(Path(setup.config_path).read_text())
    config["email"] = {"from": "digest@example.com", "to": "reader@example.com",
                       "smtp": {"host": "smtp.example.com", "port": 465, "username": "digest@example.com"}}
    Path(setup.config_path).write_text(yaml.safe_dump(config))
    entry = {"title": "A ranked paper in papers.db", "link": "https://example.org/ranked",
             "summary": "Summary", "authors": [{"name": "A. Author"}],
             "published_parsed": time.strptime("2026-11-20", "%Y-%m-%d")}
    entry_id = setup.db.compute_entry_id(entry)
    setup.db.save_current_entry(entry, "Feed A", "alpha", entry_id)
    setup.db.update_entry_rank(entry_id, "alpha", 0.8)

    for n in range(3):
        setup.add(f"a{n}", "alpha")
    client, _ = fake_client()
    synthesize_cmd.run(setup.config_path, client=client)

    email_cmd.run(setup.config_path, "alpha", dry_run=True)

    previews = list(setup.data_dir.glob("email_preview_*.html"))
    assert len(previews) == 1
    text = previews[0].read_text()
    assert "Synthesis across papers" in text and "The papers show X." in text
    assert text.index("Synthesis across papers") < text.index("A ranked paper in papers.db")
