"""
LLM summarization command: write concise summaries into papers.db (entries.llm_summary)
and matched_entries_history.db (matched_entries.llm_summary), guided by topic YAML.

Behavior
- Reads topic.llm_summary: { prompt, score_cutoff, top_n }
- Selects entries by topic with rank_score >= score_cutoff, ordered desc, up to top_n.
- Builds input text from title + abstract (fallback summary if abstract missing).
- Calls OpenAI Chat Completions (via REST) with configured model and API key from config.llm.api_key_env.
- Enforces length: summary must not exceed len(title) + len(abstract_or_summary).
- Skips entries that already have llm_summary unless --overwrite is set.

Global config
- config.llm.model (model id), config.llm.api_key_env (env var name for API key)
- defaults.llm: { rps, max_retries, mailto (optional UA email) }
"""

from __future__ import annotations

import os
import time
from typing import Optional, Dict, Any, List
import logging
import requests
from openai import OpenAI

from core.config import ConfigManager
from core.database import DatabaseManager

logger = logging.getLogger(__name__)


def _load_key_from_file(path: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        # Accept either raw key or KEY=VALUE format
        if '=' in content:
            for line in content.splitlines():
                if line.strip().startswith('OPENAI_API_KEY'):
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"').strip("'")
                        if val:
                            return val
        if content:
            return content
    except Exception:
        return None
    return None


def _resolve_api_key(config: Dict[str, Any]) -> str:
    """Prefer key from repo-root openaikulcs.env, else env OPENAI_API_KEY."""
    # Try CWD (repo root typical) first
    key = _load_key_from_file('openaikulcs.env')
    if not key:
        # Try computing path relative to this file two levels up (src/commands -> repo root)
        here = os.path.dirname(__file__)
        repo_root_guess = os.path.abspath(os.path.join(here, '..', '..'))
        key = _load_key_from_file(os.path.join(repo_root_guess, 'openaikulcs.env'))
    if not key:
        key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise RuntimeError("Missing OpenAI API key in openaikulcs.env or environment variable OPENAI_API_KEY")
    return key


def _resolve_model(config: Dict[str, Any]) -> str:
    llm = config.get('llm') or {}
    return llm.get('model') or 'gpt-5'


def _iter_candidates(db: DatabaseManager, topic: str, score_cutoff: float, top_n: int) -> List[Dict[str, Any]]:
    import sqlite3
    conn = sqlite3.connect(db.db_paths['current'])
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, topic, title, abstract, summary, rank_score
        FROM entries
        WHERE topic = ?
          AND COALESCE(rank_score, 0) >= ?
        ORDER BY rank_score DESC
        LIMIT ?
        """,
        (topic, score_cutoff, top_n),
    )
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def _get_token_param(model: str) -> dict:
    """Get the appropriate token parameter for the model."""
    # GPT-5 models require max_output_tokens, but the OpenAI client library
    # doesn't support it yet. We'll handle this in the API call logic.
    if model.startswith('gpt-5'):
        return {"max_output_tokens": 400}
    else:
        return {"max_tokens": 400}


def _call_gpt5_direct(api_key: str, model: str, system: str, user_text: str, char_limit: int, max_retries: int = 3, verbosity: str = "low", reasoning_effort: str = "minimal") -> Optional[str]:
    """Direct HTTP call for GPT-5 models to bypass client library limitations."""
    import requests
    
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text}
        ],
        "response_format": {"type": "json_object"},
        "verbosity": verbosity,
        "reasoning_effort": reasoning_effort
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if content:
                return content
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            continue
    
    # Try without JSON format
    payload["response_format"] = None
    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if content:
                return content
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            continue
    
    return None


def _call_openai(api_key: str, models: List[str], prompt: str, title: str, abstract_or_summary: str, char_limit: int, max_retries: int = 3, config: Dict[str, Any] = None) -> Optional[str]:
    """Use Chat Completions for multiple models with robust fallbacks.

    - Try JSON response_format first. If unsupported, fall back to plain text.
    - Retry on 429/5xx with exponential backoff.
    - Try next model on 400/unsupported.
    - Uses max_output_tokens for GPT-5 models, max_tokens for others.
    """
    client = OpenAI(api_key=api_key)
    system = (
        "You are a concise technical summarizer. Write a brief, information-dense summary suitable "
        "for experts. Avoid hype or superlatives."
    )
    user_text = (
        f"Title: {title}\n\n"
        f"Abstract/Context:\n{abstract_or_summary}\n\n"
        f"Instructions: {prompt}\n\n"
        f"Length rule: Do not exceed {char_limit} characters."
    )

    for model in models:
        # Use direct HTTP call for GPT-5 models to bypass client library limitations
        if model.startswith('gpt-5'):
            # Get GPT-5 specific parameters from config
            llm_config = (config or {}).get('llm', {})
            verbosity = llm_config.get('verbosity', 'low')
            reasoning_effort = llm_config.get('reasoning_effort', 'minimal')
            
            result = _call_gpt5_direct(api_key, model, system, user_text, char_limit, max_retries, verbosity, reasoning_effort)
            if result:
                return result
            continue
        
        backoff = 1.0
        token_params = _get_token_param(model)
        
        for attempt in range(max_retries):
            # Try JSON-formatted response
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_text},
                    ],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                    **token_params,
                )
                content = (resp.choices[0].message.content or "").strip()
                if content:
                    return content
            except Exception as e_json:
                es = str(e_json).lower()
                # If JSON format unsupported -> try plain text
                if any(code in es for code in [" 400 ", "unsupported", "invalid", "response_format", "not supported"]):
                    try:
                        resp2 = client.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": system},
                                {"role": "user", "content": user_text},
                            ],
                            temperature=0.2,
                            **token_params,
                        )
                        content2 = (resp2.choices[0].message.content or "").strip()
                        if content2:
                            return content2[:char_limit]
                        # Empty content → backoff
                        time.sleep(backoff)
                        backoff = min(8.0, backoff * 2)
                        continue
                    except Exception as e_plain:
                        es2 = str(e_plain).lower()
                        if any(code in es2 for code in [" 400 ", " 404 ", "invalid", "unsupported"]):
                            # switch model
                            logger.info("Summarizer: model '%s' not supported for chat; trying fallback model. Reason: %s", model, es2[:120])
                            break
                        time.sleep(backoff)
                        backoff = min(8.0, backoff * 2)
                        continue
                else:
                    # Non-client error on JSON path → transient
                    time.sleep(backoff)
                    backoff = min(8.0, backoff * 2)
                    continue

            # If we reached here with no exception but empty content, backoff and retry
            time.sleep(backoff)
            backoff = min(8.0, backoff * 2)
            continue
    return None


def run(config_path: str, topic: Optional[str] = None, *, rps: Optional[float] = None) -> None:
    logger.info("Starting LLM summarization")
    cfg_mgr = ConfigManager(config_path)
    if not cfg_mgr.validate_config():
        logger.error("Configuration validation failed")
        return
    config = cfg_mgr.load_config()
    db = DatabaseManager(config)

    topics: List[str] = [topic] if topic else cfg_mgr.get_available_topics()
    api_key = _resolve_api_key(config)
    model = _resolve_model(config)
    model_fallback = (config.get('llm') or {}).get('model_fallback') or 'gpt-4o-mini'
    defaults = (config.get('defaults') or {})
    llm_defaults = (defaults.get('llm') or {})
    mailto = (defaults.get('abstracts') or {}).get('mailto') or os.environ.get('MAILTO')
    rps_eff = rps if rps else float(llm_defaults.get('rps', 0.5))  # slower by default
    max_retries = int(llm_defaults.get('max_retries', 3))
    min_interval = 1.0 / max(rps_eff, 0.01)

    total_updated = 0

    for t in topics:
        try:
            tcfg = cfg_mgr.load_topic_config(t)
        except Exception as e:
            logger.error("Topic '%s' config load failed: %s", t, e)
            continue

        ls = (tcfg.get('llm_summary') or {})
        prompt = (ls.get('prompt') or '').strip()
        score_cutoff = float(ls.get('score_cutoff', 0.35))
        top_n = int(ls.get('top_n', 5))
        if not prompt:
            logger.info("Topic '%s': no llm_summary.prompt; skipping", t)
            continue
        
        # Get ranking query to inject into prompt
        ranking_cfg = (tcfg.get('ranking') or {})
        ranking_query = ranking_cfg.get('query', '').strip()
        
        # Replace placeholder in prompt with actual ranking query
        if ranking_query and '{ranking_query}' in prompt:
            prompt = prompt.replace('{ranking_query}', ranking_query)

        candidates = _iter_candidates(db, t, score_cutoff, top_n)
        if not candidates:
            logger.info("Topic '%s': no candidates for summarization", t)
            continue

        updated = 0
        for row in candidates:
            title = (row.get('title') or '').strip()
            source = (row.get('abstract') or '').strip()
            if not source:
                source = (row.get('summary') or '').strip()
            if not title or not source:
                continue
            char_limit = len(title) + len(source)
            summary = _call_openai(api_key, [model, model_fallback], prompt, title, source, char_limit, max_retries=max_retries, config=config)
            time.sleep(min_interval)
            if not summary:
                continue
            # Note: Removed hard truncation to preserve JSON structure integrity

            # Write to current DB and history DB (best-effort for history)
            import sqlite3
            try:
                conn = sqlite3.connect(db.db_paths['current'])
                cur = conn.cursor()
                cur.execute("UPDATE entries SET llm_summary = ? WHERE id = ? AND topic = ?", (summary, row['id'], t))
                conn.commit()
                conn.close()
                try:
                    hconn = sqlite3.connect(db.db_paths['history'])
                    hcur = hconn.cursor()
                    hcur.execute("UPDATE matched_entries SET llm_summary = ? WHERE entry_id = ?", (summary, row['id']))
                    hconn.commit()
                    hconn.close()
                except Exception:
                    pass
                updated += 1
            except Exception as e:
                logger.error("Topic '%s': failed to write summary for %s: %s", t, row['id'][:8], e)
                continue
        total_updated += updated
        logger.info("Topic '%s': wrote llm_summary for %d entries", t, updated)

    # Generate summarized HTML for each topic that has summaries
    if total_updated > 0:
        try:
            from processors.html_generator import HTMLGenerator
            html_gen = HTMLGenerator(template_path="llmsummary_template.html")
            
            for t in topics:
                try:
                    tcfg = cfg_mgr.load_topic_config(t)
                    output_config = tcfg.get('output', {})
                    summary_filename = output_config.get('filename_summary')
                    
                    if summary_filename:
                        # Check if this topic has any summaries
                        topic_entries = db.get_current_entries(topic=t)
                        has_summaries = any(e.get('llm_summary') and e.get('llm_summary').strip() for e in topic_entries)
                        
                        if has_summaries:
                            topic_name = tcfg.get('name', t)
                            html_gen.generate_summarized_html_from_database(db, t, summary_filename, f"LLM Summaries - {topic_name}")
                            logger.info("Generated summarized HTML for topic '%s': %s", t, summary_filename)
                except Exception as e:
                    logger.error("Failed to generate summarized HTML for topic '%s': %s", t, e)
        except Exception as e:
            logger.error("Failed to generate summarized HTML: %s", e)

    db.close_all_connections()
    logger.info("LLM summarization completed; total updated=%d", total_updated)
