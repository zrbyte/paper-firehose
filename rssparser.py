import os
import re
import pickle
import datetime
import feedparser
import html
import logging
import time
import shutil
import ftplib
import json
import sys
from string import Template

# Setup logging
logging.basicConfig(level=logging.INFO)

# Constants
TIME_DELTA = datetime.timedelta(days=182)  # Approximately 6 months
# MAIN_DIR = os.getcwd() + '/'
# ASSETS_DIR = os.getcwd() + '/assets' + '/'
MAIN_DIR = '/uu/nemes/cond-mat/'
ARCHIVE_DIR = '/uu/nemes/cond-mat/archive/'
ASSETS_DIR = '/uu/nemes/cond-mat/assets/'

# HTML template used when creating new files
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>$title</title>
<script type="text/javascript">
  MathJax = {
    tex: {
      inlineMath: [['$', '$'], ['\\(', '\\)']],
      displayMath: [['$$', '$$'], ['\\[', '\\]']],
      processEscapes: true
    }
  };
</script>
<script type="text/javascript" id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js">
</script>
<style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    .entry { margin-bottom: 20px; }
    h2 { color: #2E8B57; }
    h3 { color: #4682B4; }
    hr { border: 0; border-top: 1px solid #ccc; }
    .no-entries { font-style: italic; color: #555; }
</style>
</head>
<body>
<h1>$title</h1>
<h1>New papers on $date</h1>
<hr>
$content
</body>
</html>
"""

# FTP credentials are provided via environment variables
FTP_HOST = os.environ.get('FTP_HOST', 'nemeslab.com')
FTP_USER = os.environ.get('FTP_USER')
FTP_PASS = os.environ.get('FTP_PASS')

if not FTP_USER or not FTP_PASS:
    raise ValueError('FTP_USER and FTP_PASS must be set as environment variables')

# Path to the file containing the regular expressions used for searching
SEARCHTERMS_FILE = os.path.join(os.path.dirname(__file__), 'search_terms.json')
# Path to the file containing the RSS feed URLs
FEEDS_FILE = os.path.join(os.path.dirname(__file__), 'feeds.json')

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

search_pattern_all = re.compile(terms['primary'], re.IGNORECASE)
search_pattern_rg = re.compile(terms['rg'], re.IGNORECASE)
search_pattern_perovs = re.compile(terms['perovskites'], re.IGNORECASE)

# Default feed URLs in case the external file is missing
# Only the arXiv condensed matter feed is enabled by default. Use ``feeds.json``
# to provide a custom list with additional sources.
DEFAULT_FEEDS = {
    'cond-mat': 'https://rss.arxiv.org/rss/cond-mat'
}

def load_feeds():
    """Load feed URLs from FEEDS_FILE or fall back to defaults."""
    try:
        with open(FEEDS_FILE, 'r', encoding='utf-8') as f:
            feeds = json.load(f)
            logging.info(f"Loaded feeds from {FEEDS_FILE}")
    except Exception as e:
        logging.warning(f"Could not read feeds file: {e}. Using defaults.")
        feeds = DEFAULT_FEEDS
    return feeds

# Database of feed URLs
database = load_feeds()

# List of feeds to process
feeds = list(database.keys())

def load_seen_entries(tracking_file):
    """Load the set of seen entry IDs from the tracking file."""
    if os.path.exists(ASSETS_DIR + tracking_file):
        with open(ASSETS_DIR + tracking_file, 'rb') as f:
            seen_entries = pickle.load(f)
    else:
        seen_entries = {}
    return seen_entries

def save_seen_entries(seen_entries, tracking_file):
    """Save the set of seen entry IDs to the tracking file."""
    with open(ASSETS_DIR + tracking_file, 'wb') as f:
        pickle.dump(seen_entries, f)

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
    """Generate or append HTML content for the list of entries, including MathJax support."""
    # Check if the HTML file exists
    file_exists = os.path.exists(html_file_path)

    # if the file doesn't exist
    if not file_exists:
        # Create the initial HTML structure using a template
        template = Template(HTML_TEMPLATE)
        rendered = template.safe_substitute(
            title=html.escape(search_description),
            date=datetime.date.today(),
            content="",
        )
        with open(html_file_path, 'w', encoding='utf-8') as f:
            f.write(rendered)

    # Read the existing HTML content if the file exists
    with open(html_file_path, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # Find the position before the closing </body> tag
    insert_position = html_content.rfind('</body>')
    
    if insert_position == -1:
        # If </body> not found, append at the end
        insert_position = len(html_content)

    # Prepare new entries to insert
    new_entries_html = []

    # If no new entries, add a message indicating no matches
    if not any(all_entries_per_feed.values()):
        new_entries_html.append('<p class="no-entries"> </p>') # append a blank message if there are no new entries
        # new_entries_html.append('<hr>') # adds a horizontal line
    else:
        # new_entries_html.append(f'<h1>New papers on {datetime.date.today()}</h1>')
        for feed_name in feeds:
            entries = all_entries_per_feed.get(feed_name, [])
            if not entries:
                continue  # Skip feeds with no new entries

            # Add a header for the feed
            feed_title = entries[0].get('feed_title', feed_name) if entries else feed_name
            new_entries_html.append(f'<h2>Feed: {html.escape(feed_title)}</h2>')

            for entry in entries:
                title = entry.get('title', 'No title')
                link = entry.get('link', '#')
                published = entry.get('published', entry.get('updated', 'No published date'))
                summary = entry.get('summary', entry.get('description', 'No summary'))
                feed_title = entry.get('feed_title', 'Unknown Feed')

                # Process the title, author(s), and summary to handle LaTeX
                title = process_text(title)

                # Handle authors
                authors = entry.get('authors', [])
                if authors:
                    # Combine author names
                    author_names = ', '.join([author.get('name', '') for author in authors])
                else:
                    author_names = entry.get('author', 'No author')
                author_names = process_text(author_names)

                summary = process_text(summary)

                new_entries_html.append('<div class="entry">')
                new_entries_html.append(f'<h3><a href="{link}">{title}</a></h3>')
                new_entries_html.append(f'<p><strong>Authors:</strong> {author_names}</p>')
                new_entries_html.append(f'<p><em>Published: {published}</em></p>')
                new_entries_html.append(f'<p>{summary}</p>')
                new_entries_html.append('</div>')
                new_entries_html.append('<hr>')

    # Insert the new entries before </body>
    updated_html = html_content[:insert_position] + '\n'.join(new_entries_html) + html_content[insert_position:]

    # Write back the updated HTML content
    with open(html_file_path, 'w', encoding='utf-8') as f:
        f.write(updated_html)

def main():
    # Initialize dictionaries to hold new entries for each search
    all_new_entries_primary = {feed: [] for feed in feeds}
    all_new_entries_rg = {feed: [] for feed in feeds}
    all_new_entries_perovs = {feed: [] for feed in feeds}
    
    # Define output HTML file paths
    primary_html_file = f'filtered_articles_{datetime.date.today()}.html'
    rg_html_file = f'rg_filtered_articles.html'
    rg_html_file_archive = f'rg_filtered_articles_{datetime.date.today()}.html'
    perovs_html_file = f'perovs_filtered_articles_{datetime.date.today()}.html'

    for feed_name in feeds:
        rss_feed_url = database.get(feed_name)
        if rss_feed_url is None:
            logging.warning(f"No URL found for feed '{feed_name}'")
            continue

        logging.info(f"Processing feed '{feed_name}'")

        # Each feed has its own tracking files
        tracking_file_primary = f'{feed_name}_seen_entries_primary.pkl'
        tracking_file_rg = f'{feed_name}_seen_entries_rg.pkl'
        tracking_file_perovs = f'{feed_name}_seen_entries_perovs.pkl'

        # Load previously seen entries for primary search
        seen_entries_primary = load_seen_entries(tracking_file_primary)

        # Load previously seen entries for RG search
        seen_entries_rg = load_seen_entries(tracking_file_rg)

        # Load previously seen entries for perovskite search
        seen_entries_perovs = load_seen_entries(tracking_file_perovs)

        # Fetch and parse the RSS feed
        feed = feedparser.parse(rss_feed_url)
        feed_entries = feed.entries

        # Add feed title to each entry
        feed_title = feed.feed.get('title', feed_name)
        for entry in feed_entries:
            entry['feed_title'] = feed_title

        # Get new entries that match the primary search terms
        new_entries_primary = get_new_entries(feed_entries, seen_entries_primary, search_pattern_all)
        all_new_entries_primary[feed_name].extend(new_entries_primary)

        # Get new entries that match the RG search terms
        new_entries_rg = get_new_entries(feed_entries, seen_entries_rg, search_pattern_rg)
        all_new_entries_rg[feed_name].extend(new_entries_rg)

        # Get new entries that match the perovskite search terms
        new_entries_perovs = get_new_entries(feed_entries, seen_entries_perovs, search_pattern_perovs)
        all_new_entries_perovs[feed_name].extend(new_entries_perovs)

        # Clean old entries from seen_entries_primary
        clean_old_entries(seen_entries_primary)

        # Clean old entries from seen_entries_rg
        clean_old_entries(seen_entries_rg)

        # Clean old entries from seen_entries_perosv
        clean_old_entries(seen_entries_perovs)

        # Save updated seen entries for primary search
        # `get_new_entries` updates the seen_entries
        save_seen_entries(seen_entries_primary, tracking_file_primary)

        # Save updated seen entries for RG search
        save_seen_entries(seen_entries_rg, tracking_file_rg)

        # Save updated seen entries for perovskite search
        save_seen_entries(seen_entries_perovs, tracking_file_perovs)

    # Generate and save primary HTML
    generate_html(
        all_new_entries_primary,
        MAIN_DIR + primary_html_file,
        search_description="Filtered Articles Matching Search Terms"
    )
    print(f"Generated/Updated HTML file: {primary_html_file}")
    # copy the search terms to a new file to be uploaded
    shutil.copy(MAIN_DIR + primary_html_file, MAIN_DIR + 'results_primary.html')
    # move the archive to the archive directory
    shutil.move(MAIN_DIR + primary_html_file, ARCHIVE_DIR + primary_html_file)

    # Generate and save RG HTML (always update)
    generate_html(
        all_new_entries_rg,
        MAIN_DIR + rg_html_file,
        search_description="Articles related to rhombohedral graphite"
    )
    print(f"Generated/Updated HTML file: {rg_html_file}")

    # Generate and save RG HTML as backup
    generate_html(
        all_new_entries_rg,
        MAIN_DIR + rg_html_file_archive,
        search_description="Articles related to rhombohedral graphite"
    )
    print(f"Generated/Updated HTML file: {rg_html_file_archive}")
    # move the archive to the archive directory
    shutil.move(MAIN_DIR + rg_html_file_archive, ARCHIVE_DIR + rg_html_file_archive)

    # Generate and save perovskite HTML (always update)
    generate_html(
        all_new_entries_perovs,
        MAIN_DIR + perovs_html_file,
        search_description="Articles related to perovskites"
    )
    print(f"Generated/Updated HTML file: {perovs_html_file}")
    # copy the to a new file to be uploaded
    shutil.copy(MAIN_DIR + perovs_html_file, MAIN_DIR + 'perovs_filtered_articles.html')
    # move to the archive directory
    shutil.move(MAIN_DIR + perovs_html_file, ARCHIVE_DIR + perovs_html_file)


    ## write to FTP server using credentials from environment variables
    try:
        with ftplib.FTP(FTP_HOST) as session:
            session.login(user=FTP_USER, passwd=FTP_PASS)
            session.cwd('/public_html/cond-mat/')
            with open('/uu/nemes/cond-mat/results_primary.html', 'rb') as f:
                session.storbinary('STOR ' + 'results_primary.html', f)
            with open('/uu/nemes/cond-mat/' + rg_html_file, 'rb') as f:
                session.storbinary('STOR ' + rg_html_file, f)
            with open('/uu/nemes/cond-mat/' + 'perovs_filtered_articles.html', 'rb') as f:
                session.storbinary('STOR ' + 'perovs_filtered_articles.html', f)
            # upload to archive
            session.cwd('/public_html/wp-content/uploads/simple-file-list/')
            with open('/uu/nemes/cond-mat/archive/' + primary_html_file, 'rb') as f:
                session.storbinary('STOR ' + primary_html_file, f)
            with open('/uu/nemes/cond-mat/archive/' + rg_html_file_archive, 'rb') as f:
                session.storbinary('STOR ' + rg_html_file_archive, f)
            with open('/uu/nemes/cond-mat/archive/' + perovs_html_file, 'rb') as f:
                session.storbinary('STOR ' + perovs_html_file, f)
    except ftplib.all_errors as e:
        logging.error("FTP upload failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
    
