# Roadmap

Focus: add LLM summarization, keep pipeline minimal and robust.

## Now (Next Step)
- Abstract population via Crossref
  - Fetch and store abstracts into `entries.abstract` and `matched_entries_history.abstract`.
  - Respect rate limits and add retry/backoff.
- Ranking polish
  - Honor cutoffs (`score_cutoff`, `top_n`), negative queries, and configurable model per topic.
  - Display top-N ranked in HTML with score badges.
- Robustness
  - Improve network timeouts and error handling for feeds.
  - Optional caching for repeated runs.
 - CI caching for models
   - Use `actions/cache` to restore/persist `models/` and HF caches to avoid re-downloading models on each run.

## Near term
- LLM summarization
  - Add processor to generate concise expert summaries per entry.
  - Persist to `papers.db.entries.llm_summary`.
  - New CLI: `python cli/main.py summarize [--topic TOPIC]` (or integrate into the filter/rank flow with a flag).
  - Generate HTML files for summaries, similar to the old summarizer.
  - PaperQA Top-Paper Summaries (plan)
    - For arXiv cond-mat items, download versioned PDFs via the public endpoints (e.g., `https://arxiv.org/pdf/<id>vN.pdf`) with a descriptive User-Agent and ~1 request/3s throttle per arXiv guidance.
    - Select candidates from top-ranked entries per topic (above `rank_threshold`, within `top_n`).
    - Cache PDFs under `assets/pdfs/` and reuse on subsequent runs.
    - Run PaperQA on cached PDFs to produce concise technical summaries; store into `entries.llm_summary` and render in HTML.
    - Keep provenance (PDF path, arXiv id/version) to allow later refresh or replacement with publisher versions.

## Later
- Make a cumulative html page for RG, catalysis, 2D metals
- Web UI for browsing, configuration, and manual curation.
- Check functionality of the `papers_firehose/__init__.py`
- Set up `pages.yaml`, similarly to the one in the main branch. Ensure the caching of the models folder so that the sentence transformer is not downloaded each time.
