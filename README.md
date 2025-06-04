# Cond-Mat Parser

This project fetches articles from various scientific feeds, filters them by
regular expressions, and uploads HTML summaries via FTP. Seen article IDs are
tracked in an SQLite database stored under `assets/seen_entries.db`.

## Usage
For FTP upload to a website, you have to set this up for yourself.

1. Set the following environment variables (for example in your crontab):
   - `FTP_HOST` – hostname of the FTP server
   - `FTP_USER` – FTP username
   - `FTP_PASS` – FTP password

2. Optionally place a `search_terms.json` file next to `rssparser.py` to
   override the default search regular expressions. The file should contain a
   JSON object with keys `primary`, `rg`, and `perovskites`.

3. Run the parser:

```bash
python3 'server version/rewrite/rssparser.py'
```

The generated HTML files will be uploaded to the FTP server. Entry state is
stored in `assets/seen_entries.db` instead of the old pickle tracking files.
