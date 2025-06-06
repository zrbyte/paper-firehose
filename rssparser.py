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

# Setup logging
logging.basicConfig(level=logging.INFO)

# Constants
TIME_DELTA = datetime.timedelta(days=182)  # Approximately 6 months
MAIN_DIR = os.getcwd()
ASSETS_DIR = os.path.join(MAIN_DIR, 'assets')
ARCHIVE_DIR = os.path.join(MAIN_DIR, 'archive')
# MAIN_DIR = '/uu/nemes/cond-mat/'
# ASSETS_DIR = '/uu/nemes/cond-mat/assets/'

# Initialize SQLite database for tracking seen entries
DB_PATH = os.path.join(ASSETS_DIR, 'seen_entries.db')
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute(
    """CREATE TABLE IF NOT EXISTS seen_entries (
        feed_name TEXT,
        search_type TEXT,
        entry_id TEXT PRIMARY KEY,
        timestamp TEXT
    )"""
)
conn.commit()

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
        "SELECT entry_id, timestamp FROM seen_entries WHERE feed_name=? AND search_type=?",
        (feed_name, search_type),
    )
    rows = cursor.fetchall()
    return {entry_id: datetime.datetime.fromisoformat(ts) for entry_id, ts in rows}


def save_seen_entries(entries, feed_name, search_type):
    """Persist seen entries for a feed/search type to the database."""
    cutoff = (datetime.datetime.now() - TIME_DELTA).isoformat()
    cursor.execute(
        "DELETE FROM seen_entries WHERE feed_name=? AND search_type=? AND timestamp < ?",
        (feed_name, search_type, cutoff),
    )
    for entry_id, ts in entries.items():
        cursor.execute(
            "INSERT OR REPLACE INTO seen_entries (feed_name, search_type, entry_id, timestamp) VALUES (?, ?, ?, ?)",
            (feed_name, search_type, entry_id, ts.isoformat()),
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

def get_new_entries(feed_entries, seen_entries, search_pattern):
    """Return a list of new entries not present in seen_entries and matching the search pattern."""
    new_entries = []
    current_time = datetime.datetime.now()
    for entry in feed_entries:
        entry_id = entry.get('id', entry.get('link'))
        entry_published = entry.get('published_parsed') or entry.get('updated_parsed')

        if entry_published:
            if isinstance(entry_published, time.struct_time):
                entry_datetime = datetime.datetime(*entry_published[:6])
            else:
                # In some cases, entry_published might already be a datetime object
                entry_datetime = entry_published
        else:
            entry_datetime = current_time  # If no publication date, assume current time

        # Skip entries older than 6 months
        if (current_time - entry_datetime) > TIME_DELTA:
            continue

        # Check if entry is new and matches search terms
        if entry_id not in seen_entries and matches_search_terms(entry, search_pattern):
            new_entries.append(entry)
            # Add to seen entries with timestamp
            seen_entries[entry_id] = entry_datetime

    return new_entries

def clean_old_entries(seen_entries):
    """Remove entries older than 6 months from seen_entries."""
    current_time = datetime.datetime.now()
    keys_to_delete = []
    for entry_id, entry_datetime in seen_entries.items():
        if (current_time - entry_datetime) > TIME_DELTA:
            keys_to_delete.append(entry_id)
    for key in keys_to_delete:
        del seen_entries[key]

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
        feed_title = feed.feed.get('title', feed_name)
        for entry in feed_entries:
            entry['feed_title'] = feed_title

        # Load seen entries for all topics once
        seen_entries_per_topic = {
            topic: load_seen_entries(feed_name, topic) for topic in topics
        }

        current_time = datetime.datetime.now()

        # Iterate over each entry a single time and test against all patterns
        for entry in feed_entries:
            entry_id = entry.get('id', entry.get('link'))
            entry_published = entry.get('published_parsed') or entry.get('updated_parsed')

            if entry_published:
                if isinstance(entry_published, time.struct_time):
                    entry_datetime = datetime.datetime(*entry_published[:6])
                else:
                    entry_datetime = entry_published
            else:
                entry_datetime = current_time

            # Skip entries older than the TIME_DELTA window
            if (current_time - entry_datetime) > TIME_DELTA:
                continue

            for topic, pattern in search_patterns.items():
                seen_entries = seen_entries_per_topic[topic]

                if (
                    entry_id not in seen_entries
                    and matches_search_terms(entry, pattern)
                ):
                    # Record new entry for this topic
                    all_new_entries[topic][feed_name].append(entry)
                    seen_entries[entry_id] = entry_datetime

        # After processing all entries, persist the databases per topic
        for topic in topics:
            clean_old_entries(seen_entries_per_topic[topic])
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process RSS feeds")
    parser.add_argument(
        "--no-upload",
        action="store_false",
        dest="upload",
        help="skip FTP upload",
    )
    args = parser.parse_args()

    main(upload=args.upload)
    conn.close()
    
