"""
HTML output generation for filtered articles.
Based on the original feedfilter.py HTML generation logic.
"""

import os
import html
import datetime
from string import Template
from typing import Dict, List, Any, Optional
import logging

logger = logging.getLogger(__name__)


class HTMLGenerator:
    """Generates HTML output files for filtered articles."""
    
    def __init__(self, template_path: str = "html_template.html"):
        self.template_path = template_path
    
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
    
    def generate_html_from_database(self, db_manager, topic_name: str, output_path: str, topic_description: str = None) -> None:
        """
        Generate HTML file for filtered articles directly from papers.db.
        
        Args:
            db_manager: Database manager instance
            topic_name: Name of the topic
            output_path: Path to output HTML file
            topic_description: Description for the topic
        """
        # Always create a fresh HTML file for each run
        self._create_new_html_file(output_path, topic_name, topic_description)
        
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
    
    def generate_html(self, entries_per_feed: Dict[str, List[Dict[str, Any]]], 
                     output_path: str, topic_name: str, topic_description: str = None) -> None:
        """
        Generate HTML file for filtered articles (legacy method for backward compatibility).
        
        Args:
            entries_per_feed: Dict mapping feed names to entry lists
            output_path: Path to output HTML file
            topic_name: Name of the topic
            topic_description: Description for the topic
        """
        # Always create a fresh HTML file for each run
        self._create_new_html_file(output_path, topic_name, topic_description)
        
        # Generate HTML for entries
        entries_html = self._generate_entries_html(entries_per_feed)
        
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
        
        logger.info(f"Generated fresh HTML file: {output_path}")
    
    def _create_new_html_file(self, output_path: str, topic_name: str, topic_description: str = None) -> None:
        """Create a new HTML file using the template."""
        if not os.path.exists(self.template_path):
            # Create a basic template if the original doesn't exist
            self._create_basic_template()
        
        class PercentTemplate(Template):
            delimiter = '%'
        
        with open(self.template_path, 'r', encoding='utf-8') as tmpl:
            template = PercentTemplate(tmpl.read())
        
        title = topic_description or f"Filtered Articles - {topic_name}"
        current_date = datetime.date.today()
        rendered = template.substitute(
            title=html.escape(title),
            date=current_date,
            content="",
        )
        
        # Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir:  # Only create directory if path contains a directory component
            os.makedirs(output_dir, exist_ok=True)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(rendered)
    
    def _create_basic_template(self) -> None:
        """Create a basic HTML template if none exists."""
        basic_template = '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>%{title}</title>
<script type="text/javascript">
  MathJax = {
    tex: {
      inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
      displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
      processEscapes: true
    }
  };
</script>
<script type="text/javascript" id="MathJax-script" async
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js">
</script>
<style>
    body { font-family: Arial, sans-serif; margin: 20px; }
    .entry { margin-bottom: 20px; }
    h2 { color: #2E8B57; }
    h3 { color: #4682B4; }
    hr { border: 0; border-top: 1px solid #ccc; }
    .no-entries { font-style: italic; color: #555; }
</style>
</head>
<body>
<h1>%{title}</h1>
<h1>New papers on %{date}</h1>
<hr>
%{content}
</body>
</html>'''
        
        with open(self.template_path, 'w', encoding='utf-8') as f:
            f.write(basic_template)
    
    def _generate_entries_html_from_db(self, entries_per_feed: Dict[str, List[Dict[str, Any]]]) -> List[str]:
        """Generate HTML content for database entries organized by feed."""
        html_parts = []
        
        FEED_HEADER = Template('<h2>Feed: $title</h2>')
        ENTRY_TEMPLATE = Template(
            '<div class="entry">\n'
            '  <h3><a href="$link">$title</a></h3>\n'
            '  <p><strong>Authors:</strong> $authors</p>\n'
            '  <p><em>Published: $published</em></p>\n'
            '  <p>$summary</p>\n'
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
                    summary = self.process_text(entry.get('summary', 'No summary'))
                    authors = self.process_text(entry.get('authors', 'No author'))
                    
                    context = {
                        'link': link,
                        'title': title,
                        'authors': authors,
                        'published': published,
                        'summary': summary,
                    }
                    html_parts.append(ENTRY_TEMPLATE.substitute(context))
        
        return html_parts
    
    def _generate_entries_html(self, entries_per_feed: Dict[str, List[Dict[str, Any]]]) -> List[str]:
        """Generate HTML content for RSS feed entries organized by feed (legacy method)."""
        html_parts = []
        
        FEED_HEADER = Template('<h2>Feed: $title</h2>')
        ENTRY_TEMPLATE = Template(
            '<div class="entry">\n'
            '  <h3><a href="$link">$title</a></h3>\n'
            '  <p><strong>Authors:</strong> $authors</p>\n'
            '  <p><em>Published: $published</em></p>\n'
            '  <p>$summary</p>\n'
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
                feed_title = entries[0].get('feed_title', feed_name) if entries else feed_name
                html_parts.append(FEED_HEADER.substitute(title=html.escape(feed_title)))
                
                # Add entries for this feed
                for entry in entries:
                    title = self.process_text(entry.get('title', 'No title'))
                    link = entry.get('link', '#')
                    published = entry.get('published', entry.get('updated', 'No published date'))
                    summary = self.process_text(entry.get('summary', entry.get('description', 'No summary')))
                    
                    # Extract authors
                    authors = entry.get('authors', [])
                    if authors:
                        author_names = ', '.join(author.get('name', '') for author in authors)
                    else:
                        author_names = entry.get('author', 'No author')
                    author_names = self.process_text(author_names)
                    
                    context = {
                        'link': link,
                        'title': title,
                        'authors': author_names,
                        'published': published,
                        'summary': summary,
                    }
                    html_parts.append(ENTRY_TEMPLATE.substitute(context))
        
        return html_parts
    
    def generate_topic_html(self, topic_name: str, entries: List[Dict[str, Any]], 
                           output_path: str, topic_config: Dict[str, Any] = None) -> None:
        """
        Generate HTML for a specific topic with entries organized by feed (legacy method).
        
        Args:
            topic_name: Name of the topic
            entries: List of entries for this topic
            output_path: Path to output HTML file
            topic_config: Topic configuration dictionary
        """
        # Organize entries by feed
        entries_per_feed = {}
        for entry in entries:
            feed_name = entry.get('feed_name', 'unknown')
            if feed_name not in entries_per_feed:
                entries_per_feed[feed_name] = []
            entries_per_feed[feed_name].append(entry)
        
        # Get topic description
        description = None
        if topic_config:
            description = topic_config.get('description', f"Articles related to {topic_name}")
        else:
            description = f"Articles related to {topic_name}"
        
        # Generate HTML
        self.generate_html(entries_per_feed, output_path, topic_name, description)
    
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
