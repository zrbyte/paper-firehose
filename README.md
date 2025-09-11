# Paper Firehose

Fetches academic RSS feeds, filters entries with per-topic regex, and writes results into SQLite databases. HTML is rendered directly from the database. Ranking exists; LLM summarization is the next step.

## Overview

- Three-DB architecture for scale and clarity:
  - `assets/all_feed_entries.db`: Every fetched item (for deduplication).
  - `assets/matched_entries_history.db`: All matched items across topics and runs (historical archive).
  - `assets/papers.db`: Current-run working set (filtered → ranked → summarized).
- YAML-driven configuration for feeds and topics.
- HTML generated from `papers.db` so you can re-render without refetching.

## Databases

- `all_feed_entries.db` (table `feed_entries`)
  - Keys: `entry_id` (pk), `feed_name` (display name from `config.yaml`), `title`, `link`.
  - Metadata: `summary`, `authors`, `published_date`, `first_seen`, `last_seen`, `raw_data` (JSON).
  - Used only for dedup; populated after filtering completes.

- `matched_entries_history.db` (table `matched_entries`)
  - Keys: `entry_id` (pk), `feed_name`, `topics` (CSV of topic names).
  - Metadata: `title`, `link`, `summary`, `authors`, `abstract` (nullable), `doi` (nullable), `published_date`, `matched_date`, `raw_data` (JSON).
  - Written only when a topic’s `output.archive: true`.

- `papers.db` (table `entries`)
  - Primary key: composite `PRIMARY KEY(id, topic)` so the same entry can appear once per topic.
  - Columns: `id`, `topic`, `feed_name` (display name), `title`, `link`, `summary`, `authors`, `abstract` (nullable), `doi` (nullable), `published_date`, `discovered_date`, `status` (`filtered|ranked|summarized`), `rank_score`, `rank_reasoning`, `llm_summary`, `raw_data` (JSON).

Notes
- `feed_name` is the human-readable name from `config.yaml -> feeds.<key>.name` (e.g., "Nature Physics").
- `doi` is best-effort and can be found in `doi`, `dc:identifier`, `prism:doi`, `id`, `link`, `summary`, `summary_detail.value`, or embedded `content[].value`. arXiv feeds may not include DOIs; no external lookup is performed (by design for now).
- `abstract` is reserved for later population via Crossref.

## CLI

Run with Python 3.11+.

- Filter (fetch, dedup, match, write DBs, render HTML)
  - `python cli/main.py -v filter [--topic TOPIC]`
  - Backs up `all_feed_entries.db` and `matched_entries_history.db` (keeps 3 latest backups).
  - Clears `papers.db` working table before processing this run.
  - Fetches configured feeds for each topic, dedups by title against `all_feed_entries.db`, filters by topic regex.
  - Writes matches to `papers.db` (`status='filtered'`); optionally archives to `matched_entries_history.db` if `output.archive: true`.
  - Saves ALL processed entries (matched and non-matched) to `all_feed_entries.db` for future dedup.
  - Renders per-topic HTML from `papers.db`.

- Rank (optional)
  - `python cli/main.py rank [--topic TOPIC]`
  - Computes and writes `rank_score` for `papers.db` entries using sentence-transformers. HTML files with ranked entries are generated.
  - Model selection: if `models/all-MiniLM-L6-v2` exists, it is used; otherwise it falls back to the Hugging Face repo id `all-MiniLM-L6-v2` and downloads once into cache. You can vendor the model with `python scripts/vendor_model.py`.
  - Scoring details: applies a small penalty for `ranking.negative_queries` matches (title/summary). Optional boosts: per-topic `ranking.preferred_authors` with `ranking.priority_author_boost`, and global `priority_journal_boost` for feeds listed in `priority_journals`.

- Abstracts
  - `python cli/main.py abstracts [--topic TOPIC] [--mailto you@example.com] [--limit N] [--rps 1.0]`
  - Order: 1) Fill arXiv/cond-mat abstracts from `summary` (no threshold)  2) Above-threshold: Crossref (DOI, then title)  3) Above-threshold: Semantic Scholar → OpenAlex → PubMed
  - Threshold: topic `abstract_fetch.rank_threshold` else global `defaults.rank_threshold`.
  - Only topics with `abstract_fetch.enabled: true` are processed.
  - Writes to both `papers.db.entries.abstract` and `matched_entries_history.db.matched_entries.abstract`.
  - Rate limiting: descriptive User-Agent (includes `--mailto` or `$MAILTO`), respects Retry-After; default ~1 req/sec via `--rps`.
  - Populates the `entries.abstract` column; leaves other fields unchanged.
  - Contact email: if `--mailto` is not provided, the command reads `$MAILTO` from the environment; if unset, it uses a safe default.

- HTML (re-render only; no fetching)
  - `python cli/main.py html [--topic TOPIC]`
  - Reads from `papers.db` to generate filtered and ranked HTML pages.
  - If the entries in `papers.db` have been ranked it generates in addition to the list of matches the ranked list of entries.


- Purge
  - `python cli/main.py purge --days N` removes entries with `published_date` within the most recent N days across all DBs.
  - `python cli/main.py purge --all` deletes all DB files and reinitializes schemas (no confirmation prompt).

- Status
  - `python cli/main.py status`
  - Validates config, lists topics/feeds, and shows DB paths.

## Configuration

- `config/config.yaml` (feeds, DB paths, defaults)
  - Each feed has a key and a display `name`; the key is used in topic files, the name is stored in DBs.
- `config/topics/<topic>.yaml`
  - `feeds`: list of feed keys from `config.yaml`.
  - `filter.pattern` and `filter.fields`: regex and fields to match (defaults include `title` and `summary`).
  - `ranking`: optional `query`, `model`, cutoffs, etc. (for the rank command).
    - Optional: `negative_queries` (list), `preferred_authors` (list of names), `priority_author_boost` (float, e.g., 0.1).
  - `output.filename` and `output.filename_ranked`: HTML output; `archive: true` enables history DB writes.

## Data Flow

1) Fetch feeds per topic → 2) Dedup against `all_feed_entries.db` → 3) Regex match →
4) Write matches to `papers.db` (and optionally to history) → 5) Write all processed to `all_feed_entries.db` → 6) Generate HTML.

## Development Notes

- Testing only: no migrations. Schemas are ensured on startup; existing tables are preserved. The filter command clears only the working table in `papers.db` for each run.
- If you want a clean slate, remove DB files in `assets/` and re-run.

## Next

- Implement LLM summarization of filtered and ranked entries, writing into `entries.llm_summary` and updating HTML rendering.

## History Viewer

- `history_viewer.html` is a static browser viewer for `assets/matched_entries_history.db` (table `matched_entries`).
- By default it auto-loads the latest history DB from GitHub:
  - Displayed: `https://github.com/zrbyte/paper-firehose/tree/data/assets/matched_entries_history.latest.db`
  - The viewer automatically normalizes GitHub page links to their raw content (e.g., `raw.githubusercontent.com`) before fetching.
- You can override with a query param or local file:
  - `history_viewer.html?db=<url>` to load a specific remote DB
  - Use the file input or drag-and-drop a local `matched_entries_history.db`
- The viewer uses `sql.js` (WASM SQLite) from a CDN and includes a runtime fallback. If you encounter `initSqlJs is not defined`, verify network access; for fully offline usage, vendor `sql-wasm.js` and `sql-wasm.wasm` locally and update the script path.
