import os
import re
import sqlite3
import datetime
import feedparser
import html
from string import Template
import logging
import time
import shutil
import ftplib
import json
import sys
import argparse
import hashlib
import urllib.parse
import llmsummary

# Setup logging
logging.basicConfig(level=logging.INFO)

# Constants
TIME_DELTA = datetime.timedelta(days=182)  # Approximately 6 months
MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(MAIN_DIR, 'assets')
ARCHIVE_DIR = os.path.join(MAIN_DIR, 'archive')
# Path to the daily summary produced by llmsummary.py
SUMMARY_FILE = os.path.join(MAIN_DIR, 'summary.html')
# MAIN_DIR = '/uu/nemes/cond-mat/'
# ASSETS_DIR = '/uu/nemes/cond-mat/assets/'

# Ensure required directories exist
os.makedirs(ASSETS_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# Initialize SQLite database for tracking seen entries
DB_PATH = os.path.join(ASSETS_DIR, 'seen_entries.db')
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()


def ensure_database_schema():
    """Create or migrate the seen_entries table as needed."""
    cursor.execute("PRAGMA table_info(seen_entries)")
    info = cursor.fetchall()
    if not info:
        cursor.execute(
            """CREATE TABLE seen_entries (
                feed_name TEXT,
                search_type TEXT,
                entry_id TEXT,
                timestamp TEXT,
                title TEXT,
                PRIMARY KEY (feed_name, search_type, entry_id)
            )"""
        )
        conn.commit()
        return

    columns = [row[1] for row in info]
    pk_columns = [row[1] for row in info if row[5] > 0]
    needs_migration = False
    if "title" not in columns:
        needs_migration = True
    if pk_columns != ["feed_name", "search_type", "entry_id"]:
        needs_migration = True

    if needs_migration:
        cursor.execute(
            "SELECT feed_name, search_type, entry_id, timestamp, COALESCE(title, '') FROM seen_entries"
        )
        rows = cursor.fetchall()
        cursor.execute("DROP TABLE seen_entries")
        cursor.execute(
            """CREATE TABLE seen_entries (
                feed_name TEXT,
                search_type TEXT,
                entry_id TEXT,
                timestamp TEXT,
                title TEXT,
                PRIMARY KEY (feed_name, search_type, entry_id)
            )"""
        )
        cursor.executemany(
            "INSERT OR REPLACE INTO seen_entries (feed_name, search_type, entry_id, timestamp, title) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


ensure_database_schema()

# FTP credentials are provided via environment variables
FTP_HOST = os.environ.get('FTP_HOST', 'nemeslab.com')
FTP_USER = os.environ.get('FTP_USER')
FTP_PASS = os.environ.get('FTP_PASS')

# Path to the file containing the regular expressions used for searching
SEARCHTERMS_FILE = os.path.join(os.path.dirname(__file__), 'search_terms.json')

# Default search terms in case the external file is missing
DEFAULT_SEARCHTERMS = {
    'primary': '(topolog[a-z]+)|(graphit[a-z]+)|(rhombohedr[a-z]+)|(graphe[a-z]+)|(ABC.+)|(chalcog[a-z]+)|(landau)|(weyl)|(dirac)|(STM)|(scan[a-z]+ tunne[a-z]+ micr[a-z]+)|(scan[a-z]+ tunne[a-z]+ spectr[a-z]+)|(scan[a-z]+ prob[a-z]+ micr[a-z]+)|(MoS.+\\d+|MoS\\d+)|(MoSe.+\\d+|MoSe\\d+)|(MoTe.+\\d+|MoTe\\d+)|(WS.+\\d+|WS\\d+)|(WSe.+\\d+|WSe\\d+)|(WTe.+\\d+|WTe\\d+)|(Bi\\d+Rh\\d+I\\d+|Bi.+\\d+.+Rh.+\\d+.+I.+\\d+.+)|(BiTeI)|(BiTeBr)|(BiTeCl)|(ZrTe5|ZrTe.+5)|(Pt2HgSe3|Pt.+2HgSe.+3)|(jacuting[a-z]+)|(flatband)|(flat.{1}band)',
    'rg': '(rhombohedr[a-z]+.*graph[a-z]+)|(ABC.*graph[a-z]+)|(ABC.*trilay[a-z]+)|(ABCA.*tetralay[a-z]+)|(ABCB.*tetralay[a-z]+)|(tetralay[a-z]+.*graph[a-z]+)|(pentalay[a-z]+.*graph[a-z]+)|(graph[a-z]+.*pentalay[a-z]+)|(hexalay[a-z]+.*graph[a-z]+)|(graph[a-z]+.*hexalay[a-z]+)|(heptalay[a-z]+.*graph[a-z]+)|(graph[a-z]+.*heptalay[a-z]+)',
    'perovskites': '(perovskit.*photoelec.*)|(perovskit.*photocatho.*)|(perovskit.*photoano.*)|(organi.*photoelec.*)|(solar.*water.*splitt.*)|(photoelectrochem.*water.*splitt.*)|(photoelectrochem.*biom.*oxid.*)|(photoelectrochem.*valu.*add.*)|(perovsk.*sola.*cell)|(organi.*sola.*cell)'
}

def load_searchterms():
    """Load search terms from SEARCHTERMS_FILE or fall back to defaults."""
    try:
        with open(SEARCHTERMS_FILE, 'r', encoding='utf-8') as f:
            terms = json.load(f)
            logging.info(f"Loaded search terms from {SEARCHTERMS_FILE}")
    except Exception as e:
        logging.warning(f"Could not read search terms file: {e}. Using defaults.")
        terms = DEFAULT_SEARCHTERMS
    # Ensure all keys are present
    for key, val in DEFAULT_SEARCHTERMS.items():
        terms.setdefault(key, val)
    return terms

# Load and compile the search terms
terms = load_searchterms()

# Compile each search term into a regular expression.  This allows adding new
# topics by simply inserting additional key/value pairs in ``search_terms.json``.
search_patterns = {
    key: re.compile(pattern, re.IGNORECASE) for key, pattern in terms.items()
}

# Path to the file containing the list of feeds
FEEDS_FILE = os.path.join(os.path.dirname(__file__), 'feeds.json')
HTML_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'html_template.html')


def load_feeds():
    """Load feed URLs from FEEDS_FILE or exit on failure."""
    if not os.path.exists(FEEDS_FILE):
        logging.error(f"Feed list file '{FEEDS_FILE}' not found. Exiting.")
        sys.exit(1)
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
            feeds_data = json.load(f)
    except Exception as e:
        logging.error(f"Could not read feed list: {e}. Exiting.")
        sys.exit(1)
    return feeds_data


# Database of feed URLs loaded from the JSON file
database = load_feeds()

# List of feeds to process
feeds = list(database.keys())

def load_seen_entries(feed_name, search_type):
    """Load seen entries for a feed/search type from the database."""
    cursor.execute(
        "SELECT entry_id, timestamp, title FROM seen_entries WHERE feed_name=? AND search_type=?",
        (feed_name, search_type),
    )
    rows = cursor.fetchall()
    return {
        entry_id: (datetime.datetime.fromisoformat(ts), title or "")
        for entry_id, ts, title in rows
    }


def save_seen_entries(entries, feed_name, search_type):
    """Persist seen entries for a feed/search type to the database."""
    cutoff = (datetime.datetime.now() - TIME_DELTA).isoformat()
    cursor.execute(
        "DELETE FROM seen_entries WHERE feed_name=? AND search_type=? AND timestamp < ?",
        (feed_name, search_type, cutoff),
    )
    for entry_id, value in entries.items():
        ts, title = value
        cursor.execute(
            "INSERT OR REPLACE INTO seen_entries (feed_name, search_type, entry_id, timestamp, title) VALUES (?, ?, ?, ?, ?)",
            (feed_name, search_type, entry_id, ts.isoformat(), title),
        )
    conn.commit()

def clear_database():
    """Remove all entries from the SQLite database."""
    cursor.execute("DELETE FROM seen_entries")
    conn.commit()

def purge_database(days: int):
    """Remove entries older than the specified number of days from the database."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
    cursor.execute(
        "DELETE FROM seen_entries WHERE timestamp < ?",
        (cutoff,),
    )
    conn.commit()

def matches_search_terms(entry, search_pattern):
    """Check if the entry matches the given search pattern."""
    fields_to_search = []
    # Collect text from title, summary, and author fields
    title = entry.get('title', '')
    summary = entry.get('summary', '')

    fields_to_search.extend([title, summary])

    # Search for the pattern in all collected fields
    for text in fields_to_search:
        if text and search_pattern.search(text):
            return True
    return False


def clean_old_entries(seen_entries):
    """Remove entries older than 6 months from seen_entries."""
    current_time = datetime.datetime.now()
    keys_to_delete = []
    for entry_id, (entry_datetime, _title) in seen_entries.items():
        if (current_time - entry_datetime) > TIME_DELTA:
            keys_to_delete.append(entry_id)
    for key in keys_to_delete:
        del seen_entries[key]


def compute_entry_id(entry):
    """Return a stable SHA-1 based ID for a feed entry."""
    candidate = entry.get("id") or entry.get("link")
    if candidate:
        parsed = urllib.parse.urlparse(candidate)
        candidate = urllib.parse.urlunparse(
            parsed._replace(query="", fragment="")
        )
        return hashlib.sha1(candidate.encode("utf-8")).hexdigest()

    parts = [
        entry.get("title", ""),
        entry.get("published", entry.get("updated", "")),
    ]
    concat = "||".join(parts)
    return hashlib.sha1(concat.encode("utf-8")).hexdigest()

def process_text(text):
    """Process text to escape HTML characters and handle LaTeX code."""
    if not text:
        return ''
    # Escape HTML characters
    text = html.escape(text, quote=False)
    # Unescape LaTeX-related characters to preserve LaTeX code
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
    # Replace double backslashes with single backslash
    text = text.replace('\\\\', '\\')

    # Ensure dollar signs are not escaped
    text = text.replace('&#36;', '$')

    return text

def generate_html(all_entries_per_feed, html_file_path, search_description):
    """Generate or append HTML content using a simple template."""
    file_exists = os.path.exists(html_file_path)

    if not file_exists:
        class PercentTemplate(Template):
            delimiter = '%'

        with open(HTML_TEMPLATE_PATH, 'r', encoding='utf-8') as tmpl:
            template = PercentTemplate(tmpl.read())
        rendered = template.substitute(
            title=html.escape(search_description),
            date=datetime.date.today(),
            content="",
        )
        with open(html_file_path, 'w', encoding='utf-8') as f:
            f.write(rendered)

    with open(html_file_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    insert_position = html_content.rfind('</body>')
    if insert_position == -1:
        insert_position = len(html_content)

    new_entries_html = []

    FEED_HEADER = Template('<h2>Feed: $title</h2>')
    ENTRY_TEMPLATE = Template(
        '<div class="entry">\n'
        '  <h3><a href="$link">$title</a></h3>\n'
        '  <p><strong>Authors:</strong> $authors</p>\n'
        '  <p><em>Published: $published</em></p>\n'
        '  <p>$summary</p>\n'
        '</div>\n<hr>'
    )

    if not any(all_entries_per_feed.values()):
        new_entries_html.append('<p class="no-entries"> </p>')
    else:
        for feed_name in feeds:
            entries = all_entries_per_feed.get(feed_name, [])
            if not entries:
                continue

            feed_title = entries[0].get('feed_title', feed_name) if entries else feed_name
            new_entries_html.append(FEED_HEADER.substitute(title=html.escape(feed_title)))

            for entry in entries:
                title = process_text(entry.get('title', 'No title'))
                link = entry.get('link', '#')
                published = entry.get('published', entry.get('updated', 'No published date'))
                summary = process_text(entry.get('summary', entry.get('description', 'No summary')))

                authors = entry.get('authors', [])
                if authors:
                    author_names = ', '.join(author.get('name', '') for author in authors)
                else:
                    author_names = entry.get('author', 'No author')
                author_names = process_text(author_names)

                context = {
                    'link': link,
                    'title': title,
                    'authors': author_names,
                    'published': published,
                    'summary': summary,
                }
                new_entries_html.append(ENTRY_TEMPLATE.substitute(context))

    updated_html = (
        html_content[:insert_position]
        + '\n'.join(new_entries_html)
        + html_content[insert_position:]
    )

    with open(html_file_path, 'w', encoding='utf-8') as f:
        f.write(updated_html)

def main(upload: bool = True):
    today = datetime.date.today()

    topics = list(search_patterns.keys())

    # Dictionary holding new entries per topic and per feed
    all_new_entries = {
        topic: {feed: [] for feed in feeds} for topic in topics
    }

    # Pre-compute output file names for each topic
    html_files = {}
    archive_files = {}
    stable_files = {}

    for topic in topics:
        if topic == 'primary':
            archive_files[topic] = f'filtered_articles_{today}.html'
            html_files[topic] = archive_files[topic]
            stable_files[topic] = 'results_primary.html'
        elif topic == 'rg':
            html_files[topic] = 'rg_filtered_articles.html'
            archive_files[topic] = f'rg_filtered_articles_{today}.html'
            stable_files[topic] = html_files[topic]
        else:
            archive_files[topic] = f'{topic}_filtered_articles_{today}.html'
            html_files[topic] = archive_files[topic]
            stable_files[topic] = f'{topic}_filtered_articles.html'

    for feed_name in feeds:
        rss_feed_url = database.get(feed_name)
        if rss_feed_url is None:
            logging.warning(f"No URL found for feed '{feed_name}'")
            continue

        logging.info(f"Processing feed '{feed_name}'")

        # Fetch and parse the RSS feed
        feed = feedparser.parse(rss_feed_url)
        feed_entries = feed.entries

        # Add feed title to each entry
        feed_title = feed.feed.get('title', feed_name) # type: ignore
        for entry in feed_entries:
            entry['feed_title'] = feed_title

        # Load seen entries for all topics once
        seen_entries_per_topic = {
            topic: load_seen_entries(feed_name, topic) for topic in topics
        }
        seen_titles_per_topic = {
            topic: {
                details[1] for details in seen_entries_per_topic[topic].values()
            }
            for topic in topics
        }

        current_time = datetime.datetime.now()

        # Iterate over each entry a single time and test against all patterns
        for entry in feed_entries:
            entry_id = compute_entry_id(entry)
            entry_title = entry.get("title", "").strip()
            entry_published = entry.get('published_parsed') or entry.get('updated_parsed')

            if entry_published:
                if isinstance(entry_published, time.struct_time):
                    entry_datetime = datetime.datetime(*entry_published[:6])
                else:
                    entry_datetime = entry_published
            else:
                entry_datetime = current_time

            # Skip entries older than the TIME_DELTA window
            if (current_time - entry_datetime) > TIME_DELTA: # type: ignore
                continue

            for topic, pattern in search_patterns.items():
                seen_entries = seen_entries_per_topic[topic]
                seen_titles = seen_titles_per_topic[topic]

                # add entry to all_new_entries if it is new and matches the search term
                if (
                    entry_title not in seen_titles
                    and matches_search_terms(entry, pattern)
                ):
                    # Record new entry for this topic
                    all_new_entries[topic][feed_name].append(entry)
                    seen_entries[entry_id] = (
                        entry_datetime, entry_title
                    )
                    seen_titles.add(entry_title)

        # After processing all entries, persist the databases per topic
        for topic in topics:
            clean_old_entries(seen_entries_per_topic[topic])
            seen_titles_per_topic[topic] = {
                details[1] for details in seen_entries_per_topic[topic].values()
            }
            save_seen_entries(seen_entries_per_topic[topic], feed_name, topic)


    for topic in topics:
        description = (
            "Filtered Articles Matching Search Terms" if topic == "primary" else
            f"Articles related to {topic}"
        )

        generate_html(
            all_new_entries[topic],
            os.path.join(MAIN_DIR, html_files[topic]),
            search_description=description,
        )
        print(f"Generated/Updated HTML file: {html_files[topic]}")

        if topic == "rg":
            generate_html(
                all_new_entries[topic],
                os.path.join(MAIN_DIR, archive_files[topic]),
                search_description=description,
            )
            shutil.move(
                os.path.join(MAIN_DIR, archive_files[topic]),
                os.path.join(ARCHIVE_DIR, archive_files[topic]),
            )
        else:
            shutil.copy(
                os.path.join(MAIN_DIR, html_files[topic]),
                os.path.join(MAIN_DIR, stable_files[topic]),
            )
            shutil.move(
                os.path.join(MAIN_DIR, html_files[topic]),
                os.path.join(ARCHIVE_DIR, archive_files[topic]),
            )
    if upload:
        if not FTP_USER or not FTP_PASS:
            raise ValueError(
                "FTP_USER and FTP_PASS must be set as environment variables for FTP upload"
            )

        ## write to FTP server using credentials from environment variables
        try:
            with ftplib.FTP(FTP_HOST) as session:
                session.login(user=FTP_USER, passwd=FTP_PASS)
                session.cwd('/public_html/cond-mat/')
                for topic in topics:
                    filename = stable_files[topic]
                    with open(os.path.join(MAIN_DIR, filename), 'rb') as f:
                        session.storbinary('STOR ' + filename, f)

                # upload to archive
                session.cwd('/public_html/wp-content/uploads/simple-file-list/')
                for topic in topics:
                    archive_name = archive_files[topic]
                    with open(os.path.join(ARCHIVE_DIR, archive_name), 'rb') as f:
                        session.storbinary('STOR ' + archive_name, f)
        except ftplib.all_errors as e:
            logging.error("FTP upload failed: %s", e)
            sys.exit(1)

    return all_new_entries


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
        clear_database()
        print("All entries removed from the database.")
        conn.close()
        sys.exit(0)

    if args.purge_days is not None:
        purge_database(args.purge_days)
        print(f"Entries older than {args.purge_days} days removed from the database.")
        conn.close()
        sys.exit(0)

    new_entries = main(upload=args.upload)
    conn.close()

    if not args.no_summary:
        llmsummary.main(new_entries)
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
    