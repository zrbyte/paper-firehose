import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import pqa_summary  # noqa: E402


def test_resolve_mailto_prefers_config(monkeypatch):
    config = {
        "defaults": {
            "abstracts": {
                "mailto": "config@example.com",
            }
        }
    }
    monkeypatch.delenv("MAILTO", raising=False)
    assert pqa_summary._resolve_mailto(config) == "config@example.com"


def test_arxiv_user_agent_includes_contact():
    ua = pqa_summary._arxiv_user_agent("person@example.com")
    assert ua.startswith("paper-firehose")
    assert "person@example.com" in ua


def test_extract_arxiv_id_from_link_and_doi():
    link = "https://arxiv.org/abs/2501.12345v2"
    doi = "10.48550/arXiv.2501.12345v2"
    assert pqa_summary._extract_arxiv_id_from_link(link) == "2501.12345v2"
    assert pqa_summary._extract_arxiv_id_from_doi(doi) == "2501.12345v2"


def test_extract_arxiv_id_from_text_variants():
    text = "Discussed as arXiv:2501.12345 throughout."
    assert pqa_summary._extract_arxiv_id_from_text(text) == "2501.12345"


def test_resolve_arxiv_id_priority_order():
    entry = {
        "link": "https://arxiv.org/pdf/2501.99999v3.pdf",
        "doi": "10.1234/example",
        "summary": "arXiv:2501.99999v1 mention",
        "title": "Fallback title",
    }
    assert pqa_summary._resolve_arxiv_id(entry) == "2501.99999v3"


def test_resolve_arxiv_id_uses_summary_when_link_missing():
    entry = {
        "link": "",
        "doi": None,
        "summary": "Includes reference arXiv:2401.00001",
        "title": "Graphene",
    }
    assert pqa_summary._resolve_arxiv_id(entry) == "2401.00001"


def test_find_archived_pdf_matches_variants(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "2401.11111.pdf").write_bytes(b"dummy")
    (archive / "2401.22222v2.pdf").write_bytes(b"dummy2")

    path_exact = pqa_summary._find_archived_pdf(str(archive), "2401.22222v2")
    assert path_exact.endswith("2401.22222v2.pdf")

    path_base = pqa_summary._find_archived_pdf(str(archive), "2401.11111v5")
    assert path_base.endswith("2401.11111.pdf")


def test_normalize_summary_json_handles_code_fence():
    raw = """```json
{
  "summary": "Key findings",
  "methods": "Method details"
}
```"""
    cleaned = pqa_summary._normalize_summary_json(raw)
    data = json.loads(cleaned)
    assert data["summary"] == "Key findings"
    assert data["methods"] == "Method details"


def test_normalize_summary_json_wraps_plain_text():
    raw = "Plain text response"
    cleaned = pqa_summary._normalize_summary_json(raw)
    data = json.loads(cleaned)
    assert data["summary"] == "Plain text response"
    assert data["methods"] == ""


def test_normalize_arxiv_arg_accepts_variants():
    assert pqa_summary._normalize_arxiv_arg("https://arxiv.org/pdf/2401.00001.pdf") == "2401.00001"
    assert pqa_summary._normalize_arxiv_arg("2401.00002v2") == "2401.00002v2"
    assert pqa_summary._normalize_arxiv_arg("arXiv:2401.00003") == "2401.00003"
    assert pqa_summary._normalize_arxiv_arg("") is None


def test_ensure_dirs_creates_directories(tmp_path):
    download = tmp_path / "downloads"
    archive = tmp_path / "archive"
    assert not download.exists()
    assert not archive.exists()
    pqa_summary._ensure_dirs(str(download), str(archive))
    assert download.is_dir()
    assert archive.is_dir()


def test_move_to_archive_avoids_overwrite(tmp_path):
    download = tmp_path / "dl"
    archive = tmp_path / "archive"
    download.mkdir()
    archive.mkdir()
    # Existing file to force suffix logic
    existing = archive / "2401.12345.pdf"
    existing.write_bytes(b"x")
    download_file = download / "2401.12345.pdf"
    download_file.write_bytes(b"y")

    pqa_summary._move_to_archive([str(download_file)], str(archive))

    archived = sorted(p.name for p in archive.iterdir())
    assert "2401.12345.pdf" in archived
    assert any(name.startswith("2401.12345.") and name.endswith(".pdf") for name in archived if name != "2401.12345.pdf")


def test_cleanup_archive_removes_old_files(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    recent = archive / "recent.pdf"
    old = archive / "old.pdf"
    recent.write_bytes(b"r")
    old.write_bytes(b"o")
    old_time = time.time() - 40 * 24 * 60 * 60
    os.utime(old, (old_time, old_time))

    pqa_summary._cleanup_archive(str(archive), max_age_days=30)

    assert recent.exists()
    assert not old.exists()


def test_write_pqa_summary_to_dbs_updates_tables(tmp_path):
    current = tmp_path / "papers.db"
    history = tmp_path / "history.db"

    conn = sqlite3.connect(current)
    conn.execute(
        "CREATE TABLE entries (id TEXT, topic TEXT, paper_qa_summary TEXT)"
    )
    conn.execute("INSERT INTO entries VALUES (?, ?, ?)", ("entry-1", "topic-a", ""))
    conn.commit()
    conn.close()

    hconn = sqlite3.connect(history)
    hconn.execute(
        "CREATE TABLE matched_entries (entry_id TEXT, paper_qa_summary TEXT)"
    )
    hconn.execute("INSERT INTO matched_entries VALUES (?, ?)", ("entry-1", ""))
    hconn.commit()
    hconn.close()

    class DummyDB:
        def __init__(self, current_path: Path, history_path: Path):
            self.db_paths = {
                "current": str(current_path),
                "history": str(history_path),
            }

    db = DummyDB(current, history)
    payload = json.dumps({"summary": "done", "methods": "m"})

    pqa_summary._write_pqa_summary_to_dbs(db, "entry-1", payload, topic="topic-a")

    conn = sqlite3.connect(current)
    cur = conn.cursor()
    cur.execute("SELECT paper_qa_summary FROM entries WHERE id='entry-1'")
    current_value = cur.fetchone()[0]
    conn.close()

    hconn = sqlite3.connect(history)
    hcur = hconn.cursor()
    hcur.execute("SELECT paper_qa_summary FROM matched_entries WHERE entry_id='entry-1'")
    history_value = hcur.fetchone()[0]
    hconn.close()

    assert json.loads(current_value) == {"summary": "done", "methods": "m"}
    assert json.loads(history_value) == {"summary": "done", "methods": "m"}
