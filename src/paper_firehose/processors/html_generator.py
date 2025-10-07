"""
HTML output generation for filtered articles.
Based on the original feedfilter.py HTML generation logic.
"""

import html
import datetime
import logging
import shutil
from pathlib import Path
from string import Template
from typing import Dict, List, Any, Optional

from ..core.paths import get_system_path, resolve_data_path

logger = logging.getLogger(__name__)


class HTMLGenerator:
    """Generates HTML output files for filtered articles."""
    
    def __init__(self, template_path: str = "html_template.html"):
        """Prepare the generator, resolving the template path into the data directory."""
        self.template_path = self._resolve_template(template_path)
    
    def process_text(self, text: str) -> str:
        """Process text to escape HTML characters and handle LaTeX code."""
        if not text:
            return ''
        
        # Escape HTML characters
        text = html.escape(text, quote=False)
        
        # Unescape LaTeX-related characters to preserve LaTeX code
        text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
        
        # Replace double backslashes with single backslash
        text = text.replace('\\\\', '\\')
        
        # Ensure dollar signs are not escaped
        text = text.replace('&#36;', '$')
        
        return text
    
    def generate_html_from_database(self, db_manager, topic_name: str, output_path: str, heading: str = None, description: str = None) -> None:
        """
        Generate an HTML file for filtered entries pulled directly from papers.db.

        Args:
            db_manager: Database manager instance
            topic_name: Name of the topic
            output_path: Path to the output HTML file
            description: Optional subheading text to include beneath the page title
        """
        # Always create a fresh HTML file for each run
        self._create_new_html_file(output_path, heading or topic_name, description)
        
        # Get entries from papers.db for this topic
        entries = db_manager.get_current_entries(topic=topic_name, status='filtered')
        
        # Organize entries by feed
        entries_per_feed = {}
        for entry in entries:
            feed_name = entry.get('feed_name', 'unknown')
            if feed_name not in entries_per_feed:
                entries_per_feed[feed_name] = []
            entries_per_feed[feed_name].append(entry)
        
        # Generate HTML for entries
        entries_html = self._generate_entries_html_from_db(entries_per_feed)
        
        # Read the template file we just created
        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Find insertion point (before </body>)
        insert_position = html_content.rfind('</body>')
        if insert_position == -1:
            insert_position = len(html_content)
        
        # Insert entries content
        updated_html = (
            html_content[:insert_position]
            + '\n'.join(entries_html)
            + html_content[insert_position:]
        )
        
        # Write the complete content
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(updated_html)
        
        logger.info(f"Generated fresh HTML file from database: {output_path}")

    def generate_ranked_html_from_database(self, db_manager, topic_name: str, output_path: str, heading: str = None, description: str = None) -> None:
        """
        Generate an HTML file with entries sorted by descending rank_score for a topic.

        Displays the rank score truncated to two decimals next to each entry.
        """
        display_title = heading or f"Ranked Articles - {topic_name}"
        self._create_new_html_file(output_path, display_title, description)

        # Load entries from DB and sort by rank_score desc (only those with a score)
        entries = db_manager.get_current_entries(topic=topic_name)
        ranked = [e for e in entries if e.get('rank_score') is not None]
        ranked.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)

        # Build ranked entries HTML
        html_parts: List[str] = []
        if not ranked:
            html_parts.append('<p class="no-entries">No ranked entries available.</p>')
        else:
            html_parts.append('<h2>Ranked Entries</h2>')
            for idx, e in enumerate(ranked, 1):
                title = self.process_text(e.get('title', 'No title'))
                link = e.get('link', '#')
                authors = self.process_text(e.get('authors', ''))
                published = e.get('published_date', '')
                abstract_raw = e.get('abstract', '')
                summary_raw = e.get('summary', '')
                body_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                feed_name_entry = self.process_text(e.get('feed_name', ''))
                score = float(e.get('rank_score') or 0.0)
                # Truncate to two decimals (not round)
                score_trunc = int(score * 100) / 100.0
                score_str = f"{score_trunc:.2f}"
                entry_html = (
                    '<div class="entry">\n'
                    f'  <h3><a href="{link}">{title}</a> <span class="badge">Score {score_str}</span></h3>\n'
                    f'  <p><strong>Authors:</strong> {authors}</p>\n'
                    f'  <p><em>Published: {published}</em></p>\n'
                    f'  <p>{body_text}</p>\n'
                    f'  <p><strong>{feed_name_entry}</strong></p>\n'
                )
                entry_html += '</div>\n<hr>'
                html_parts.append(entry_html)

        # Insert into template
        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        insert_position = html_content.rfind('</body>')
        if insert_position == -1:
            insert_position = len(html_content)
        updated_html = html_content[:insert_position] + '\n'.join(html_parts) + html_content[insert_position:]
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(updated_html)
        logger.info(f"Generated ranked HTML file from database: {output_path}")
    
    def generate_summarized_html_from_database(self, db_manager, topic_name: str, output_path: str, title: str = None) -> None:
        """
        Generate an HTML file with entries that have either paper_qa_summary or llm_summary for a topic.

        Preference: display paper_qa_summary when available; otherwise fall back to llm_summary.
        Shows entry title (as link), authors, summary block, and original abstract in dropdown.
        """
        if title is None:
            title = f"LLM Summaries - {topic_name}"
        
        self._create_new_html_file(output_path, title)
        
        # Load entries for this topic; prefer those with rank_score to match ranked order
        entries = db_manager.get_current_entries(topic=topic_name)
        ranked_entries = [e for e in entries if e.get('rank_score') is not None]
        # If nothing has rank_score, fall back to all entries
        base_entries = ranked_entries if ranked_entries else entries

        if not base_entries:
            html_parts = ['<p class="no-entries">No entries available for this topic.</p>']
        else:
            # Sort entries by rank_score in descending order (highest scores first)
            base_entries.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)

            html_parts = []
            html_parts.append(f'<div class="entry-count">{len(base_entries)} summarized entries (sorted by rank score)</div>')

            # Entries for this topic
            for idx, entry in enumerate(base_entries):
                title_text = self.process_text(entry.get('title', 'No title'))
                link = entry.get('link', '#')
                authors = self.process_text(entry.get('authors', ''))
                feed_name_entry = self.process_text(entry.get('feed_name', ''))
                published = entry.get('published_date', '')
                # Prefer PQA summary; fall back to LLM summary
                pqa_summary_raw = entry.get('paper_qa_summary', '')
                llm_summary_raw = entry.get('llm_summary', '')
                abstract_raw = entry.get('abstract', '')
                summary_raw = entry.get('summary', '')
                # For dropdown context (when AI summary exists), prefer abstract; else fall back to summary
                context_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                # For no-AI-summary fallback block, include only the abstract (never the summary)
                abstract_text_only = self.process_text(abstract_raw) if (abstract_raw and str(abstract_raw).strip()) else ''
                rank_score = entry.get('rank_score')
                
                # Create unique ID for dropdown
                dropdown_id = f"abstract_{topic_name}_{idx}".replace(' ', '_').replace('-', '_')
                
                # Format rank score if available
                score_badge = ""
                if rank_score is not None:
                    score = float(rank_score)
                    # Truncate to two decimals (not round)
                    score_trunc = int(score * 100) / 100.0
                    score_str = f"{score_trunc:.2f}"
                    score_badge = f' <span class="badge">Score {score_str}</span>'
                
                # Build summary HTML per entry:
                # 1) Prefer PQA summary
                # 2) Else use LLM summary
                # 3) Else fall back to ranked fields (abstract -> summary)
                used_fallback = False
                if (pqa_summary_raw or '').strip():
                    summary_block_html = f'''<div class="pqa-summary">
        <h4 class="pqa-heading">Fulltext summary</h4>
        {self._format_pqa_summary(pqa_summary_raw)}
    </div>'''
                elif (llm_summary_raw or '').strip():
                    # Parse LLM summary JSON if possible, otherwise display as plain text
                    summary_block_html = f'''<div class="llm-summary">
        {self._format_llm_summary(llm_summary_raw)}
    </div>'''
                else:
                    used_fallback = True
                    # Only show the abstract from DB; do not use the RSS summary
                    fallback_text = abstract_text_only if abstract_text_only else 'No abstract available.'
                    summary_block_html = f'''<div class="llm-summary">
        <p>{fallback_text}</p>
    </div>'''
                
                entry_html = f'''
<div class="entry">
    <div class="entry-title">
        <h3><a href="{link}" target="_blank">{title_text}</a>{score_badge}</h3>
    </div>
    <div class="entry-authors"><strong>Authors:</strong> {authors}</div>
    <div class="entry-feed"><strong>{feed_name_entry}</strong></div>
    <div class="entry-published"><em>Published:</em> {published}</div>
    {summary_block_html}'''
                
                # Only show the dropdown if we are not already showing the context as the main block
                if context_text and not used_fallback:
                    # Feed name shown below the abstract/summary inside the dropdown
                    feed_name_entry = self.process_text(entry.get('feed_name', ''))
                    entry_html += f'''
    <div class="abstract-toggle">
        <button onclick="toggleAbstract('{dropdown_id}')">Show/Hide Original Abstract</button>
    </div>
    <div id="{dropdown_id}" class="abstract-content">
        <strong>Original Abstract/Summary:</strong><br>
        {context_text}
        <div class="entry-feed"><strong>{feed_name_entry}</strong></div>
    </div>'''
                
                entry_html += '</div>'
                html_parts.append(entry_html)
        
        # Add JavaScript for dropdown functionality
        js_script = '''
<script>
function toggleAbstract(id) {
    var element = document.getElementById(id);
    element.classList.toggle('show');
}
</script>'''
        
        # Insert content into template
        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Find insertion point (before </body>)
        insert_position = html_content.rfind('</body>')
        if insert_position == -1:
            insert_position = len(html_content)
        
        # Insert entries content and JavaScript
        updated_html = (
            html_content[:insert_position]
            + '\n'.join(html_parts)
            + js_script
            + html_content[insert_position:]
        )
        
        # Write the complete content
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(updated_html)
        
        logger.info(f"Generated summarized HTML file for topic '{topic_name}': {output_path}")
    
    def generate_pqa_summarized_html_from_database(self, db_manager, topic_name: str, output_path: str, title: str = None) -> None:
        """
        Generate an HTML file with entries that have paper_qa_summary for a specific topic.

        Uses a yellow-highlighted box and a "Fulltext summary" heading, and
        renders Summary and Methods sections from the JSON payload.
        """
        if title is None:
            title = f"PDF Summaries - {topic_name}"

        self._create_new_html_file(output_path, title)

        entries = db_manager.get_current_entries(topic=topic_name)
        summarized_entries = [e for e in entries if e.get('paper_qa_summary') and e.get('paper_qa_summary').strip()]

        if not summarized_entries:
            html_parts = ['<p class="no-entries">No PDF-based summaries available for this topic.</p>']
        else:
            summarized_entries.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)

            html_parts = []
            html_parts.append(f'<div class="entry-count">{len(summarized_entries)} summarized entries (sorted by rank score)</div>')

            for idx, entry in enumerate(summarized_entries):
                title_text = self.process_text(entry.get('title', 'No title'))
                link = entry.get('link', '#')
                authors = self.process_text(entry.get('authors', ''))
                feed_name_entry = self.process_text(entry.get('feed_name', ''))
                published = entry.get('published_date', '')
                pqa_raw = entry.get('paper_qa_summary', '')
                abstract_raw = entry.get('abstract', '')
                summary_raw = entry.get('summary', '')
                context_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                rank_score = entry.get('rank_score')

                dropdown_id = f"abstract_{topic_name}_{idx}".replace(' ', '_').replace('-', '_')

                score_badge = ""
                if rank_score is not None:
                    score = float(rank_score)
                    score_trunc = int(score * 100) / 100.0
                    score_str = f"{score_trunc:.2f}"
                    score_badge = f' <span class="badge">Score {score_str}</span>'

                pqa_html = self._format_pqa_summary(pqa_raw)

                entry_html = f'''
<div class="entry">
    <div class="entry-title">
        <h3><a href="{link}" target="_blank">{title_text}</a>{score_badge}</h3>
    </div>
    <div class="entry-authors"><strong>Authors:</strong> {authors}</div>
    <div class="entry-feed"><strong>{feed_name_entry}</strong></div>
    <div class="entry-published"><em>Published:</em> {published}</div>
    <div class="pqa-summary">
        <h4 class="pqa-heading">Fulltext summary</h4>
        {pqa_html}
    </div>'''

                if context_text:
                    feed_name_entry = self.process_text(entry.get('feed_name', ''))
                    entry_html += f'''
    <div class="abstract-toggle">
        <button onclick="toggleAbstract('{dropdown_id}')">Show/Hide Original Abstract</button>
    </div>
    <div id="{dropdown_id}" class="abstract-content">
        <strong>Original Abstract/Summary:</strong><br>
        {context_text}
        <div class="entry-feed"><strong>{feed_name_entry}</strong></div>
    </div>'''

                entry_html += '</div>'
                html_parts.append(entry_html)

        js_script = '''
<script>
function toggleAbstract(id) {
    var element = document.getElementById(id);
    element.classList.toggle('show');
}
</script>'''

        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        insert_position = html_content.rfind('</body>')
        if insert_position == -1:
            insert_position = len(html_content)

        updated_html = (
            html_content[:insert_position]
            + '\n'.join(html_parts)
            + js_script
            + html_content[insert_position:]
        )

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(updated_html)

        logger.info(f"Generated PQA summarized HTML file for topic '{topic_name}': {output_path}")

    
    def _format_llm_summary(self, llm_summary_raw: str) -> str:
        """
        Parse LLM summary JSON and format it with proper subheadings.
        Falls back to plain text if JSON parsing fails.
        """
        if not llm_summary_raw:
            return '<p class="no-summary">No summary available.</p>'
        
        try:
            import json
            # Try to parse as JSON
            summary_data = json.loads(llm_summary_raw)
            
            # Extract fields with fallbacks
            summary_text = summary_data.get('summary', 'No summary provided')
            topical_relevance = summary_data.get('topical_relevance', 'No relevance assessment provided')
            novelty_impact = summary_data.get('novelty_impact', 'No impact assessment provided')
            
            # Process text to escape HTML
            summary_text = self.process_text(summary_text)
            topical_relevance = self.process_text(topical_relevance)
            novelty_impact = self.process_text(novelty_impact)
            
            # Format with subheadings
            return f'''
        <div class="summary-section">
            <h4>Summary of abstract</h4>
            <p>{summary_text}</p>
        </div>
        <div class="summary-section">
            <h4>Topical Relevance</h4>
            <p>{topical_relevance}</p>
        </div>
        <div class="summary-section">
            <h4>Novelty & Impact</h4>
            <p>{novelty_impact}</p>
        </div>'''
            
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            # Debug: log the error and first 200 chars of the raw text
            logger.debug(f"JSON parsing failed: {e}. Raw text (first 200 chars): {llm_summary_raw[:200]}")
            # Fall back to plain text if JSON parsing fails
            processed_text = self.process_text(llm_summary_raw)
            return f'<p><strong>LLM Summary:</strong><br>{processed_text}</p>'

    def _format_pqa_summary(self, pqa_raw: str) -> str:
        """Parse paper_qa_summary JSON and format Summary/Methods sections.

        Falls back to plain text if JSON parsing fails.
        """
        if not pqa_raw:
            return '<p class="no-summary">No summary available.</p>'

        try:
            import json
            data = json.loads(pqa_raw)
            if not isinstance(data, dict):
                raise ValueError('Not a JSON object')

            summary_raw = data.get('summary')
            methods_raw = data.get('methods')

            summary_text = self.process_text(summary_raw if (summary_raw is not None and str(summary_raw).strip()) else 'No summary provided')
            methods_text = self.process_text(methods_raw if (methods_raw is not None and str(methods_raw).strip()) else 'No methods provided')

            sections = [
                f'''
        <div class="summary-section">
            <h4>Summary</h4>
            <p>{summary_text}</p>
        </div>'''
            ]

            if methods_text:
                sections.append(
                    f'''
        <div class="summary-section">
            <h4>Methods</h4>
            <p>{methods_text}</p>
        </div>'''
                )

            return ''.join(sections)
        except Exception as e:
            logger.debug(f"PQA JSON parsing failed: {e}. Raw text (first 200 chars): {pqa_raw[:200]}")
            processed_text = self.process_text(pqa_raw)
            return f'<p><strong>PDF Summary:</strong><br>{processed_text}</p>'
    
    # Note: legacy `generate_html` method removed; the system now renders
    # exclusively from papers.db via `generate_html_from_database`.
    
    def _create_new_html_file(self, output_path: str, title_text: str, subtitle_text: str = None) -> None:
        """Create a new HTML file using the template."""
        template_path = Path(self.template_path)
        if not template_path.exists():
            template_path = self._ensure_template_available(template_path.name)

        class PercentTemplate(Template):
            delimiter = '%'

        with open(template_path, 'r', encoding='utf-8') as tmpl:
            template = PercentTemplate(tmpl.read())

        title = title_text or "Filtered Articles"
        current_date = datetime.date.today()
        rendered = template.substitute(title=html.escape(title), date=current_date, content="")

        if subtitle_text:
            sub = f"\n<h2>{html.escape(subtitle_text)}</h2>\n"
            end_h1 = rendered.find('</h1>')
            if end_h1 != -1:
                rendered = rendered[: end_h1 + 5] + sub + rendered[end_h1 + 5 :]

        output_path_obj = Path(output_path)
        if output_path_obj.parent:
            output_path_obj.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path_obj, 'w', encoding='utf-8') as f:
            f.write(rendered)

    def _create_basic_template(self, target: Optional[Path] = None) -> None:
        """Create a basic HTML template if none exists."""
        basic_template = (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "<head>\n"
            "<meta charset=\"UTF-8\">\n"
            "<title>%{title}</title>\n"
            "<script type=\"text/javascript\">\n"
            "  MathJax = {\n"
            "    tex: {\n"
            "      inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],\n"
            "      displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],\n"
            "      processEscapes: true\n"
            "    }\n"
            "  };\n"
            "</script>\n"
            "<script type=\"text/javascript\" id=\"MathJax-script\" async\n"
            "  src=\"https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js\">\n"
            "</script>\n"
            "<style>\n"
            "    body { font-family: Arial, sans-serif; margin: 20px; }\n"
            "    .entry { margin-bottom: 20px; }\n"
            "    h2 { color: #2E8B57; }\n"
            "    h3 { color: #4682B4; }\n"
            "    hr { border: 0; border-top: 1px solid #ccc; }\n"
            "    .no-entries { font-style: italic; color: #555; }\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            "<h1>%{title}</h1>\n"
            "<h1>New papers on %{date}</h1>\n"
            "<hr>\n"
            "%{content}\n"
            "</body>\n"
            "</html>\n"
        )

        target_path = target or Path(self.template_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, 'w', encoding='utf-8') as f:
            f.write(basic_template)

    def _ensure_template_available(self, template_name: str) -> Path:
        """Ensure a template is present in the runtime data directory."""
        data_template = resolve_data_path('templates', template_name)
        if data_template.exists():
            return data_template

        system_template = get_system_path('templates', template_name)
        if system_template.exists():
            data_template.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(system_template, data_template)
            return data_template

        self._create_basic_template(data_template)
        return data_template

    def _resolve_template(self, template_path: str) -> str:
        """Locate a template by checking runtime, system, and fallback locations."""
        candidate = Path(template_path)

        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

        data_candidate = resolve_data_path('templates', *candidate.parts)
        if data_candidate.exists():
            return str(data_candidate)

        system_candidate = get_system_path('templates', *candidate.parts)
        if system_candidate.exists():
            data_candidate.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(system_candidate, data_candidate)
            return str(data_candidate)

        if candidate.exists():
            return str(candidate)

        fallback = resolve_data_path('templates', candidate.name)
        self._create_basic_template(fallback)
        return str(fallback)
    
    def _generate_entries_html_from_db(self, entries_per_feed: Dict[str, List[Dict[str, Any]]]) -> List[str]:
        """Generate HTML content for database entries organized by feed."""
        html_parts = []
        
        FEED_HEADER = Template('<h2>Feed: $title</h2>')
        ENTRY_TEMPLATE = Template(
            '<div class="entry">\n'
            '  <h3><a href="$link">$title</a></h3>\n'
            '  <p><strong>Authors:</strong> $authors</p>\n'
            '  <p><em>Published: $published</em></p>\n'
            '  <p>$body_text</p>\n'
            '  <p><strong>$feed_name</strong></p>\n'
            '</div>\n<hr>'
        )
        
        # Check if there are any entries
        has_entries = any(entries for entries in entries_per_feed.values())
        
        if not has_entries:
            html_parts.append('<p class="no-entries">No new entries found.</p>')
        else:
            for feed_name, entries in entries_per_feed.items():
                if not entries:
                    continue
                
                # Add feed header
                html_parts.append(FEED_HEADER.substitute(title=html.escape(feed_name)))
                
                # Add entries for this feed
                for entry in entries:
                    title = self.process_text(entry.get('title', 'No title'))
                    link = entry.get('link', '#')
                    published = entry.get('published_date', 'No published date')
                    abstract_raw = entry.get('abstract', '')
                    summary_raw = entry.get('summary', '')
                    # Show abstract if present; otherwise fall back to summary
                    body_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw or 'No summary')
                    authors = self.process_text(entry.get('authors', 'No author'))
                    feed_name_entry = self.process_text(entry.get('feed_name', ''))
                    
                    context = {
                        'link': link,
                        'title': title,
                        'authors': authors,
                        'published': published,
                        'body_text': body_text,
                        'feed_name': feed_name_entry,
                    }
                    html_parts.append(ENTRY_TEMPLATE.substitute(context))
        
        return html_parts
    
    # Note: legacy `_generate_entries_html` removed with the legacy path.
    
    # Note: legacy `generate_topic_html` removed; callers should load from DB
    # or use `generate_html_for_topic_from_database`.
    
    def generate_html_for_topic_from_database(self, db_manager, topic_name: str, output_path: str, topic_description: str = None) -> None:
        """
        Standalone method to generate HTML for a topic directly from papers.db.
        This method can be called independently without going through the filter command.
        
        Args:
            db_manager: Database manager instance
            topic_name: Name of the topic
            output_path: Path to output HTML file
            topic_description: Description for the topic
        """
        self.generate_html_from_database(db_manager, topic_name, output_path, topic_description)
