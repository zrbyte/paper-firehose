# cond-mat, paper RSS parser

The project fetches articles from various journal feeds, filters them using regular expressions, summarizes them using an OpenAI language model, and uploads HTML summaries via FTP.

## Installation

Developed with **Python 3.8**. Install dependencies:

```bash
pip install -r requirements.txt
```
The only non standard python package that is needed is [feedparser](https://pypi.org/project/feedparser/).

## Setup

### Configuration Files

- **feeds.json**: Required. Map feed names to RSS URLs. Must be placed next to `rssparser.py`.
- **search_terms.json**: Optional. Defines topics as key/value pairs, where keys are topic names and values are regular expressions.
  - The special topic `rg` (rhombohedral graphite) is updated daily.
  - The search terms and the behavior of the `rg` topic is tailored to the needs of our research group, but adding new topics is easy, by appending the `search_terms.json` file. Future development may make this behavior more general, or maybe it won't :)
  - Other topics (e.g., `primary`, `perovskites`) generate daily HTML summaries that are automatically archived.
- **llm_prompts.json**: Optional. Custom instructions for language model summaries. Place next to `llmsummary.py`, keys correspond to topic names.

### Environment Variables

Set these variables (e.g., in your crontab):

- `FTP_HOST`: FTP server hostname
- `FTP_USER`: FTP username
- `FTP_PASS`: FTP password
- `OPENAI_API_KEY`: OpenAI API key for summarization (optional; see below)

Alternatively, store your OpenAI API key in a file named `openaikulcs.env` next to `llmsummary.py`.

## Usage

Run the parser:

```bash
python rssparser.py [options]
```

### Command-Line Options

- `--no-upload`: Skip FTP upload (useful for testing)
- `--no-summary`: Skip summarization step
- `--clear-db`: Clear stored article IDs and exit
- `--purge-days X`: Remove database entries older than `X` days and exit

By default, the script generates a ranked, topic‑organized report with `llmsummary.py` into `summary.html` for the entries discovered in the current run.

### Daily summary (topic-ranked report)

- **What it produces**: `summary.html` with per‑topic sections (e.g., `primary`, `rg`, `perovskites`). Each section contains:
  - **Overview**: 1–3 sentence high‑level takeaways (when using LLM ranking).
  - **Top items**: The most important papers from this run, each showing title (linked), authors, journal (bold), a 4–5 sentence summary, and a **Score** badge.

- **How “Score” is calculated**:
  - **LLM ranking (default when `OPENAI_API_KEY` is available)**
    - The model ranks entries per topic and assigns an `importance_score` on a 1–5 scale (higher is more important) considering topical relevance, novelty/impact, and experimental/theory significance.
    - The page displays that `importance_score` in the badge.

- **Configuration**:
  - `llm_prompts.json`: optional per‑topic guidance to steer ranking and phrasing.
  - `search_terms.json`: topic regexes are provided as context to the model.
  - API key: set `OPENAI_API_KEY` or place it in `openaikulcs.env` next to `llmsummary.py`.

- **“This run”**: The summary only considers the entries returned by `rssparser.py` during the current invocation, not historical data.

## GitHub Actions and Pages

A GitHub Actions workflow (`.github/workflows/pages.yml`) runs the parser daily and publishes results to GitHub Pages:

- Set the `OPENAI_API_KEY` repository secret for summarization (optional; if omitted, the ranked lists will be empty and only section headers/overviews may appear).
- Use `--no-summary` in the workflow to disable summarization if desired.
- Enable GitHub Pages in repository settings with source set to **GitHub Actions**.

Your summaries will be available at the URL provided in the workflow output. The `summary.html` is the topic‑ranked report for that day’s run.

Current URLs:
- [Summary](https://zrbyte.github.io/paper-firehose/summary.html)
- [Primary Results](https://zrbyte.github.io/paper-firehose/results_primary.html)

## Database Details

Seen article IDs and titles are tracked in an SQLite database (`assets/seen_entries.db`):

- Each database row is unique per feed and topic to prevent cross-feed collisions.
- IDs are generated via SHA‑1 hash from entry `id`, URL (cleaned of query parameters), or a combination of title and publication date.
- Duplicate detection uses article titles, skipping identical entries even if links change.

Metadata for all new matching entries is stored in a separate SQLite database (`assets/matched_entries_history.db`).
Each row stores the feed name, topic, entry ID, timestamp and the full entry metadata as JSON for later queries.
The history database is never automatically purged, so it accumulates all matched entries across runs.

## Future Development

- Planned enhancements and more details available in the repository wiki.
