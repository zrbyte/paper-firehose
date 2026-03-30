"""Query paper databases for entries with flexible filtering and output."""

import json
import logging
from typing import Any, Dict, List, Optional

from ..core.command_context import CommandContext
from ..core.model_manager import ensure_local_model
from ..processors.st_ranker import STRanker

logger = logging.getLogger(__name__)

# Default columns shown in human-readable table output per database.
_DEFAULT_TABLE_FIELDS = {
    'current': ['rank_score', 'published_date', 'title', 'topic', 'authors'],
    'history': ['rank_score', 'published_date', 'matched_date', 'title', 'topics'],
    'all_feeds': ['published_date', 'first_seen', 'title', 'feed_name', 'authors'],
}

_SORT_MAP = {
    'rank': 'rank_score DESC',
    'date': 'published_date DESC',
    'title': 'title ASC',
}


def _resolve_sort(sort_arg: str, db_key: str) -> str:
    """Map a short sort name to an ORDER BY clause."""
    order = _SORT_MAP.get(sort_arg)
    if order is None:
        raise ValueError(f"Unknown sort key '{sort_arg}'. Choose from: {', '.join(_SORT_MAP)}")
    if sort_arg == 'rank' and db_key == 'all_feeds':
        return 'published_date DESC'
    return order


def _truncate(text: Optional[str], width: int) -> str:
    if not text:
        return ''
    text = ' '.join(text.split())  # collapse whitespace
    if len(text) <= width:
        return text
    return text[:width - 1] + '\u2026'


def _format_table(rows: List[Dict[str, Any]], total: int,
                  fields: List[str], offset: int, limit: int) -> str:
    """Format rows as a human-readable table."""
    if not rows:
        return 'No entries found.'

    lines: list[str] = []
    end = offset + len(rows)
    lines.append(f'Found {total} entries (showing {offset + 1}-{end})')
    lines.append('')

    # Column widths
    col_widths: Dict[str, int] = {}
    display_rows: list[dict] = []
    for row in rows:
        display: dict = {}
        for f in fields:
            val = row.get(f)
            if f == 'rank_score' and val is not None:
                display[f] = f'{val:.3f}'
            elif f in ('title', 'authors'):
                display[f] = _truncate(str(val) if val else '', 55)
            else:
                display[f] = str(val) if val is not None else ''
        display_rows.append(display)
        for f in fields:
            col_widths[f] = max(col_widths.get(f, len(f)), len(display[f]))

    # Header
    hdr = ' #  ' + '  '.join(f.ljust(col_widths[f]) for f in fields)
    lines.append(hdr)

    # Rows
    for i, display in enumerate(display_rows, start=offset + 1):
        num = str(i).rjust(2)
        cells = '  '.join(display[f].ljust(col_widths[f]) for f in fields)
        lines.append(f'{num}  {cells}')

    if total > end:
        lines.append('')
        lines.append(f'Showing {len(rows)} of {total}. Use --offset {end} for next page.')

    return '\n'.join(lines)


def _format_json(rows: List[Dict[str, Any]], total: int,
                 fields: Optional[List[str]],
                 offset: int, limit: int) -> str:
    """Format rows as a JSON object."""
    if fields:
        rows = [{k: v for k, v in r.items() if k in fields} for r in rows]
    obj = {
        'total': total,
        'offset': offset,
        'limit': limit,
        'entries': rows,
    }
    return json.dumps(obj, indent=2, default=str)


# Map db_key -> (id column, topic/group column, abstract/text column)
_DB_FIELD_MAP = {
    'current': ('id', 'topic', 'abstract'),
    'history': ('entry_id', 'topics', 'abstract'),
    'all_feeds': ('entry_id', 'feed_name', 'summary'),
}


def _build_rerank_text(row: Dict[str, Any], text_col: str) -> str:
    """Build the text to embed for reranking: title + abstract/summary."""
    title = (row.get('title') or '').strip()
    body = (row.get(text_col) or '').strip()
    if body:
        return f"{title} {body}"
    return title


def run(
    config_path: Optional[str],
    *,
    db_key: str = 'current',
    topic: Optional[str] = None,
    min_rank: Optional[float] = None,
    status: Optional[str] = None,
    has_doi: bool = False,
    has_abstract: bool = False,
    since: Optional[str] = None,
    until: Optional[str] = None,
    search: Optional[str] = None,
    fuzzy: Optional[str] = None,
    rerank: Optional[str] = None,
    sort: str = 'rank',
    limit: int = 20,
    offset: int = 0,
    output_json: bool = False,
    count_only: bool = False,
    fields: Optional[str] = None,
) -> None:
    """Execute a query against one of the paper databases."""
    # Validate incompatible options
    if db_key == 'all_feeds':
        if min_rank is not None:
            raise ValueError("--min-rank is not available for --all-feeds (no rank_score column)")
        if status:
            raise ValueError("--status is not available for --all-feeds")
        if has_abstract:
            raise ValueError("--has-abstract is not available for --all-feeds (no abstract column)")
    if db_key == 'history' and status:
        raise ValueError("--status is not available for --history")

    ctx = CommandContext(config_path)
    order_by = _resolve_sort(sort, db_key)

    # When reranking or BM25-sorting search results, fetch all candidates
    # (no SQL-level pagination) so we can re-sort before applying limit/offset.
    needs_client_sort = bool(rerank) or bool(search)
    fetch_limit = 0 if needs_client_sort else limit
    fetch_offset = 0 if needs_client_sort else offset

    rows, total = ctx.db.query_entries(
        db_key=db_key,
        topic=topic,
        min_rank=min_rank,
        status=status,
        has_doi=has_doi or None,
        has_abstract=has_abstract or None,
        since=since,
        until=until,
        search=search,
        fuzzy=fuzzy,
        order_by=order_by,
        limit=fetch_limit,
        offset=fetch_offset,
    )

    # BM25 relevance sort for keyword search (when not reranking)
    if search and not rerank and rows:
        rows.sort(key=lambda r: r.get('bm25_score') or 0.0)  # FTS5 rank is negative; lower = more relevant
        total = len(rows)
        if limit:
            rows = rows[offset:offset + limit]
        elif offset:
            rows = rows[offset:]

    # Semantic reranking
    if rerank and rows:
        model_name = ensure_local_model("all-MiniLM-L6-v2")
        ranker = STRanker(model_name=model_name)
        if not ranker.available():
            raise RuntimeError(
                "Sentence-transformer model unavailable. "
                "Install sentence-transformers or check model path."
            )

        id_col, group_col, text_col = _DB_FIELD_MAP[db_key]

        batch = [
            (row[id_col], row.get(group_col, ''), _build_rerank_text(row, text_col))
            for row in rows
        ]
        scores = ranker.score_entries(rerank, batch)

        # Build score lookup: (id, group) -> score
        score_map = {(eid, grp): score for eid, grp, score in scores}

        for row in rows:
            key = (row[id_col], row.get(group_col, ''))
            row['rerank_score'] = round(score_map.get(key, 0.0), 4)

        rows.sort(key=lambda r: r['rerank_score'], reverse=True)

        # Apply limit/offset to reranked results
        total = len(rows)
        if limit:
            rows = rows[offset:offset + limit]
        elif offset:
            rows = rows[offset:]

    if count_only:
        print(total)
        return

    field_list: Optional[List[str]] = None
    if fields:
        field_list = [f.strip() for f in fields.split(',')]

    if output_json:
        print(_format_json(rows, total, field_list, offset, limit))
    else:
        table_fields = field_list or _DEFAULT_TABLE_FIELDS.get(db_key, ['title'])
        if rerank and not fields:
            table_fields = ['rerank_score'] + table_fields
        print(_format_table(rows, total, table_fields, offset, limit))
