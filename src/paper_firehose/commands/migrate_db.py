"""
Database migration command.

Migrates existing databases to the optimised schema:
- Archives raw_data to a separate raw_archive.db
- Drops the raw_data column from all three databases
- Applies performance pragmas (auto_vacuum, page_size)
- Runs VACUUM to reclaim space
"""

import logging
import os
import shutil
import sqlite3
from typing import Dict, Optional

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.paths import resolve_data_file

logger = logging.getLogger(__name__)


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check whether *table* contains *column*."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the ordered list of column names for *table*."""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def _get_create_sql(conn: sqlite3.Connection, table: str) -> Optional[str]:
    """Return the CREATE TABLE statement for *table*."""
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _get_index_sqls(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return CREATE INDEX statements for *table*."""
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
    )
    return [row[0] for row in cursor.fetchall() if row[0]]


def _archive_raw_data(
    conn: sqlite3.Connection,
    archive_conn: sqlite3.Connection,
    table: str,
    id_column: str,
    db_source: str,
) -> int:
    """Copy raw_data values into the archive database. Returns row count."""
    cursor = conn.execute(
        f"SELECT {id_column}, raw_data FROM {table} WHERE raw_data IS NOT NULL"
    )
    rows = cursor.fetchall()
    if not rows:
        return 0
    archive_conn.executemany(
        "INSERT OR IGNORE INTO raw_data_archive (entry_id, db_source, raw_data) VALUES (?, ?, ?)",
        [(row[0], db_source, row[1]) for row in rows],
    )
    archive_conn.commit()
    return len(rows)


def _rebuild_without_column(
    conn: sqlite3.Connection, table: str, drop_column: str
) -> None:
    """Rebuild *table* without *drop_column*.

    SQLite < 3.35 lacks ALTER TABLE DROP COLUMN, so we recreate the table.
    """
    columns = _get_columns(conn, table)
    if drop_column not in columns:
        return

    keep_columns = [c for c in columns if c != drop_column]
    cols_csv = ", ".join(keep_columns)

    # Get original CREATE TABLE SQL and remove the dropped column from it
    create_sql = _get_create_sql(conn, table)
    index_sqls = _get_index_sqls(conn, table)

    tmp_table = f"__{table}_migrate"

    conn.execute(f"ALTER TABLE {table} RENAME TO {tmp_table}")
    # Recreate from the original SQL with the column removed.
    # Safer approach: build a new CREATE TABLE from the kept columns by
    # selecting into a fresh table, which inherits types and defaults.
    # We use CREATE TABLE ... AS SELECT which loses constraints, so instead
    # re-derive the DDL.
    new_create = _derive_create_without_column(create_sql, table, drop_column)
    conn.execute(new_create)
    conn.execute(f"INSERT INTO {table} ({cols_csv}) SELECT {cols_csv} FROM {tmp_table}")
    conn.execute(f"DROP TABLE {tmp_table}")

    # Recreate indexes
    for idx_sql in index_sqls:
        try:
            conn.execute(idx_sql)
        except sqlite3.OperationalError:
            pass  # index already exists after CREATE TABLE

    conn.commit()


def _derive_create_without_column(
    original_sql: str, table: str, drop_column: str
) -> str:
    """Remove *drop_column* from a CREATE TABLE SQL string.

    Handles both multi-line (one column per line) and single-line DDL.
    """
    import re

    # Extract the portion between the outermost parentheses
    m = re.search(r"\((.+)\)", original_sql, re.DOTALL)
    if not m:
        return original_sql

    body = m.group(1)

    # Split body into top-level comma-separated parts (respecting nested parens)
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())

    # Filter out the column definition for drop_column
    kept: list[str] = []
    for part in parts:
        # A column definition starts with the column name
        token = part.split()[0].strip('"').strip("'").strip("`") if part.split() else ""
        if token.lower() == drop_column.lower():
            continue
        kept.append(part)

    new_body = ",\n                    ".join(kept)
    return f"CREATE TABLE {table} (\n                    {new_body}\n                )"


def _file_size_mb(path: str) -> float:
    """Return file size in MB, or 0 if file doesn't exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def run(
    config_path: str,
    *,
    skip_archive: bool = False,
    dry_run: bool = False,
) -> None:
    """Run the database migration.

    1. Back up all three DBs
    2. Archive raw_data to raw_archive.db (unless --skip-archive)
    3. Rebuild tables without raw_data column
    4. Apply pragmas and VACUUM
    """
    logger.info("Starting database migration")

    config_manager = ConfigManager(config_path)
    config = config_manager.load_config()

    db_specs = {
        "all_feeds": {
            "path_key": "all_feeds_path",
            "table": "feed_entries",
            "id_column": "entry_id",
        },
        "history": {
            "path_key": "history_path",
            "table": "matched_entries",
            "id_column": "entry_id",
        },
        "current": {
            "path_key": "path",
            "table": "entries",
            "id_column": "id",
        },
    }

    # Resolve paths
    db_paths: Dict[str, str] = {}
    for key, spec in db_specs.items():
        db_paths[key] = str(resolve_data_file(config["database"][spec["path_key"]]))

    archive_path = os.path.join(os.path.dirname(db_paths["all_feeds"]), "raw_archive.db")

    # Report current sizes
    logger.info("Current database sizes:")
    for key, path in db_paths.items():
        logger.info(f"  {key}: {_file_size_mb(path):.2f} MB ({path})")

    if dry_run:
        logger.info("[DRY RUN] Would perform the following:")
        for key, spec in db_specs.items():
            path = db_paths[key]
            if not os.path.exists(path):
                logger.info(f"  {key}: file not found, skip")
                continue
            conn = sqlite3.connect(path)
            has_raw = _table_has_column(conn, spec["table"], "raw_data")
            conn.close()
            if has_raw:
                logger.info(f"  {key}: drop raw_data column, VACUUM")
            else:
                logger.info(f"  {key}: raw_data already absent, VACUUM only")
        if not skip_archive:
            logger.info(f"  Archive raw_data to: {archive_path}")
        return

    # Step 1: Back up all DBs
    logger.info("Step 1: Backing up databases")
    for key, path in db_paths.items():
        if not os.path.exists(path):
            logger.info(f"  {key}: not found, skipping backup")
            continue
        backup_path = path.replace(".db", ".pre-migration.db")
        shutil.copy2(path, backup_path)
        logger.info(f"  {key}: backed up to {backup_path}")

    # Step 2: Archive raw_data
    if not skip_archive:
        logger.info("Step 2: Archiving raw_data")
        archive_conn = sqlite3.connect(archive_path)
        archive_conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_data_archive (
                entry_id TEXT NOT NULL,
                db_source TEXT NOT NULL,
                raw_data TEXT,
                PRIMARY KEY (entry_id, db_source)
            )
        """)
        archive_conn.commit()

        total_archived = 0
        for key, spec in db_specs.items():
            path = db_paths[key]
            if not os.path.exists(path):
                continue
            conn = sqlite3.connect(path)
            if _table_has_column(conn, spec["table"], "raw_data"):
                count = _archive_raw_data(
                    conn, archive_conn, spec["table"], spec["id_column"], key
                )
                total_archived += count
                logger.info(f"  {key}: archived {count} raw_data entries")
            else:
                logger.info(f"  {key}: no raw_data column, skipping")
            conn.close()

        archive_conn.close()
        logger.info(f"  Total archived: {total_archived} entries to {archive_path}")
        logger.info(f"  Archive size: {_file_size_mb(archive_path):.2f} MB")
    else:
        logger.info("Step 2: Skipped (--skip-archive)")

    # Step 3: Rebuild tables without raw_data
    logger.info("Step 3: Removing raw_data column from databases")
    for key, spec in db_specs.items():
        path = db_paths[key]
        if not os.path.exists(path):
            continue
        conn = sqlite3.connect(path)
        if _table_has_column(conn, spec["table"], "raw_data"):
            _rebuild_without_column(conn, spec["table"], "raw_data")
            logger.info(f"  {key}: raw_data column removed")
        else:
            logger.info(f"  {key}: raw_data already absent")
        conn.close()

    # Step 4: Apply pragmas and VACUUM
    logger.info("Step 4: Applying pragmas and VACUUM")
    for key, path in db_paths.items():
        if not os.path.exists(path):
            continue
        conn = sqlite3.connect(path)
        DatabaseManager._apply_pragmas(conn)
        conn.execute("VACUUM")
        conn.close()
        logger.info(f"  {key}: optimised ({_file_size_mb(path):.2f} MB)")

    # Report final sizes
    logger.info("Migration complete. Final sizes:")
    for key, path in db_paths.items():
        logger.info(f"  {key}: {_file_size_mb(path):.2f} MB")
