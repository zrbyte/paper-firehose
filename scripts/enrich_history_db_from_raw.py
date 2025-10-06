#!/usr/bin/env python3
"""
Enrich an existing matched_entries_history SQLite DB by filling empty columns
from JSON stored in raw_data.

Usage:
  python scripts/enrich_history_db_from_raw.py --db ~/.paper_firehose/matched_entries_history.<timestamp>.backup.db
"""

import argparse
import json
import re
import sqlite3
from typing import Any, Optional


def _normalize_empty(x: Any) -> str:
    return '' if x is None else str(x).strip()


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)


def _find_doi_in_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = str(text).strip()
    if t.lower().startswith('doi:'):
        t = t[4:].strip()
    m = DOI_RE.search(t)
    return m.group(0) if m else None


def _extract_from_raw(raw: str) -> dict:
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    data: dict[str, Any] = {}
    data['title'] = obj.get('title') or ''
    data['link'] = obj.get('link') or obj.get('id') or ''
    data['summary'] = obj.get('summary') or obj.get('description') or ''
    authors = obj.get('authors')
    if isinstance(authors, list) and authors:
        data['authors'] = ', '.join(a.get('name', '') for a in authors if isinstance(a, dict))
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
    # Published date (best-effort YYYY-MM-DD)
    import datetime
    pub = obj.get('published') or obj.get('updated') or ''
    s = str(pub)
    try:
        dt = datetime.datetime.fromisoformat(s.replace('Z', '+00:00'))
        data['published_date'] = dt.date().isoformat()
    except Exception:
        m = re.search(r'(\d{4})[-/](\d{2})[-/](\d{2})', s)
        data['published_date'] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ''
    data['feed_name'] = obj.get('feed_name') or obj.get('feed_title') or ''
    return data


def main():
    ap = argparse.ArgumentParser(description='Enrich matched_entries_history DB from raw_data JSON')
    ap.add_argument('--db', required=True, help='Path to SQLite database to enrich')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
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
        new_feed = feed_name or extracted.get('feed_name') or ''
        new_title = title or extracted.get('title') or ''
        new_link = link or extracted.get('link') or ''
        new_summary = summary or extracted.get('summary') or ''
        new_authors = authors or extracted.get('authors') or ''
        new_doi = doi or extracted.get('doi')
        new_pub = published or extracted.get('published_date') or ''
        if (
            _normalize_empty(new_feed) != _normalize_empty(feed_name)
            or _normalize_empty(new_title) != _normalize_empty(title)
            or _normalize_empty(new_link) != _normalize_empty(link)
            or _normalize_empty(new_summary) != _normalize_empty(summary)
            or _normalize_empty(new_authors) != _normalize_empty(authors)
            or _normalize_empty(new_pub) != _normalize_empty(published)
            or (new_doi or '') != (doi or '')
        ):
            cur.execute(
                "UPDATE matched_entries SET feed_name=?, title=?, link=?, summary=?, authors=?, doi=?, published_date=? WHERE entry_id=?",
                (new_feed, new_title, new_link, new_summary, new_authors, new_doi, new_pub, entry_id),
            )
            updated += 1
    conn.commit()
    conn.close()
    print(f"Enriched {updated} rows in {args.db}")


if __name__ == '__main__':
    main()
