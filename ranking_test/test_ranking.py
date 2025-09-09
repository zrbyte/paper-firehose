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


def regex_to_canonical_phrases(pattern: str) -> List[str]:
    """Convert a topic regex pattern into a small set of canonical phrases.

    This applies lightweight heuristics tailored to common patterns in the
    repo's topic YAMLs (graphene/graphite/STM/TMDs/etc.). It is intentionally
    conservative (few phrases, no brute-force expansion) and aims to provide
    human-readable phrases suitable for sentence-transformer queries.

    Examples handled:
    - topolog[a-z]+ -> ["topology", "topological"]
    - graphit[a-z]+ -> ["graphite"], graphe[a-z]+ -> ["graphene"]
    - scan[a-z]+ tunne[a-z]+ micr[a-z]+ -> ["scanning tunneling microscopy", "STM"]
    - TMDs like (MoS\d+, WSe\d+) -> ["MoS2", "WSe2"]
    - Specific compounds: ["ZrTe5", "Pt2HgSe3", "BiTeI", ...]
    - Fallback: if pattern has no regex metacharacters, return it as-is
    """
    import re

    phrases: set[str] = set()
    s = pattern or ""
    s_l = s.lower()

    # If the pattern is a simple phrase without regex operators, use it directly.
    if not re.search(r"[\[\]{}()|+*?.\\]", s):
        simple = re.sub(r"\s+", " ", s.strip())
        if simple:
            phrases.add(simple)

    # Heuristic mappings for known stems and constructs
    if re.search(r"topolog", s_l):
        phrases.update(["topology", "topological"])
    if re.search(r"graphit", s_l):
        phrases.add("graphite")
    if re.search(r"graphe", s_l):
        phrases.add("graphene")
    if re.search(r"rhombohedr", s_l):
        # Commonly refers to ABC-stacked/rhombohedral graphite/graphene
        phrases.update(["rhombohedral graphite", "rhombohedral"])
    if re.search(r"\babc", s, flags=re.IGNORECASE):
        phrases.update(["ABC-stacked graphene", "ABC stacking"])  # context specific
    if re.search(r"chalcog", s_l):
        phrases.update(["chalcogenide", "chalcogenides"])  # broad material class
    if re.search(r"\blandau\b", s_l):
        phrases.add("Landau levels")
    if re.search(r"\bweyl\b", s_l):
        phrases.update(["Weyl semimetal", "Weyl"])
    if re.search(r"\bdirac\b", s_l):
        phrases.update(["Dirac material", "Dirac"]) 
    if re.search(r"\bSTM\b", s):
        phrases.update(["scanning tunneling microscopy", "STM"]) 

    # Scanning microscopy variants (robust to stem patterns in the regex)
    if re.search(r"scan[a-z]*\s*tunne[a-z]*\s*micr[a-z]*", s_l):
        phrases.update(["scanning tunneling microscopy", "STM"])
    if re.search(r"scan[a-z]*\s*tunne[a-z]*\s*spectr[a-z]*", s_l):
        phrases.update(["scanning tunneling spectroscopy", "STS"]) 
    if re.search(r"scan[a-z]*\s*prob[a-z]*\s*micr[a-z]*", s_l):
        phrases.update(["scanning probe microscopy", "SPM"]) 

    # Transition metal dichalcogenides (TMDs) — assume common 2H stoichiometry '2'
    if re.search(r"MoS", s):
        phrases.add("MoS2")
    if re.search(r"MoSe", s):
        phrases.add("MoSe2")
    if re.search(r"MoTe", s):
        phrases.add("MoTe2")
    if re.search(r"WS[^(e)]", s):  # plain WS (avoid double-matching WSe)
        phrases.add("WS2")
    if re.search(r"WSe", s):
        phrases.add("WSe2")
    if re.search(r"WTe", s):
        phrases.add("WTe2")

    # Specific compounds and families
    if re.search(r"BiTeI", s):
        phrases.add("BiTeI")
    if re.search(r"BiTeBr", s):
        phrases.add("BiTeBr")
    if re.search(r"BiTeCl", s):
        phrases.add("BiTeCl")
    if re.search(r"Bi\\d\+Rh\\d\+I\\d\+|Bi\W+\\d\W+Rh\W+\\d\W+I\W+\\d", s):
        phrases.add("BiRhI")  # generic family shorthand
    if re.search(r"ZrTe5|ZrTe\W*5", s):
        phrases.add("ZrTe5")
    if re.search(r"Pt2HgSe3|Pt\W*2HgSe\W*3", s):
        phrases.add("Pt2HgSe3")
    if re.search(r"jacuting", s_l):
        phrases.add("jacutingaite")

    # Flat band variants
    if re.search(r"flat\s*band|flat.?band", s_l):
        phrases.update(["flat band", "flat bands"]) 

    # Normalize: dedupe, keep readable casing, and sort for determinism
    out = sorted({p.strip() for p in phrases if p.strip()})
    return out


def canonical_phrases_from_topic_yaml(topic_yaml_path: str) -> List[str]:
    """Load a topic YAML file and return canonical phrases from its regex.

    Reads `filter.pattern` and converts it via `regex_to_canonical_phrases`.
    If PyYAML is unavailable, falls back to a naive pattern extraction.
    """
    pattern = None
    try:
        if yaml is not None:
            with open(topic_yaml_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if isinstance(cfg, dict):
                filt = cfg.get("filter", {}) or {}
                pattern = filt.get("pattern")
        else:
            # Fallback: naive regex to extract a quoted pattern line
            import re
            with open(topic_yaml_path, "r", encoding="utf-8") as f:
                text = f.read()
            m = re.search(r"pattern:\s*\"([^\"]*)\"", text)
            if not m:
                m = re.search(r"pattern:\s*'([^']*)'", text)
            if m:
                pattern = m.group(1)
    except Exception:
        pattern = None

    if not pattern:
        return []
    return regex_to_canonical_phrases(str(pattern))


if __name__ == "__main__":
    # Run with defaults for convenience
    run_ranking_test()
