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
PRIORITY_JOURNALS_FILE = os.path.join(MAIN_DIR, 'priority_journals.json')
SUMMARY_HTML_PATH = os.path.join(MAIN_DIR, 'summary.html')

# Configuration constants (no environment variables)
OPENAI_MODEL = "gpt-5"  # default model to use
OPENAI_MAX_RETRIES = 5  # total attempts including the first
OPENAI_BACKOFF_SECONDS = 1.0  # initial backoff before exponential growth
OPENAI_SLEEP_BETWEEN_TOPICS = 0.0  # pause between topic calls
SUMMARY_TOP_N_DEFAULT = 10  # max number of items returned per topic
PROMPT_MAX_ITEMS_PER_TOPIC = 30  # cap entries included in the prompt to limit tokens
OPENAI_MODEL_FALLBACK = "gpt-4o-mini"  # fallback if primary model fails


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


def read_priority_journals() -> List[str]:
    try:
        with open(PRIORITY_JOURNALS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data.get('priority_journals', [])
    except Exception:
        return []


def _extract_text_from_openai_response(result: Dict[str, Any]) -> str:
    """Extract assistant text from either Chat Completions or Responses API payloads.

    Handles:
    - Chat Completions: choices[0].message.content as str or list of parts
    - Responses API: output_text shortcut, or output[*].content[*].text{value}
    - Fallbacks: choices[0].text, top-level content/message.content
    """
    if not result:
        return ""
    # Responses API convenience field
    try:
        output_text = result.get('output_text')
        if isinstance(output_text, str) and output_text.strip():
            return output_text
    except Exception:
        pass

    def _texts_from_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        texts: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            # Common shapes: {'type': 'text', 'text': '...'} or {'type': 'output_text', 'text': {'value': '...'}}
            text_val = part.get('text')
            if isinstance(text_val, str):
                if text_val:
                    texts.append(text_val)
                    continue
            if isinstance(text_val, dict):
                val = text_val.get('value')
                if isinstance(val, str) and val:
                    texts.append(val)
                    continue
            # Some providers use 'value' directly
            direct_val = part.get('value')
            if isinstance(direct_val, str) and direct_val:
                texts.append(direct_val)
        return "\n".join([t for t in texts if t])
    # Chat Completions style
    try:
        choices = result.get('choices')
        if isinstance(choices, list) and choices:
            message = choices[0].get('message') or {}
            content = message.get('content')
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list):
                joined = _texts_from_parts(content)
                if joined:
                    return joined
            # some models return 'text' at the choice level
            text = choices[0].get('text')
            if isinstance(text, str) and text:
                return text
    except Exception:
        pass
    # Responses API style (2024+)
    try:
        output = result.get('output')
        if isinstance(output, list) and output:
            # Look for message items with content parts
            for item in output:
                if not isinstance(item, dict):
                    continue
                content_parts = item.get('content')
                joined = _texts_from_parts(content_parts)
                if joined:
                    return joined
    except Exception:
        pass
    # Some implementations may use a top-level 'content' or 'message'
    try:
        content = result.get('content')
        if isinstance(content, str) and content:
            return content
        message = result.get('message')
        if isinstance(message, dict):
            content2 = message.get('content')
            if isinstance(content2, str) and content2:
                return content2
    except Exception:
        pass
    return ""


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
    prefer_responses = str(model_to_use).lower().startswith('gpt-5')
    chat_body: Dict[str, Any] = {
        'model': model_to_use,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.2,
        'response_format': {'type': 'json_object'},  # Enable JSON formatting
    }
    chat_payload = json.dumps(chat_body).encode('utf-8')

    url_chat = 'https://api.openai.com/v1/chat/completions'
    url_resp = 'https://api.openai.com/v1/responses'
    last_debug_blob: str = ''
    max_retries = max_retries if max_retries is not None else OPENAI_MAX_RETRIES
    base_backoff = base_backoff_seconds if base_backoff_seconds is not None else OPENAI_BACKOFF_SECONDS
    backoff = base_backoff
    for attempt in range(1, max_retries + 1):
        # Optionally try Responses API first for next-gen models (e.g., gpt-5)
        if prefer_responses:
            try:
                headers_resp = dict(headers)
                headers_resp['OpenAI-Beta'] = 'assistants=v2'
                resp_payload = json.dumps({
                    'model': model_to_use,
                    'input': [
                        {
                            'role': 'user',
                            'content': [
                                {'type': 'input_text', 'text': prompt}
                            ],
                        }
                    ],
                    'max_output_tokens': max(256, int(max_tokens)),
                    'text': {'format': {'type': 'json_object'}},
                }).encode('utf-8')
                req_first = urllib.request.Request(url_resp, headers=headers_resp, data=resp_payload)
                with urllib.request.urlopen(req_first) as resp_first:
                    result_first = json.load(resp_first)
                try:
                    last_debug_blob = json.dumps(result_first)[:4000]
                except Exception:
                    last_debug_blob = str(result_first)[:4000]
                text_first = _extract_text_from_openai_response(result_first)
                if text_first:
                    return text_first
            except HTTPError as he:
                # Capture 4xx/5xx body for debugging
                try:
                    err_body = he.read().decode('utf-8', errors='ignore') if hasattr(he, 'read') else ''
                except Exception:
                    err_body = ''
                if err_body:
                    try:
                        assets_dir = os.path.join(MAIN_DIR, 'assets')
                        os.makedirs(assets_dir, exist_ok=True)
                        debug_path = os.path.join(assets_dir, 'llm_debug_latest.json')
                        dbg_obj = {
                            'endpoint': 'responses',
                            'status': getattr(he, 'code', None),
                            'payload': json.loads(resp_payload.decode('utf-8')) if resp_payload else {},
                            'error': err_body[:3500],
                        }
                        with open(debug_path, 'w', encoding='utf-8') as dbg:
                            json.dump(dbg_obj, dbg)
                        logging.error("Responses-first HTTPError; wrote body to %s", debug_path)
                    except Exception:
                        logging.error("Responses-first HTTPError; could not write debug body")
                # fall back to chat
            except Exception:
                # fall back to chat
                pass

        # Try Chat Completions only if model is not a next-gen Responses-only model
        if not prefer_responses:
            try:
                req = urllib.request.Request(url_chat, headers=headers, data=chat_payload)
                with urllib.request.urlopen(req) as resp:
                    result = json.load(resp)
                try:
                    last_debug_blob = json.dumps(result)[:4000]
                except Exception:
                    last_debug_blob = str(result)[:4000]
                text = _extract_text_from_openai_response(result)
                if text:
                    return text
                # fallthrough to try responses API if empty
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
                # Try Responses API as fallback for 400/404 errors
                try:
                    err_body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
                except Exception:
                    err_body = ''
                
                if status in (400, 404):
                    try:
                        headers_resp = dict(headers)
                        headers_resp['OpenAI-Beta'] = 'assistants=v2'
                        resp_payload = json.dumps({
                            'model': model_to_use,
                            'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': prompt}]}],
                            'max_output_tokens': max(256, int(max_tokens)),
                            'text': {'format': {'type': 'json_object'}},
                        }).encode('utf-8')
                        req2 = urllib.request.Request(url_resp, headers=headers_resp, data=resp_payload)
                        with urllib.request.urlopen(req2) as resp2:
                            result2 = json.load(resp2)
                        text2 = _extract_text_from_openai_response(result2)
                        if text2:
                            return text2
                    except Exception as e2:
                        logging.error("Responses API fallback failed: %s", e2)
                # Persist error body for debugging before returning
                if err_body:
                    try:
                        assets_dir = os.path.join(MAIN_DIR, 'assets')
                        os.makedirs(assets_dir, exist_ok=True)
                        debug_path = os.path.join(assets_dir, 'llm_debug_latest.json')
                        dbg_obj = {
                            'endpoint': 'chat.completions',
                            'status': status,
                            'payload': json.loads(chat_payload.decode('utf-8')) if chat_payload else {},
                            'error': err_body[:3500],
                        }
                        with open(debug_path, 'w', encoding='utf-8') as dbg:
                            json.dump(dbg_obj, dbg)
                        logging.error("API request failed (non-retryable %s); wrote body to %s", status, debug_path)
                    except Exception:
                        logging.error("API request failed (non-retryable %s); could not write debug body", status)
                logging.error("API request failed (non-retryable %s): %s", status, e)
                return ""
    if last_debug_blob:
        try:
            assets_dir = os.path.join(MAIN_DIR, 'assets')
            os.makedirs(assets_dir, exist_ok=True)
            debug_path = os.path.join(assets_dir, 'llm_debug_latest.json')
            with open(debug_path, 'w', encoding='utf-8') as dbg:
                dbg.write(last_debug_blob)
            logging.error("Exhausted retries; wrote last LLM response to %s", debug_path)
        except Exception:
            logging.error("Exhausted retries; could not write LLM debug blob")
    else:
        logging.error("Exhausted retries calling OpenAI API.")
    return ""


def chat_completion_with_fallback(prompt: str, max_tokens: int = 2000) -> str:
    """Try primary model first; if it fails/empty, try fallback model."""
    primary_model = OPENAI_MODEL
    fallback_model = OPENAI_MODEL_FALLBACK
    
    # Try primary model
    result = chat_completion_raw(
        prompt,
        max_tokens=max_tokens,
        model=primary_model,
        max_retries=OPENAI_MAX_RETRIES,
        base_backoff_seconds=OPENAI_BACKOFF_SECONDS,
    )
    if result:
        return result
    
    # Try fallback model if different from primary
    if fallback_model and fallback_model != primary_model:
        logging.warning("Primary model '%s' failed or returned empty. Falling back to '%s'.", primary_model, fallback_model)
        return chat_completion_raw(
            prompt,
            max_tokens=max_tokens,
            model=fallback_model,
            max_retries=OPENAI_MAX_RETRIES,
            base_backoff_seconds=OPENAI_BACKOFF_SECONDS,
        )
    
    return ""


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
            'is_priority': e.get('is_priority', False),  # Preserve priority flag
        })
    return compact


def merge_priority_entries(regular_entries: List[dict], priority_entries: List[dict], priority_journals: List[str]) -> List[Dict[str, Any]]:
    """Merge priority journal entries with regular entries, ensuring priority entries are always included."""
    merged = []
    seen_links = set()
    
    # First, add all priority entries
    for entry in priority_entries:
        link = entry.get('link', '')
        if link and link not in seen_links:
            # Mark priority entries with a special flag
            entry_copy = entry.copy()
            entry_copy['is_priority'] = True
            merged.append(entry_copy)
            seen_links.add(link)
    
    # Then add regular entries that aren't duplicates
    for entry in regular_entries:
        link = entry.get('link', '')
        if link and link not in seen_links:
            entry_copy = entry.copy()
            entry_copy['is_priority'] = False
            merged.append(entry_copy)
            seen_links.add(link)
    
    return merged


def build_ranking_prompt(
    topic: str,
    items: List[Dict[str, Any]],
    search_terms: Dict[str, str],
    ranking_prompt: str,
    topic_prompt: str | None,
    top_n: int,
    priority_journals: List[str] | None = None,
) -> Tuple[str, List[int]]:
    # Separate priority and regular items
    priority_items = [item for item in items if item.get('is_priority', False)]
    regular_items = [item for item in items if not item.get('is_priority', False)]
    
    # Priority items are always included, regular items are limited
    item_limit = PROMPT_MAX_ITEMS_PER_TOPIC
    limited_regular_items = regular_items[:max(0, item_limit - len(priority_items))]
    limited_items = priority_items + limited_regular_items
    
    numbered = []
    for idx, it in enumerate(limited_items, start=1):
        title = it.get('title', '')
        link = it.get('link', '')
        summary = it.get('summary', '')
        authors = it.get('authors', '')
        feed_title = it.get('feed_title', '')
        is_priority = it.get('is_priority', False)
        priority_marker = " [PRIORITY JOURNAL]" if is_priority else ""
        numbered.append(f"{idx}) title: {title}\n   link: {link}\n   authors: {authors}\n   feed: {feed_title}{priority_marker}\n   summary: {summary}")
    base_instruction = ranking_prompt or (
        "You are a domain expert. From the list of entries for the given topic, select the most important papers for today and write a concise multi-sentence summary for each."
    )
    terms_text = json.dumps(search_terms, indent=2)
    # Calculate max summary length for each entry to prevent hallucinations
    max_summary_lengths = []
    for it in limited_items:
        title = it.get('title', '')
        original_summary = it.get('summary', '')
        max_length = len(title) + len(original_summary)
        max_summary_lengths.append(max_length)
    
    payload = (
        f"Topic: {topic}\n"
        + (f"Topic-specific guidance:\n{topic_prompt}\n\n" if topic_prompt else "")
        + f"Search term regex for this topic (for context only):\n{terms_text}\n\n"
        f"Entries (each has title, link, authors, feed, summary):\n" + "\n\n".join(numbered) + "\n\n"
        "Task: Rank the entries by importance for an expert reader. Consider topical relevance, novelty, likely impact, experimental/theory significance, and match to the topic. "
        "IMPORTANT: Entries marked with [PRIORITY JOURNAL] are from high-impact journals and should be strongly favored for inclusion unless they are completely irrelevant. "
        f"Return ONLY a valid RFC 8259 JSON object with EXACTLY {top_n} items or fewer. Keep response under 15000 tokens. No markdown, no code fences, no comments, no trailing commas.\n"
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
        "Rules:\n- Use only the provided entries.\n- link must be copied from the matching entry.\n- Keep overview to 2-3 sentences.\n- Each item's summary must be 4-5 sentences.\n- Each item's summary length must NOT exceed the combined length of its title and original summary to prevent hallucinations.\n- items must be ordered by descending importance_score, ties broken by better match to topic.\n"
    )
    return f"{base_instruction}\n\n{payload}", max_summary_lengths


def rank_entries_with_llm(
    topic: str,
    items: List[Dict[str, Any]],
    search_term_for_topic: str | None,
    ranking_prompt: str,
    topic_prompt: str | None,
    top_n: int,
    priority_journals: List[str] | None = None,
) -> Dict[str, Any]:
    """
    Rank entries using LLM and enforce summary length constraints to prevent hallucinations.
    
    Summary length is constrained to not exceed the combined length of the title and original summary.
    This prevents the LLM from generating content beyond what's available in the source material.
    """
    if not items:
        return {"topic": topic, "overview": "", "items": []}
    # Build a map from link -> original metadata (authors, feed_title, original_summary)
    link_to_meta: Dict[str, Dict[str, str]] = {}
    for it in items:
        link = str(it.get('link', '')).strip()
        if not link:
            continue
        link_to_meta[link] = {
            'authors': str(it.get('authors', '')).strip(),
            'feed_title': str(it.get('feed_title', '')).strip(),
            'original_summary': str(it.get('summary', '')).strip(),
        }
    prompt, max_summary_lengths = build_ranking_prompt(
        topic=topic,
        items=items,
        search_terms={topic: search_term_for_topic or ""},
        ranking_prompt=ranking_prompt,
        topic_prompt=topic_prompt,
        top_n=top_n,
        priority_journals=priority_journals,
    )
    # Increase output tokens for gpt-5 models that use reasoning tokens separately
    raw = chat_completion_with_fallback(prompt, max_tokens=20000)
    if not raw:
        logging.error("LLM returned empty content for topic '%s'", topic)
        return {"topic": topic, "overview": "", "items": []}
    data = extract_json_block(raw)
    if not data or not isinstance(data, dict) or 'items' not in data:
        # Persist the raw (non-parseable) LLM output for debugging
        try:
            assets_dir = os.path.join(MAIN_DIR, 'assets')
            os.makedirs(assets_dir, exist_ok=True)
            safe_topic = re.sub(r"[^a-zA-Z0-9_-]+", "_", topic)
            debug_path = os.path.join(assets_dir, f"llm_raw_{safe_topic}.json")
            with open(debug_path, 'w', encoding='utf-8') as f:
                f.write(raw)
            logging.error("Wrote non-parseable LLM output for topic '%s' to %s", topic, debug_path)
        except Exception:
            pass
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
        
        # Get the generated summary
        generated_summary = str(it.get('summary', '') or it.get('one_sentence_summary', '')).strip()
        
        # Find the corresponding max length for this item
        # We need to find the item in the original items list that matches this link
        max_length = None
        for j, original_item in enumerate(items):
            if str(original_item.get('link', '')).strip() == link:
                if j < len(max_summary_lengths):
                    max_length = max_summary_lengths[j]
                break
        
        # Truncate summary if it exceeds the limit
        if max_length is not None and len(generated_summary) > max_length:
            original_length = len(generated_summary)
            # Try to truncate at a sentence boundary
            sentences = generated_summary.split('. ')
            truncated_summary = ""
            for sentence in sentences:
                if len(truncated_summary + sentence + '. ') <= max_length:
                    truncated_summary += sentence + '. '
                else:
                    break
            
            # If we couldn't fit even one sentence, truncate at word boundary
            if not truncated_summary:
                words = generated_summary.split()
                truncated_summary = ""
                for word in words:
                    if len(truncated_summary + word + ' ') <= max_length:
                        truncated_summary += word + ' '
                    else:
                        break
                truncated_summary = truncated_summary.strip()
            
            # If still too long, hard truncate
            if len(truncated_summary) > max_length:
                truncated_summary = generated_summary[:max_length-3] + "..."
            
            generated_summary = truncated_summary
            logging.info(f"Truncated summary for '{title[:50]}...' from {original_length} to {len(generated_summary)} chars (max: {max_length})")
        
        sanitized.append({
            'rank': int(it.get('rank', i + 1)),
            'importance_score': int(it.get('importance_score', 3)),
            'title': title,
            'link': link,
            'summary': generated_summary,
            'authors': meta.get('authors', ''),
            'feed_title': meta.get('feed_title', ''),
            'original_summary': meta.get('original_summary', ''),
        })
    overview = str(data.get('overview', '')).strip()
    return {'topic': topic, 'overview': overview, 'items': sanitized}


def escape_html_preserve_latex(text: str) -> str:
    """Escape HTML while preserving LaTeX equations enclosed in $ symbols."""
    if not text:
        return ""
    
    # Store original text to work with
    result = text
    placeholder_map = {}
    
    # Handle display math $$...$$ first (to avoid conflicts with inline math)
    display_pattern = r'\$\$([^$]+?)\$\$'
    display_matches = list(re.finditer(display_pattern, result))
    for i, match in enumerate(display_matches):
        placeholder = f"__LATEX_DISPLAY_{i}__"
        placeholder_map[placeholder] = match.group(0)
        result = result.replace(match.group(0), placeholder, 1)
    
    # Handle inline math $...$ (non-greedy to avoid capturing across multiple equations)
    inline_pattern = r'\$([^$]+?)\$'
    inline_matches = list(re.finditer(inline_pattern, result))
    for i, match in enumerate(inline_matches):
        placeholder = f"__LATEX_INLINE_{i}__"
        placeholder_map[placeholder] = match.group(0)
        result = result.replace(match.group(0), placeholder, 1)
    
    # Escape HTML in the remaining text
    escaped = html.escape(result)
    
    # Restore LaTeX equations
    for placeholder, latex in placeholder_map.items():
        escaped = escaped.replace(placeholder, latex)
    
    return escaped


def render_html(sections: List[Dict[str, Any]], generated_for: datetime.date, output_path: str) -> None:
    style = (
        "body{font-family:Arial,sans-serif;margin:24px;}"
        "h1{margin-bottom:4px;} h2{color:#2E8B57;margin-top:28px;}"
        ".topic{margin-bottom:28px;}"
        ".item{margin:10px 0;padding:10px;border:1px solid #e5e5e5;border-radius:8px;}"
        ".badge{display:inline-block;background:#1f6feb;color:#fff;padding:2px 8px;border-radius:12px;font-size:12px;margin-left:8px;}"
        ".meta{color:#666;font-size:13px;margin-top:4px;}"
        ".abstract-toggle{cursor:pointer;color:#4682B4;text-decoration:none;font-size:12px;margin-top:8px;display:inline-block;}"
        ".abstract-toggle:hover{text-decoration:underline;}"
        ".abstract-content{display:none;margin-top:8px;padding:8px;background:#f8f9fa;border-left:3px solid #4682B4;font-size:12px;color:#555;border-radius:3px;}"
        ".abstract-content.show{display:block;}"
        ".abstract-arrow{transition:transform 0.2s;display:inline-block;margin-right:4px;}"
        ".abstract-arrow.rotated{transform:rotate(90deg);}"
    )
    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append(f"<title>Summary {generated_for}</title>")
    # Add MathJax for LaTeX equation rendering
    parts.append("<script src='https://polyfill.io/v3/polyfill.min.js?features=es6'></script>")
    parts.append("<script id='MathJax-script' async src='https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js'></script>")
    parts.append("<script>")
    parts.append("window.MathJax = {")
    parts.append("  tex: {")
    parts.append("    inlineMath: [['$', '$']],")
    parts.append("    displayMath: [['$$', '$$']]")
    parts.append("  },")
    parts.append("  options: {")
    parts.append("    processHtmlClass: 'tex2jax_process',")
    parts.append("    processEscapes: true")
    parts.append("  }")
    parts.append("};")
    parts.append("</script>")
    parts.append(f"<style>{style}</style>")
    parts.append("<script>")
    parts.append("function toggleAbstract(id) {")
    parts.append("  var content = document.getElementById('abstract-' + id);")
    parts.append("  var arrow = document.getElementById('arrow-' + id);")
    parts.append("  if (content.classList.contains('show')) {")
    parts.append("    content.classList.remove('show');")
    parts.append("    arrow.classList.remove('rotated');")
    parts.append("  } else {")
    parts.append("    content.classList.add('show');")
    parts.append("    arrow.classList.add('rotated');")
    parts.append("  }")
    parts.append("}")
    parts.append("</script>")
    parts.append("</head><body>")
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
            for idx, it in enumerate(items):
                title = escape_html_preserve_latex(it.get('title', ''))
                link = html.escape(it.get('link', ''), quote=True)
                summary = html.escape(it.get('summary', ''))
                original_abstract = html.escape(it.get('original_summary', ''))
                score = int(it.get('importance_score', 0))
                authors = html.escape(it.get('authors', '') or '')
                journal = html.escape(it.get('feed_title', '') or '')
                
                # Create unique ID for this item
                item_id = f"{topic}_{idx}"
                
                parts.append("<div class='item tex2jax_process'>")
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
                
                # Add dropdown for original abstract if it exists
                if original_abstract and original_abstract != summary:
                    parts.append(f"<a class='abstract-toggle' onclick='toggleAbstract(\"{item_id}\")'>")
                    parts.append(f"<span class='abstract-arrow' id='arrow-{item_id}'>▶</span>Show original abstract")
                    parts.append("</a>")
                    parts.append(f"<div class='abstract-content' id='abstract-{item_id}'>")
                    parts.append(f"<strong>Original Abstract:</strong><br>{original_abstract}")
                    parts.append("</div>")
                
                parts.append("</div>")
        parts.append("</div>")

    parts.append("</body></html>")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(parts))


def main(all_entries: Dict[str, Dict[str, Dict[str, List[dict]]]] | Dict[str, Dict[str, List[dict]]] | None = None, top_n: int | None = None) -> None:
    # This script is intended to be called with the entries from the current run.
    # If entries are not provided, create an empty summary page noting that no new
    # papers were processed in this invocation.
    top_n_effective = top_n or SUMMARY_TOP_N_DEFAULT
    terms = read_search_terms()
    prompts = read_llm_prompts()
    priority_journals = read_priority_journals()

    generated_for = datetime.date.today()

    # Handle both old format (Dict[str, Dict[str, List[dict]]]) and new format
    # (Dict[str, Dict[str, Dict[str, List[dict]]]] with 'regular_entries' and 'priority_entries' keys)
    if all_entries is None:
        entries_per_topic = None
        priority_entries_per_topic = None
    elif 'regular_entries' in all_entries and 'priority_entries' in all_entries:
        # New format with separate regular and priority entries
        entries_per_topic = all_entries['regular_entries']  # type: ignore
        priority_entries_per_topic = all_entries['priority_entries']  # type: ignore
    else:
        # Old format - treat all as regular entries
        entries_per_topic = all_entries  # type: ignore
        priority_entries_per_topic = None

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
        # Get regular entries for this topic
        regular_flat = flatten_entries(per_feed)
        
        # Get priority entries for this topic if available
        priority_flat = []
        if priority_entries_per_topic and topic in priority_entries_per_topic:
            priority_flat = flatten_entries(priority_entries_per_topic[topic])
        
        # Merge priority and regular entries
        all_flat = merge_priority_entries(regular_flat, priority_flat, priority_journals)
        compact = to_compact_items(all_flat)
        
        topic_prompt = prompts.get(topic)
        ranked = rank_entries_with_llm(
            topic=topic,
            items=compact,
            search_term_for_topic=terms.get(topic),
            ranking_prompt=ranking_prompt,
            topic_prompt=topic_prompt,
            top_n=top_n_effective,
            priority_journals=priority_journals,
        )
        sections.append(ranked)
        if sleep_between_topics > 0:
            time.sleep(sleep_between_topics)

    render_html(sections, generated_for, SUMMARY_HTML_PATH)


if __name__ == '__main__':
    main()
