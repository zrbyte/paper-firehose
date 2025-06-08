import os
import re
import json
import html
import datetime
import urllib.request

from openai_cli import load_api_key

MAIN_DIR = os.path.dirname(os.path.abspath(__file__))
SEARCHTERMS_FILE = os.path.join(MAIN_DIR, 'search_terms.json')


def read_search_terms():
    try:
        with open(SEARCHTERMS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def extract_titles(file_path):
    """Return list of (title, link) tuples from an HTML file."""
    if not os.path.exists(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        data = f.read()

    entries = []
    pattern = re.compile(r'<h3><a href="(?P<link>[^"]+)">(?P<title>.*?)</a></h3>', re.DOTALL)
    for match in pattern.finditer(data):
        title = html.unescape(match.group('title')).strip()
        link = match.group('link')
        entries.append((title, link))
    return entries


def chat_completion(prompt, max_tokens=200):
    api_key = load_api_key()
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = json.dumps({
        'model': 'gpt-4.1-nano',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        headers=headers,
        data=payload,
    )
    with urllib.request.urlopen(req) as resp:
        result = json.load(resp)
    return result['choices'][0]['message']['content']


def summarize_titles(titles, prompt_prefix, char_limit=400):
    if not titles:
        return 'No new papers.'
    joined = '; '.join(t for t, _ in titles)
    prompt = (
        f"{prompt_prefix}\nTitles: {joined}\n"
        f"Provide a concise summary under {char_limit} characters."
    )
    return chat_completion(prompt, max_tokens=200)


def generate_html(primary_summary, rg_info, topic_summaries, output_path):
    today = datetime.date.today()
    sections = [
        f"<h2>Primary</h2><p>{html.escape(primary_summary)}</p>",
        f"<h2>RG</h2><p>{html.escape(rg_info)}</p>",
    ]
    for topic, summ in topic_summaries.items():
        sections.append(f"<h2>{html.escape(topic)}</h2><p>{html.escape(summ)}</p>")

    content = '\n'.join(sections)
    out_html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>Summary {today}</title></head><body>"
        f"<h1>Summary for {today}</h1>" + content + "</body></html>"
    )
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(out_html)


def main():
    terms = read_search_terms()
    topics = list(terms.keys())

    stable_files = {
        'primary': 'results_primary.html',
        'rg': 'rg_filtered_articles.html',
    }
    for t in topics:
        if t not in stable_files:
            stable_files[t] = f'{t}_filtered_articles.html'

    primary_titles = extract_titles(os.path.join(MAIN_DIR, stable_files['primary']))
    primary_summary = summarize_titles(
        primary_titles,
        "Summarize the following papers with emphasis on those best matching the search terms.",
        char_limit=400,
    )

    rg_titles = extract_titles(os.path.join(MAIN_DIR, stable_files['rg']))
    if rg_titles:
        rg_info = f"There are {len(rg_titles)} new RG papers. See {stable_files['rg']}"
    else:
        rg_info = "No new RG papers today."

    topic_summaries = {}
    for t in topics:
        if t in ('primary', 'rg'):
            continue
        titles = extract_titles(os.path.join(MAIN_DIR, stable_files[t]))
        topic_summaries[t] = summarize_titles(
            titles,
            f"Summary of today's {t} papers:",
            char_limit=400,
        )

    generate_html(
        primary_summary,
        rg_info,
        topic_summaries,
        os.path.join(MAIN_DIR, 'summary.html'),
    )


if __name__ == '__main__':
    main()
