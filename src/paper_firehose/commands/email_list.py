"""
Email list command: generate HTML digest from papers.db and send via SMTP.

Prefers to be run after: filter, rank, abstracts, summarize.
"""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path
from typing import Optional, List, Dict, Any

from ..core.config import ConfigManager
from ..core.database import DatabaseManager
from ..core.paths import resolve_data_path
from ..processors.emailer import EmailRenderer, SMTPSender

logger = logging.getLogger(__name__)


def _select_entries(db: DatabaseManager, topic: str, *, only_with_summary: bool, limit: Optional[int], min_rank_score: Optional[float]) -> List[Dict[str, Any]]:
    """Return ranked entries for a topic honoring summary/rank filters and limit."""
    entries = db.get_current_entries(topic=topic)
    # Optional rank cutoff
    if min_rank_score is not None:
        try:
            th = float(min_rank_score)
            entries = [e for e in entries if (e.get('rank_score') or 0.0) >= th]
        except Exception:
            pass
    # Sort by rank desc
    try:
        entries.sort(key=lambda e: (e.get('rank_score') or 0.0), reverse=True)
    except Exception:
        pass
    if limit is not None:
        entries = entries[:limit]
    return entries


def _resolve_email_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize email configuration and validate required SMTP fields."""
    email_cfg = (config.get('email') or {})
    # Provide minimal structure defaults
    email_cfg.setdefault('from', email_cfg.get('from_address'))
    email_cfg.setdefault('to', email_cfg.get('list_address'))
    smtp = email_cfg.get('smtp') or {}
    if not smtp:
        raise RuntimeError("Missing email.smtp configuration in the configuration file")
    required = ['host', 'port', 'username']
    for k in required:
        if not smtp.get(k):
            raise RuntimeError(f"email.smtp.{k} is required in the configuration file")
    if not (email_cfg.get('from') or '').strip():
        # default to username
        email_cfg['from'] = smtp.get('username')
    return email_cfg


def _extract_ranked_entries_from_file(file_path: str) -> Optional[str]:
    """Read a ranked HTML file and extract the entries section for embedding.

    Heuristic: find the substring starting at '<h2>Ranked Entries</h2>' until '</body>'.
    Falls back to the whole <body> inner HTML if the marker is not found.
    Returns None if the file does not exist or cannot be read.
    """
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            html = f.read()
        # Prefer the Ranked Entries section
        start_idx = html.find('<h2>Ranked Entries</h2>')
        if start_idx != -1:
            end_body = html.rfind('</body>')
            if end_body == -1:
                end_body = len(html)
            return html[start_idx:end_body].strip()
        # Fallback: inner body
        body_start = html.find('<body')
        if body_start != -1:
            # find first '>' after <body ...>
            first_gt = html.find('>', body_start)
            if first_gt != -1:
                body_end = html.rfind('</body>')
                if body_end == -1:
                    body_end = len(html)
                return html[first_gt + 1: body_end].strip()
        return html
    except Exception:
        return None


def run(
    config_path: str,
    topic: Optional[str] = None,
    *,
    mode: str = 'auto',  # 'ranked' | 'auto'
    limit: Optional[int] = None,
    dry_run: bool = False,
    recipients_file: Optional[str] = None,
) -> None:
    """Build HTML digest(s) and send via SMTP.

    Args:
        config_path: Path to main config file
        topic: Optional topic to send; if None, include all topics
        mode: Accepted for CLI compatibility; the implementation currently always renders
            ranked-style sections directly from the database regardless of value.
        limit: Optional per-topic limit of items
        dry_run: If True, do not send; write preview HTML under the runtime data directory
        recipients_file: Optional YAML file describing per-recipient overrides
    """
    cfg_mgr = ConfigManager(config_path)
    if not cfg_mgr.validate_config():
        logger.error("Configuration validation failed")
        return

    config = cfg_mgr.load_config()
    db = DatabaseManager(config)

    email_cfg = _resolve_email_settings(config)
    # Recipients file precedence: CLI flag -> config[email].recipients_file
    if not recipients_file:
        recipients_file = (config.get('email') or {}).get('recipients_file')

    if recipients_file:
        candidate = Path(str(recipients_file)).expanduser()
        if not candidate.is_absolute():
            candidate = (Path(cfg_mgr.base_dir) / candidate).resolve()
        recipients_file = str(candidate)
    to_addr = email_cfg['to']
    from_addr = email_cfg['from']
    smtp_sender = SMTPSender(email_cfg['smtp'], config_dir=cfg_mgr.base_dir)

    topics = [topic] if topic else cfg_mgr.get_available_topics()
    renderer = EmailRenderer()
    def build_sections(chosen_topics: List[str], *, mode_choice: str, rank_cutoff: Optional[float]) -> tuple[List[tuple[str, str]], int]:
        """Render ranked sections for the requested topics and return HTML fragments."""
        sections: List[tuple[str, str]] = []
        included_count = 0
        for t in chosen_topics:
            try:
                tcfg = cfg_mgr.load_topic_config(t)
            except Exception as e:
                logger.error("Failed to load topic '%s': %s", t, e)
                continue
            display_name = tcfg.get('name', t)
            # Always render from DB with rank cutoff; do not embed pre-generated HTML
            entries = _select_entries(db, t, only_with_summary=False, limit=limit, min_rank_score=rank_cutoff)
            included_count += len(entries)
            if entries:
                section_html = renderer.render_ranked_entries(display_name, entries, max_items=limit)
                sections.append((f"{display_name} — Ranked", section_html))
        return sections, included_count

    # If a recipients file is specified, send individualized emails
    if recipients_file and os.path.exists(recipients_file):
        import yaml
        with open(recipients_file, 'r', encoding='utf-8') as f:
            recipients_cfg = yaml.safe_load(f) or {}
        recipients = recipients_cfg.get('recipients') or []
        today = datetime.date.today().isoformat()
        subject_prefix = email_cfg.get('subject_prefix') or 'Paper Firehose'
        for rec in recipients:
            try:
                to_specific = (rec.get('to') or to_addr).strip()
                rec_topics = rec.get('topics') or topics
                # Intersect with available topics if CLI --topic is specified
                if topic:
                    rec_topics = [t for t in rec_topics if t == topic]
                rec_mode = rec.get('mode') or mode
                rec_limit = int(rec.get('limit')) if rec.get('limit') is not None else limit
                rec_cutoff = rec.get('min_rank_score')
                sections, included = build_sections(rec_topics, mode_choice=rec_mode, rank_cutoff=rec_cutoff)
                if included <= 0:
                    logger.info("No sections for recipient %s; skipping", to_specific)
                    continue
                if len(rec_topics) == 1:
                    subj = f"{subject_prefix}: {rec_topics[0]} — {today}"
                else:
                    subj = f"{subject_prefix}: Digest — {today}"
                html_body = renderer.render_full_email(subj, sections)
                if dry_run:
                    local = to_specific.split('@')[0]
                    out_path = resolve_data_path(f"email_preview_{local}_{today}.html")
                    with open(out_path, 'w', encoding='utf-8') as f:
                        f.write(html_body)
                    logger.info("Email dry-run: wrote preview for %s to %s", to_specific, out_path)
                    continue
                smtp_sender.send(subject=subj, from_addr=from_addr, to_addrs=[to_specific], html_body=html_body)
                logger.info("Email sent to %s", to_specific)
            except Exception as e:
                logger.error("Failed sending to %s: %s", rec.get('to'), e)
        db.close_all_connections()
        return

    # Otherwise, single email using global CLI/config settings
    sections, included = build_sections(topics, mode_choice=mode, rank_cutoff=None)
    subject_prefix = email_cfg.get('subject_prefix') or 'Paper Firehose'
    today = datetime.date.today().isoformat()
    subject = f"{subject_prefix}: {topic} — {today}" if topic else f"{subject_prefix}: Digest — {today}"
    if included <= 0:
        logger.info("Email: no entries to include; not sending")
        db.close_all_connections()
        return
    html_body = renderer.render_full_email(subject, sections)
    if dry_run:
        out_path = resolve_data_path(f"email_preview_{today}.html")
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(html_body)
        logger.info("Email dry-run: wrote preview to %s", out_path)
        db.close_all_connections()
        return
    try:
        smtp_sender.send(subject=subject, from_addr=from_addr, to_addrs=[to_addr], html_body=html_body)
        logger.info("Email sent to %s", to_addr)
    except Exception as e:
        logger.error("Failed to send email: %s", e)
        raise
    finally:
        db.close_all_connections()
