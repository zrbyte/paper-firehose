import os
import re
import json
import glob
import datetime
from html import unescape
from typing import List, Dict

try:
    import openai
except ImportError:
    openai = None  # openai package not available

HTML_TEMPLATE = """<html>
<head><meta charset='UTF-8'><title>LLM Summary</title></head>
<body>
<h1>LLM Generated Summary</h1>
{body}
</body></html>"""

ENTRY_RE = re.compile(r'<div class="entry">(.*?)</div>', re.S)
TITLE_RE = re.compile(r'<h3><a href="(?P<link>[^"]+)">(?P<title>.*?)</a></h3>', re.S)
SUMMARY_RE = re.compile(r'<p>(?P<summary>.*?)</p>', re.S)


def extract_entries(html_text: str) -> List[Dict[str, str]]:
    entries = []
    for block in ENTRY_RE.findall(html_text):
        title_match = TITLE_RE.search(block)
        sum_match = SUMMARY_RE.search(block)
        if title_match:
            entry = {
                'title': unescape(title_match.group('title')),
                'link': title_match.group('link')
            }
            if sum_match:
                entry['summary'] = unescape(sum_match.group('summary'))
            else:
                entry['summary'] = ''
            entries.append(entry)
    return entries


def call_llm(prompt: str, api_key: str) -> str:
    if openai is None:
        raise RuntimeError('openai package not available')
    openai.api_key = api_key
    resp = openai.ChatCompletion.create(
        model='gpt-3.5-turbo',
        messages=[{'role': 'user', 'content': prompt}],
    )
    return resp.choices[0].message.content.strip()


def summarize_entries(entries: List[Dict[str, str]], api_key: str) -> str:
    text = "\n".join(f"{e['title']}: {e.get('summary','')}" for e in entries)
    prompt = (
        "Summarize the following papers in under 400 characters, with emphasis "
        "on items most relevant to provided search terms:\n" + text
    )
    return call_llm(prompt, api_key)


def generate_summary():
    """Generate ``llm_summary.html`` from archived HTML files."""
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key or openai is None:
        print('Skipping LLM summary: OPENAI_API_KEY not set or openai package missing')
        return

    script_dir = os.path.dirname(os.path.abspath(__file__))
    archive_dir = os.path.join(script_dir, 'archive')
    primary_files = sorted(glob.glob(os.path.join(archive_dir, 'filtered_articles_*.html')))
    rg_files = sorted(glob.glob(os.path.join(archive_dir, 'rg_filtered_articles_*.html')))
    if not primary_files:
        raise SystemExit('No primary result files found')

    primary_html = primary_files[-1]
    with open(primary_html, 'r', encoding='utf-8') as f:
        primary_entries = extract_entries(f.read())

    rg_new = False
    rg_link = ''
    if rg_files:
        with open(rg_files[-1], 'r', encoding='utf-8') as f:
            rg_entries = extract_entries(f.read())
            rg_new = len(rg_entries) > 0
            rg_link = rg_files[-1]
    else:
        rg_entries = []

    primary_summary = summarize_entries(primary_entries, api_key) if primary_entries else 'No new primary papers.'

    body_parts = [f'<p>{primary_summary}</p>']
    if rg_new:
        body_parts.append(f'<p>New RG papers available: <a href="{rg_link}">{rg_link}</a></p>')
    else:
        body_parts.append('<p>No new RG papers today.</p>')

    # summary for each search term
    with open(os.path.join(script_dir, 'search_terms.json'), 'r', encoding='utf-8') as f:
        terms = json.load(f)

    today = datetime.date.today().isoformat()
    for term_name, pattern in terms.items():
        pat = re.compile(pattern, re.I)
        todays = [e for e in primary_entries if pat.search(e['title']) or pat.search(e.get('summary',''))]
        if todays:
            summary = summarize_entries(todays, api_key)
        else:
            summary = 'No matching papers.'
        body_parts.append(f'<h2>{term_name}</h2><p>{summary}</p>')

    html_out = HTML_TEMPLATE.format(body='\n'.join(body_parts))
    out_file = os.path.join(script_dir, 'llm_summary.html')
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f'Wrote {out_file}')


if __name__ == '__main__':
    generate_summary()
