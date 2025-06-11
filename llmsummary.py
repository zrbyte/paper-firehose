import os
import re
import json
import html
import datetime
import urllib.request


def load_api_key(path="openaikulcs.env"):
    """Return the OpenAI API key from the environment or a file."""
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key

    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)

    with open(path, "r", encoding="utf-8") as f:
        key = f.read().strip()
        if "=" in key:
            key = key.split("=", 1)[-1].strip()
        return key

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


def extract_entry_details(entries):
    """Return list of (title, summary, link) tuples from RSS entries."""
    details = []
    for entry in entries:
        title = html.unescape(entry.get('title', '')).strip()
        summary = html.unescape(entry.get('summary', '')).strip()
        link = entry.get('link')
        details.append((title, summary, link))
    return details


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


def summarize_entries(entries, prompt_prefix, char_limit=3000, search_context=None):
    if not entries:
        return 'No new papers.'
    def to_text(item):
        if len(item) == 3:
            t, s, _ = item
            return f"{t} - {s}"
        else:
            t, _ = item
            return t

    joined = '; '.join(to_text(e) for e in entries)
    context = f"Search terms: {search_context}\n" if search_context else ''
    prompt = (
        f"{prompt_prefix}\n"
        f"{context}"
        f"Titles: {joined}\n"
        f"Provide a concise summary under {char_limit} characters."
    )
    return chat_completion(prompt, max_tokens=2000)


def summarize_primary(entries, search_terms, char_limit=4000):
    """Summarize primary entries with titles, links and summaries."""
    if not entries:
        return 'No new papers.'
    def to_text(item):
        if len(item) == 3:
            t, s, link = item
            return f"{t} ({link}) - {s}"
        else:
            t, link = item
            return f"{t} ({link})"

    titles_links = '; '.join(to_text(e) for e in entries)
    prompt = (
        f"Search terms:\n{json.dumps(search_terms, indent=2)}\n"
        "Summarize the following papers with emphasis on those best matching the primary search terms. "
        "Place the 5 best matching the primary search terms into a list of links at the end of the summary.\n"
        f"Titles and links: {titles_links}\n"
        f"Provide a concise summary under {char_limit} characters."
    )
    return chat_completion(prompt, max_tokens=4000)


def generate_html(primary_summary, rg_info, topic_summaries, output_path):
    today = datetime.date.today()
    def extract_links(text: str):
        """Return text without Markdown links and list of (title, url) tuples."""
        link_pat = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
        match = re.search(r"Top\s*\d+.*?:", text, re.IGNORECASE)
        if match:
            main = text[: match.start()].strip()
            link_text = text[match.end() :]
        else:
            main = text
            link_text = ""
        links = link_pat.findall(link_text)
        return main, links

    primary_text, primary_links = extract_links(primary_summary)

    def format_links(links):
        if not links:
            return ""
        parts = [f'<a href="{url}">{html.escape(title)}</a>' for title, url in links]
        return "<p>" + "<br>".join(parts) + "</p>"

    sections = [
        f"<h2>Primary</h2><p>{html.escape(primary_text)}</p>" + format_links(primary_links),
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


def main(entries_per_topic=None):
    terms = read_search_terms()
    topics = list(terms.keys())

    stable_files = {
        'primary': 'results_primary.html',
        'rg': 'rg_filtered_articles.html',
    }
    for t in topics:
        if t not in stable_files:
            stable_files[t] = f'{t}_filtered_articles.html'

    if entries_per_topic is None:
        primary_entries = extract_titles(os.path.join(MAIN_DIR, stable_files['primary']))
        rg_entries = extract_titles(os.path.join(MAIN_DIR, stable_files['rg']))
    else:
        def flatten(topic):
            entries = []
            for feed_entries in entries_per_topic.get(topic, {}).values():
                entries.extend(feed_entries)
            return entries

        primary_entries = extract_entry_details(flatten('primary'))
        rg_entries = extract_entry_details(flatten('rg'))

    primary_summary = summarize_primary(
        primary_entries,
        terms,
        char_limit=4000,
    )

    if rg_entries:
        rg_info = f"There are {len(rg_entries)} new RG papers. See {stable_files['rg']}"
    else:
        rg_info = "No new RG papers today."

    topic_summaries = {}
    for t in topics:
        if t in ('primary', 'rg'):
            continue
        if entries_per_topic is None:
            entries = extract_titles(os.path.join(MAIN_DIR, stable_files[t]))
        else:
            entries = extract_entry_details(flatten(t))
        topic_summaries[t] = summarize_entries(
            entries,
            f"Summary of today's {t} papers:",
            char_limit=2000,
            search_context=terms.get(t)
        )

    generate_html(
        primary_summary,
        rg_info,
        topic_summaries,
        os.path.join(MAIN_DIR, 'summary.html'),
    )


if __name__ == '__main__':
    main()
