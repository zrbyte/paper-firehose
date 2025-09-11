#!/usr/bin/env python3
"""
Quick tester for OpenAI gpt-5 via the Responses API, with fallback to Chat Completions.

Usage examples:
  python scripts/test_gpt5_responses.py --prompt "Summarize: Graphene is..." --model gpt-5
  python scripts/test_gpt5_responses.py --prompt "Hello" --model gpt-4o-mini

API key resolution:
  - Tries ./openaikulcs.env (raw key or OPENAI_API_KEY=...)
  - Else uses env var OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional, Any, List

from openai import OpenAI


def load_key_from_file(path: str) -> Optional[str]:
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if '=' in content:
            for line in content.splitlines():
                if line.strip().startswith('OPENAI_API_KEY'):
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        val = parts[1].strip().strip('"').strip("'")
                        if val:
                            return val
        return content or None
    except Exception:
        return None


def resolve_api_key() -> str:
    key = load_key_from_file('openaikulcs.env')
    if not key:
        here = os.path.dirname(__file__)
        root_guess = os.path.abspath(os.path.join(here, '..'))
        key = load_key_from_file(os.path.join(root_guess, 'openaikulcs.env'))
    if not key:
        key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise SystemExit("OPENAI_API_KEY not found (env) and openaikulcs.env missing")
    return key


def extract_responses_text(resp_obj: Any) -> str:
    # Try SDK convenience property
    try:
        txt = getattr(resp_obj, 'output_text', None)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
    except Exception:
        pass
    # Try iterating output items
    try:
        output = getattr(resp_obj, 'output', None)
        if isinstance(output, list):
            parts: List[str] = []
            for item in output:
                content = getattr(item, 'content', None)
                if not isinstance(content, list):
                    continue
                for c in content:
                    txt_obj = getattr(c, 'text', None)
                    val = getattr(txt_obj, 'value', None) if txt_obj is not None else None
                    if isinstance(val, str) and val.strip():
                        parts.append(val.strip())
            if parts:
                return "\n".join(parts)
    except Exception:
        pass
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Test gpt-5 via Responses, fallback to Chat")
    ap.add_argument('--prompt', required=True, help='Text prompt to send')
    ap.add_argument('--model', default='gpt-5', help='Primary model (default: gpt-5)')
    ap.add_argument('--fallback', default='gpt-4o-mini', help='Fallback chat model (default: gpt-4o-mini)')
    ap.add_argument('--max-output-tokens', type=int, default=400)
    ap.add_argument('--retries', type=int, default=3)
    args = ap.parse_args()

    api_key = resolve_api_key()
    client = OpenAI(api_key=api_key)

    # Try Responses for gpt-5 family
    used = None
    if args.model.lower().startswith('gpt-5'):
        backoff = 1.0
        for attempt in range(args.retries):
            try:
                r = client.responses.create(
                    model=args.model,
                    input=args.prompt,
                    max_output_tokens=args.max_output_tokens,
                )
                text = extract_responses_text(r)
                if text:
                    used = f"responses:{args.model}"
                    print(f"[OK] {used}\n{text}")
                    return 0
                else:
                    print(f"[WARN] Responses returned empty content on attempt {attempt+1}")
                    break
            except Exception as e:
                es = str(e).lower()
                if any(code in es for code in [' 400 ', ' 404 ', 'invalid', 'unsupported']):
                    print(f"[INFO] Responses unsupported for {args.model}; switching to chat.")
                    break
                time.sleep(backoff)
                backoff = min(8.0, backoff * 2)

    # Fallback to Chat Completions
    backoff = 1.0
    for attempt in range(args.retries):
        try:
            resp = client.chat.completions.create(
                model=args.fallback if used is None else args.model,
                messages=[{"role": "user", "content": args.prompt}],
                temperature=0.2,
                max_tokens=args.max_output_tokens,
            )
            text = (resp.choices[0].message.content or '').strip()
            if text:
                used = f"chat:{args.fallback if 'responses' in (used or '') else args.model}"
                print(f"[OK] {used}\n{text}")
                return 0
            print(f"[WARN] Chat returned empty content on attempt {attempt+1}")
        except Exception as e:
            es = str(e).lower()
            if any(code in es for code in [' 400 ', ' 404 ', 'invalid', 'unsupported']):
                print(f"[ERR] Client error: {e}")
                return 2
            time.sleep(backoff)
            backoff = min(8.0, backoff * 2)

    print("[ERR] No content produced after retries")
    return 3


if __name__ == '__main__':
    sys.exit(main())

