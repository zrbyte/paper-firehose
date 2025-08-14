#!/usr/bin/env python3
"""Simple CLI viewer for the seen_entries SQLite database."""

import argparse
import os
import sqlite3
from textwrap import shorten

# Paths relative to this file
MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(MAIN_DIR, "assets")
DB_PATH = os.path.join(ASSETS_DIR, "seen_entries.db")


def format_rows(rows):
    """Return formatted table rows for printing."""
    headers = ["feed_name", "search_type", "entry_id", "timestamp", "title"]
    table = []
    for row in rows:
        table.append(
            [
                row["feed_name"],
                row["search_type"],
                row["entry_id"],
                row["timestamp"],
                shorten(row["title"], width=60, placeholder="…"),
            ]
        )
    widths = [len(h) for h in headers]
    for r in table:
        for i, cell in enumerate(r):
            widths[i] = min(max(widths[i], len(str(cell))), 60)
    lines = []
    lines.append(" | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    lines.append("-+-".join("-" * w for w in widths))
    for r in table:
        lines.append(" | ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(r)))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Display contents of seen_entries.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of rows to display (default: 50)",
    )
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        SELECT feed_name, search_type, entry_id,
               datetime(timestamp, 'unixepoch') AS timestamp, title
        FROM seen_entries
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (args.limit,),
    )
    rows = cur.fetchall()
    if not rows:
        print("No entries found.")
        return

    print(format_rows(rows))


if __name__ == "__main__":
    main()
