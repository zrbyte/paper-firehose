"""Full-text PDF summarization with a single OpenAI call per paper.

The paper's text is extracted locally with ``pypdf`` and sent, together with the
topic's prompt, to the OpenAI Responses API. Structured outputs (a strict JSON
schema) guarantee a ``{"summary": ..., "methods": ...}`` object, which is the
shape the HTML and email renderers expect in ``paper_qa_summary``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-6.2-sol"

# ~150k tokens of English text (≈4 chars/token). Typical arXiv papers are
# 7k-40k tokens; the cap only guards against huge supplementary files.
MAX_TEXT_CHARS = 600_000

DEFAULT_PROMPT = (
    "Summarize this paper for an expert reader interested in {ranking_query}. "
    "Keep text information-dense. Avoid superlatives."
)

SUMMARY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Up to 8 information-dense sentences on the key findings and contributions.",
        },
        "methods": {
            "type": "string",
            "description": "Experimental setup, samples, parameters, analysis and calculation methods, tool names.",
        },
    },
    "required": ["summary", "methods"],
    "additionalProperties": False,
}

_BASE_INSTRUCTIONS = (
    "You summarize a single research paper from its extracted full text. "
    "Base every statement on the paper itself. "
    "Return plain text in each field (no Markdown)."
)


def extract_pdf_text(pdf_path: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    """Extract text from a PDF, truncated to ``max_chars``. Returns '' on failure."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - pypdf is a hard dependency
        logger.error("pypdf is not installed; cannot extract PDF text")
        return ""

    try:
        reader = PdfReader(pdf_path)
        parts = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        text = "\n".join(parts).strip()
    except Exception as exc:
        logger.error("Failed to extract text from %s: %s", pdf_path, exc)
        return ""

    if len(text) > max_chars:
        logger.info("Truncating extracted text of %s from %d to %d chars", pdf_path, len(text), max_chars)
        text = text[:max_chars]
    return text


def build_instructions(prompt: Optional[str], ranking_query: Optional[str]) -> str:
    """Combine the base instructions with the topic prompt (``{ranking_query}`` substituted)."""
    topic_prompt = (prompt or "").strip() or DEFAULT_PROMPT
    rq = (ranking_query or "").strip()
    topic_prompt = topic_prompt.replace("{ranking_query}", rq or "this research field")
    return f"{_BASE_INSTRUCTIONS}\n\n{topic_prompt}"


class PdfSummarizer:
    """Summarize paper full text with one OpenAI Responses API call."""

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        reasoning_effort: Optional[str] = None,
        client: Any = None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.reasoning_effort = reasoning_effort
        if client is None:
            from openai import OpenAI

            client = OpenAI()  # raises OpenAIError when OPENAI_API_KEY is missing
        self.client = client

    def summarize_text(self, text: str, instructions: str) -> Optional[Dict[str, str]]:
        """Return ``{"summary", "methods"}`` for the given paper text, or None on failure."""
        if not text.strip():
            return None

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": f"Full text of the paper:\n\n{text}",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "paper_summary",
                    "schema": SUMMARY_SCHEMA,
                    "strict": True,
                }
            },
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}

        try:
            response = self.client.responses.create(**kwargs)
        except Exception as exc:
            logger.error("OpenAI request failed (model=%s): %s", self.model, exc)
            return None

        raw = (getattr(response, "output_text", None) or "").strip()
        if not raw:
            logger.error("OpenAI returned an empty response (model=%s)", self.model)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("Could not parse model output as JSON: %s; first 200 chars: %r", exc, raw[:200])
            return None
        if not isinstance(data, dict):
            return None

        summary = str(data.get("summary") or "").strip()
        methods = str(data.get("methods") or "").strip()
        if not summary:
            return None
        return {"summary": summary, "methods": methods}

    def summarize_pdf(self, pdf_path: str, instructions: str) -> Optional[Dict[str, str]]:
        """Extract the PDF's text and summarize it."""
        text = extract_pdf_text(pdf_path)
        if not text:
            logger.warning("No text extracted from %s", pdf_path)
            return None
        return self.summarize_text(text, instructions)
