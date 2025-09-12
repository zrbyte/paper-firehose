# Roadmap
  - PaperQA Top-Paper Summaries
    - For arXiv cond-mat items, download versioned PDFs via the public endpoints (e.g., `https://arxiv.org/pdf/<id>vN.pdf`) with a descriptive User-Agent and ~1 request/3s throttle per arXiv guidance.
    - Select candidates from top-ranked entries per topic (above `rank_threshold`, within `top_n`).
    - Cache PDFs under `assets/pdfs/` and reuse on subsequent runs.
    - Run PaperQA on cached PDFs to produce concise technical summaries; store into `entries.llm_summary` and render in the LLM summary html in a dropdown menu.
  - Set up email mailing list
