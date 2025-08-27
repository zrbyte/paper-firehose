#!/usr/bin/env python3
"""
RSS Parser - Main Script

This is the main entry point for the RSS feed processing system.
It handles command line options and orchestrates the feed processing workflow.
"""

import os
import sys
import argparse
import logging
import ftplib
import llmsummary
import feedfilter

# Setup logging
logging.basicConfig(level=logging.INFO)

# Constants
MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_FILE = os.path.join(MAIN_DIR, 'summary.html')

# FTP credentials are provided via environment variables
FTP_HOST = os.environ.get('FTP_HOST', 'nemeslab.com')
FTP_USER = os.environ.get('FTP_USER')
FTP_PASS = os.environ.get('FTP_PASS')


def main(upload: bool = True):
    """Main function to process RSS feeds and generate filtered articles."""
    return feedfilter.process_feeds(upload=upload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process RSS feeds")
    parser.add_argument(
        "--no-upload",
        action="store_false",
        dest="upload",
        help="skip FTP upload",
    )
    parser.add_argument(
        "--clear-db",
        action="store_true",
        dest="clear_db",
        help="remove all entries in the SQLite database and exit",
    )
    parser.add_argument(
        "--purge-days",
        type=int,
        metavar="DAYS",
        dest="purge_days",
        help="remove database entries older than DAYS days and exit",
    )
    parser.add_argument(
        "--no-summary",
        action="store_true",
        dest="no_summary",
        help="skip running the LLM summary step",
    )
    args = parser.parse_args()

    if args.clear_db:
        feedfilter.clear_database()
        print("All entries removed from the database.")
        feedfilter.close_connections()
        sys.exit(0)

    if args.purge_days is not None:
        feedfilter.purge_database(args.purge_days)
        print(f"Entries older than {args.purge_days} days removed from the database.")
        feedfilter.close_connections()
        sys.exit(0)

    try:
        result = main(upload=args.upload)
        feedfilter.close_connections()

        if not args.no_summary:
            llmsummary.main(result)
            if args.upload:
                if not FTP_USER or not FTP_PASS:
                    raise ValueError(
                        "FTP_USER and FTP_PASS must be set as environment variables for FTP upload"
                    )
                try:
                    with ftplib.FTP(FTP_HOST) as session:
                        session.login(user=FTP_USER, passwd=FTP_PASS)
                        session.cwd('/public_html/cond-mat/')
                        with open(SUMMARY_FILE, 'rb') as f:
                            session.storbinary('STOR ' + os.path.basename(SUMMARY_FILE), f)
                except ftplib.all_errors as e:
                    logging.error("FTP upload failed: %s", e)
                    sys.exit(1)
    except Exception as e:
        logging.error("Error during feed processing: %s", e)
        feedfilter.close_connections()
        sys.exit(1)
    