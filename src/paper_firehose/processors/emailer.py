"""
Email rendering and sending utilities for Paper Firehose.

Generates a simple, email-friendly HTML digest from papers.db and sends it via
SMTP (SSL) based on configuration stored in the runtime data directory
(`config/config.yaml` under the path resolved by PAPER_FIREHOSE_DATA_DIR).

Uses only the Python standard library.
"""

from __future__ import annotations

from typing import Dict, Any, List, Optional, Tuple
import datetime
import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path


def _fmt_score_badge(score: Optional[float]) -> str:
    """Render a small inline badge showing the rank score, or empty string on failure."""
    if score is None:
        return ""
    try:
        s = float(score)
        trunc = int(s * 100) / 100.0
        return f'<span style="background:#eef;border:1px solid #99c;border-radius:6px;padding:2px 6px;margin-left:6px;font-size:12px;color:#224;">Score {trunc:.2f}</span>'
    except Exception:
        return ""


class EmailRenderer:
    """Create compact HTML suitable for email clients (no external JS/CSS)."""

    def __init__(self) -> None:
        """Construct the renderer; currently stateless but kept for symmetry."""
        pass

    def render_topic_digest(
        self,
        topic_display_name: str,
        entries: List[Dict[str, Any]],
        *,
        max_items: Optional[int] = None,
    ) -> str:
        """Return HTML body for a single topic.

        Entries expected to contain keys: title, link, authors, published_date,
        feed_name, abstract, summary, rank_score.
        """
        today = datetime.date.today().isoformat()

        # Sort by rank desc if scores present
        sorted_entries = list(entries)
        try:
            sorted_entries.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)
        except Exception:
            pass
        if max_items is not None:
            sorted_entries = sorted_entries[: max_items]

        parts: List[str] = []
        parts.append(
            f"<h2 style=\"margin:16px 0 8px;\">{html.escape(topic_display_name)} — {today}</h2>"
        )
        if not sorted_entries:
            parts.append('<p style="font-style:italic;color:#555;">No entries.</p>')
            return "\n".join(parts)

        for e in sorted_entries:
            title = html.escape((e.get('title') or '').strip() or 'No title')
            link = (e.get('link') or '#').strip()
            authors = html.escape((e.get('authors') or '').strip())
            published = html.escape((e.get('published_date') or '').strip())
            feed_name = html.escape((e.get('feed_name') or '').strip())
            score_badge = _fmt_score_badge(e.get('rank_score'))

            # pick content: abstract -> summary
            body = (e.get('abstract') or '').strip() or (e.get('summary') or '').strip()
            content_html = html.escape(body) if body else '<em>No abstract/summary.</em>'

            parts.append(
                f"""
<div style=\"margin:12px 0 18px;\">\n
  <div style=\"font-size:16px;line-height:1.35;\">\n
    <a href=\"{link}\" target=\"_blank\" style=\"color:#18457a;text-decoration:none;\">{title}</a>
    {score_badge}
  </div>\n
  <div style=\"color:#333;margin:6px 0;\"><strong>Authors:</strong> {authors}</div>\n
  <div style=\"color:#333;margin:6px 0;\">{content_html}</div>\n
  <div style=\"color:#666;font-size:12px;\"><strong>{feed_name}</strong> — <em>Published: {published}</em></div>\n
</div>
"""
            )
        return "\n".join(parts)

    # --- HTML sanitization for abstracts (allow <img>) ---
    def _sanitize_abstract_html(self, html_text: str) -> str:
        """Return a sanitized HTML string suitable for email, preserving <img>.

        - Allows a small whitelist of tags: b,strong,i,em,u,sub,sup,br,p,ul,ol,li,span,a,img
        - For <a>, only http/https href; adds rel and target
        - For <img>, only http/https src; forces style max-width:100%; height:auto
        - Escapes all text and disallowed tags/attributes
        """
        if not html_text or ('<' not in html_text and '>' not in html_text):
            # No tags likely present; escape and return
            return html.escape(html_text or '')

        allowed_tags = {
            'b', 'strong', 'i', 'em', 'u', 'sub', 'sup', 'br', 'p', 'ul', 'ol', 'li', 'span', 'a', 'img'
        }
        allowed_attrs = {
            'a': {'href'},
            'img': {'src', 'alt', 'width', 'height'},
            'span': {'style'},
            'p': {'style'},
        }

        def is_http_url(url: str) -> bool:
            """Return True when the URL is an http(s) link; reject mailto/javascript/etc."""
            u = (url or '').strip().lower()
            return u.startswith('http://') or u.startswith('https://')

        out: list[str] = []
        skip_stack: list[str] = []
        skip_tags = {'cite', 'footer'}  # drop content fully inside these

        class Sanitizer(HTMLParser):
            def handle_starttag(self, tag, attrs):
                """Emit sanitized start tags or replace with safe alternatives."""
                # If entering a skip-only tag, push and ignore until endtag
                if tag in skip_tags:
                    skip_stack.append(tag)
                    return
                if tag not in allowed_tags:
                    return
                if tag == 'a':
                    href = ''
                    for k, v in attrs:
                        if k == 'href' and is_http_url(v):
                            href = html.escape(v, quote=True)
                            break
                    if href:
                        out.append(f'<a href="{href}" target="_blank" rel="noopener noreferrer">')
                    else:
                        out.append('<span>')
                elif tag == 'img':
                    src = ''
                    alt = ''
                    width = ''
                    height = ''
                    for k, v in attrs:
                        if k == 'src' and is_http_url(v):
                            src = html.escape(v, quote=True)
                        elif k == 'alt':
                            alt = html.escape(v or '', quote=True)
                        elif k == 'width':
                            width = html.escape(v or '', quote=True)
                        elif k == 'height':
                            height = html.escape(v or '', quote=True)
                    if src:
                        style = 'max-width:100%;height:auto;'
                        dim = ''
                        if width:
                            dim += f' width="{width}"'
                        if height:
                            dim += f' height="{height}"'
                        out.append(f'<img src="{src}" alt="{alt}" style="{style}"{dim}>')
                else:
                    # Generic allowed tag; filter attrs to allowed ones, escape values
                    attrs_map = {k: v for k, v in attrs if k in allowed_attrs.get(tag, set())}
                    attr_str = ''.join([f' {k}="{html.escape(v or "", quote=True)}"' for k, v in attrs_map.items()])
                    out.append(f'<{tag}{attr_str}>')

            def handle_endtag(self, tag):
                """Emit matching end tags for allowed elements, respecting replacements."""
                if skip_stack and tag == skip_stack[-1]:
                    skip_stack.pop()
                    return
                if tag not in allowed_tags:
                    return
                # If we replaced <a> with <span>, close span here gracefully; it's okay to emit </a> or </span>
                if tag == 'a':
                    out.append('</a>')
                elif tag in ('img', 'br'):
                    # already self-closed or no close tag required
                    return
                else:
                    out.append(f'</{tag}>')

            def handle_data(self, data):
                """Append escaped text content, dropping boilerplate like DOI references."""
                # Skip data if we're inside a skipped tag
                if skip_stack:
                    return
                # Drop common publisher footer lines like DOI
                d = data.strip()
                if not d:
                    return
                low = d.lower()
                if low.startswith('doi:') or low.startswith('https://doi.org'):
                    return
                out.append(html.escape(data))

            def handle_entityref(self, name):
                """Preserve HTML entity references such as &alpha;."""
                out.append(f'&{name};')

            def handle_charref(self, name):
                """Preserve numeric character references such as &#8217;."""
                out.append(f'&#{name};')

        try:
            Sanitizer().feed(html_text)
            return ''.join(out)
        except Exception:
            # On any parse error, escape whole content
            return html.escape(html_text)

    def render_full_email(
        self,
        title: str,
        sections: List[Tuple[str, str]],
    ) -> str:
        """Return a complete HTML email with a title and named sections.

        sections: list of (section_title, section_html)
        """
        safe_title = html.escape(title)
        # Basic, inline CSS only; avoid external assets for maximum deliverability.
        head = f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset=\"UTF-8\">
  <title>{safe_title}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 12px 16px; color: #111; }}
    h1 {{ color: #153e75; font-size: 22px; margin: 0 0 12px; }}
    h2 {{ color: #1e5aa8; font-size: 18px; margin: 16px 0 8px; }}
    a  {{ color: #18457a; }}
    hr {{ border: none; border-top: 1px solid #ddd; margin: 12px 0; }}
  </style>
</head>
<body>
  <h1>{safe_title}</h1>
"""
        body_parts: List[str] = [head]
        for sec_title, sec_html in sections:
            body_parts.append(f"<h2>{html.escape(sec_title)}</h2>")
            body_parts.append(sec_html)
            body_parts.append("<hr>")
        body_parts.append("</body></html>")
        return "\n".join(body_parts)

    def _format_pqa_summary(self, pqa_raw: Optional[str]) -> Optional[str]:
        """Format paper_qa_summary JSON for email.

        Returns a compact HTML block with Summary and Methods.
        Falls back to plain escaped text if not JSON.
        """
        if not pqa_raw:
            return None
        try:
            import json
            data = json.loads(pqa_raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            summary = html.escape(data.get('summary') or '')
            methods = html.escape(data.get('methods') or '')
            parts: List[str] = []
            if summary:
                parts.append(f"<div><strong>Summary:</strong> {summary}</div>")
            if methods:
                parts.append(f"<div><strong>Methods:</strong> {methods}</div>")
            return "\n".join(parts) if parts else None
        except Exception:
            # Fallback to plain text
            return html.escape(pqa_raw)

    def render_ranked_entries(
        self,
        topic_display_name: str,
        entries: List[Dict[str, Any]],
        *,
        max_items: Optional[int] = None,
    ) -> str:
        """Render a ranked-style section for email with minimal, inline CSS.

        Entry layout:
        - Title (link) with Score badge
        - Authors
        - Feed name
        - Abstract if present; otherwise summary if available
        """
        # Defensive copy and ordering by score desc
        items = list(entries)
        try:
            items.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)
        except Exception:
            pass
        if max_items is not None:
            items = items[: max_items]

        parts: List[str] = []
        # Do not include a section header here; the caller provides the header.
        if not items:
            return ""

        for e in items:
            title = html.escape((e.get('title') or '').strip() or 'No title')
            link = (e.get('link') or '#').strip()
            authors = html.escape((e.get('authors') or '').strip())
            feed_name = html.escape((e.get('feed_name') or '').strip())
            score_badge = _fmt_score_badge(e.get('rank_score'))
            abstract_raw = (e.get('abstract') or '').strip()
            summary_raw = (e.get('summary') or '').strip()
            content_src = abstract_raw or summary_raw
            if content_src:
                body_text = self._sanitize_abstract_html(content_src)
            else:
                body_text = '<em>No abstract/summary.</em>'
            pqa_block = self._format_pqa_summary(e.get('paper_qa_summary'))
            if pqa_block:
                pqa_html = (
                    '<div style="background:#fff8d5;border-left:4px solid #d4b106;padding:8px 10px;margin:8px 0;">\n'
                    '<div style="font-weight:bold;color:#8a6d3b;margin-bottom:4px;">Fulltext summary</div>'
                    f"{pqa_block}"
                    '</div>'
                )
            else:
                pqa_html = ''

            parts.append(
                f"""
<div style=\"margin:12px 0 18px;\">\n
  <div style=\"font-size:16px;line-height:1.35;\">\n
    <a href=\"{link}\" target=\"_blank\" style=\"color:#18457a;text-decoration:none;\">{title}</a>
    {score_badge}
  </div>\n
  <div style=\"color:#333;margin:6px 0;\"><strong>Authors:</strong> {authors}</div>\n
  <div style=\"color:#333;margin:6px 0;\"><strong>{feed_name}</strong></div>\n
  <div style=\"color:#333;margin:6px 0;\">{body_text}</div>\n
  {pqa_html}

</div>
"""
            )

        return "\n".join(parts)


class SMTPSender:
    """Send emails via SMTP (SSL) using settings under config['email']['smtp']."""

    def __init__(self, smtp_cfg: Dict[str, Any], config_dir: Optional[str] = None) -> None:
        """Initialize SMTP connection parameters and optional password lookup directory."""
        self.host = str(smtp_cfg.get('host') or '')
        self.port = int(smtp_cfg.get('port') or 465)
        self.username = str(smtp_cfg.get('username') or '')
        self.password = str(smtp_cfg.get('password') or '')  # discouraged; prefer file
        self.password_file = smtp_cfg.get('password_file')
        self._config_dir = Path(config_dir).expanduser().resolve() if config_dir else None

    def _load_password(self) -> str:
        """Fetch SMTP password via inline config, password file, or environment fallback."""
        if self.password:
            return self.password
        if self.password_file:
            candidate = Path(str(self.password_file)).expanduser()
            if not candidate.is_absolute() and self._config_dir:
                candidate = (self._config_dir / candidate).resolve()
            if os.path.exists(candidate):
                with open(candidate, 'r', encoding='utf-8') as f:
                    return f.read().strip()
        # Last resort: env var based on username
        env_name = 'SMTP_PASSWORD'
        return os.environ.get(env_name, '')

    def send(self, *, subject: str, from_addr: str, to_addrs: List[str], html_body: str, text_body: Optional[str] = None) -> None:
        """Send a multipart email with HTML alternative using SMTP over SSL."""
        if not self.host or not self.port or not self.username:
            raise RuntimeError("SMTP configuration incomplete: host/port/username required")
        password = self._load_password()
        if not password:
            raise RuntimeError("SMTP password not found. Set email.smtp.password_file or email.smtp.password in config.")

        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = from_addr
        msg['To'] = ", ".join(to_addrs)
        msg.set_content(text_body or "HTML email; open in an HTML-capable client.")
        msg.add_alternative(html_body, subtype='html')

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.host, self.port, context=context) as server:
            server.login(self.username, password)
            server.send_message(msg)
