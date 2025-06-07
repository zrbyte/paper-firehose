# Cond-Mat Parser

This project fetches articles from various scientific feeds, filters them by
regular expressions, and uploads HTML summaries via FTP.

## Usage
Current use case is FTP upload to a website, you can set this up for yourself. However, FTP upload is not needed, in this case use it with the --no-upload option.

1. Set the following environment variables (for example in your crontab):
   - `FTP_HOST` – hostname of the FTP server
   - `FTP_USER` – FTP username
   - `FTP_PASS` – FTP password

2. Optionally place a `search_terms.json` file next to `rssparser.py` to
   override the default search regular expressions. The file should contain a
   JSON object with keys `primary`, `rg`, and `perovskites`.

3. By default the parser only checks the arXiv `cond-mat` feed. To use a
   custom list of sources, place a `feeds.json` file next to `rssparser.py`
   containing a JSON object that maps feed names to their URLs.

4. Run the parser:

```bash
python3 rssparser.py
```

## LLM Summary

When `OPENAI_API_KEY` is set, running `rssparser.py` will automatically
generate an `llm_summary.html` file summarizing the most recent results.
The summary is created using the OpenAI API and placed next to the parser
scripts. If the key is missing or the `openai` package is unavailable the
summary step is skipped.

