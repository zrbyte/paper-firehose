"""Status command: show configuration, database freshness, and pipeline state."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import click

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.paths import resolve_data_file


def _db_file_info(path: str) -> Dict[str, Any]:
    """Return file-level metadata for a database path."""
    if not os.path.exists(path):
        return {"exists": False, "path": path}
    stat = os.stat(path)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return {
        "exists": True,
        "path": path,
        "size_bytes": stat.st_size,
        "modified_utc": mtime.isoformat(),
    }


def _safe_scalar(conn: sqlite3.Connection, sql: str) -> Any:
    """Execute a scalar query, returning None on any error."""
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _current_db_stats(db: DatabaseManager) -> Dict[str, Any]:
    """Gather stats from papers.db (current run)."""
    info = _db_file_info(db.db_paths["current"])
    if not info["exists"]:
        return info

    with db.get_connection("current") as conn:
        total = _safe_scalar(conn, "SELECT COUNT(*) FROM entries")
        by_status = {}
        for status_val in ("new", "filtered", "ranked", "summarized"):
            by_status[status_val] = _safe_scalar(
                conn,
                f"SELECT COUNT(*) FROM entries WHERE status = '{status_val}'",
            )
        topics_rows = conn.execute(
            "SELECT DISTINCT topic FROM entries ORDER BY topic"
        ).fetchall()
        topics = [r[0] for r in topics_rows]
        latest_discovered = _safe_scalar(
            conn, "SELECT MAX(discovered_date) FROM entries"
        )
        latest_published = _safe_scalar(
            conn, "SELECT MAX(published_date) FROM entries"
        )

    info.update(
        {
            "entry_count": total,
            "by_status": by_status,
            "topics": topics,
            "latest_discovered_date": latest_discovered,
            "latest_published_date": latest_published,
        }
    )
    return info


def _history_db_stats(db: DatabaseManager) -> Dict[str, Any]:
    """Gather stats from matched_entries_history.db."""
    info = _db_file_info(db.db_paths["history"])
    if not info["exists"]:
        return info

    with db.get_connection("history") as conn:
        total = _safe_scalar(conn, "SELECT COUNT(*) FROM matched_entries")
        latest_matched = _safe_scalar(
            conn, "SELECT MAX(matched_date) FROM matched_entries"
        )

    info.update(
        {
            "entry_count": total,
            "latest_matched_date": latest_matched,
        }
    )
    return info


def _all_feeds_db_stats(db: DatabaseManager) -> Dict[str, Any]:
    """Gather stats from all_feed_entries.db."""
    info = _db_file_info(db.db_paths["all_feeds"])
    if not info["exists"]:
        return info

    with db.get_connection("all_feeds") as conn:
        total = _safe_scalar(conn, "SELECT COUNT(*) FROM feed_entries")
        latest_seen = _safe_scalar(
            conn, "SELECT MAX(first_seen) FROM feed_entries"
        )

    info.update(
        {
            "entry_count": total,
            "latest_first_seen": latest_seen,
        }
    )
    return info


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def run(
    config_path: Optional[str],
    *,
    output_json: bool = False,
) -> None:
    """Show system status, configuration, and database freshness."""
    # Suppress noisy log lines when producing machine-readable output.
    if output_json:
        logging.getLogger("paper_firehose").setLevel(logging.WARNING)

    config_manager = ConfigManager(config_path)
    valid = config_manager.validate_config()
    unknown_keys = config_manager.check_unknown_keys()
    topics = config_manager.get_available_topics()
    feeds = config_manager.get_enabled_feeds()
    config = config_manager.load_config()

    # Build database stats
    db = DatabaseManager(config)
    current = _current_db_stats(db)
    history = _history_db_stats(db)
    all_feeds = _all_feeds_db_stats(db)

    if output_json:
        result = {
            "config": {
                "path": str(config_manager.config_path),
                "valid": valid,
                "unknown_keys": unknown_keys,
                "topics": topics,
                "enabled_feeds": len(feeds),
            },
            "databases": {
                "current": current,
                "history": history,
                "all_feeds": all_feeds,
            },
        }
        click.echo(json.dumps(result, indent=2, default=str))
        return

    # Human-readable output
    click.echo(f"📄 Config file: {config_manager.config_path}")
    if valid:
        click.echo("✅ Configuration is valid")
    else:
        click.echo("❌ Configuration validation failed")
        return

    if unknown_keys:
        click.echo(f"⚠️  {len(unknown_keys)} unknown config key(s):")
        for w in unknown_keys:
            click.echo(f"   {w}")

    click.echo(f"📚 Available topics: {', '.join(topics)}")
    click.echo(f"📡 Enabled feeds: {len(feeds)}")

    click.echo("🗄️  Databases:")
    for label, stats in [
        ("Current run", current),
        ("History", history),
        ("All feeds", all_feeds),
    ]:
        click.echo(f"   {label}: {stats['path']}")
        if not stats["exists"]:
            click.echo("      (not created yet)")
            continue
        size = _format_size(stats["size_bytes"])
        click.echo(f"      Size: {size}  |  Modified: {stats['modified_utc']}")
        count = stats.get("entry_count", 0)
        click.echo(f"      Entries: {count}")

        if "by_status" in stats:
            parts = [
                f"{k}: {v}" for k, v in stats["by_status"].items() if v
            ]
            if parts:
                click.echo(f"      Pipeline: {', '.join(parts)}")
            if stats.get("latest_discovered_date"):
                click.echo(
                    f"      Latest discovered: {stats['latest_discovered_date']}"
                )
        elif stats.get("latest_matched_date"):
            click.echo(
                f"      Latest matched: {stats['latest_matched_date']}"
            )
        elif stats.get("latest_first_seen"):
            click.echo(
                f"      Latest first seen: {stats['latest_first_seen']}"
            )
