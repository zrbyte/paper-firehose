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
   Summaries for `rg` only include the new entries discovered on a given day.
4. To add your own topics, edit `search_terms.json` and add a new key/value
   pair with the topic name as the key and a regular expression as the value.
   The parser will automatically generate an HTML summary for every topic it
   finds in this file.

### OpenAI API key

`llmsummary.py` expects an OpenAI API key either provided via the `OPENAI_API_KEY` environment variable or stored in a file named `openaikulcs.env` next to the script. Without this key the summary step will fail. See `load_api_key` in `llmsummary.py` for details.

The generated HTML files will be uploaded to the FTP server by default. Pass
`--no-upload` when running the script to skip the FTP step, which can be useful
for testing.  Pass `--clear-db` to remove all stored article IDs from the database and exit.
By default the script runs `llmsummary.py` at the end to create a daily summary.
The parser now passes the collected feed entries directly to `llmsummary.py` rather than
having it parse the generated HTML files. Each entry includes its title and summary so the
  language model receives more context. Use the `--no-summary` option if you want to skip
  this step. Custom LLM instructions can be placed in an `llm_prompts.json` file
next to `llmsummary.py` where the keys correspond to topic names.

The `rg` topic and any additional topics now pass each paper's link to the
language model, enabling summaries with numbered citation links like
`[1](URL)`.

Summaries now list each entry's main point on its own line and finish with the
matching citation link, so you might see `[1](URL)` at the end of every line.

