"""Tests for email sending functionality."""

import pytest
from paper_firehose.processors.emailer import EmailRenderer, SMTPSender


def test_html_to_text_conversion():
    """Test that HTML is properly converted to plain text."""
    sender = SMTPSender({'host': 'test.com', 'port': 465, 'username': 'test'})

    html_body = """
    <!DOCTYPE html>
    <html>
    <head><title>Test</title></head>
    <body>
      <h1>Main Title</h1>
      <h2>Section Title</h2>
      <p>This is a <a href="https://example.com">link</a> in text.</p>
      <style>body { color: red; }</style>
      <div>Multiple   spaces    should be   normalized.</div>
    </body>
    </html>
    """

    text = sender._html_to_text(html_body)

    # Check that HTML tags are removed
    assert '<html>' not in text
    assert '<body>' not in text
    assert '<p>' not in text

    # Check that links are converted to text (url) format
    assert 'link (https://example.com)' in text or 'link(https://example.com)' in text

    # Check that style tags are removed
    assert '<style>' not in text
    assert 'color: red' not in text

    # Check that headers are preserved with decoration
    assert 'Main Title' in text
    assert 'Section Title' in text

    # Check that multiple spaces are normalized
    assert '   ' not in text  # No triple spaces


def test_render_topic_digest():
    """Test that topic digest renders correctly."""
    renderer = EmailRenderer()

    entries = [
        {
            'title': 'Test Paper 1',
            'link': 'https://example.com/paper1',
            'authors': 'Smith, J.; Doe, A.',
            'published_date': '2025-01-12',
            'feed_name': 'Test Journal',
            'abstract': 'This is an abstract.',
            'summary': 'Summary text.',
            'rank_score': 0.85,
        },
        {
            'title': 'Test Paper 2',
            'link': 'https://example.com/paper2',
            'authors': 'Brown, B.',
            'published_date': '2025-01-11',
            'feed_name': 'Another Journal',
            'abstract': 'Another abstract.',
            'summary': '',
            'rank_score': 0.42,
        },
    ]

    html = renderer.render_topic_digest('Test Topic', entries)

    # Check that entries are present
    assert 'Test Paper 1' in html
    assert 'Test Paper 2' in html
    assert 'Smith, J.; Doe, A.' in html
    assert 'Brown, B.' in html

    # Check that scores are formatted
    assert 'Score 0.85' in html
    assert 'Score 0.42' in html

    # Check that links are present
    assert 'https://example.com/paper1' in html
    assert 'https://example.com/paper2' in html


def test_render_full_email():
    """Test that full email structure is created."""
    renderer = EmailRenderer()

    sections = [
        ('Topic 1', '<p>Content 1</p>'),
        ('Topic 2', '<p>Content 2</p>'),
    ]

    html = renderer.render_full_email('Daily Digest', sections)

    # Check structure
    assert '<!DOCTYPE html>' in html
    assert '<html>' in html
    assert '<head>' in html
    assert '<body>' in html

    # Check title and sections
    assert 'Daily Digest' in html
    assert 'Topic 1' in html
    assert 'Topic 2' in html
    assert 'Content 1' in html
    assert 'Content 2' in html

    # Check that CSS is inline (no external links)
    assert 'stylesheet' not in html.lower()
    assert 'link rel=' not in html.lower()


def test_sanitize_abstract_html():
    """Test HTML sanitization for abstracts."""
    renderer = EmailRenderer()

    # Test malicious content
    malicious = '<script>alert("xss")</script><p>Safe text</p>'
    result = renderer._sanitize_abstract_html(malicious)
    assert '<script>' not in result
    assert 'Safe text' in result

    # Test image preservation
    img_html = '<p>Text with <img src="https://example.com/img.png" alt="diagram"> image</p>'
    result = renderer._sanitize_abstract_html(img_html)
    assert '<img' in result
    assert 'src="https://example.com/img.png"' in result
    assert 'max-width:100%' in result  # Image sizing applied

    # Test link sanitization
    link_html = '<a href="https://example.com">Safe link</a><a href="javascript:alert()">Bad link</a>'
    result = renderer._sanitize_abstract_html(link_html)
    assert 'href="https://example.com"' in result
    assert 'javascript:' not in result
    assert 'Safe link' in result
