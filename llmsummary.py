import os
import re
import json
import html
import datetime
import urllib.request
import logging
from urllib.error import HTTPError, URLError


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
        'model': 'gpt-4o', # used gpt-4.1-nano before gpt-4o-mini
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': max_tokens,
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        headers=headers,
        data=payload,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
        return result['choices'][0]['message']['content']
    except (HTTPError, URLError) as e:
        logging.error("API request failed: %s", e)
        return "API request failed"


def summarize_entries(entries, prompt_prefix, char_limit=3000, search_context=None, all_terms=None):
    if not entries:
        return 'No new papers.'
    def to_text(item):
        if len(item) == 3:
            t, s, link = item
            return f"{t} ({link}) - {s}"
        else:
            t, link = item
            return f"{t} ({link})"

    joined = '; '.join(f"{i+1}) {to_text(e)}" for i, e in enumerate(entries))
    context = f"Search terms: {search_context}\n" if search_context else ''
    # Append the entire search term mapping so the model can see the patterns
    terms_text = f"\nSearch terms:\n{json.dumps(all_terms, indent=2)}" if all_terms else ''
    prompt = (
        f"{prompt_prefix}\n"
        f"{context}"
        f"Titles: {joined}\n"
        "Use these numbers when referencing papers.\n"
        f"Provide a concise summary under {char_limit} characters."
        f"{terms_text}"
    )
    result = chat_completion(prompt, max_tokens=2000)
    return result


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

    titles_links = '; '.join(f"{i+1}) {to_text(e)}" for i, e in enumerate(entries))
    prompt = (
        f"{prompt_prefix}\n"
        f"Titles and links: {titles_links}\n"
        "Number referenced papers sequentially starting from 1.\n"
        f"Provide a concise summary under {char_limit} characters."
        # Include all search terms so the model is aware of every topic
        f"\nSearch terms:\n{json.dumps(search_terms['primary'], indent=2)}"
    )
    result = chat_completion(prompt, max_tokens=4000)
    return result


def markdown_to_html(text: str) -> str:
    """Convert a small subset of Markdown to HTML using only the standard library.

    This parser handles links with nested parentheses such as ``[1](url(a)b)`` and
    removes stray closing parentheses that occasionally appear after citation
    links in the language model output.
    """

    result = []
    pos = 0
    while pos < len(text):
        start = text.find('[', pos)
        if start == -1:
            result.append(html.escape(text[pos:]))
            break
        end = text.find(']', start)
        if end == -1:
            result.append(html.escape(text[pos:]))
            break
        result.append(html.escape(text[pos:start]))
        link_text = text[start + 1 : end]

        p = end + 1
        while p < len(text) and text[p].isspace():
            p += 1
        if p < len(text) and text[p] == '(':  # possible link
            p += 1
            depth = 1
            i = p
            while i < len(text) and depth > 0:
                if text[i] == '(':
                    depth += 1
                elif text[i] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth == 0:
                url = text[p:i]
                result.append(
                    f'<a href="{html.escape(url, quote=True)}">'
                    f'{html.escape(link_text)}</a>'
                )
                pos = i + 1
                continue

        # not a valid link, treat literally
        result.append(html.escape(text[start:end + 1]))
        pos = end + 1

    html_text = ''.join(result)
    html_text = re.sub(r'\*\*(.+?)\*\*', lambda m: f'<strong>{html.escape(m.group(1))}</strong>', html_text)
    html_text = re.sub(r'\*(.+?)\*', lambda m: f'<em>{html.escape(m.group(1))}</em>', html_text)
    html_text = re.sub(r'(</a>)\)', r'\1', html_text)  # remove stray parenthesis
    html_text = html_text.replace('\n', '<br>')
    return f'<p>{html_text}</p>'


def generate_html(primary_summary, rg_info, topic_summaries, output_path):
    today = datetime.date.today()
    sections = [
        "<h2>Primary</h2>" + markdown_to_html(primary_summary),
        "<h2>RG</h2>" + markdown_to_html(rg_info),
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
        'Summarize each entry in a bullet point and append [n](URL) at the end.'
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
        "Summarize today's rg papers in bullet points and append [n](URL) at the end of each line."
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
            f"Summarize today's {t} papers in bullet points and append [n](URL) at the end of each line."
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
