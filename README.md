# cond-mat, paper RSS parser

The project fetches articles from various journal feeds, filters them using regular expressions, summarizes them using an OpenAI language model, and uploads HTML summaries via FTP.

## Installation

Developed with **Python 3.11** (requires Python 3.8+ for PaperQA compatibility). Install dependencies:

```bash
pip install -r requirements.txt
```
Key dependencies include:
- [feedparser](https://pypi.org/project/feedparser/) for RSS parsing
- [paper-qa](https://github.com/Future-House/paper-qa) for advanced paper analysis and ranking

## Setup

### Configuration Files

- **feeds.json**: Required. Map feed names to RSS URLs. Must be placed next to `rssparser.py`.
- **search_terms.json**: Optional. Defines topics as key/value pairs, where keys are topic names and values are regular expressions.
  - The special topic `rg` (rhombohedral graphite) is updated daily.
  - The search terms and the behavior of the `rg` topic is tailored to the needs of our research group, but adding new topics is easy, by appending the `search_terms.json` file. Future development may make this behavior more general, or maybe it won't :)
  - Other topics (e.g., `primary`, `perovskites`) generate daily HTML summaries that are automatically archived.
- **llm_prompts.json**: Optional. Custom instructions for language model summaries. Place next to `llmsummary.py`, keys correspond to topic names.
- **priority_journals.json**: Optional. List of high-priority journals whose articles are always processed by LLM summary regardless of search term matches. Defaults to Nature, Science, and Nature family journals.

### Environment Variables

Set these variables (e.g., in your crontab):

- `FTP_HOST`: FTP server hostname
- `FTP_USER`: FTP username
- `FTP_PASS`: FTP password
- `OPENAI_API_KEY`: OpenAI API key for summarization (optional; see below)

Alternatively, store your OpenAI API key in a file named `openaikulcs.env` next to `llmsummary.py`.

## Usage

### RSS Parser

Run the main parser:

```bash
python rssparser.py [options]
```

#### Command-Line Options

- `--no-upload`: Skip FTP upload (useful for testing)
- `--no-summary`: Skip summarization step
- `--clear-db`: Clear stored article IDs and exit
- `--purge-days X`: Remove database entries older than `X` days and exit

By default, the script generates a ranked, topic‑organized report with `llmsummary.py` into `summary.html` for the entries discovered in the current run.

### PaperQA Summarizer

Testing PaperQA summarizer:

```bash
python paperqa_summarizer.py [arxiv_id_or_url]
```

This tool:
- Downloads PDFs from arXiv URLs or IDs (e.g., `2506.20738` or `https://arxiv.org/abs/2506.20738`)
- Uses PaperQA's AI-powered analysis to answer comprehensive questions about the paper
- Provides detailed summaries including research objectives, methods, findings, and implications
- Requires OpenAI API key (loaded from `openaikulcs.env` or `OPENAI_API_KEY` environment variable)

### Daily summary (topic-ranked report)

- **What it produces**: `summary.html` with per‑topic sections (e.g., `primary`, `rg`, `perovskites`). Each section contains:
  - **Overview**: 1–3 sentence high‑level takeaways (when using LLM ranking).
  - **Top items**: The most important papers from this run, each showing title (linked), authors, journal (bold), a 4–5 sentence summary, and a **Score** badge.

- **How "Score" is calculated**:
  - **LLM ranking (default when `OPENAI_API_KEY` is available)**
    - The model ranks entries per topic and assigns an `importance_score` on a 1–5 scale (higher is more important) considering topical relevance, novelty/impact, and experimental/theory significance.
    - Articles from priority journals (Nature, Science, etc.) are always included and given preferential treatment in ranking.
    - The page displays that `importance_score` in the badge.
  - **Future Enhancement**: Planning on implementing PaperQA-based ranking for more sophisticated analysis using two-stage evaluation (abstract screening + full PDF analysis for top candidates).

- **Configuration**:
  - `llm_prompts.json`: optional per‑topic guidance to steer ranking and phrasing.
  - `search_terms.json`: topic regexes are provided as context to the model.
  - API key: set `OPENAI_API_KEY` or place it in `openaikulcs.env` next to `llmsummary.py`.


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

- **Two-Stage PaperQA Ranking**: Integration of PaperQA into the main RSS workflow for more sophisticated paper ranking
  - Stage 1: Fast abstract-based screening of all papers
  - Stage 2: Deep PDF analysis of top candidates
  - Enhanced relevance scoring based on full content analysis
- Additional features and details available in the repository wiki
