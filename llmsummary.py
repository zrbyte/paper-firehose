from __future__ import annotations

import os
import re
import json
import html
import datetime
import urllib.request
import time
import random
import logging
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError


MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCHTERMS_FILE = os.path.join(MAIN_DIR, 'search_terms.json')
LLM_PROMPTS_FILE = os.path.join(MAIN_DIR, 'llm_prompts.json')
SUMMARY_HTML_PATH = os.path.join(MAIN_DIR, 'summary.html')

# Configuration constants (no environment variables)
OPENAI_MODEL = "gpt-4o-mini"  # default model to use
OPENAI_MAX_RETRIES = 5  # total attempts including the first
OPENAI_BACKOFF_SECONDS = 1.0  # initial backoff before exponential growth
OPENAI_SLEEP_BETWEEN_TOPICS = 0.0  # pause between topic calls
SUMMARY_TOP_N_DEFAULT = 8  # max number of items per topic
OPENAI_MODEL_FALLBACK = "gpt-4o-mini"  # fallback if primary model fails
PROMPT_MAX_ITEMS_PER_TOPIC = 50  # cap entries included in the prompt to limit tokens


def load_api_key(path: str = "openaikulcs.env") -> str | None:
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key
    try:
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(__file__), path)
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if "=" in key:
                key = key.split("=", 1)[-1].strip()
            return key
    except Exception:
        return None


def read_search_terms() -> Dict[str, str]:
    try:
        with open(SEARCHTERMS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def read_llm_prompts() -> Dict[str, str]:
    try:
        with open(LLM_PROMPTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def chat_completion_raw(
    prompt: str,
    max_tokens: int = 2000,
    model: str | None = None,
    max_retries: int | None = None,
    base_backoff_seconds: float | None = None,
) -> str:
    api_key = load_api_key()
    if not api_key:
        logging.error("OPENAI_API_KEY not found. Cannot perform LLM ranking.")
        return ""
    model_to_use = model or OPENAI_MODEL
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = json.dumps({
        'model': model_to_use,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
        'temperature': 0.2,
    }).encode('utf-8')

    url = 'https://api.openai.com/v1/chat/completions'
    max_retries = max_retries if max_retries is not None else OPENAI_MAX_RETRIES
    base_backoff = base_backoff_seconds if base_backoff_seconds is not None else OPENAI_BACKOFF_SECONDS
    backoff = base_backoff
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers=headers, data=payload)
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.load(resp)
            return result['choices'][0]['message']['content']
        except HTTPError as e:
            status = getattr(e, 'code', None)
            # Retry on rate limit and transient server errors
            if status in (429, 500, 502, 503, 504):
                retry_after = None
                try:
                    retry_after = e.headers.get('Retry-After')  # type: ignore[attr-defined]
                except Exception:
                    retry_after = None
                if retry_after:
                    try:
                        delay = max(float(retry_after), backoff)
                    except ValueError:
                        delay = backoff
                else:
                    # Exponential backoff with jitter
                    jitter = random.uniform(0, 0.25 * backoff)
                    delay = backoff + jitter
                logging.warning(
                    "OpenAI HTTP %s on attempt %d/%d. Sleeping %.2fs before retry.",
                    status, attempt, max_retries, delay,
                )
                time.sleep(delay)
                backoff *= 2
                continue
            logging.error("API request failed (non-retryable %s): %s", status, e)
            return ""
        except URLError as e:
            # Network issues; retry
            jitter = random.uniform(0, 0.25 * backoff)
            delay = backoff + jitter
            logging.warning(
                "Network error on attempt %d/%d. Sleeping %.2fs before retry: %s",
                attempt, max_retries, delay, e,
            )
            time.sleep(delay)
            backoff *= 2
            continue
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logging.error("Malformed API response: %s", e)
            return ""
    logging.error("Exhausted retries calling OpenAI API.")
    return ""


def chat_completion_with_fallback(prompt: str, max_tokens: int = 2000) -> str:
    """Try primary model first; if it fails/empty, try fallback model."""
    primary_model = OPENAI_MODEL
    fallback_model = OPENAI_MODEL_FALLBACK
    out = chat_completion_raw(
        prompt,
        max_tokens=max_tokens,
        model=primary_model,
        max_retries=OPENAI_MAX_RETRIES,
        base_backoff_seconds=OPENAI_BACKOFF_SECONDS,
    )
    if out:
        return out
    if fallback_model and fallback_model != primary_model:
        logging.warning("Primary model '%s' failed or returned empty. Falling back to '%s'.", primary_model, fallback_model)
        return chat_completion_raw(
            prompt,
            max_tokens=max_tokens,
            model=fallback_model,
            max_retries=OPENAI_MAX_RETRIES,
            base_backoff_seconds=OPENAI_BACKOFF_SECONDS,
        )
    return out


def _sanitize_json_text(raw_text: str) -> str:
    """Strip common non-JSON artifacts the model may emit.

    - Remove ```json ... ``` or ``` ... ``` fences
    - Remove // line comments and /* ... */ block comments
    - Remove trailing commas before } or ]
    """
    if not raw_text:
        return ""
    s = raw_text
    try:
        s = re.sub(r"```(?:json)?\s*([\s\S]*?)\s*```", r"\1", s, flags=re.IGNORECASE)
        s = re.sub(r"^\s*//.*?$", "", s, flags=re.MULTILINE)
        s = re.sub(r"/\*[\s\S]*?\*/", "", s)
        s = re.sub(r",\s*([}\]])", r"\1", s)
    except Exception:
        pass
    return s.strip()


def extract_json_block(text: str) -> dict | None:
    if not text:
        return None
    # Try direct parse first after sanitizing
    sanitized = _sanitize_json_text(text)
    try:
        return json.loads(sanitized)
    except Exception:
        pass
    # Fallback: extract the largest JSON object
    first = sanitized.find('{')
    last = sanitized.rfind('}')
    if first != -1 and last != -1 and last > first:
        candidate = _sanitize_json_text(sanitized[first:last+1])
        try:
            return json.loads(candidate)
        except Exception:
            return None
    return None


def flatten_entries(entries_per_feed: Dict[str, List[dict]]) -> List[dict]:
    flat: List[dict] = []
    for _feed, entries in (entries_per_feed or {}).items():
        flat.extend(entries)
    return flat


def to_compact_items(entries: List[dict]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    for e in entries:
        compact.append({
            'title': str(e.get('title', '')).strip(),
            'summary': str(e.get('summary', e.get('description', ''))).strip(),
            'link': e.get('link', ''),
            'authors': ', '.join([a.get('name', '') for a in e.get('authors', [])]) if e.get('authors') else str(e.get('author', '')),
            'published': str(e.get('published', e.get('updated', ''))),
            'feed_title': str(e.get('feed_title', '')),
        })
    return compact


def build_ranking_prompt(
    topic: str,
    items: List[Dict[str, Any]],
    search_terms: Dict[str, str],
    ranking_prompt: str,
    topic_prompt: str | None,
    top_n: int,
) -> str:
    # Limit the number of entries included in the prompt to control token usage
    limited_items = items[:PROMPT_MAX_ITEMS_PER_TOPIC] if items else []
    numbered = []
    for idx, it in enumerate(limited_items, start=1):
        title = it.get('title', '')
        link = it.get('link', '')
        summary = it.get('summary', '')
        authors = it.get('authors', '')
        feed_title = it.get('feed_title', '')
        numbered.append(f"{idx}) title: {title}\n   link: {link}\n   authors: {authors}\n   feed: {feed_title}\n   summary: {summary}")
    base_instruction = ranking_prompt or (
        "You are a domain expert. From the list of entries for the given topic, select the most important papers for today and write a concise multi-sentence summary for each."
    )
    terms_text = json.dumps(search_terms, indent=2)
    payload = (
        f"Topic: {topic}\n"
        + (f"Topic-specific guidance:\n{topic_prompt}\n\n" if topic_prompt else "")
        + f"Search term regex for this topic (for context only):\n{terms_text}\n\n"
        f"Entries (each has title, link, authors, feed, summary):\n" + "\n\n".join(numbered) + "\n\n"
        "Task: Rank the entries by importance for an expert reader. Consider topical relevance, novelty, likely impact, experimental/theory significance, and match to the topic. "
        f"Return ONLY a valid RFC 8259 JSON object with at most {top_n} items. No markdown, no code fences, no comments, no trailing commas.\n"
        "JSON shape (values shown for type only):\n"
        "{\n"
        "  \"topic\": \"string\",\n"
        "  \"overview\": \"string\",\n"
        "  \"items\": [\n"
        "    {\n"
        "      \"rank\": 1,\n"
        "      \"importance_score\": 5,\n"
        "      \"title\": \"string\",\n"
        "      \"link\": \"string\",\n"
        "      \"summary\": \"string\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "Rules:\n- Use only the provided entries.\n- link must be copied from the matching entry.\n- Keep overview to 2-3 sentences.\n- Each item's summary must be 4-5 sentences.\n- items must be ordered by descending importance_score, ties broken by better match to topic.\n"
    )
    return f"{base_instruction}\n\n{payload}"


def rank_entries_with_llm(
    topic: str,
    items: List[Dict[str, Any]],
    search_term_for_topic: str | None,
    ranking_prompt: str,
    topic_prompt: str | None,
    top_n: int,
) -> Dict[str, Any]:
    if not items:
        return {"topic": topic, "overview": "", "items": []}
    # Build a map from link -> original metadata (authors, feed_title)
    link_to_meta: Dict[str, Dict[str, str]] = {}
    for it in items:
        link = str(it.get('link', '')).strip()
        if not link:
            continue
        link_to_meta[link] = {
            'authors': str(it.get('authors', '')).strip(),
            'feed_title': str(it.get('feed_title', '')).strip(),
        }
    prompt = build_ranking_prompt(
        topic=topic,
        items=items,
        search_terms={topic: search_term_for_topic or ""},
        ranking_prompt=ranking_prompt,
        topic_prompt=topic_prompt,
        top_n=top_n,
    )
    raw = chat_completion_with_fallback(prompt, max_tokens=2000)
    data = extract_json_block(raw)
    if not data or not isinstance(data, dict) or 'items' not in data:
        logging.error("LLM ranking failed or returned invalid data for topic '%s'. No items will be listed.", topic)
        return {"topic": topic, "overview": "", "items": []}
    # Sanitize items and enrich with authors/journal
    sanitized: List[Dict[str, Any]] = []
    seen_links = set()
    for i, it in enumerate(data.get('items', [])[:top_n]):
        title = str(it.get('title', '')).strip()
        link = str(it.get('link', '')).strip()
        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)
        meta = link_to_meta.get(link, {})
        sanitized.append({
            'rank': int(it.get('rank', i + 1)),
            'importance_score': int(it.get('importance_score', 3)),
            'title': title,
            'link': link,
            'summary': str(it.get('summary', '') or it.get('one_sentence_summary', '')).strip(),
            'authors': meta.get('authors', ''),
            'feed_title': meta.get('feed_title', ''),
        })
    overview = str(data.get('overview', '')).strip()
    return {'topic': topic, 'overview': overview, 'items': sanitized}


def render_html(sections: List[Dict[str, Any]], generated_for: datetime.date, output_path: str) -> None:
    style = (
        "body{font-family:Arial,sans-serif;margin:24px;}"
        "h1{margin-bottom:4px;} h2{color:#2E8B57;margin-top:28px;}"
        ".topic{margin-bottom:28px;}"
        ".item{margin:10px 0;padding:10px;border:1px solid #e5e5e5;border-radius:8px;}"
        ".badge{display:inline-block;background:#1f6feb;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;margin-left:8px;}"
        ".meta{color:#666;font-size:13px;margin-top:4px;}"
    )
    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append(f"<title>Summary {generated_for}</title>")
    parts.append(f"<style>{style}</style></head><body>")
    parts.append(f"<h1>Most important papers</h1>")
    parts.append(f"<div class='meta'>Processed on {html.escape(str(generated_for))}</div>")

    for sec in sections:
        topic = sec['topic']
        overview = sec.get('overview') or ''
        items: List[Dict[str, Any]] = sec.get('items', [])
        parts.append(f"<div class='topic'>")
        parts.append(f"<h2>{html.escape(topic)}</h2>")
        if overview:
            parts.append(f"<p>{html.escape(overview)}</p>")
        if not items:
            parts.append("<p class='meta'>No new papers in this run.</p>")
        else:
            for it in items:
                title = html.escape(it.get('title', ''))
                link = html.escape(it.get('link', ''), quote=True)
                summary = html.escape(it.get('summary', ''))
                score = int(it.get('importance_score', 0))
                authors = html.escape(it.get('authors', '') or '')
                journal = html.escape(it.get('feed_title', '') or '')
                parts.append("<div class='item'>")
                parts.append(f"<div><a href='{link}'><strong>{title}</strong></a><span class='badge'>Score {score}</span></div>")
                if authors or journal:
                    details = []
                    if authors:
                        details.append(f"Authors: {authors}")
                    if journal:
                        details.append(f"Journal: <strong>{journal}</strong>")
                    parts.append(f"<div class='meta'>{' — '.join(details)}</div>")
                if summary:
                    parts.append(f"<div class='meta'>{summary}</div>")
                parts.append("</div>")
        parts.append("</div>")

    parts.append("</body></html>")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(parts))


def main(entries_per_topic: Dict[str, Dict[str, List[dict]]] | None = None, top_n: int | None = None) -> None:
    # This script is intended to be called with the entries from the current run.
    # If entries are not provided, create an empty summary page noting that no new
    # papers were processed in this invocation.
    top_n_effective = top_n or SUMMARY_TOP_N_DEFAULT
    terms = read_search_terms()
    prompts = read_llm_prompts()

    generated_for = datetime.date.today()

    if not entries_per_topic:
        logging.warning("No entries_per_topic passed to llmsummary.main; generating placeholder summary.")
        sections: List[Dict[str, Any]] = []
        for topic in (terms.keys() or []):
            sections.append({'topic': topic, 'overview': '', 'items': []})
        render_html(sections, generated_for, SUMMARY_HTML_PATH)
        return

    sections: List[Dict[str, Any]] = []
    sleep_between_topics = OPENAI_SLEEP_BETWEEN_TOPICS
    ranking_prompt = prompts.get('ranking_prompt', '').strip()
    for topic, per_feed in entries_per_topic.items():
        flat = flatten_entries(per_feed)
        compact = to_compact_items(flat)
        topic_prompt = prompts.get(topic)
        ranked = rank_entries_with_llm(
            topic=topic,
            items=compact,
            search_term_for_topic=terms.get(topic),
            ranking_prompt=ranking_prompt,
            topic_prompt=topic_prompt,
            top_n=top_n_effective,
        )
        sections.append(ranked)
        if sleep_between_topics > 0:
            time.sleep(sleep_between_topics)

    render_html(sections, generated_for, SUMMARY_HTML_PATH)


if __name__ == '__main__':
    main()
