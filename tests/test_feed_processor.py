"""Tests for processors.feed_processor.FeedProcessor.

Exercises regex filtering, pattern matching, feed deduplication, and entry
storage using the same data flow as a real filter run, but with local feeds.
"""

import re
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paper_firehose.core.config import ConfigManager
from paper_firehose.core.database import DatabaseManager
from paper_firehose.processors.feed_processor import FeedProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_env(tmp_path, topic_yaml=None, config_yaml=None, feed_uri=None):
    """Bootstrap a config + DB in tmp_path; return (config_manager, db_manager)."""
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)

    feed_path = Path(__file__).parent / "fixtures" / "sample_feed.xml"
    uri = feed_uri or feed_path.resolve().as_uri()

    if config_yaml is None:
        config_yaml = textwrap.dedent(f"""
            database:
              path: "{tmp_path}/papers.db"
              all_feeds_path: "{tmp_path}/all_feed_entries.db"
              history_path: "{tmp_path}/matched_entries_history.db"
            feeds:
              local_feed:
                name: "Local Test Feed"
                url: "{uri}"
                enabled: true
            priority_journals: []
            defaults:
              time_window_days: 365
        """).strip() + "\n"

    if topic_yaml is None:
        topic_yaml = textwrap.dedent("""
            name: "test_topic"
            description: "Test topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "graphene"
              fields: ["title", "summary"]
            ranking:
              query: "graphene materials"
            output:
              archive: true
        """).strip() + "\n"

    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    (topics_dir / "test_topic.yaml").write_text(topic_yaml, encoding="utf-8")

    from paper_firehose.core import config as core_config
    with patch.object(core_config, "_copy_tree", return_value=False):
        cfg_mgr = ConfigManager(str(config_path))

    cfg = cfg_mgr.load_config()
    db = DatabaseManager(cfg)
    return cfg_mgr, db


# ---------------------------------------------------------------------------
# Pattern matching
# ---------------------------------------------------------------------------

class TestPatternMatching:
    def test_matches_title(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Graphene nanoribbon study", "summary": "Unrelated."}
        assert proc._matches_pattern(entry, regex, ["title"]) is True

    def test_matches_summary(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Materials study", "summary": "New results on graphene oxide."}
        assert proc._matches_pattern(entry, regex, ["summary"]) is True

    def test_no_match(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("graphene", re.IGNORECASE)
        entry = {"title": "Silicon solar cells", "summary": "Photovoltaic efficiency."}
        assert proc._matches_pattern(entry, regex, ["title", "summary"]) is False

    def test_matches_author_field(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("Novoselov", re.IGNORECASE)
        entry = {"title": "2D materials", "summary": "s",
                 "authors": [{"name": "K. S. Novoselov"}]}
        assert proc._matches_pattern(entry, regex, ["authors"]) is True

    def test_case_insensitive_match(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile("GRAPHENE", re.IGNORECASE)
        entry = {"title": "graphene", "summary": ""}
        assert proc._matches_pattern(entry, regex, ["title"]) is True

    def test_complex_regex_pattern(self, tmp_path):
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        regex = re.compile(r"perovsk|halide.*solar|CsPb", re.IGNORECASE)
        assert proc._matches_pattern({"title": "CsPbBr3 quantum dots", "summary": ""},
                                     regex, ["title"]) is True
        assert proc._matches_pattern({"title": "halide solar cell", "summary": ""},
                                     regex, ["title"]) is True
        assert proc._matches_pattern({"title": "silicon wafer", "summary": ""},
                                     regex, ["title"]) is False


# ---------------------------------------------------------------------------
# apply_filters — end-to-end with local feed
# ---------------------------------------------------------------------------

class TestApplyFilters:
    def test_filters_local_feed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        # Fetch real entries from the local sample feed
        entries_per_feed = proc.fetch_feeds("test_topic")

        matched = proc.apply_filters(entries_per_feed, "test_topic")
        titles = [e["title"] for e in matched]
        assert "Graphene breakthroughs in materials science" in titles
        assert "Other topic unrelated to filters" not in titles

    def test_matched_entries_saved_to_history(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.apply_filters(entries_per_feed, "test_topic")

        with db.get_connection("history") as conn:
            count = conn.execute("SELECT COUNT(*) FROM matched_entries").fetchone()[0]
        assert count >= 1

    def test_matched_entries_saved_to_current(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)
        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.apply_filters(entries_per_feed, "test_topic")

        rows = db.get_current_entries(topic="test_topic")
        assert len(rows) >= 1
        assert all(r["status"] == "filtered" for r in rows)

    def test_invalid_regex_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        topic_yaml = textwrap.dedent("""
            name: "test_topic"
            description: "Bad regex topic"
            feeds:
              - "local_feed"
            filter:
              pattern: "[invalid(regex"
              fields: ["title"]
            ranking:
              query: "test"
        """).strip() + "\n"
        cfg_mgr, db = _make_env(tmp_path, topic_yaml=topic_yaml)
        proc = FeedProcessor(db, cfg_mgr)
        entries = {"local_feed": [{"title": "test", "link": "http://x", "id": "1"}]}
        matched = proc.apply_filters(entries, "test_topic")
        assert matched == []


# ---------------------------------------------------------------------------
# save_all_entries_to_dedup_db
# ---------------------------------------------------------------------------

class TestSaveAllEntries:
    def test_entries_persisted_to_dedup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.save_all_entries_to_dedup_db(entries_per_feed)

        with db.get_connection("all_feeds") as conn:
            count = conn.execute("SELECT COUNT(*) FROM feed_entries").fetchone()[0]
        # sample_feed.xml has 2 entries
        assert count >= 2

    def test_dedup_prevents_recount(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _make_env(tmp_path)
        proc = FeedProcessor(db, cfg_mgr)

        entries_per_feed = proc.fetch_feeds("test_topic")
        proc.save_all_entries_to_dedup_db(entries_per_feed)

        # Second fetch — titles already in dedup DB
        entries_per_feed2 = proc.fetch_feeds("test_topic")
        total_new = sum(len(v) for v in entries_per_feed2.values())
        assert total_new == 0


# ---------------------------------------------------------------------------
# HTTP fetching: cookie checks, HTTP errors, timeouts (issue #87)
# ---------------------------------------------------------------------------

import email.utils
import gzip
import io
import threading
import feedparser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _rss(titles):
    now = email.utils.formatdate(usegmt=True)
    items = "".join(
        f"<item><title>{t}</title><link>http://example.com/{i}</link>"
        f"<description>graphene {i}</description><pubDate>{now}</pubDate></item>"
        for i, t in enumerate(titles)
    )
    return (f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            f"<title>HTTP Test Feed</title>{items}</channel></rss>").encode("utf-8")


_GZIP_BOMB = gzip.compress(b" " * (30 * 1024 * 1024), 9)
_DOUBLE_GZIP = gzip.compress(gzip.compress(b" " * (8 * 1024 * 1024), 9), 9)


class _FeedHandler(BaseHTTPRequestHandler):
    """Mimics an Atypon-style cookie check plus error/slow endpoints."""

    feed_body = _rss(["Graphene paper A", "Graphene paper B"])

    def log_message(self, *args):  # keep pytest output quiet
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        has_cookie = "session=ok" in (self.headers.get("Cookie") or "")
        if self.path == "/action/showFeed":
            self._send(302, headers={"Location": "/action/showFeed?cookieSet=1",
                                     "Set-Cookie": "session=ok; Path=/"})
        elif self.path == "/action/showFeed?cookieSet=1":
            if has_cookie:
                self._send(200, self.feed_body, {"Content-Type": "application/rss+xml"})
            else:
                self._send(302, headers={"Location": "/action/cookieAbsent"})
        elif self.path == "/action/cookieAbsent":
            self._send(403, b"cookies required")
        elif self.path == "/forbidden":
            self._send(403, b"no")
        elif self.path == "/slow":
            time.sleep(2)
            self._send(200, self.feed_body, {"Content-Type": "application/rss+xml"})
        elif self.path == "/filename":
            # The whole answer is the path of a local feed file (issue #90).
            body = str(Path(__file__).parent / "fixtures" / "sample_feed.xml").encode()
            self._send(200, body, {"Content-Type": "application/rss+xml"})
        elif self.path == "/gzip-bomb":
            # ~30 KB on the wire, 30 MB unpacked (one gzip layer).
            self._send(200, _GZIP_BOMB, {"Content-Type": "application/rss+xml",
                                         "Content-Encoding": "gzip"})
        elif self.path == "/gzip-gzip":
            # Packed twice (issue #91). Kept small so a regression stays harmless.
            self._send(200, _DOUBLE_GZIP, {"Content-Type": "application/rss+xml",
                                           "Content-Encoding": "gzip, gzip"})
        elif self.path == "/gzip-ok":
            self._send(200, gzip.compress(self.feed_body),
                       {"Content-Type": "application/rss+xml", "Content-Encoding": "gzip"})
        elif self.path == "/huge":
            self._send(200, b"<rss>" + b" " * 5000 + b"</rss>",
                       {"Content-Type": "application/rss+xml"})
        elif self.path == "/trickle":
            # Each read is quick, but the whole answer takes ~2 s.
            body = self.feed_body
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            step = max(1, len(body) // 20)
            try:
                for i in range(0, len(body), step):
                    self.wfile.write(body[i:i + step])
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self._send(404, b"not found")


@pytest.fixture
def feed_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FeedHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _http_env(tmp_path, url, timeout=None):
    extra = f"\n  feed_timeout_seconds: {timeout}" if timeout is not None else ""
    config_yaml = textwrap.dedent(f"""
        database:
          path: "{tmp_path}/papers.db"
          all_feeds_path: "{tmp_path}/all_feed_entries.db"
          history_path: "{tmp_path}/matched_entries_history.db"
        feeds:
          local_feed:
            name: "HTTP Test Feed"
            url: "{url}"
            enabled: true
        priority_journals: []
        defaults:
          time_window_days: 365
    """).strip() + extra + "\n"
    return _make_env(tmp_path, config_yaml=config_yaml)


class TestHttpFetching:
    def test_cookie_check_feed_is_read(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        url = f"{feed_server}/action/showFeed"
        # Sanity check: plain feedparser drops the cookie and gets nothing.
        assert len(feedparser.parse(url).entries) == 0

        cfg_mgr, db = _http_env(tmp_path, url)
        proc = FeedProcessor(db, cfg_mgr)
        entries = proc.fetch_feeds("test_topic")["local_feed"]
        assert sorted(e["title"] for e in entries) == ["Graphene paper A", "Graphene paper B"]
        assert proc.failed_feeds == []

    def test_http_error_recorded_as_failed(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        url = f"{feed_server}/forbidden"
        cfg_mgr, db = _http_env(tmp_path, url)
        proc = FeedProcessor(db, cfg_mgr)
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert len(proc.failed_feeds) == 1
        failure = proc.failed_feeds[0]
        assert failure["feed"] == "local_feed"
        assert failure["url"] == url
        assert "403" in failure["error"]

    def test_failure_recorded_once_per_feed(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/forbidden")
        proc = FeedProcessor(db, cfg_mgr)
        proc.fetch_feeds("test_topic")
        proc.fetch_feeds("test_topic")
        assert len(proc.failed_feeds) == 1

    def test_slow_feed_times_out(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/slow", timeout=0.5)
        proc = FeedProcessor(db, cfg_mgr)
        start = time.monotonic()
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert time.monotonic() - start < 1.9
        assert len(proc.failed_feeds) == 1

    def test_unreadable_local_feed_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        missing = (tmp_path / "missing.xml").as_uri()
        cfg_mgr, db = _http_env(tmp_path, missing)
        proc = FeedProcessor(db, cfg_mgr)
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert [f["feed"] for f in proc.failed_feeds] == ["local_feed"]


class TestHttpBodyHandling:
    """Issue #90: the response body is data, never a file name; size and time are bounded."""

    def test_body_naming_a_local_file_is_not_opened(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/filename")
        proc = FeedProcessor(db, cfg_mgr)
        # The named file is a valid feed: reading it would yield its entries.
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert [f["feed"] for f in proc.failed_feeds] == ["local_feed"]
        assert "could not parse feed" in proc.failed_feeds[0]["error"]

    def test_oversized_body_rejected(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/huge")
        proc = FeedProcessor(db, cfg_mgr)
        proc.feed_max_bytes = 1000
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert "larger than 1000 bytes" in proc.failed_feeds[0]["error"]

    def test_body_within_limit_accepted(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/action/showFeed")
        proc = FeedProcessor(db, cfg_mgr)
        proc.feed_max_bytes = len(_FeedHandler.feed_body)
        assert len(proc.fetch_feeds("test_topic")["local_feed"]) == 2
        assert proc.failed_feeds == []

    def test_trickling_body_bounded_by_total_timeout(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/trickle", timeout=0.5)
        proc = FeedProcessor(db, cfg_mgr)
        start = time.monotonic()
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert time.monotonic() - start < 1.5
        assert "longer than 0.5 s" in proc.failed_feeds[0]["error"]


class TestCompressedBodies:
    """Issue #91: compressed bodies are bounded while they are unpacked."""

    def test_gzip_feed_is_read(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/gzip-ok")
        proc = FeedProcessor(db, cfg_mgr)
        assert len(proc.fetch_feeds("test_topic")["local_feed"]) == 2
        assert proc.failed_feeds == []

    def test_gzip_bomb_refused_by_size_limit(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/gzip-bomb")
        proc = FeedProcessor(db, cfg_mgr)
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert "larger than" in proc.failed_feeds[0]["error"]

    def test_chained_content_encoding_refused(self, tmp_path, monkeypatch, feed_server):
        monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
        cfg_mgr, db = _http_env(tmp_path, f"{feed_server}/gzip-gzip")
        proc = FeedProcessor(db, cfg_mgr)
        assert proc.fetch_feeds("test_topic") == {"local_feed": []}
        assert "Content-Encoding" in proc.failed_feeds[0]["error"]

    def test_urllib3_read1_bounds_decompression(self):
        """Guards the urllib3 pin: read1(n) must not unpack far more than n bytes.

        urllib3 < 2.6 returns at most n bytes but first unpacks everything the
        compressed bytes it read expand to into an internal buffer (here 30 MB
        from one call), which defeats the size limit in ``_download``.
        """
        import tracemalloc
        import urllib3
        response = urllib3.HTTPResponse(
            body=io.BytesIO(_GZIP_BOMB),
            headers={"content-encoding": "gzip"},
            preload_content=False,
        )
        tracemalloc.start()
        try:
            chunk = response.read1(64 * 1024, decode_content=True)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert 0 < len(chunk) <= 64 * 1024
        assert peak < 5 * 1024 * 1024, (
            f"urllib3 {urllib3.__version__} allocated {peak // (1024 * 1024)} MB to return {len(chunk)} bytes"
        )
