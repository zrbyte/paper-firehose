import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.commands import export_recent as export_cmd  # noqa: E402


def create_test_history_db(db_path: Path, entries: list[dict]) -> None:
    """Create a test history database with the given entries.

    Args:
        db_path: Path to the database file
        entries: List of entry dicts with keys: title, link, matched_date, rank_score
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Create schema matching the real matched_entries table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matched_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            published TEXT,
            summary TEXT,
            authors TEXT,
            feed_name TEXT,
            matched_date TEXT NOT NULL,
            rank_score REAL,
            abstract TEXT,
            doi TEXT,
            paper_qa_summary TEXT,
            raw_data TEXT
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_matched_date ON matched_entries(matched_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_topic ON matched_entries(topic)")

    # Insert test data
    for entry in entries:
        cursor.execute("""
            INSERT INTO matched_entries
            (topic, title, link, matched_date, rank_score, published, summary, authors, feed_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.get('topic', 'test_topic'),
            entry['title'],
            entry['link'],
            entry['matched_date'],
            entry.get('rank_score', 0.5),
            entry.get('published', entry['matched_date']),
            entry.get('summary', 'Test summary'),
            entry.get('authors', 'Test Author'),
            entry.get('feed_name', 'Test Feed')
        ))

    conn.commit()
    conn.close()


def count_entries(db_path: Path) -> int:
    """Count entries in the database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM matched_entries")
    count = cursor.fetchone()[0]
    conn.close()
    return count


def get_entries(db_path: Path) -> list[dict]:
    """Get all entries from the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM matched_entries ORDER BY matched_date DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_table_schema(db_path: Path) -> list[tuple]:
    """Get the table schema from the database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(matched_entries)")
    schema = cursor.fetchall()
    conn.close()
    return schema


def get_indexes(db_path: Path) -> list[str]:
    """Get index names from the database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='matched_entries'")
    indexes = [row[0] for row in cursor.fetchall()]
    conn.close()
    return indexes


def test_export_recent_with_default_days(tmp_path, monkeypatch):
    """Test export with default 60-day window."""
    # Setup
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    topics_dir = config_dir / "topics"
    topics_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    # Create config
    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create test data with entries spanning 90 days
    now = datetime.now()
    entries = [
        {
            'title': 'Recent paper 1',
            'link': 'http://example.com/1',
            'matched_date': (now - timedelta(days=10)).strftime('%Y-%m-%d'),
            'rank_score': 0.9
        },
        {
            'title': 'Recent paper 2',
            'link': 'http://example.com/2',
            'matched_date': (now - timedelta(days=30)).strftime('%Y-%m-%d'),
            'rank_score': 0.8
        },
        {
            'title': 'Recent paper 3',
            'link': 'http://example.com/3',
            'matched_date': (now - timedelta(days=50)).strftime('%Y-%m-%d'),
            'rank_score': 0.7
        },
        {
            'title': 'Old paper 1',
            'link': 'http://example.com/4',
            'matched_date': (now - timedelta(days=70)).strftime('%Y-%m-%d'),
            'rank_score': 0.6
        },
        {
            'title': 'Old paper 2',
            'link': 'http://example.com/5',
            'matched_date': (now - timedelta(days=85)).strftime('%Y-%m-%d'),
            'rank_score': 0.5
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # Verify source has 5 entries
    assert count_entries(history_db_path) == 5

    # Run export with default 60 days
    export_cmd.run(str(config_path), days=60)

    # Check output database
    output_db_path = data_dir / "matched_entries_history.recent.db"
    assert output_db_path.exists()

    # Should have 3 recent entries (within 60 days)
    exported_count = count_entries(output_db_path)
    assert exported_count == 3

    # Verify the correct entries were exported
    exported_entries = get_entries(output_db_path)
    exported_titles = {entry['title'] for entry in exported_entries}
    assert 'Recent paper 1' in exported_titles
    assert 'Recent paper 2' in exported_titles
    assert 'Recent paper 3' in exported_titles
    assert 'Old paper 1' not in exported_titles
    assert 'Old paper 2' not in exported_titles


def test_export_recent_with_custom_days(tmp_path):
    """Test export with custom day window."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create test data
    now = datetime.now()
    entries = [
        {
            'title': 'Very recent',
            'link': 'http://example.com/1',
            'matched_date': (now - timedelta(days=5)).strftime('%Y-%m-%d'),
            'rank_score': 0.9
        },
        {
            'title': 'Medium recent',
            'link': 'http://example.com/2',
            'matched_date': (now - timedelta(days=20)).strftime('%Y-%m-%d'),
            'rank_score': 0.8
        },
        {
            'title': 'Older',
            'link': 'http://example.com/3',
            'matched_date': (now - timedelta(days=40)).strftime('%Y-%m-%d'),
            'rank_score': 0.7
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # Export only last 15 days
    export_cmd.run(str(config_path), days=15)

    output_db_path = data_dir / "matched_entries_history.recent.db"
    assert output_db_path.exists()

    # Should have only 1 entry (within 15 days)
    exported_count = count_entries(output_db_path)
    assert exported_count == 1

    exported_entries = get_entries(output_db_path)
    assert exported_entries[0]['title'] == 'Very recent'


def test_export_recent_with_custom_output_name(tmp_path):
    """Test export with custom output filename."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create test data
    now = datetime.now()
    entries = [
        {
            'title': 'Test paper',
            'link': 'http://example.com/1',
            'matched_date': now.strftime('%Y-%m-%d'),
            'rank_score': 0.9
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # Export with custom output name
    custom_output = str(data_dir / "custom_export.db")
    export_cmd.run(str(config_path), days=30, output_name=custom_output)

    # Check custom output exists
    assert Path(custom_output).exists()
    assert count_entries(Path(custom_output)) == 1


def test_export_recent_with_no_entries(tmp_path):
    """Test export when no entries match the time window."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create old entries only
    now = datetime.now()
    entries = [
        {
            'title': 'Very old paper',
            'link': 'http://example.com/1',
            'matched_date': (now - timedelta(days=365)).strftime('%Y-%m-%d'),
            'rank_score': 0.5
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # Export last 60 days (should find nothing)
    export_cmd.run(str(config_path), days=60)

    output_db_path = data_dir / "matched_entries_history.recent.db"
    assert output_db_path.exists()

    # Should have 0 entries
    assert count_entries(output_db_path) == 0


def test_export_recent_missing_source_database(tmp_path, caplog):
    """Test export when source database doesn't exist."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/nonexistent.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Run export (should handle gracefully)
    export_cmd.run(str(config_path), days=60)

    # Check that error was logged
    assert any("not found" in record.message.lower() for record in caplog.records)


def test_export_recent_schema_and_indexes_copied(tmp_path):
    """Test that schema and indexes are correctly copied to output database."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create test data
    now = datetime.now()
    entries = [
        {
            'title': 'Test paper',
            'link': 'http://example.com/1',
            'matched_date': now.strftime('%Y-%m-%d'),
            'rank_score': 0.9
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # Get source schema and indexes
    source_schema = get_table_schema(history_db_path)
    source_indexes = get_indexes(history_db_path)

    # Run export
    export_cmd.run(str(config_path), days=60)

    output_db_path = data_dir / "matched_entries_history.recent.db"

    # Verify schema matches
    output_schema = get_table_schema(output_db_path)
    assert output_schema == source_schema

    # Verify indexes match
    output_indexes = get_indexes(output_db_path)
    # Filter out auto-created sqlite indexes
    source_indexes_filtered = [idx for idx in source_indexes if not idx.startswith('sqlite_')]
    output_indexes_filtered = [idx for idx in output_indexes if not idx.startswith('sqlite_')]
    assert set(source_indexes_filtered) == set(output_indexes_filtered)


def test_export_recent_replaces_existing_output(tmp_path):
    """Test that export replaces existing output database."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create test data - first run
    now = datetime.now()
    entries = [
        {
            'title': 'First run paper',
            'link': 'http://example.com/1',
            'matched_date': now.strftime('%Y-%m-%d'),
            'rank_score': 0.9
        }
    ]

    history_db_path = data_dir / "matched_entries_history.db"
    create_test_history_db(history_db_path, entries)

    # First export
    export_cmd.run(str(config_path), days=60)

    output_db_path = data_dir / "matched_entries_history.recent.db"
    first_mtime = output_db_path.stat().st_mtime

    # Update database with new entry
    conn = sqlite3.connect(history_db_path)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO matched_entries
        (topic, title, link, matched_date, rank_score, published, summary, authors, feed_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'test_topic',
        'Second run paper',
        'http://example.com/2',
        now.strftime('%Y-%m-%d'),
        0.8,
        now.strftime('%Y-%m-%d'),
        'Test summary',
        'Test Author',
        'Test Feed'
    ))
    conn.commit()
    conn.close()

    # Second export
    export_cmd.run(str(config_path), days=60)

    # Verify file was replaced
    second_mtime = output_db_path.stat().st_mtime
    assert second_mtime >= first_mtime

    # Verify new data is present
    exported_entries = get_entries(output_db_path)
    assert len(exported_entries) == 2
    titles = {entry['title'] for entry in exported_entries}
    assert 'First run paper' in titles
    assert 'Second run paper' in titles


def test_export_recent_with_all_fields_populated(tmp_path):
    """Test export preserves all database fields including abstracts and DOIs."""
    data_dir = tmp_path / "data"
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    config_yaml = f"""
database:
  path: "papers.db"
  all_feeds_path: "all_feed_entries.db"
  history_path: "{data_dir}/matched_entries_history.db"
feeds:
  test_feed:
    name: "Test Feed"
    url: "http://example.com/feed"
    enabled: true
"""
    config_path = config_dir / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")

    # Create entry with all fields populated
    history_db_path = data_dir / "matched_entries_history.db"
    conn = sqlite3.connect(history_db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS matched_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            published TEXT,
            summary TEXT,
            authors TEXT,
            feed_name TEXT,
            matched_date TEXT NOT NULL,
            rank_score REAL,
            abstract TEXT,
            doi TEXT,
            paper_qa_summary TEXT,
            raw_data TEXT
        )
    """)

    now = datetime.now()
    cursor.execute("""
        INSERT INTO matched_entries
        (topic, title, link, published, summary, authors, feed_name, matched_date,
         rank_score, abstract, doi, paper_qa_summary, raw_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        'test_topic',
        'Complete paper',
        'http://example.com/1',
        now.strftime('%Y-%m-%d'),
        'Short summary',
        'Author A, Author B',
        'Nature',
        now.strftime('%Y-%m-%d'),
        0.95,
        'This is the full abstract text',
        '10.1234/test.doi',
        '{"summary": "PQA summary", "methods": "Test methods"}',
        '{"raw": "data"}'
    ))

    conn.commit()
    conn.close()

    # Run export
    export_cmd.run(str(config_path), days=60)

    # Verify all fields were preserved
    output_db_path = data_dir / "matched_entries_history.recent.db"
    exported_entries = get_entries(output_db_path)

    assert len(exported_entries) == 1
    entry = exported_entries[0]

    assert entry['topic'] == 'test_topic'
    assert entry['title'] == 'Complete paper'
    assert entry['link'] == 'http://example.com/1'
    assert entry['summary'] == 'Short summary'
    assert entry['authors'] == 'Author A, Author B'
    assert entry['feed_name'] == 'Nature'
    assert entry['rank_score'] == 0.95
    assert entry['abstract'] == 'This is the full abstract text'
    assert entry['doi'] == '10.1234/test.doi'
    assert entry['paper_qa_summary'] == '{"summary": "PQA summary", "methods": "Test methods"}'
    assert entry['raw_data'] == '{"raw": "data"}'
