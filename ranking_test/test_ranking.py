"""
Ad-hoc ranking test script.

Samples 20 random titles from the historical matches database and ranks them
using BM25 (rank_bm25) and, if available, sentence-transformers cosine
similarity with a simple query built from keywords.

Usage (from repo root):
  python "ranking_test/test_ranking.py"  # runs with defaults

From Jupyter:
  import importlib.util
  spec = importlib.util.spec_from_file_location("test_ranking", "ranking_test/test_ranking.py")
  tr = importlib.util.module_from_spec(spec); spec.loader.exec_module(tr)
  results = tr.run_ranking_test(query="graphene topology", sample_size=20, print_results=False)

Notes:
- Requires PyYAML (already in requirements) to read config for DB path.
- BM25: install `rank-bm25` (pip install rank-bm25).
- Sentence-Transformers (optional): install `sentence-transformers` and ensure
  the model can be loaded (may require network on first run).
"""

import os
import sqlite3
from typing import Dict, List, Tuple

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


def load_history_db_path(config_path: str = "config/config.yaml") -> str:
    """Read history DB path from YAML if PyYAML is available; fallback to default."""
    try:
        if yaml is None:
            raise RuntimeError("yaml not available")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        db_cfg = cfg.get("database", {}) if isinstance(cfg, dict) else {}
        return db_cfg.get("history_path", "assets/matched_entries_history.db")
    except Exception:
        return "assets/matched_entries_history.db"


def sample_titles(db_path: str, n: int = 20) -> List[Tuple[str, str]]:
    """Return a list of (entry_id, title) sampled randomly from history DB."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"History DB not found at {db_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Title-only sampling; avoid empty/null titles
    cur.execute(
        """
        SELECT entry_id, title
        FROM matched_entries
        WHERE title IS NOT NULL AND TRIM(title) != ''
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (n,),
    )
    rows = cur.fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def bm25_rank(query: str, docs: Dict[str, str]) -> List[Tuple[str, float]]:
    """Rank docs with BM25Okapi; returns list of (doc_id, score) sorted desc.

    If rank_bm25 is not installed, returns an empty list.
    """
    try:
        from rank_bm25 import BM25Okapi  # type: ignore
    except Exception:
        print("[bm25] rank-bm25 not installed. Skipping BM25 test.")
        return []

    import re

    def tokenize(text: str) -> List[str]:
        return re.findall(r"\w+", (text or "").lower())

    tokenized_corpus = [tokenize(t) for t in docs.values()]
    bm25 = BM25Okapi(tokenized_corpus)
    q_tokens = tokenize(query)
    scores = bm25.get_scores(q_tokens)
    doc_ids = list(docs.keys())
    ranked = sorted(zip(doc_ids, scores), key=lambda x: x[1], reverse=True)
    return ranked


def st_rank(query: str, docs: Dict[str, str], model_name: str = "all-MiniLM-L6-v2") -> List[Tuple[str, float]]:
    """Rank docs with sentence-transformers cosine similarity.

    If sentence-transformers is not installed or model cannot be loaded,
    returns an empty list.
    """
    try:
        from sentence_transformers import SentenceTransformer, util  # type: ignore
    except Exception:
        print("[st] sentence-transformers not installed. Skipping ST test.")
        return []

    try:
        model = SentenceTransformer(model_name)
    except Exception as e:
        print(f"[st] Could not load model '{model_name}': {e}. Skipping ST test.")
        return []

    # Simple query text from keywords
    query_text = query.strip()
    doc_texts = list(docs.values())
    doc_ids = list(docs.keys())

    q_emb = model.encode([query_text], normalize_embeddings=True)
    d_emb = model.encode(doc_texts, normalize_embeddings=True)
    sims = util.cos_sim(q_emb, d_emb).tolist()[0]

    ranked = sorted(zip(doc_ids, sims), key=lambda x: x[1], reverse=True)
    return ranked


def run_ranking_test(
    query: str = "graphene topology",
    sample_size: int = 20,
    config_path: str = "config/config.yaml",
    st_model: str = "all-MiniLM-L6-v2",
    print_results: bool = True,
):
    """Run the ranking test with function arguments (Jupyter-friendly).

    Returns dict with keys: sampled_docs, bm25, st.
    """
    history_db = load_history_db_path(config_path)
    rows = sample_titles(history_db, sample_size)

    sampled_docs: Dict[str, str] = {rid: title for rid, title in rows}

    if print_results:
        print("\nSampled titles (doc_id -> title):")
        for k, v in sampled_docs.items():
            print(f"- {k[:8]}...: {v}")

    bm25_results = bm25_rank(query, sampled_docs)
    if print_results and bm25_results:
        print("\nBM25 ranking (top 10):")
        for doc_id, score in bm25_results[:10]:
            print(f"  {doc_id[:8]}...  score={score:.3f}  title={sampled_docs[doc_id]}")

    st_results = st_rank(query, sampled_docs, model_name=st_model)
    if print_results and st_results:
        print("\nSentence-Transformers ranking (top 10):")
        for doc_id, score in st_results[:10]:
            print(f"  {doc_id[:8]}...  sim={score:.3f}  title={sampled_docs[doc_id]}")

    return {"sampled_docs": sampled_docs, "bm25": bm25_results, "st": st_results}


if __name__ == "__main__":
    # Run with defaults for convenience
    run_ranking_test()
