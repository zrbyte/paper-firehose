# Paper Firehose

- Fetches academic RSS feeds, filters entries with per-topic regex, and writes results into SQLite databases. HTML pages with the results are rendered directly from the database.
- Results are ranked by cosine similarity to a set of user defined keywords. Configurable list of authors can get a ranking boost. So papers from your friends / competitors can be boosted in ranking.
- Highest ranked results are summarized by an LLM. For summarization to work, you need an OpenAI API key.
- New search terms can be created by simply adding a yaml config file under: `config/topics/your_topic_name.yaml`. Look at the other topics for guidance. The script is configured to run using Github actions each day at 6 AM to fetch new papers. For your own runs, configure the `.github/workflows/pages.yml` file.

Written using Python 3.11.
For dependencies check `requirements.txt`.
OpenAI API key is searched for in the `openaikulcs.env` file in the repo root or `OPENAI_API_KEY` environment variable.

## CLI

- **Filter**: fetch from RSS feed list, dedup, match regex, write DBs, render HTML
  - `python cli/main.py -v filter [--topic TOPIC]`
  - Backs up `all_feed_entries.db` and `matched_entries_history.db` (keeps 3 latest backups).
  - Clears `papers.db` working table before processing this run.
  - Fetches configured feeds for each topic, dedups by title against `all_feed_entries.db`, filters by topic regex.
  - Writes matches to `papers.db` (`status='filtered'`); optionally archives to `matched_entries_history.db` if `output.archive: true`.
  - Saves ALL processed entries (matched and non-matched) to `all_feed_entries.db` for future dedup.
  - Renders per-topic HTML from `papers.db`.

- **Rank**: ranks fetched papers by title, wrt keywords in yaml config (optional)
  - `python cli/main.py rank [--topic TOPIC]`
  - Computes and writes `rank_score` for `papers.db` entries using sentence-transformers. HTML files with ranked entries are generated.
  - Model selection: if `models/all-MiniLM-L6-v2` exists, it is used; otherwise it falls back to the Hugging Face repo id `all-MiniLM-L6-v2` and downloads once into cache. You can vendor the model with `python scripts/vendor_model.py`.
  - Scoring details: applies a small penalty for `ranking.negative_queries` matches (title/summary). Optional boosts: per-topic `ranking.preferred_authors` with `ranking.priority_author_boost`, and global `priority_journal_boost` for feeds listed in `priority_journals`.

- **Abstracts**: try to fetch abstracts
  - `python cli/main.py abstracts [--topic TOPIC] [--mailto you@example.com] [--limit N] [--rps 1.0]`
  - Order: 1) Fill arXiv/cond-mat abstracts from `summary` (no threshold)  2) Above-threshold: Crossref (DOI, then title)  3) Above-threshold: Semantic Scholar → OpenAlex → PubMed
  - Threshold: topic `abstract_fetch.rank_threshold` else global `defaults.rank_threshold`.
  - Only topics with `abstract_fetch.enabled: true` are processed.
  - Writes to both `papers.db.entries.abstract` and `matched_entries_history.db.matched_entries.abstract`.
  - Rate limiting: descriptive User-Agent (includes `--mailto` or `$MAILTO`), respects Retry-After; default ~1 req/sec via `--rps`.
  - Populates the `entries.abstract` column; leaves other fields unchanged.
  - Contact email: if `--mailto` is not provided, the command reads `$MAILTO` from the environment; if unset, it uses a safe default.

- **Summarize** (optional)
  - `python cli/main.py summarize [--topic TOPIC] [--rps 0.5]`
  - Selects top entries per topic based on `llm_summary.score_cutoff` and `llm_summary.top_n`, builds input strictly from `title + abstract` (skips entries without an abstract), and calls the configured OpenAI chat model.
  - Writes summaries to `papers.db.entries.llm_summary` and, when present, `matched_entries_history.db.matched_entries.llm_summary`.
  - Uses `config.llm` settings. Supports JSON or plain-text responses; JSON is preferred and rendered with headings.
  - Note: This command only updates databases. Use `html` to render pages.

- **pqa_summary** (PDF-based summarization)
  - `python cli/main.py pqa_summary [--topic TOPIC]`
  - Selects preprints from arXiv in `papers.db` with `rank_score >= config.paperqa.download_rank_threshold`, detects arXiv IDs, and downloads PDFs (polite arXiv API usage).
  - Runs paper-qa to summarize full text into JSON keys: `summary`, `topical_relevance`, `methods`, `novelty_impact`.
  - Writes summaries to `papers.db.entries.paper_qa_summary` only for the specific topic row the item was selected under (no longer cross-updating all topics for the same entry id), and to `matched_entries_history.db.matched_entries.paper_qa_summary`.
  - Note: This command only updates databases. Use `html` to render pages.

- **HTML** (render only; no fetching)
  - `python cli/main.py html [--topic TOPIC]`
  - Reads from `papers.db` and generates, per topic:
    - Filtered page: `output.filename` (if configured)
    - Ranked page: `output.filename_ranked` (if configured and entries exist)
    - Summary page: `output.filename_summary` (if configured). Content priority: PDF summaries → LLM summaries → abstract-only fallback; always ordered by rank.

### Summary pages

- When `output.filename_summary` is set for a topic, summary pages prefer content in this order:
  1) `paper_qa_summary` (PDF-based)
  2) `llm_summary`
  3) Fallback to ranked fields (abstract → summary)
- Entries are ordered by descending `rank_score`.

- **Purge**
  - `python cli/main.py purge --days N` removes entries with `published_date` within the most recent N days in the seen entries DB.
  - `python cli/main.py purge --all` deletes all DB files and reinitializes schemas (no confirmation prompt).

- **Status**
  - `python cli/main.py status`
  - Validates config, lists topics/feeds, and shows DB paths.

- **Email** (Mailman digest)
  - `python cli/main.py email [--topic TOPIC] [--mode auto|ranked] [--limit N] [--dry-run]`
  - Builds an email‑friendly HTML digest from `papers.db` and sends via SMTP (SSL).
  - `--mode auto` renders a ranked‑style list directly from `papers.db`; `--mode ranked` embeds the pre‑generated ranked HTML if present, otherwise falls back to the ranked‑style list.
  - `--dry-run` writes a preview HTML to `assets/` instead of sending.
  - Per‑recipient routing: `python cli/main.py email --recipients config/secrets/mailing_lists.yaml` or set `config.email.recipients_file`.

### Email configuration

Add an `email` section in `config/config.yaml` (secrets in a separate file):

```
email:
  to: "LIST_ADDRESS@yourdomain"             # Mailman list address
  subject_prefix: "Paper Firehose"           # Optional
  from: "_mainaccount@nemeslab.com"          # Defaults to smtp.username
  smtp:
    host: "mail.nemeslab.com"
    port: 465
    username: "_mainaccount@nemeslab.com"
    password_file: "config/secrets/email_password.txt"  # store only the password here
```

Notes
- Store the SMTP password in `config/secrets/email_password.txt` (gitignored).
- The command prefers to be run after `filter`, `rank`, `abstracts`, and `summarize` so it can include LLM summaries when available.

Per‑recipient YAML (config/secrets/mailing_lists.yaml)
Example:
```
recipients:
  - to: "materials-list@nemeslab.com"
    topics: ["primary", "perovskites"]
    min_rank_score: 0.40
  - to: "2d-list@nemeslab.com"
    topics: ["rg", "2d_metals"]
    min_rank_score: 0.35
```

## Python API

You can call the main steps programmatically via `paper_firehose`.

Basics
- `import paper_firehose as pf`
- All functions default to `config/config.yaml`; override with `config_path="..."`.

Functions
- `pf.filter(topic=None, config_path=None)`: Runs the filter step for one topic or all.
- `pf.rank(topic=None, config_path=None)`: Computes and writes `rank_score` for entries.
- `pf.abstracts(topic=None, *, mailto=None, limit=None, rps=None, config_path=None)`: Fetches abstracts for above‑threshold entries and writes to DBs.
- `pf.summarize(topic=None, *, rps=None, config_path=None)`: Runs LLM summarization for top‐ranked entries per topic, writing JSON (or text) to `llm_summary`. Generates summary HTML if configured.
- `pf.generate_html(topic, output_path=None, config_path=None)`: Regenerates the filtered list HTML directly from `papers.db` for the given topic (uses topic description, and defaults to the topic’s configured `output.filename` if `output_path` is omitted).
- `pf.purge(days=None, all_data=False, config_path=None)`: Purges entries based on publication date. When `days` is provided, removes entries from the most recent N days (including today) across all DBs; when `all_data=True`, reinitializes all DBs.
- `pf.status(config_path=None) -> dict`: Returns a dict with config validity, topics, feed count, and DB paths.

## History Viewer

- `history_viewer.html` is a static browser viewer for `assets/matched_entries_history.db` (table `matched_entries`).
- By default it auto-loads the latest history DB from GitHub:
  - Displayed: `https://github.com/zrbyte/paper-firehose/tree/data/assets/matched_entries_history.latest.db`
  - The viewer automatically normalizes GitHub page links to their raw content (e.g., `raw.githubusercontent.com`) before fetching.
- You can override with a query param or local file:
  - `history_viewer.html?db=<url>` to load a specific remote DB
  - Use the file input or drag-and-drop a local `matched_entries_history.db`

- `history_viewer_cards.html` provides a cleaner, card‑style view of history entries with just the key fields (title, authors, feed name, abstract, matched date). It supports the same controls and query params as `history_viewer.html` (topic, order, search, `?db=<url>` and file drag‑and‑drop) but focuses on readability instead of tabular data.


## Architecture overview

- Three-DB architecture:
  - `assets/all_feed_entries.db`: Every fetched item (for deduplication).
  - `assets/matched_entries_history.db`: All matched items across topics and runs (historical archive).
  - `assets/papers.db`: Current-run working set (filtered → ranked → summarized).
- YAML-driven configuration for feeds and topics.
- HTML generated from `papers.db` so you can re-render without refetching.
- Optional LLM summarization writes JSON summaries to DB and renders dedicated summary pages.

### Databases

- `all_feed_entries.db` (table `feed_entries`)
  - Keys: `entry_id` (pk), `feed_name` (display name from `config.yaml`), `title`, `link`.
  - Metadata: `summary`, `authors`, `published_date`, `first_seen`, `last_seen`, `raw_data` (JSON).
  - Used only for dedup; populated after filtering completes.

- `matched_entries_history.db` (table `matched_entries`)
  - Keys: `entry_id` (pk), `feed_name`, `topics` (CSV of topic names).
  - Metadata: `title`, `link`, `summary`, `authors`, `abstract` (nullable), `doi` (nullable), `published_date`, `matched_date`, `raw_data` (JSON), `llm_summary` (nullable), `paper_qa_summary` (nullable).
  - Written only when a topic’s `output.archive: true`.

- `papers.db` (table `entries`)
  - Primary key: composite `PRIMARY KEY(id, topic)` so the same entry can appear once per topic.
  - Columns: `id`, `topic`, `feed_name` (display name), `title`, `link`, `summary`, `authors`, `abstract` (nullable), `doi` (nullable), `published_date`, `discovered_date`, `status` (`filtered|ranked|summarized`), `rank_score`, `rank_reasoning`, `llm_summary`, `raw_data` (JSON).

Notes
- `feed_name` is the human-readable name from `config.yaml -> feeds.<key>.name` (e.g., "Nature Physics").
- `doi` is best-effort, fetched from RSS feed and can be found in `doi`, `dc:identifier`, `prism:doi`, `id`, `link`, `summary`, `summary_detail.value`, or embedded `content[].value`. arXiv feeds may not include DOIs; no external lookup is performed (by design for now).
- `abstract` populated via Crossref API or publisher APIs.


## Configuration

- `config/config.yaml` (feeds, DB paths, defaults)
  - Each feed has a key and a display `name`; the key is used in topic files, the name is stored in DBs.
  - `paperqa`: settings for the arXiv downloader (Phase 1)
    - `download_rank_threshold`: minimum `rank_score` to download (default 0.35)
    - `rps`: requests/second throttle (default 0.3; ~1 request/3.3s per arXiv API guidance)
    - `max_retries`: per-item retry attempts on transient errors
    - `prompt`: paper-qa question used for summarization; should instruct the model to return only JSON with keys `summary`, `topical_relevance`, `methods`, `novelty_impact` (supports `{ranking_query}` placeholder)
- `config/topics/<topic>.yaml`
  - `feeds`: list of feed keys from `config.yaml`.
  - `filter.pattern` and `filter.fields`: regex and fields to match (defaults include `title` and `summary`).
  - `ranking`: optional `query`, `model`, cutoffs, etc. (for the rank command).
    - Optional: `negative_queries` (list), `preferred_authors` (list of names), `priority_author_boost` (float, e.g., 0.1).
  - `output.filename` and `output.filename_ranked`: HTML output; `archive: true` enables history DB writes.
  - `llm_summary`: topic-level controls for LLM summarization.
    - `enabled: true|false`
    - `prompt`: instruction given to the model. You can reference `{ranking_query}` and it will be replaced with the topic’s `ranking.query`.
    - `score_cutoff`: minimum `rank_score` to consider (0.0–1.0)
    - `top_n`: hard cap on the number of entries considered (after filtering by score)
    - Works together with global `config.llm` below.

- `config.llm` (global model settings)
  - `model`: preferred chat model id
  - `model_fallback`: secondary model if the primary is unsupported/unavailable
  - `api_key_env`: environment variable name to read if `openaikulcs.env` is missing
  - `rps`: default requests/second throttle for summarization
  - `max_retries`: retry attempts per item on transient errors
  - Optional GPT‑5 parameters: `verbosity`, `reasoning_effort` (used when the model starts with `gpt-5`)

## Acknowledgments

Thank you to arXiv for use of its open access interoperability.
