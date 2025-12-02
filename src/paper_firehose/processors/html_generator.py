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

CUSTOM_TEMPLATE_MARKER = "paper-firehose:custom-template"

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

        # Try to insert at CONTENT_PLACEHOLDER first, fallback to before </body>
        insert_position = html_content.find('<!-- CONTENT_PLACEHOLDER -->')
        if insert_position != -1:
            # Replace the placeholder
            updated_html = html_content.replace('<!-- CONTENT_PLACEHOLDER -->', '\n'.join(entries_html))
        else:
            # Fallback to old behavior (before </body>)
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

        entries = db_manager.get_current_entries(topic=topic_name)
        ranked = [e for e in entries if e.get('rank_score') is not None]
        ranked.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)

        html_parts: List[str] = []
        if not ranked:
            html_parts.append('<p class="no-entries">No ranked entries available.</p>')
        else:
            html_parts.append(f'<div class="entry-count" data-entry-counter="true">{len(ranked)} ranked entries (highest score first)</div>')
            for idx, entry in enumerate(ranked, 1):
                title = self.process_text(entry.get('title', 'No title'))
                link = entry.get('link', '#')
                authors = self.process_text(entry.get('authors', ''))
                published = entry.get('published_date', '')
                abstract_raw = entry.get('abstract', '')
                summary_raw = entry.get('summary', '')
                body_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                feed_name_entry = self.process_text(entry.get('feed_name', ''))
                score = float(entry.get('rank_score') or 0.0)
                score_trunc = int(score * 100) / 100.0
                score_str = f"{score_trunc:.2f}"

                # Optional image preview if present on the entry payload
                image_url = (
                    entry.get('image_url')
                    or entry.get('thumbnail')
                    or entry.get('thumbnail_url')
                    or entry.get('image')
                )
                image_html = ""
                if image_url:
                    image_safe = html.escape(str(image_url), quote=True)
                    image_html = (
                        f'<div class="entry-figure">'
                        f'<img src="{image_safe}" alt="Preview image for {title}">'
                        "</div>"
                    )

                entry_html = [
                    '<div class="entry" data-entry-type="ranked">',
                    '  <div class="entry-grid">',
                    '    <div class="entry-info">',
                    '      <div class="entry-title">',
                    f'        <h3><a href="{link}" target="_blank" rel="noopener noreferrer">{title}</a> <span class="badge">Score {score_str}</span></h3>',
                    '      </div>',
                    '      <div class="entry-meta">',
                    f'        <span class="meta-item"><strong>Authors:</strong> {authors}</span>',
                    f'        <span class="meta-item"><em>Published:</em> {published}</span>',
                    '      </div>',
                    '      <div class="entry-tags">',
                    f'        <span class="tag tag-feed">{feed_name_entry or "Unknown feed"}</span>',
                    '        <span class="tag tag-ranked">Ranked</span>',
                    '      </div>',
                    '      <div class="entry-actions">',
                    f'        <a class="action-link" href="{link}" target="_blank" rel="noopener noreferrer">Open article</a>',
                    '      </div>',
                    '    </div>',
                    '    <div class="entry-content">',
                    image_html,
                    '      <div class="summary-section ranked-summary">',
                    f'        <p>{body_text}</p>',
                    '      </div>',
                    '      <div class="entry-actions entry-actions--mobile">',
                    f'        <a class="action-link" href="{link}" target="_blank" rel="noopener noreferrer">Open article</a>',
                    '      </div>',
                    '    </div>',
                    '  </div>',
                    '</div>'
                ]

                html_parts.append('\n'.join(line for line in entry_html if line.strip() != ""))

        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        # Try to insert at CONTENT_PLACEHOLDER first, fallback to before </body>
        insert_position = html_content.find('<!-- CONTENT_PLACEHOLDER -->')
        if insert_position != -1:
            # Replace the placeholder
            updated_html = html_content.replace('<!-- CONTENT_PLACEHOLDER -->', '\n'.join(html_parts))
        else:
            # Fallback to old behavior
            insert_position = html_content.rfind('</body>')
            if insert_position == -1:
                insert_position = len(html_content)
            updated_html = html_content[:insert_position] + '\n'.join(html_parts) + html_content[insert_position:]
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(updated_html)
        logger.info(f"Generated ranked HTML file from database: {output_path}")

    def generate_pqa_summarized_html_from_database(self, db_manager, topic_name: str, output_path: str, title: str = None, description: str = None) -> None:
        """
        Generate an HTML file with all ranked entries for a specific topic.

        Entries with paper_qa_summary show the full PQA summary box.
        Entries without paper_qa_summary show just the abstract/summary (like ranked HTML).
        All entries are sorted by rank_score descending.
        """
        if title is None:
            title = f"PDF Summaries - {topic_name}"

        self._create_new_html_file(output_path, title, description)

        # Get all entries with rank scores (same as ranked HTML)
        entries = db_manager.get_current_entries(topic=topic_name)
        ranked_entries = [e for e in entries if e.get('rank_score') is not None]

        if not ranked_entries:
            html_parts = ['<p class="no-entries">No ranked entries available for this topic.</p>']
        else:
            # Sort by rank_score descending (same as ranked HTML)
            ranked_entries.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)

            # Count entries with summaries
            summarized_count = sum(1 for e in ranked_entries if e.get('paper_qa_summary') and e.get('paper_qa_summary').strip())

            html_parts = []
            html_parts.append(f'<div class="entry-count" data-entry-counter="true">{len(ranked_entries)} ranked entries (highest score first, {summarized_count} with PDF summaries)</div>')

            for idx, entry in enumerate(ranked_entries):
                title_text = self.process_text(entry.get('title', 'No title'))
                link = entry.get('link', '#')
                authors = self.process_text(entry.get('authors', ''))
                feed_name_entry = self.process_text(entry.get('feed_name', ''))
                published = entry.get('published_date', '')
                pqa_raw = entry.get('paper_qa_summary', '')
                abstract_raw = entry.get('abstract', '')
                summary_raw = entry.get('summary', '')
                rank_score = entry.get('rank_score')

                # Check if this entry has a PQA summary
                has_pqa_summary = pqa_raw and pqa_raw.strip()

                dropdown_id = f"abstract_{topic_name}_{idx}".replace(' ', '_').replace('-', '_')

                score_badge = ""
                if rank_score is not None:
                    score = float(rank_score)
                    score_trunc = int(score * 100) / 100.0
                    score_str = f"{score_trunc:.2f}"
                    score_badge = f' <span class="badge">Score {score_str}</span>'

                # Determine tag based on whether entry has PQA summary
                tag_label = 'PDF summary' if has_pqa_summary else 'Ranked'
                tag_class = 'tag-pqa' if has_pqa_summary else 'tag-ranked'

                # Build entry using same grid structure as ranked HTML
                entry_html = [
                    '<div class="entry" data-entry-type="summarized">',
                    '  <div class="entry-grid">',
                    '    <div class="entry-info">',
                    '      <div class="entry-title">',
                    f'        <h3><a href="{link}" target="_blank" rel="noopener noreferrer">{title_text}</a>{score_badge}</h3>',
                    '      </div>',
                    '      <div class="entry-meta">',
                    f'        <span class="meta-item"><strong>Authors:</strong> {authors}</span>',
                    f'        <span class="meta-item"><em>Published:</em> {published}</span>',
                    '      </div>',
                    '      <div class="entry-tags">',
                    f'        <span class="tag tag-feed">{feed_name_entry or "Unknown feed"}</span>',
                    f'        <span class="tag {tag_class}">{tag_label}</span>',
                    '      </div>',
                    '      <div class="entry-actions">',
                    f'        <a class="action-link" href="{link}" target="_blank" rel="noopener noreferrer">Open article</a>',
                    '      </div>',
                    '    </div>',
                    '    <div class="entry-content">',
                ]

                # Show PQA summary if available, otherwise show abstract/summary (like ranked HTML)
                if has_pqa_summary:
                    pqa_html = self._format_pqa_summary(pqa_raw)
                    entry_html.extend([
                        '      <div class="pqa-summary">',
                        '        <h4 class="pqa-heading">Fulltext summary</h4>',
                        f'        {pqa_html}',
                        '      </div>',
                    ])

                    # Add abstract toggle for entries with PQA summary
                    context_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                    if context_text:
                        entry_html.extend([
                            '      <div class="abstract-toggle">',
                            f'        <button class="toggle-button" onclick="toggleAbstract(\'{dropdown_id}\')">Show/Hide Original Abstract</button>',
                            '      </div>',
                            f'      <div id="{dropdown_id}" class="abstract-content">',
                            '        <strong>Original Abstract/Summary:</strong><br>',
                            f'        {context_text}',
                            '      </div>',
                        ])
                else:
                    # No PQA summary - show abstract/summary like in ranked HTML
                    body_text = self.process_text(abstract_raw if (abstract_raw and abstract_raw.strip()) else summary_raw)
                    entry_html.extend([
                        '      <div class="summary-section ranked-summary">',
                        f'        <p>{body_text}</p>',
                        '      </div>',
                    ])

                entry_html.extend([
                    '      <div class="entry-actions entry-actions--mobile">',
                    f'        <a class="action-link" href="{link}" target="_blank" rel="noopener noreferrer">Open article</a>',
                    '      </div>',
                    '    </div>',
                    '  </div>',
                    '</div>'
                ])

                html_parts.append('\n'.join(line for line in entry_html if line.strip() != ""))

        js_script = '''
<script>
function toggleAbstract(id) {
    var element = document.getElementById(id);
    element.classList.toggle('show');
}
</script>'''

        with open(output_path, 'r', encoding='utf-8') as f:
            html_content = f.read()

        # Try to insert at CONTENT_PLACEHOLDER first, fallback to before </body>
        content_to_insert = '\n'.join(html_parts) + js_script
        insert_position = html_content.find('<!-- CONTENT_PLACEHOLDER -->')
        if insert_position != -1:
            # Replace the placeholder
            updated_html = html_content.replace('<!-- CONTENT_PLACEHOLDER -->', content_to_insert)
        else:
            # Fallback to old behavior
            insert_position = html_content.rfind('</body>')
            if insert_position == -1:
                insert_position = len(html_content)
            updated_html = (
                html_content[:insert_position]
                + content_to_insert
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
        template_path = self._ensure_template_available(Path(self.template_path))

        with open(template_path, 'r', encoding='utf-8') as tmpl:
            template = tmpl.read()

        title = html.escape(title_text or "Filtered Articles")
        current_date = html.escape(str(datetime.date.today()))

        rendered = (
            template
            .replace("%{title}", title)
            .replace("%{date}", current_date)
            .replace("%{content}", "<!-- CONTENT_PLACEHOLDER -->")
        )

        if subtitle_text:
            sub = f"\n<p class=\"site-subtitle\">{html.escape(subtitle_text)}</p>\n"
            end_header = rendered.find('</header>')
            if end_header != -1:
                rendered = rendered[: end_header] + sub + rendered[end_header:]

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

    def _ensure_template_available(self, template_path: Path) -> Path:
        """
        Ensure a template is present in the runtime data directory.

        If a system template exists and the runtime copy differs, overwrite it unless the
        runtime template carries the custom-template marker comment. This keeps user
        customisations stable while still propagating updated built-in templates.
        """
        if template_path.is_absolute():
            if template_path.exists():
                return template_path
            # For non-existent absolute paths, fall back to basic template in data dir.
            return self._ensure_template_available(Path(template_path.name))

        data_template = resolve_data_path('templates', *template_path.parts)
        system_template = get_system_path('templates', *template_path.parts)

        if system_template.exists():
            data_template.parent.mkdir(parents=True, exist_ok=True)

            runtime_has_marker = False
            if data_template.exists():
                try:
                    runtime_contents = data_template.read_text(encoding='utf-8')
                    runtime_has_marker = CUSTOM_TEMPLATE_MARKER in runtime_contents
                except (OSError, UnicodeDecodeError):
                    runtime_has_marker = False

            if runtime_has_marker:
                logger.debug("Skipping template refresh for %s (custom marker present)", data_template)
                return data_template

            needs_copy = True
            if data_template.exists():
                try:
                    needs_copy = data_template.read_bytes() != system_template.read_bytes()
                except OSError:
                    needs_copy = True

            if needs_copy:
                shutil.copyfile(system_template, data_template)
                logger.info("Refreshed HTML template %s from system copy", data_template.name)

            return data_template

        if data_template.exists():
            return data_template

        self._create_basic_template(data_template)
        return data_template

    def _resolve_template(self, template_path: str) -> str:
        """Locate a template by checking runtime, system, and fallback locations."""
        candidate = Path(template_path)

        resolved = self._ensure_template_available(candidate)
        if resolved.exists():
            return str(resolved)

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
