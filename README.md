# Paper Firehose

Filter, rank, and summarize research-paper RSS feeds. Stores results in SQLite and can generate HTML pages or an email digest. Optional LLM- and full‑text (paper-qa) summaries.

Quick install
- `pip install paper_firehose`
- CLI entrypoint: `paper-firehose`

After install, run `paper-firehose --help` for available command line options

Configuration is done using only YAML text files. On first run the default YAML configs are copied into your runtime data directory (defaults to `~/.paper_firehose`, override with `PAPER_FIREHOSE_DATA_DIR`) from `src/paper_firehose/system/config`. Edit those files to customize feeds and topics.

## Quick Start

1) Seed and inspect config
```
paper-firehose status
```

2) Run the core pipeline for one topic
```
paper-firehose filter --topic perovskites
paper-firehose rank --topic perovskites
paper-firehose abstracts --topic perovskites --mailto you@example.com --rps 1.0
paper-firehose pqa_summary --topic perovskites    # optional (needs OpenAI key)
paper-firehose html --topic perovskites           # write HTML from DB
```
If no topic is specified, all topics are processed.

3) Optional: full‑text summaries via [paper‑qa](https://futurehouse.gitbook.io/futurehouse-cookbook/paperqa) and email digest
```
# Download arXiv PDFs for high‑ranked entries and summarize with paper‑qa
paper-firehose pqa_summary --topic perovskites --rps 0.33 --limit 20 --summarize

# Send a ranked email digest (SMTP config required)
paper-firehose email --limit 10 --dry-run                # writes preview HTML under data dir
```

## CLI Reference

Global options
- `--config PATH` use a specific YAML config (defaults to `~/.paper_firehose/config/config.yaml`)
- `-v/--verbose` enable debug logging

Commands
- `filter [--topic TOPIC]`
  - Fetch RSS feeds, dedup by title, apply per‑topic regex, write matches to databases.
  - Backs up `all_feed_entries.db` and `matched_entries_history.db`, then clears current `papers.db` working table.

- `rank [--topic TOPIC]`
  - Compute `rank_score` using Sentence‑Transformers similarity to `ranking.query`.
  - Optional boosts: per‑topic `ranking.preferred_authors` (`priority_author_boost`) and global `priority_journals` (`priority_journal_boost`).
  - Models can be vendored under the data dir `models/`. The default alias `all-MiniLM-L6-v2` is supported.

- `abstracts [--topic TOPIC] [--mailto EMAIL] [--limit N] [--rps FLOAT]`
  - Fetch abstracts above a rank threshold (topic `abstract_fetch.rank_threshold` or global `defaults.rank_threshold`).
  - Uses polite rate limits; sets a descriptive arXiv/Crossref User‑Agent including your contact email.

- `summarize [--topic TOPIC] [--rps FLOAT]`
  - LLM summaries of abstracts for top‑ranked entries using `config.llm` and per‑topic `llm_summary` settings.
  - Requires an OpenAI API key.

- `html [--topic TOPIC]`
  - Generate HTML page(s) directly from `papers.db`. For a single topic, `output.filename` is used unless you override via the Python API (see below).

- `pqa_summary [--topic TOPIC] [--rps FLOAT] [--limit N] [--arxiv ID|URL ...] [--entry-id ID ...] [--use-history] [--history-date YYYY-MM-DD] [--history-feed-like STR] [--summarize]`
  - Download arXiv PDFs for ranked entries (or explicit IDs/URLs) with polite rate limiting, archive them, optionally run paper‑qa, and write normalized JSON into DBs. Old pdfs are discarded from the archive. We don't do scraping.
  - Accepts `--arxiv` values like `2501.12345`, `2501.12345v2`, `https://arxiv.org/abs/2501.12345`, or `https://arxiv.org/pdf/2501.12345.pdf`.

- `email [--topic TOPIC] [--mode auto|ranked] [--limit N] [--recipients PATH] [--dry-run]`
  - Send a compact HTML digest via SMTP (SSL). In dry‑run, writes a preview HTML to the data dir.
  - `--recipients` points to a YAML file with per‑recipient overrides (see Configuration).

- `purge (--days N | --all)`
  - Remove entries by date from databases, or clear all and reinitialize schemas (`--all`).

- `status`
  - Validate configuration and list available topics, enabled feeds, and database paths.

## Python API

Import functions directly from the package for programmatic workflows:

```python
from paper_firehose import (
    filter, rank, abstracts, summarize, pqa_summary, email, purge, status, html,
)

# Run steps
filter(topic="perovskites")
rank(topic="perovskites")
abstracts(topic="perovskites", mailto="you@example.com", rps=1.0)
summarize(topic="perovskites", rps=0.5)

# Generate HTML (single topic can override output path)
html(topic="perovskites")
html(topic="perovskites", output_path="results_perovskites.html")

# Paper‑QA download + summarize
pqa_summary(topic="perovskites", rps=0.33, limit=10)
pqa_summary(arxiv=["2501.12345", "https://arxiv.org/abs/2501.12345v2"], summarize=True)

# Email digest
email(limit=10, dry_run=True)

# Maintenance
purge(days=7)
info = status()
print(info["valid"], info["topics"])  # dict with config + paths
```

Aliases
- `paperqa_summary` is available as an alias of `pqa_summary`.
- `generate_html` is an alias of `html`.

## Configuration

Runtime data dir
- Default: `~/.paper_firehose` on your home folder on MacOS or Linux. On Windows it's: `C:\Users\<YourUser>\.paper_firehose`.
- Override with `PAPER_FIREHOSE_DATA_DIR` environment variable
- First run seeds `config/`, `templates/`, and optional `models/` from the bundled `system/` directory.

Files to edit
- `config/config.yaml`: global settings (DB paths, feeds, LLM, paper‑qa, defaults, optional email/SMTP)
- `config/topics/<topic>.yaml`: topic name/description, feeds, regex filter, ranking, abstract fetch, LLM summary, and output filenames
- `config/secrets/`: secret material that should not be committed
  - `openaikulcs.env`: OpenAI API key for `summarize` and `pqa_summary`
  - `email_password.env`: SMTP password (referenced by `email.smtp.password_file`)
  - `mailing_lists.yaml`: optional per‑recipient overrides for `email`:
    ```yaml
    recipients:
      - to: person@example.com
        topics: [perovskites, batteries]   # subset of topics for this person
        mode: ranked                       # currently always renders ranked from DB
        limit: 10                          # per‑recipient cap
        min_rank_score: 0.4                # optional cutoff
    ```

Key config fields
- `feeds`: mapping of feed keys to `{name, url, enabled}`. Feed keys are referenced in topic files; `name` is stored in DBs and used in HTML.
- `priority_journals` and `priority_journal_boost`: optional global score boost by feed key.
- Topic `ranking`: `query`, `model`, optional `negative_queries`, `preferred_authors`, `priority_author_boost`.
- Topic `output`: `filename`, `filename_ranked`, optional `filename_summary`, `archive: true|false`.
- Topic `llm_summary`: `enabled`, `prompt` (can reference `{ranking_query}`), `score_cutoff`, `top_n`.
- `paperqa`: `download_rank_threshold`, `rps` (≤ 0.33 recommended), `max_retries`, and `prompt` for JSON‑only answers.
- `llm`: `model`, `model_fallback`, `api_key_env`, default `rps`, `max_retries`, plus optional GPT‑5 `verbosity` and `reasoning_effort`.

Environment variables
- `PAPER_FIREHOSE_DATA_DIR` select/override the runtime data location
- `OPENAI_API_KEY` (or `config.llm.api_key_env`) for `summarize`
- `MAILTO` used for polite arXiv/Crossref User‑Agent when not specified on CLI

## Data & Outputs

Databases (under the data dir unless absolute paths are used)
- `all_feed_entries.db` (table `feed_entries`): every fetched item for deduplication
- `matched_entries_history.db` (table `matched_entries`): historical archive of matches, optional JSON summaries
- `papers.db` (table `entries`): current‑run working set with `status`, `rank_score`, `llm_summary`, `paper_qa_summary`

HTML
- Generated by the `html` command from `papers.db` using templates in `templates/`. Ranked and LLM‑summary pages are produced when configured.

Email
- Requires `email.smtp` config: `host`, `port`, `username`, and either `password` or `password_file`. Uses SSL.

## Future dev
- Improve the history browser HTML interface.
- Run ranking on the historic database, with a unique query. To search for specific papers.
- The abstract summarizer doesn't make much sense at this point, might remove it in the future.

## Final notes

- Python 3.11+ recommended. See `pyproject.toml` for dependencies.
- Thank you to arXiv for use of its open access interoperability. This project links to arXiv/publisher pages and does not serve PDFs.

