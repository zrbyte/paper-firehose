#!/usr/bin/env python3
"""
Merge historical databases and HTML entries from assets_old/ into a backup copy
of the current `matched_entries_history.db` stored under the runtime data
directory (defaults to `~/.paper_firehose/`).

Steps:
1) Create a timestamped backup copy of the active history DB in the data dir.
2) Merge entries from assets_old/matched_entries_history.latest.db into the backup.
3) Merge entries from assets_old/matched_entries_history_ek-server.db into the backup.
4) Parse assets_old/rg_filtered_articles.html and add entries (topic = 'rg')
   with title, authors, link, and published_date.

Fields missing in older data are left empty.

Usage:
  python scripts/merge_assets_old.py

You can override paths with CLI args; see --help.
"""

import argparse
import os
import re
import json
import shutil
import sqlite3
import hashlib
import urllib.parse
from datetime import datetime
from typing import Dict, Any, Tuple, Optional
import html as htmllib
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / 'src'
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from core.paths import ensure_data_dir

DATA_DIR = ensure_data_dir()
ASSETS_DIR = str(DATA_DIR)
ASSETS_OLD_DIR = os.path.join(str(REPO_ROOT), 'assets_old')


def timestamp() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def normalize_date(value: str) -> str:
    if not value:
        return ''
    s = str(value).strip()
    # Try ISO 8601
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return dt.date().isoformat()
    except Exception:
        pass
    # Try RFC 2822 style: Tue, 24 Sep 2024 00:00:00 -0400
    for fmt in [
        '%a, %d %b %Y %H:%M:%S %z',
        '%a, %d %b %Y %H:%M:%S %Z',
        '%d %b %Y %H:%M:%S %z',
        '%Y-%m-%d %H:%M:%S',
        '%Y/%m/%d %H:%M:%S',
    ]:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date().isoformat()
        except Exception:
            continue
    # Extract YYYY-MM-DD if present
    m = re.search(r'(\d{4})[-/](\d{2})[-/](\d{2})', s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s


def compute_entry_id(link: str, title: str = '', published: str = '') -> str:
    candidate = link or ''
    if candidate:
        parsed = urllib.parse.urlparse(candidate)
        candidate = urllib.parse.urlunparse(parsed._replace(query='', fragment=''))
        return hashlib.sha1(candidate.encode('utf-8')).hexdigest()
    concat = '||'.join([title or '', published or ''])
    return hashlib.sha1(concat.encode('utf-8')).hexdigest()


def ensure_dest_schema(dest_path: str) -> None:
    conn = sqlite3.connect(dest_path)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS matched_entries (
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
            matched_date TEXT DEFAULT (datetime('now')),
            raw_data TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_matched_entries_topics ON matched_entries(topics)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_matched_entries_matched_date ON matched_entries(matched_date)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_matched_entries_entry_id ON matched_entries(entry_id)')
    conn.commit()
    conn.close()


def get_table_columns(conn: sqlite3.Connection, table: str) -> Dict[str, int]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = {}
    for row in cur.fetchall():
        cols[row[1]] = row[0]
    return cols


def row_value(row: Tuple, idx_by_name: Dict[str, int], names) -> Optional[str]:
    for n in names:
        if n in idx_by_name:
            v = row[idx_by_name[n]]
            return v if v is not None else None
    return None


def merge_topics(existing: str, incoming: str) -> str:
    def split_topics(s: str):
        if not s:
            return []
        parts = [p.strip() for part in s.split(';') for p in part.split(',')]
        return [p for p in parts if p]

    xs = set(split_topics(existing))
    for t in split_topics(incoming):
        xs.add(t)
    return ', '.join(sorted(xs))


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)


def _find_doi_in_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = str(text).strip()
    if t.lower().startswith('doi:'):
        t = t[4:].strip()
    m = DOI_RE.search(t)
    return m.group(0) if m else None


def _extract_from_raw(raw: Optional[str]) -> Dict[str, Any]:
    data = {}
    if not raw:
        return data
    try:
        obj = json.loads(raw)
    except Exception:
        return data
    # Title / link / summary
    data['title'] = obj.get('title') or ''
    data['link'] = obj.get('link') or obj.get('id') or ''
    data['summary'] = obj.get('summary') or obj.get('description') or ''
    # Authors
    auth = obj.get('authors')
    if isinstance(auth, list) and auth:
        data['authors'] = ', '.join(a.get('name', '') for a in auth if isinstance(a, dict))
    else:
        data['authors'] = obj.get('author') or ''
    # DOI
    doi = (
        obj.get('doi')
        or _find_doi_in_text(obj.get('dc_identifier') or obj.get('dc:identifier') or obj.get('dc.identifier') or obj.get('prism:doi'))
        or _find_doi_in_text(obj.get('id'))
        or _find_doi_in_text(obj.get('link'))
        or _find_doi_in_text(obj.get('summary'))
        or _find_doi_in_text((obj.get('summary_detail') or {}).get('value') if isinstance(obj.get('summary_detail'), dict) else None)
        or _find_doi_in_text(obj.get('description'))
    )
    if not doi and isinstance(obj.get('content'), list):
        for c in obj['content']:
            if isinstance(c, dict):
                doi = _find_doi_in_text(c.get('value') or c.get('content'))
                if doi:
                    break
    if not doi and isinstance(obj.get('links'), list):
        for l in obj['links']:
            href = l.get('href') if isinstance(l, dict) else str(l)
            doi = _find_doi_in_text(href)
            if doi:
                break
    data['doi'] = doi or None
    # Published date
    pub = obj.get('published') or obj.get('updated') or ''
    data['published_date'] = normalize_date(pub)
    # Feed name if present
    data['feed_name'] = obj.get('feed_name') or obj.get('feed_title') or ''
    # Topic from raw, if present
    raw_topic = obj.get('topic') or obj.get('topics')
    if isinstance(raw_topic, list):
        data['topics'] = ', '.join(t for t in raw_topic if isinstance(t, str))
    elif isinstance(raw_topic, str):
        data['topics'] = raw_topic
    else:
        data['topics'] = ''
    return data


def _coalesce_empty(existing: Optional[str], incoming: Optional[str]) -> Optional[str]:
    if existing is None:
        existing_s = ''
    else:
        existing_s = str(existing).strip()
    inc_s = '' if incoming is None else str(incoming).strip()
    if (not existing_s) or (existing_s.lower() == 'null'):
        return incoming if inc_s else existing
    return existing


def upsert_entry(dest: sqlite3.Connection, rec: Dict[str, Any]) -> None:
    cur = dest.cursor()
    cur.execute(
        "SELECT topics, raw_data, feed_name, title, link, summary, authors, abstract, doi, published_date FROM matched_entries WHERE entry_id = ?",
        (rec['entry_id'],),
    )
    existing = cur.fetchone()
    if existing:
        existing_topics = existing[0] or ''
        existing_raw = existing[1]
        # Enrich incoming from its raw_data if incoming lacks fields
        rec_enriched = rec.copy()
        if (not rec_enriched.get('title') or not rec_enriched.get('link') or not rec_enriched.get('authors') or not rec_enriched.get('published_date') or not rec_enriched.get('doi')) and rec_enriched.get('raw_data'):
            extracted = _extract_from_raw(rec_enriched.get('raw_data'))
            for k in ['title', 'link', 'summary', 'authors', 'doi', 'published_date', 'feed_name']:
                if not rec_enriched.get(k):
                    rec_enriched[k] = extracted.get(k) or rec_enriched.get(k)
            # Merge topics from raw JSON too
            if extracted.get('topics'):
                rec_enriched['topics'] = merge_topics(rec_enriched.get('topics') or '', extracted.get('topics'))
        merged_topics = merge_topics(existing_topics, rec_enriched.get('topics') or '')
        # Update raw_data only if missing/empty and incoming has value
        new_raw = None
        incoming_raw = rec_enriched.get('raw_data')
        if incoming_raw is not None and str(incoming_raw).strip() != '':
            if existing_raw is None or str(existing_raw).strip() == '' or str(existing_raw).strip().lower() == 'null':
                new_raw = incoming_raw
        # Also fill other empty columns from incoming if they exist
        existing_feed, existing_title, existing_link, existing_summary, existing_authors, existing_abstract, existing_doi, existing_published = existing[2:]
        updated_fields = {
            'feed_name': _coalesce_empty(existing_feed, rec_enriched.get('feed_name')),
            'title': _coalesce_empty(existing_title, rec_enriched.get('title')),
            'link': _coalesce_empty(existing_link, rec_enriched.get('link')),
            'summary': _coalesce_empty(existing_summary, rec_enriched.get('summary')),
            'authors': _coalesce_empty(existing_authors, rec_enriched.get('authors')),
            'abstract': _coalesce_empty(existing_abstract, rec_enriched.get('abstract')),
            'doi': _coalesce_empty(existing_doi, rec_enriched.get('doi')),
            'published_date': _coalesce_empty(existing_published, rec_enriched.get('published_date')),
        }
        set_clauses = ["topics = ?", "matched_date = COALESCE(?, matched_date)"]
        params = [merged_topics, rec.get('matched_date')]
        if new_raw is not None:
            set_clauses.append("raw_data = ?")
            params.append(new_raw)
        for col, val in updated_fields.items():
            if val is not None and str(val).strip() != str(locals().get(f'existing_{col}', '')).strip():
                set_clauses.append(f"{col} = ?")
                params.append(val)
        params.append(rec['entry_id'])
        cur.execute(f"UPDATE matched_entries SET {', '.join(set_clauses)} WHERE entry_id = ?", params)
    else:
        # Enrich from raw_data before inserting to populate as many fields as possible
        rec_enriched = rec.copy()
        if rec_enriched.get('raw_data'):
            extracted = _extract_from_raw(rec_enriched.get('raw_data'))
            for k in ['title', 'link', 'summary', 'authors', 'doi', 'published_date', 'feed_name']:
                if not rec_enriched.get(k):
                    rec_enriched[k] = extracted.get(k) or rec_enriched.get(k)
        cur.execute(
            '''INSERT INTO matched_entries 
               (entry_id, feed_name, topics, title, link, summary, authors, abstract, doi,
                published_date, matched_date, raw_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)''',
            (
                rec_enriched.get('entry_id'), rec_enriched.get('feed_name') or '', rec_enriched.get('topics') or '',
                rec_enriched.get('title') or '', rec_enriched.get('link') or '', rec_enriched.get('summary') or '',
                rec_enriched.get('authors') or '', rec_enriched.get('abstract'), rec_enriched.get('doi'),
                rec_enriched.get('published_date') or '', rec_enriched.get('matched_date'), rec_enriched.get('raw_data')
            )
        )


def merge_source_db_into(dest_path: str, source_path: str) -> int:
    if not os.path.exists(source_path):
        print(f"Source DB not found: {source_path}")
        return 0
    src = sqlite3.connect(source_path)
    src_cur = src.cursor()
    src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='matched_entries'")
    if not src_cur.fetchone():
        print(f"No 'matched_entries' table in {source_path}")
        src.close()
        return 0
    cols = get_table_columns(src, 'matched_entries')
    idx_by_name = cols
    src_cur.execute('SELECT * FROM matched_entries')
    rows = src_cur.fetchall()

    dest = sqlite3.connect(dest_path)
    dest.execute('BEGIN')
    count = 0
    for row in rows:
        get = lambda *names: row_value(row, idx_by_name, names)
        link = (get('link', 'url', 'guid') or '').strip()
        title = (get('title') or '').strip()
        published_raw = get('published_date', 'published', 'date', 'published_at', 'publication date') or ''
        published_date = normalize_date(published_raw)
        entry_id = (get('entry_id') or compute_entry_id(link, title, published_date))
        # Merge topics with search_terms/search_type if present in source DB
        topics_val = get('topics', 'topic') or ''
        search_terms_val = get('search_terms', 'search_term', 'search_type') or ''
        if search_terms_val:
            topics_val = merge_topics(topics_val, search_terms_val)
        matched_date = get('matched_date', 'timestamp')
        rec = {
            'entry_id': entry_id,
            'feed_name': get('feed_name', 'feed') or '',
            'topics': topics_val,
            'title': title,
            'link': link,
            'summary': get('summary', 'description') or '',
            'authors': get('authors', 'author') or '',
            'abstract': get('abstract'),
            'doi': get('doi'),
            'published_date': published_date,
            'matched_date': matched_date,
            'raw_data': get('raw_data', 'data')
        }
        upsert_entry(dest, rec)
        count += 1
    dest.commit()
    dest.close()
    src.close()
    return count


def strip_tags(s: str) -> str:
    return re.sub(r'<[^>]+>', '', s)


def parse_rg_html_and_insert(dest_path: str, html_path: str) -> int:
    if not os.path.exists(html_path):
        print(f"rg HTML not found: {html_path}")
        return 0
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()

    # Iterate through document maintaining current feed name
    tokens = re.finditer(r'(<h2>.*?</h2>)|(<div\s+class="entry">.*?</div>)', html, re.DOTALL | re.IGNORECASE)
    current_feed = ''
    inserted = 0
    dest = sqlite3.connect(dest_path)
    dest.execute('BEGIN')

    for m in tokens:
        block = m.group(0)
        if block.lower().startswith('<h2'):
            # Extract feed name after 'Feed:' if present
            txt = strip_tags(block)
            if 'Feed:' in txt:
                current_feed = txt.split('Feed:', 1)[1].strip()
            else:
                current_feed = txt.strip()
            continue

        # Entry block
        link = ''
        title = ''
        authors = ''
        published_raw = ''

        m_link = re.search(r'<h3>\s*<a\s+href="([^"]+)">(.*?)</a>\s*</h3>', block, re.DOTALL | re.IGNORECASE)
        if m_link:
            link = htmllib.unescape(m_link.group(1).strip())
            title = htmllib.unescape(strip_tags(m_link.group(2))).strip()
        m_auth = re.search(r'<strong>\s*Authors:\s*</strong>\s*(.*?)</p>', block, re.DOTALL | re.IGNORECASE)
        if m_auth:
            authors = htmllib.unescape(strip_tags(m_auth.group(1))).strip()
        m_pub = re.search(r'<em>\s*Published:\s*(.*?)</em>', block, re.DOTALL | re.IGNORECASE)
        if m_pub:
            published_raw = strip_tags(m_pub.group(1)).strip()

        if not link and not title:
            continue

        published_date = normalize_date(published_raw)
        entry_id = compute_entry_id(link, title, published_date)

        # Merge with existing topics if present; ensure 'rg' included
        cur = dest.cursor()
        cur.execute("SELECT topics FROM matched_entries WHERE entry_id = ?", (entry_id,))
        existing = cur.fetchone()
        topics = 'rg'
        if existing and existing[0]:
            topics = merge_topics(existing[0], 'rg')

        rec = {
            'entry_id': entry_id,
            'feed_name': current_feed or 'Unknown',
            'topics': topics,
            'title': title,
            'link': link,
            'summary': '',
            'authors': authors,
            'abstract': None,
            'doi': None,
            'published_date': published_date,
            'raw_data': None,
        }
        upsert_entry(dest, rec)
        inserted += 1

    dest.commit()
    dest.close()
    return inserted


def enrich_rows_from_raw(dest_path: str) -> int:
    """Fill empty columns from JSON in raw_data for existing rows.

    Updates rows where one or more of title/link/authors/published_date/doi/summary
    are empty or null, and raw_data contains parseable JSON.
    """
    conn = sqlite3.connect(dest_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT entry_id, raw_data, feed_name, title, link, summary, authors, abstract, doi, published_date
        FROM matched_entries
        WHERE raw_data IS NOT NULL AND TRIM(COALESCE(raw_data, '')) <> ''
        """
    )
    rows = cur.fetchall()
    updated = 0
    for entry_id, raw, feed_name, title, link, summary, authors, abstract, doi, published in rows:
        extracted = _extract_from_raw(raw)
        new_feed = _coalesce_empty(feed_name, extracted.get('feed_name'))
        new_title = _coalesce_empty(title, extracted.get('title'))
        new_link = _coalesce_empty(link, extracted.get('link'))
        new_summary = _coalesce_empty(summary, extracted.get('summary'))
        new_authors = _coalesce_empty(authors, extracted.get('authors'))
        new_doi = _coalesce_empty(doi, extracted.get('doi'))
        new_pub = _coalesce_empty(published, extracted.get('published_date'))
        # Also try to fill topics if empty and raw has a topic
        cur2 = conn.cursor()
        cur2.execute("SELECT topics FROM matched_entries WHERE entry_id = ?", (entry_id,))
        topics_existing = (cur2.fetchone() or [''])[0]
        new_topics = topics_existing
        if (not topics_existing or str(topics_existing).strip() == '') and extracted.get('topics'):
            new_topics = extracted.get('topics')
        # If any field changed, update
        if (
            (new_feed != feed_name) or (new_title != title) or (new_link != link) or
            (new_summary != summary) or (new_authors != authors) or (new_doi != doi) or
            (new_pub != published) or (new_topics != topics_existing)
        ):
            cur.execute(
                "UPDATE matched_entries SET feed_name = ?, title = ?, link = ?, summary = ?, authors = ?, doi = ?, published_date = ?, topics = ? WHERE entry_id = ?",
                (new_feed or '', new_title or '', new_link or '', new_summary or '', new_authors or '', new_doi, new_pub or '', new_topics or '', entry_id)
            )
            updated += 1
    conn.commit()
    conn.close()
    return updated


def _norm_title(title: Optional[str]) -> str:
    if title is None:
        return ''
    # Normalize whitespace and lowercase for grouping
    t = ' '.join(str(title).strip().split()).lower()
    return t


def _field_score(row: Dict[str, Any]) -> float:
    # Higher score for more populated fields
    score = 0.0
    nonempty = lambda v: (v is not None) and (str(v).strip() != '') and (str(v).strip().lower() != 'null')
    if nonempty(row.get('doi')): score += 2
    if nonempty(row.get('authors')): score += 2
    if nonempty(row.get('summary')): score += 1
    if nonempty(row.get('abstract')): score += 1
    if nonempty(row.get('link')): score += 1
    if nonempty(row.get('published_date')): score += 1
    if nonempty(row.get('feed_name')): score += 1
    if nonempty(row.get('topics')): score += 1
    if nonempty(row.get('raw_data')): score += 1
    # Tie-break: prefer longer summary
    try:
        score += min(len(row.get('summary') or ''), 1000) / 10000.0
    except Exception:
        pass
    return score


def deduplicate_by_title(dest_path: str) -> int:
    """Remove duplicates with same normalized title, keeping the row with most data.

    - For each group of same normalized title (non-empty), choose the best row by a completeness score.
    - Merge topics across group into the kept row.
    - Fill empty fields of the kept row from other rows when possible.
    - Delete the other rows.
    Returns number of deleted rows.
    """
    conn = sqlite3.connect(dest_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT entry_id, title, link, summary, authors, abstract, doi, published_date,
               matched_date, feed_name, topics, raw_data
        FROM matched_entries
        WHERE TRIM(COALESCE(title,'')) <> ''
        """
    )
    rows = cur.fetchall()
    # Group by normalized title
    groups: Dict[str, list] = {}
    for (entry_id, title, link, summary, authors, abstract, doi, pub, matched, feed, topics, raw) in rows:
        key = _norm_title(title)
        groups.setdefault(key, []).append({
            'entry_id': entry_id,
            'title': title,
            'link': link,
            'summary': summary,
            'authors': authors,
            'abstract': abstract,
            'doi': doi,
            'published_date': pub,
            'matched_date': matched,
            'feed_name': feed,
            'topics': topics or '',
            'raw_data': raw,
        })

    to_delete: list[str] = []
    updated = 0

    for key, items in groups.items():
        if len(items) <= 1:
            continue
        # Choose best by score
        best = max(items, key=_field_score)
        others = [it for it in items if it['entry_id'] != best['entry_id']]
        # Merge topics across all
        merged_topics = best.get('topics') or ''
        for it in others:
            merged_topics = merge_topics(merged_topics, it.get('topics') or '')
        # Coalesce empty fields on best from others
        fields = ['feed_name', 'link', 'summary', 'authors', 'abstract', 'doi', 'published_date', 'raw_data']
        enriched = dict(best)
        for it in others:
            for f in fields:
                enriched[f] = _coalesce_empty(enriched.get(f), it.get(f))
        # Update kept row
        cur.execute(
            """
            UPDATE matched_entries
            SET topics = ?, feed_name = ?, link = ?, summary = ?, authors = ?, abstract = ?, doi = ?, published_date = ?,
                raw_data = COALESCE(NULLIF(TRIM(COALESCE(raw_data,'')), ''), ?)
            WHERE entry_id = ?
            """,
            (
                merged_topics or '',
                enriched.get('feed_name') or '',
                enriched.get('link') or '',
                enriched.get('summary') or '',
                enriched.get('authors') or '',
                enriched.get('abstract'),
                enriched.get('doi'),
                enriched.get('published_date') or '',
                enriched.get('raw_data'),
                best['entry_id'],
            ),
        )
        updated += 1
        # Delete others
        for it in others:
            to_delete.append(it['entry_id'])

    deleted = 0
    if to_delete:
        # Chunk deletes to avoid SQL limits
        for i in range(0, len(to_delete), 500):
            chunk = to_delete[i:i+500]
            qmarks = ','.join('?' for _ in chunk)
            cur.execute(f"DELETE FROM matched_entries WHERE entry_id IN ({qmarks})", chunk)
            deleted += cur.rowcount
    conn.commit()
    conn.close()
    return deleted

def main():
    ap = argparse.ArgumentParser(description='Merge assets_old DBs and rg HTML into a backup copy of the current history DB')
    ap.add_argument('--assets', default=ASSETS_DIR, help=f'Path to data directory (default: {ASSETS_DIR})')
    ap.add_argument('--assets-old', default=ASSETS_OLD_DIR, help=f'Path to legacy assets directory (default: {ASSETS_OLD_DIR})')
    ap.add_argument('--current', default='matched_entries_history.db', help='Current DB filename in assets (default: matched_entries_history.db)')
    ap.add_argument('--old-latest', default='matched_entries_history.latest.db', help='Old latest DB filename in assets_old')
    ap.add_argument('--old-ek', default='matched_entries_history_ek-server.db', help='Old ek-server DB filename in assets_old')
    ap.add_argument('--rg-html', default='rg_filtered_articles.html', help='rg HTML filename in assets_old')
    args = ap.parse_args()

    assets = os.path.abspath(args.assets)
    assets_old = os.path.abspath(args.assets_old)
    current_db = os.path.join(assets, args.current)
    old_latest = os.path.join(assets_old, args.old_latest)
    old_ek = os.path.join(assets_old, args.old_ek)
    rg_html = os.path.join(assets_old, args.rg_html)

    if not os.path.exists(current_db):
        raise SystemExit(f"Current DB not found: {current_db}")

    # 1) Backup copy
    ts = timestamp()
    backup_path = os.path.join(assets, f"matched_entries_history.{ts}.backup.db")
    shutil.copy2(current_db, backup_path)
    print(f"Backup created: {backup_path}")

    # Ensure schema (in case current DB was empty or older)
    ensure_dest_schema(backup_path)

    # 2) Merge old latest
    merged1 = merge_source_db_into(backup_path, old_latest)
    print(f"Merged {merged1} rows from {old_latest}")

    # 3) Merge old ek-server
    merged2 = merge_source_db_into(backup_path, old_ek)
    print(f"Merged {merged2} rows from {old_ek}")

    # 4) Parse rg html and insert
    inserted = parse_rg_html_and_insert(backup_path, rg_html)
    print(f"Inserted/updated {inserted} rg entries from {rg_html}")

    # 5) Enrich any remaining empty fields from raw_data JSON
    enriched = enrich_rows_from_raw(backup_path)
    print(f"Enriched {enriched} existing rows from raw_data")

    # 6) Deduplicate by title, keeping the most complete record
    removed = deduplicate_by_title(backup_path)
    print(f"Deduplicated by title, removed {removed} rows")

    print("Done. Merged data is in:", backup_path)


if __name__ == '__main__':
    main()
