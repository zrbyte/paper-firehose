# Paper Firehose

Being well read is a pillar of good science, but the volume of new papers makes it impossible to truly keep up to date. Paper-firehose is a way to filter the flood of new papers, so that it becomes a trickle, that one can go through in less than 5 minutes a day. It can check daily the RSS feeds of journals you are interested in and throw out results that are not relevant to your interests. The papers that remain get ranked by how relevant they are to keywords you specify. Our research group uses this to keep up to date with new papers that appear in our field.

Matched papers get stored in an SQLite database. Based on this one can generate HTML pages or an email digest. Optionally full‑text (paper-qa) summaries of preprints from arXiv can also be generated. Have a look at a [demo](https://zrbyte.github.io/paper-firehose/results_perovskites_summary.html) of how the resulting list looks like, when we gather the daily new papers appearing in the field of 2D, van der Waals materials.

Documentation: [zrbyte.github.io/paper-firehose](https://zrbyte.github.io/paper-firehose/index.html)

## How to use:

### Install locally
- `pip install paper-firehose`
- CLI entrypoint: `paper-firehose`
- After install, run `paper-firehose --help` for available command line options.
- In Jupyter or a Python file: `import paper_firehose as pf`

Configuration is done using only YAML text files. On first run the default YAML configs are copied into your runtime data directory (defaults to `~/.paper_firehose`, override with `PAPER_FIREHOSE_DATA_DIR`) from `src/paper_firehose/system/config`. Edit those files to customize feeds and topics. To reuse the GitHub Actions config locally, run `python scripts/bootstrap_config.py` – it copies `github_actions_config/` into your data directory so you work with the same files as the scheduled GitHub Actions workflow.

Set up an OpenAI API key environment variable for paper-qa summarization to work.

### Automated run using GitHub Actions
- Fork the repo.
- Copy `github_actions_config/topics/topic-template.yaml` to create topic files, or tweak the existing ones. See `github_actions_config/README.md` for a guided walkthrough.
- Edit the `pages.yml` file in the `schedule.cron` part to set when the automated job runs.
- Set up GitHub Secrets under Secrets and Variables / Actions. You don't need this step if you're only running the `filter` and `rank` commands. If you want the summarization to work setup an `OPENAI_API_KEY` environment variable. For email alert functionality, you will need `MAILING_LISTS_YAML` and `SMTP_PASSWORD` env variables.
  - `OPENAI_API_KEY`. This is optional if you want to run the paper-qa full text summarization. Set up as a GitHub actions environment secret.
  - `MAILING_LISTS_YAML`. This contains the emails and other config that the email alert functionality needs. Just copy the contents of your `mailing_lists.yaml` file. This is a GitHub actions secret so you don't expose user info to the outside world in the repo.
  - `SMTP_PASSWORD`. The password for your email server. Set up as a GitHub actions secret.

The `html` command in GitHub Actions (see `pages.yml`), generates HTML files (name of which is set in the YAML config) with your results. The GitHub Actions runner then pushes these generated HTML files to `https://<your GH username>.github.io/paper-firehose/<your results>.html`, where they can be accessed on the open web.

## Quick Start

1) Seed and inspect config
```
paper-firehose status
```

2) Run the core pipeline for all topics
```
paper-firehose filter
paper-firehose rank
paper-firehose abstracts --mailto you@example.com --rps 1.0
paper-firehose html           # write HTML from DB
paper-firehose export-recent  # optional: create smaller DB for fast web loading
```
You can specify to run a specific topic, with the `--topic YOUR_TOPIC` option.

3) Optional: full‑text summaries via [paper‑qa](https://futurehouse.gitbook.io/futurehouse-cookbook/paperqa)
```
# Download arXiv PDFs for high‑ranked entries and summarize with paper‑qa
paper-firehose pqa_summary
```
Costs are dependent on which model you use, but generally are less than 0.1 USD per run for one topic.

4) Email newsletter
```
# Send a ranked email digest (SMTP config required)
paper-firehose email
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

- `html [--topic TOPIC]`
  - Generate HTML page(s) directly from `papers.db`. For a single topic, `output.filename` is used unless you override via the Python API (see below).

- `export-recent [--days N] [--output PATH]`
  - Export recent entries from `matched_entries_history.db` to a smaller database file for faster web loading.
  - Default: creates `matched_entries_history.recent.db` with last 60 days of entries.
  - Used by the history viewer HTML for fast initial page loads, with full archive accessible on demand.

- `pqa_summary [--topic TOPIC] [--rps FLOAT] [--limit N] [--arxiv ID|URL ...] [--entry-id ID ...] [--use-history] [--history-date YYYY-MM-DD] [--history-feed-like STR] [--summarize]`
  - Download arXiv PDFs for ranked entries (or explicit IDs/URLs) with polite rate limiting, archive them, optionally run paper‑qa, and write normalized JSON into DBs. Old PDFs are discarded from the archive. We don't do scraping.
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
    filter, rank, abstracts, pqa_summary, email, purge, status, html, export_recent,
)

# Run steps
filter(topic="perovskites")
rank(topic="perovskites")
abstracts(topic="perovskites", mailto="you@example.com", rps=1.0)

# Generate HTML (single topic can override output path)
html(topic="perovskites")
html(topic="perovskites", output_path="results_perovskites.html")

# Export recent entries for fast web loading
export_recent(days=60)  # default
export_recent(days=30, output_name="matched_entries_history.recent.db")

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

## Configuration

Runtime data dir
- Default: `~/.paper_firehose` on your home folder on macOS or Linux. On Windows it's: `C:\Users\<YourUser>\.paper_firehose`.
- Override with `PAPER_FIREHOSE_DATA_DIR` environment variable
- First run seeds `config/`, `templates/`, and optional `models/` from the bundled `system/` directory.

Files to edit
- `config/config.yaml`: global settings (DB paths, feeds, paper‑qa, defaults, optional email/SMTP)
- `config/topics/<topic>.yaml`: topic name/description, feeds, regex filter, ranking, abstract fetch and output filenames
- `config/secrets/`: secret material that should not be committed. These secrets can be either stored as `*.env` files or as environment variables.
  - `email_password.env`: SMTP password (referenced by `email.smtp.password_file`)
  - `mailing_lists.yaml`: optional per‑recipient overrides for `email`:
    ```yaml
    recipients:
      - to: person@example.com
        topics: [perovskites, batteries]   # subset of topics for this person
        mode: ranked                       # currently always renders ranked from DB
        limit: 10                          # per‑recipient cap
        min_rank_score: 0.3                # optional cutoff
    ```

Key config fields
- `filter.pattern`: This is the regular expression that does the heavy lifting of "casting a wide net" and trying to capture papers from the RSS feeds which are related to your topic of interest. The point of using regular expressions is that they can capture the many ways in which certain terms can be written. For example: the regexp `(scan[a-z]+ tunne[a-z]+ micr[a-z]+)` will match “scanning tunneling microscopy” as well as “scanned tunneling microscopies”, as well as the British and US English spellings of 'tunnelling' and 'tunneling'. The results of the regexp match can then be ranked by similarity to the keyword list under `ranking.query`. It takes a bit of thought to set this up, but it is powerful.
- `ranking.query`: List of keywords that are used by an embedding model to rank the results. Asking an LLM to generate regex patterns from your keywords might be an easy way to set up `filter.pattern`.
- `feeds`: mapping of feed keys to `{name, url, enabled}`. Feed keys are referenced in topic files; `name` is stored in DBs and used in HTML.
- `priority_journals` and `priority_journal_boost`: optional global score boost by feed key.
- Topic `ranking`: `query`, `model`, optional `negative_queries`, `preferred_authors`, `priority_author_boost`.
- Topic `output`: `filename`, `filename_ranked`, `archive: true|false`.
- `paperqa`: `download_rank_threshold`, `rps` (≤ 0.33 recommended), `max_retries`, and `prompt` for JSON‑only answers.

Environment variables
- `PAPER_FIREHOSE_DATA_DIR` select/override the runtime data location
- `OPENAI_API_KEY` for `pqa_summary`
- `MAILTO` used for polite arXiv/Crossref User‑Agent when not specified on CLI

## Data & Outputs

Databases (under the data dir unless absolute paths are used)
- `all_feed_entries.db` (table `feed_entries`): every fetched item for deduplication
- `matched_entries_history.db` (table `matched_entries`): historical archive of matches, optional JSON summaries
- `matched_entries_history.recent.db` (table `matched_entries`): recent entries only (default: last 60 days), used for fast initial page loads
- `papers.db` (table `entries`): current‑run working set with `status`, `rank_score`, `paper_qa_summary`

HTML
- Generated by the `html` command from `papers.db` using templates in `templates/`. Ranked pages are produced when configured.
- The history viewer HTML (`history_viewer_cards_pf.html`) loads the recent database by default for faster initial load, with a "Load Full Archive" button to access the complete history.

Email
- Requires `email.smtp` config: `host`, `port`, `username`, and either `password` or `password_file`. Uses SSL.

## Future dev
- Run ranking on the historic database, with a unique query. To search for specific papers.

## Final notes

- Python 3.11+ recommended. See `pyproject.toml` for dependencies.
- Thank you to arXiv for use of its open access interoperability. This project links to arXiv/publisher pages and does not serve PDFs.
