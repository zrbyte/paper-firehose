import numpy as np
import pytest

from paper_firehose.processors import st_ranker


class DummyEmbedder:
    """Toy stand-in for ``fastembed.TextEmbedding`` used by the tests."""

    def __init__(self, mapping):
        # ``mapping`` gives deterministic vectors for each requested document,
        # letting us assert on the cosine scores downstream.
        # This fake object mirrors the real FastEmbed implementation's behavior:
        # it yields embeddings as a streaming generator and does not batch,
        # which is important for maintaining test fidelity with production code.
        self.mapping = mapping
        self.requests = []

    def embed(self, documents, **_kwargs):
        if isinstance(documents, str):
            documents = [documents]
        for doc in documents:
            self.requests.append(doc)
            yield np.array(self.mapping.get(doc, [0.0, 0.0]), dtype=np.float32)


def test_ranker_scores_entries_with_fastembed(monkeypatch):
    called = {}

    mapping = {
        # Normalised vectors keep the cosine math straightforward to reason about
        # when checking the computed similarity scores.
        "alpha": [1.0, 0.0],
        "doc one": [0.8, 0.2],
        "doc two": [0.1, 0.9],
    }

    def fake_loader(model_name):
        called["name"] = model_name
        return DummyEmbedder(mapping)

    monkeypatch.setattr(st_ranker, "_load_text_embedding", fake_loader)

    ranker = st_ranker.STRanker(model_name="all-MiniLM-L6-v2")
    assert ranker.available()
    assert ranker.backend == "fastembed"
    assert called["name"] == "BAAI/bge-small-en-v1.5"

    entries = [("1", "topic", "doc one"), ("2", "topic", "doc two")]

    results = ranker.score_entries("alpha", entries)
    assert [r[0] for r in results] == ["1", "2"]
    assert pytest.approx(results[0][2], rel=1e-6) == 0.9701425
    assert pytest.approx(results[1][2], rel=1e-6) == 0.1104315


def test_ranker_handles_loader_failure(monkeypatch):
    def boom(_model_name):
        raise RuntimeError("no backend")

    monkeypatch.setattr(st_ranker, "_load_text_embedding", boom)

    ranker = st_ranker.STRanker(model_name="BAAI/bge-small-en-v1.5")
    assert not ranker.available()
    assert ranker.backend is None
    assert ranker.score_entries("alpha", [("1", "topic", "text")]) == []
