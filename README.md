# cond-mat, paper parser

This project fetches articles from various scientific feeds, filters them by
regular expressions, and uploads HTML summaries via FTP. Seen article IDs are
tracked in an SQLite database stored under `assets/seen_entries.db`.

For FTP upload to a website, you have to set this up for yourself.

1. Set the following environment variables (for example in your crontab):
   - `FTP_HOST` – hostname of the FTP server
   - `FTP_USER` – FTP username
   - `FTP_PASS` – FTP password

2. Create a `feeds.json` file next to `rssparser.py` containing a JSON
   object mapping feed names to their RSS URLs. The script will exit with an
   error if this file is missing.
3. Optionally place a `search_terms.json` file next to `rssparser.py` to
   override the default search regular expressions. The file may define any
   number of topics as key/value pairs where the key is a name and the value is
   a regular expression.  The special topic `rg` is updated in-place each run,
   while all other topics (including `primary` and `perovskites`) generate daily
   HTML files which are archived automatically.

The generated HTML files will be uploaded to the FTP server.
