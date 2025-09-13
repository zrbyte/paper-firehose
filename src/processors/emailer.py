"""
Email rendering and sending utilities for Paper Firehose.

Generates a simple, email-friendly HTML digest from papers.db and sends it via
SMTP (SSL) based on configuration in config/config.yaml under `email`.

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


def _fmt_score_badge(score: Optional[float]) -> str:
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
        pass

    def render_topic_digest(
        self,
        topic_display_name: str,
        entries: List[Dict[str, Any]],
        *,
        prefer_llm_summary: bool = True,
        max_items: Optional[int] = None,
    ) -> str:
        """Return HTML body for a single topic.

        Entries expected to contain keys: title, link, authors, published_date,
        feed_name, abstract, summary, llm_summary, rank_score.
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

            # pick content: llm_summary (JSON or text) -> abstract -> summary
            content_html = self._format_llm_summary(e.get('llm_summary')) if prefer_llm_summary else None
            if not content_html:
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

    def _format_llm_summary(self, llm_summary_raw: Optional[str]) -> Optional[str]:
        if not llm_summary_raw:
            return None
        try:
            import json
            data = json.loads(llm_summary_raw)
            summary = html.escape(data.get('summary') or '')
            topical = html.escape(data.get('topical_relevance') or '')
            novelty = html.escape(data.get('novelty_impact') or '')
            bits: List[str] = []
            if summary:
                bits.append(f"<div><strong>Summary:</strong> {summary}</div>")
            if topical:
                bits.append(f"<div><strong>Topical Relevance:</strong> {topical}</div>")
            if novelty:
                bits.append(f"<div><strong>Novelty & Impact:</strong> {novelty}</div>")
            return "\n".join(bits) if bits else None
        except Exception:
            # Fallback to plain text
            return html.escape(llm_summary_raw)

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
            body_text = html.escape(abstract_raw if abstract_raw else summary_raw)

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
</div>
"""
            )

        return "\n".join(parts)


class SMTPSender:
    """Send emails via SMTP (SSL) using settings under config['email']['smtp']."""

    def __init__(self, smtp_cfg: Dict[str, Any]) -> None:
        self.host = str(smtp_cfg.get('host') or '')
        self.port = int(smtp_cfg.get('port') or 465)
        self.username = str(smtp_cfg.get('username') or '')
        self.password = str(smtp_cfg.get('password') or '')  # discouraged; prefer file
        self.password_file = smtp_cfg.get('password_file')

    def _load_password(self) -> str:
        if self.password:
            return self.password
        if self.password_file:
            path = str(self.password_file)
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read().strip()
        # Last resort: env var based on username
        env_name = 'SMTP_PASSWORD'
        return os.environ.get(env_name, '')

    def send(self, *, subject: str, from_addr: str, to_addrs: List[str], html_body: str, text_body: Optional[str] = None) -> None:
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
