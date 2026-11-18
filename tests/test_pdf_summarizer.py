"""Tests for processors.pdf_summarizer (OpenAI client is faked)."""

import json
from types import SimpleNamespace

import pytest

from paper_firehose.processors import pdf_summarizer
from paper_firehose.processors.pdf_summarizer import (
    PdfSummarizer,
    build_instructions,
    extract_pdf_text,
)


class FakeResponses:
    def __init__(self, output_text=None, exc=None):
        self.output_text = output_text
        self.exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(output_text=self.output_text)


def make_summarizer(output_text=None, exc=None, **kwargs):
    responses = FakeResponses(output_text, exc)
    client = SimpleNamespace(responses=responses)
    return PdfSummarizer(client=client, **kwargs), responses


def test_summarize_text_returns_summary_and_methods():
    payload = json.dumps({"summary": " Found X. ", "methods": "DFT"})
    summarizer, responses = make_summarizer(payload, model="gpt-6-luna")

    result = summarizer.summarize_text("paper text", "instructions")

    assert result == {"summary": "Found X.", "methods": "DFT"}
    call = responses.calls[0]
    assert call["model"] == "gpt-6-luna"
    assert call["instructions"] == "instructions"
    assert "paper text" in call["input"]
    fmt = call["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert set(fmt["schema"]["required"]) == {"summary", "methods"}
    assert "reasoning" not in call
    assert "temperature" not in call


def test_default_model_and_reasoning_effort():
    payload = json.dumps({"summary": "S", "methods": ""})
    summarizer, responses = make_summarizer(payload, reasoning_effort="low")

    summarizer.summarize_text("text", "inst")

    assert summarizer.model == pdf_summarizer.DEFAULT_MODEL
    assert responses.calls[0]["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("output_text", ["", "not json", "[1, 2]", json.dumps({"summary": "", "methods": "x"})])
def test_unusable_output_returns_none(output_text):
    summarizer, _ = make_summarizer(output_text)
    assert summarizer.summarize_text("text", "inst") is None


def test_api_error_returns_none():
    summarizer, _ = make_summarizer(exc=RuntimeError("boom"))
    assert summarizer.summarize_text("text", "inst") is None


def test_empty_text_skips_api_call():
    summarizer, responses = make_summarizer(json.dumps({"summary": "S", "methods": ""}))
    assert summarizer.summarize_text("   ", "inst") is None
    assert responses.calls == []


def test_build_instructions_substitutes_ranking_query():
    out = build_instructions("Readers like {ranking_query}.", "graphene transport")
    assert "Readers like graphene transport." in out


def test_build_instructions_uses_default_prompt():
    out = build_instructions(None, "perovskites")
    assert "perovskites" in out
    assert "{ranking_query}" not in out


def _write_pdf(path, pages_text):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages_text:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    with open(path, "wb") as fh:
        writer.write(fh)


def test_extract_pdf_text_and_truncation(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _write_pdf(pdf, ["Graphene band structure", "Second page text"])

    text = extract_pdf_text(str(pdf))
    assert "Graphene band structure" in text
    assert "Second page text" in text

    assert len(extract_pdf_text(str(pdf), max_chars=10)) == 10


def test_extract_pdf_text_bad_file_returns_empty(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    assert extract_pdf_text(str(bad)) == ""
