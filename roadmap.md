# Roadmap

## Now (Next Step)
  - PaperQA Top-Paper Summaries
    - For arXiv cond-mat items, download versioned PDFs via the public endpoints (e.g., `https://arxiv.org/pdf/<id>vN.pdf`) with a descriptive User-Agent and ~1 request/3s throttle per arXiv guidance.
    - Select candidates from top-ranked entries per topic (above `rank_threshold`, within `top_n`).
    - Cache PDFs under `assets/pdfs/` and reuse on subsequent runs.
    - Run PaperQA on cached PDFs to produce concise technical summaries; store into `entries.llm_summary` and render in HTML.
    - Keep provenance (PDF path, arXiv id/version) to allow later refresh or replacement with publisher versions.

## Later
- Set up `pages.yaml`, similarly to the one in the main branch. Ensure the caching of the models folder so that the sentence transformer is not downloaded each time.
- Make a cumulative html page for RG, catalysis, 2D metals
- Web UI for browsing, configuration, and manual curation.

