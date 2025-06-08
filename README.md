# cond-mat, paper parser

This project fetches articles from various scientific feeds, filters them by
regular expressions, and uploads HTML summaries via FTP. Seen article IDs are
tracked in an SQLite database stored under `assets/seen_entries.db`.

The scripts are developed with **Python&nbsp;3.11**. Install the required
dependencies using:

```bash
pip install -r requirements.txt
```

## Usage
Current use case is FTP upload to a website, you can set this up for yourself. However, FTP upload is not needed, in this case use it with the `--no-upload` command line option.

1. Set the following environment variables (for example in your crontab):
   - `FTP_HOST` – hostname of the FTP server
   - `FTP_USER` – FTP username
   - `FTP_PASS` – FTP password

2. Create a `feeds.json` file next to `rssparser.py` containing a JSON
   object mapping feed names to their RSS URLs. The script will exit with an
   error if this file is missing.
3. Place a `search_terms.json` file next to `rssparser.py` to
   override the default search regular expressions. The file may define any
   number of topics as key/value pairs where the key is a name and the value is
   a regular expression.  The special topic `rg` is updated in-place each run,
   while all other topics (including `primary` and `perovskites`) generate daily
   HTML files which are archived automatically.
4. To add your own topics, edit `search_terms.json` and add a new key/value
   pair with the topic name as the key and a regular expression as the value.
   The parser will automatically generate an HTML summary for every topic it
   finds in this file.


The generated HTML files will be uploaded to the FTP server by default. Pass
`--no-upload` when running the script to skip the FTP step, which can be useful
for testing.  Pass `--clear-db` to remove all stored article IDs from the database and exit.

## LLM Summary

When `OPENAI_API_KEY` is set, running `rssparser.py` will automatically
generate an `llm_summary.html` file summarizing the most recent results.
The summary is created using the OpenAI API and placed next to the parser
scripts. If the key is missing or the `openai` package is unavailable the
summary step is skipped.

