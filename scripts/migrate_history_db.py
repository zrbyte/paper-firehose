#!/usr/bin/env python3
"""
Merge an older matched_entries history DB into the current schema.

Usage:
  python scripts/migrate_history_db.py --source /path/to/old.db [--dest assets/matched_entries_history.db] [--create-latest]

This script:
- Ensures the destination DB has the expected `matched_entries` schema.
- Reads rows from the source DB's `matched_entries` (with flexible column mapping).
- Computes a stable `entry_id` if missing, based on link or title+published.
- Upserts into the destination DB, merging topics for duplicate `entry_id`.

Notes:
- Keep changes minimal and focused on merging data; it does not modify other DBs.
- Use `--create-latest` to write a copy to `assets/matched_entries_history.latest.db` for publishing.
"""

import argparse
import os
import sqlite3
import hashlib
import urllib.parse
from typing import Dict, Any, Tuple
import shutil
import json
import re


REQUIRED_COLUMNS = {
    'entry_id', 'feed_name', 'topics', 'title', 'link', 'summary',
    'authors', 'abstract', 'doi', 'published_date', 'matched_date', 'raw_data'
}


def ensure_dest_schema(dest_path: str) -> None:
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    conn = sqlite3.connect(dest_path)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")

    cur.execute("PRAGMA table_info(matched_entries)")
    info = cur.fetchall()
    columns = {row[1] for row in info}
    need_recreate = (len(columns) == 0) or (not REQUIRED_COLUMNS.issubset(columns))

    if need_recreate:
        cur.execute('DROP TABLE IF EXISTS matched_entries')
        cur.execute('''
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
        # row: (cid, name, type, notnull, dflt_value, pk)
        cols[row[1]] = row[0]
    return cols


def compute_entry_id(candidate_link: str, title: str, published: str) -> str:
    candidate = candidate_link or ""
    if candidate:
        parsed = urllib.parse.urlparse(candidate)
        candidate = urllib.parse.urlunparse(parsed._replace(query="", fragment=""))
        return hashlib.sha1(candidate.encode("utf-8")).hexdigest()
    concat = "||".join([title or "", published or ""])
    return hashlib.sha1(concat.encode("utf-8")).hexdigest()


def normalize_date(value: str) -> str:
    if not value:
        return ""
    s = str(value)
    # Accept common ISO formats; keep YYYY-MM-DD if present
    if len(s) >= 10 and s[0:4].isdigit() and s[4] == '-' and s[5:7].isdigit() and s[7] == '-' and s[8:10].isdigit():
        return s[0:10]
    # Try slashes YYYY/MM/DD
    if len(s) >= 10 and s[0:4].isdigit() and s[4] in ('/', '.') and s[5:7].isdigit() and s[7] in ('/', '.') and s[8:10].isdigit():
        return f"{s[0:4]}-{s[5:7]}-{s[8:10]}"
    # Fallback: return as-is
    return s


def row_value(row: Tuple, idx_by_name: Dict[str, int], names: Tuple[str, ...]) -> Any:
    for n in names:
        if n in idx_by_name:
            return row[idx_by_name[n]]
    return None


def merge_topics(existing: str, incoming: str) -> str:
    def split_topics(s: str):
        if not s:
            return []
        # Accept comma or semicolon separators
        parts = [p.strip() for part in s.split(';') for p in part.split(',')]
        return [p for p in parts if p]

    xs = set(split_topics(existing))
    for t in split_topics(incoming):
        xs.add(t)
    return ", ".join(sorted(xs))


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)


def _find_doi_in_text(text: str | None) -> str | None:
    if not text:
        return None
    t = str(text).strip()
    if t.lower().startswith('doi:'):
        t = t[4:].strip()
    m = DOI_RE.search(t)
    return m.group(0) if m else None


def _extract_from_raw(raw: str | None) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    data: Dict[str, Any] = {}
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
        obj.get('doi') or _find_doi_in_text(obj.get('dc_identifier') or obj.get('dc:identifier') or obj.get('dc.identifier') or obj.get('prism:doi'))
        or _find_doi_in_text(obj.get('id')) or _find_doi_in_text(obj.get('link'))
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
    data['feed_name'] = obj.get('feed_name') or ''
    return data


def _coalesce_empty(existing: Any, incoming: Any) -> Any:
    ex = '' if existing is None else str(existing).strip()
    inc = '' if incoming is None else str(incoming).strip()
    if (not ex) or (ex.lower() == 'null'):
        return incoming if inc else existing
    return existing


def upsert_row(dest: sqlite3.Connection, rec: Dict[str, Any]) -> None:
    cur = dest.cursor()
    cur.execute("SELECT topics, raw_data, feed_name, title, link, summary, authors, abstract, doi, published_date FROM matched_entries WHERE entry_id = ?", (rec['entry_id'],))
    existing = cur.fetchone()
    if existing:
        existing_topics = existing[0] or ""
        existing_raw = existing[1]
        # Enrich rec from its own raw if needed
        rec_enriched = rec.copy()
        if (not rec_enriched.get('title') or not rec_enriched.get('link') or not rec_enriched.get('authors') or not rec_enriched.get('published_date') or not rec_enriched.get('doi')) and rec_enriched.get('raw_data'):
            extracted = _extract_from_raw(rec_enriched.get('raw_data'))
            for k in ['title', 'link', 'summary', 'authors', 'doi', 'published_date', 'feed_name']:
                if not rec_enriched.get(k):
                    rec_enriched[k] = extracted.get(k) or rec_enriched.get(k)
        merged_topics = merge_topics(existing_topics, rec_enriched.get('topics') or "")
        # Only set raw_data if currently missing/empty and incoming has content
        new_raw = None
        incoming_raw = rec_enriched.get('raw_data')
        if incoming_raw is not None and str(incoming_raw).strip() != '':
            if existing_raw is None or str(existing_raw).strip() == '' or str(existing_raw).strip().lower() == 'null':
                new_raw = incoming_raw
        # Fill other empty columns
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
        params = [merged_topics, rec_enriched.get('matched_date')]
        if new_raw is not None:
            set_clauses.append("raw_data = ?")
            params.append(new_raw)
        for col, val in updated_fields.items():
            if val is not None:
                set_clauses.append(f"{col} = ?")
                params.append(val)
        params.append(rec['entry_id'])
        cur.execute(f"UPDATE matched_entries SET {', '.join(set_clauses)} WHERE entry_id = ?", params)
    else:
        cur.execute(
            '''INSERT INTO matched_entries 
               (entry_id, feed_name, topics, title, link, summary, authors, abstract, doi,
                published_date, matched_date, raw_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)''',
            (
                rec.get('entry_id'), rec.get('feed_name') or '', rec.get('topics') or '',
                rec.get('title') or '', rec.get('link') or '', rec.get('summary') or '',
                rec.get('authors') or '', rec.get('abstract'), rec.get('doi'),
                rec.get('published_date') or '', rec.get('matched_date'), rec.get('raw_data')
            )
        )


def main():
    ap = argparse.ArgumentParser(description="Merge an older history DB into current schema")
    ap.add_argument('--source', required=True, help='Path to the old history DB')
    ap.add_argument('--dest', default='assets/matched_entries_history.db', help='Destination DB (default: assets/matched_entries_history.db)')
    ap.add_argument('--create-latest', action='store_true', help='Also create assets/matched_entries_history.latest.db copy of dest')
    args = ap.parse_args()

    ensure_dest_schema(args.dest)

    src = sqlite3.connect(args.source)
    src_cur = src.cursor()
    # Basic existence check
    src_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='matched_entries'")
    if not src_cur.fetchone():
        raise SystemExit("Source DB does not contain a 'matched_entries' table")

    cols = get_table_columns(src, 'matched_entries')
    idx_by_name = cols

    # Read everything to keep it simple
    src_cur.execute('SELECT * FROM matched_entries')
    rows = src_cur.fetchall()

    dest = sqlite3.connect(args.dest)
    dest.execute('BEGIN')

    count = 0
    for row in rows:
        get = lambda *names: row_value(row, idx_by_name, names)
        link = get('link', 'url', 'guid') or ''
        title = get('title') or ''
        published_raw = get('published_date', 'published', 'date', 'published_at', 'publication date') or ''
        published_date = normalize_date(published_raw)
        entry_id = (get('entry_id') or compute_entry_id(link, title, published_date))

        # Merge topics with search_terms from old DB, if present
        topics_val = get('topics', 'topic') or ''
        search_terms_val = get('search_terms', 'search_term', 'search_type') or ''
        if search_terms_val:
            topics_val = merge_topics(topics_val, search_terms_val)

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
            'matched_date': get('matched_date', 'timestamp'),
            'raw_data': get('raw_data', 'data')
        }

        upsert_row(dest, rec)
        count += 1

    dest.commit()
    dest.close()
    src.close()

    print(f"Merged {count} rows from {args.source} into {args.dest}")

    if args.create_latest:
        latest_path = os.path.join(os.path.dirname(args.dest), 'matched_entries_history.latest.db')
        shutil.copy2(args.dest, latest_path)
        print(f"Also wrote latest copy to {latest_path}")


if __name__ == '__main__':
    main()
