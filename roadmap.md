# Roadmap (Concise)

Focus: add LLM summarization, keep pipeline minimal and robust.

## Now (Next Step)
- LLM summarization
  - Add processor to generate concise expert summaries per entry.
  - Persist to `papers.db.entries.llm_summary`.
  - New CLI: `python cli/main.py summarize [--topic TOPIC]` (or integrate into the filter/rank flow with a flag).
  - Update HTML rendering to include summaries for entries when present.

## Near Term
- Abstract population via Crossref
  - Fetch and store abstracts into `entries.abstract` and `matched_entries_history.abstract`.
  - Respect rate limits and add retry/backoff.
- Ranking polish
  - Honor cutoffs (`score_cutoff`, `top_n`), negative queries, and configurable model per topic.
  - Display top-N ranked in HTML with score badges.
- Robustness
  - Improve network timeouts and error handling for feeds.
  - Optional caching for repeated runs.

## Later
- DOI lookups for arXiv items (arXiv API first, Crossref fallback; opt-in).
- Web UI for browsing, configuration, and manual curation.
- CI: nightly runs + artifact retention for DBs and HTML.
