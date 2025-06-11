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
# Both the search term patterns and the LLM prompt snippets are stored next to
# this script so they can be edited without touching the code.
SEARCHTERMS_FILE = os.path.join(MAIN_DIR, 'search_terms.json')
LLM_PROMPTS_FILE = os.path.join(MAIN_DIR, 'llm_prompts.json')


def read_search_terms():
    try:
        with open(SEARCHTERMS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def read_llm_prompts():
    """Return a mapping of topic names to prompt snippets."""
    try:
        with open(LLM_PROMPTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        # Fall back to an empty mapping if the file is missing or invalid
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


def summarize_entries(entries, prompt_prefix, char_limit=3000, search_context=None, all_terms=None):
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
    # Append the entire search term mapping so the model can see the patterns
    terms_text = f"\nSearch terms:\n{json.dumps(all_terms, indent=2)}" if all_terms else ''
    prompt = (
        f"{prompt_prefix}\n"
        f"{context}"
        f"Titles: {joined}\n"
        f"Provide a concise summary under {char_limit} characters."
        f"{terms_text}"
    )
    return chat_completion(prompt, max_tokens=2000)


def summarize_primary(entries, search_terms, prompt_prefix, char_limit=4000):
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
        f"{prompt_prefix}\n"
        f"Titles and links: {titles_links}\n"
        f"Provide a concise summary under {char_limit} characters."
        # Include all search terms so the model is aware of every topic
        f"\nSearch terms:\n{json.dumps(search_terms, indent=2)}"
    )
    return chat_completion(prompt, max_tokens=4000)


def markdown_to_html(text: str) -> str:
    """Convert a small subset of Markdown to HTML using only the standard library."""
    patterns = re.finditer(
        r"\[([^\]]+)\]\((https?://[^)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*",
        text,
    )
    pos = 0
    parts = []
    for m in patterns:
        parts.append(html.escape(text[pos : m.start()]))
        if m.group(1):
            title = html.escape(m.group(1))
            url = html.escape(m.group(2), quote=True)
            parts.append(f'<a href="{url}">{title}</a>')
        elif m.group(3):
            parts.append(f'<strong>{html.escape(m.group(3))}</strong>')
        elif m.group(4):
            parts.append(f'<em>{html.escape(m.group(4))}</em>')
        pos = m.end()
    parts.append(html.escape(text[pos:]))
    html_text = "".join(parts).replace("\n", "<br>")
    return f"<p>{html_text}</p>"


def generate_html(primary_summary, rg_info, topic_summaries, output_path):
    today = datetime.date.today()
    sections = [
        f"<h2>Primary</h2>" + markdown_to_html(primary_summary),
        f"<h2>RG</h2>" + markdown_to_html(rg_info),
    ]
    for topic, summ in topic_summaries.items():
        sections.append(f"<h2>{html.escape(topic)}</h2>" + markdown_to_html(summ))

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
    prompts = read_llm_prompts()
    topics = list(terms.keys())

    stable_files = {
        'primary': 'results_primary.html',
        'rg': 'rg_filtered_articles.html',
    }
    for t in topics:
        if t not in stable_files:
            stable_files[t] = f'{t}_filtered_articles.html'

    def flatten(topic):
        entries = []
        for feed_entries in entries_per_topic.get(topic, {}).values(): # type: ignore
            entries.extend(feed_entries)
        return entries

    if entries_per_topic is None:
        primary_entries = extract_titles(os.path.join(MAIN_DIR, stable_files['primary']))
        rg_entries = extract_titles(os.path.join(MAIN_DIR, stable_files['rg']))
    else:
        primary_entries = extract_entry_details(flatten('primary'))
        rg_entries = extract_entry_details(flatten('rg'))

    # Use a custom prompt if provided for the primary topic
    # in case of no prompt, use a generic one in the second argument of the get() method
    primary_prompt = prompts.get(
        'primary',
        'Summarize the following papers with emphasis on those best matching the primary search terms. '
        'When referencing a paper, append a numbered citation such as [1](URL) directly after the relevant text.'
    )
    primary_summary = summarize_primary(
        primary_entries,
        terms,
        primary_prompt,
        char_limit=4000,
    )

    # Prompt snippet for the rhombohedral graphene topic
    # in case of no prompt, use a generic one in the second argument of the get() method
    rg_prompt = prompts.get(
        'rg',
        "Summary of today's rg papers with numbered citation links after each mention:"
    )
    rg_info = summarize_entries(
        rg_entries,
        rg_prompt,
        char_limit=2000,
        search_context=terms.get('rg'),
        all_terms=terms,
    )

    topic_summaries = {}
    for t in topics:
        if t in ('primary', 'rg'):
            continue
        if entries_per_topic is None:
            entries = extract_titles(os.path.join(MAIN_DIR, stable_files[t]))
        else:
            entries = extract_entry_details(flatten(t))
        # Fall back to a generic instruction if no prompt is defined for the topic
        topic_prompt = prompts.get(
            t,
            f"Summary of today's {t} papers with numbered citation links after each mention:"
        )
        topic_summaries[t] = summarize_entries(
            entries,
            topic_prompt,
            char_limit=2000,
            search_context=terms.get(t),
            all_terms=terms,
        )

    generate_html(
        primary_summary,
        rg_info,
        topic_summaries,
        os.path.join(MAIN_DIR, 'summary.html'),
    )


if __name__ == '__main__':
    main()
