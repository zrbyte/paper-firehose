"""
Export recent command implementation.
Creates a smaller database containing only recent entries from the history database.
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

from ..core.config import ConfigManager
from ..core.paths import resolve_data_file

logger = logging.getLogger(__name__)


def run(config_path: str, days: int = 60, output_name: Optional[str] = None) -> None:
    """Export recent entries from matched_entries_history.db to a smaller file.

    Args:
        config_path: Path to the main configuration file
        days: Number of days to include (default: 60)
        output_name: Optional output filename (default: matched_entries_history.recent.db)
    """
    logger.info(f"Starting export-recent command (last {days} days)")

    try:
        # Initialize components
        config_manager = ConfigManager(config_path)
        config = config_manager.load_config()

        # Resolve database paths
        history_db_path = str(resolve_data_file(config['database']['history_path']))

        if output_name:
            output_db_path = str(resolve_data_file(output_name, ensure_parent=True))
        else:
            # Default: matched_entries_history.recent.db in same directory as history DB
            history_dir = os.path.dirname(history_db_path)
            output_db_path = os.path.join(history_dir, 'matched_entries_history.recent.db')

        # Check source database exists
        if not os.path.exists(history_db_path):
            logger.error(f"Source database not found: {history_db_path}")
            return

        # Calculate cutoff date
        cutoff_date = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d')
        logger.info(f"Cutoff date: {cutoff_str}")

        # Connect to source database
        src_conn = sqlite3.connect(history_db_path)
        src_conn.row_factory = sqlite3.Row
        src_cursor = src_conn.cursor()

        # Get schema from source database
        src_cursor.execute("PRAGMA table_info(matched_entries)")
        columns_info = src_cursor.fetchall()
        columns = [col[1] for col in columns_info]

        logger.info(f"Source database schema has {len(columns)} columns")

        # Count total entries
        src_cursor.execute("SELECT COUNT(*) FROM matched_entries")
        total_entries = src_cursor.fetchone()[0]
        logger.info(f"Total entries in source database: {total_entries}")

        # Query recent entries
        # matched_date format is typically 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'
        query = """
            SELECT * FROM matched_entries
            WHERE matched_date >= ?
            ORDER BY matched_date DESC
        """

        src_cursor.execute(query, (cutoff_str,))
        recent_entries = src_cursor.fetchall()

        recent_count = len(recent_entries)
        logger.info(f"Found {recent_count} entries from the last {days} days")

        if recent_count == 0:
            logger.warning(f"No entries found in the last {days} days")

        # Create destination database
        if os.path.exists(output_db_path):
            os.remove(output_db_path)
            logger.info(f"Removed existing output database: {output_db_path}")

        dest_conn = sqlite3.connect(output_db_path)
        dest_cursor = dest_conn.cursor()

        # Create table with same schema
        src_cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='matched_entries'")
        create_table_sql = src_cursor.fetchone()[0]
        dest_cursor.execute(create_table_sql)

        # Create indexes
        src_cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='matched_entries'")
        index_sqls = src_cursor.fetchall()
        for (index_sql,) in index_sqls:
            if index_sql:  # Some indexes may be auto-created (NULL sql)
                dest_cursor.execute(index_sql)

        logger.info("Created destination database with matching schema")

        # Copy filtered entries
        if recent_entries:
            placeholders = ','.join(['?' for _ in columns])
            insert_sql = f"INSERT INTO matched_entries ({','.join(columns)}) VALUES ({placeholders})"

            # Convert Row objects to tuples
            rows_to_insert = [tuple(row[col] for col in columns) for row in recent_entries]

            dest_cursor.executemany(insert_sql, rows_to_insert)
            dest_conn.commit()
            logger.info(f"Copied {recent_count} entries to destination database")

        # Get file sizes
        src_size_mb = os.path.getsize(history_db_path) / (1024 * 1024)
        dest_size_mb = os.path.getsize(output_db_path) / (1024 * 1024)

        # Log statistics
        logger.info("=" * 60)
        logger.info("Export Summary:")
        logger.info(f"  Source: {history_db_path}")
        logger.info(f"  Destination: {output_db_path}")
        logger.info(f"  Time range: Last {days} days (since {cutoff_str})")
        logger.info(f"  Total entries: {total_entries}")
        logger.info(f"  Recent entries: {recent_count}")
        logger.info(f"  Percentage: {(recent_count/total_entries*100) if total_entries > 0 else 0:.1f}%")
        logger.info(f"  Source size: {src_size_mb:.2f} MB")
        logger.info(f"  Output size: {dest_size_mb:.2f} MB")
        logger.info(f"  Size reduction: {((src_size_mb - dest_size_mb)/src_size_mb*100) if src_size_mb > 0 else 0:.1f}%")
        logger.info("=" * 60)

        # Close connections
        src_conn.close()
        dest_conn.close()

        logger.info("Export-recent command completed successfully")

    except Exception as e:
        logger.error(f"Export-recent command failed: {e}")
        raise
