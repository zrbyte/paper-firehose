#!/usr/bin/env python3
"""
Paper Firehose CLI - Main entry point

Minimal but extensible paper filtering and ranking system.
"""

import click
import logging
import sys
import os

# Add src directory to Python path
src_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src')
sys.path.insert(0, src_path)

from commands import filter as filter_cmd
from commands import generate_html as html_cmd
from commands import rank as rank_cmd
from commands import abstracts as abstracts_cmd
from commands import summarize as summarize_cmd
from commands import pqa_summary as pqa_cmd
from commands import email_list as email_cmd
from commands import email_list as email_cmd

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@click.group()
@click.option('--config', default='config/config.yaml', help='Path to config file')
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
@click.pass_context
def cli(ctx, config, verbose):
    """Paper Firehose - RSS feed filtering and ranking for research papers"""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    ctx.ensure_object(dict)
    ctx.obj['config_path'] = config


@cli.command('filter')
@click.option('--topic', help='Filter specific topic only')
@click.pass_context
def filter_feeds(ctx, topic):
    """Fetch RSS feeds and filter entries by regex patterns"""
    try:
        filter_cmd.run(ctx.obj['config_path'], topic)
        click.echo(f"✅ Filter command completed successfully")
    except Exception as e:
        click.echo(f"❌ Filter command failed: {e}", err=True)
        sys.exit(1)


@cli.command('html')
@click.option('--topic', help='Generate HTML for a specific topic only')
@click.pass_context
def generate_html(ctx, topic):
    """Generate topic HTML(s) directly from papers.db (no fetching)."""
    try:
        html_cmd.run(ctx.obj['config_path'], topic)
        if topic:
            click.echo(f"✅ HTML generated for topic '{topic}'")
        else:
            click.echo("✅ HTML generated for all topics")
    except Exception as e:
        click.echo(f"❌ HTML generation failed: {e}", err=True)
        sys.exit(1)


@cli.command('rank')
@click.option('--topic', help='Rank a specific topic only')
@click.pass_context
def rank(ctx, topic):
    """Compute and write rank scores into papers.db (rank_score only)."""
    try:
        rank_cmd.run(ctx.obj['config_path'], topic)
        if topic:
            click.echo(f"✅ Ranking completed for topic '{topic}'")
        else:
            click.echo("✅ Ranking completed for all topics")
    except Exception as e:
        click.echo(f"❌ Rank command failed: {e}", err=True)
        sys.exit(1)


@cli.command('abstracts')
@click.option('--topic', help='Fetch abstracts for a specific topic only')
@click.option('--mailto', default=None, help='Contact email for Crossref User-Agent (defaults to $MAILTO env or a safe fallback)')
@click.option('--limit', type=int, help='Max number of abstracts to fetch per topic')
@click.option('--rps', type=float, default=1.0, help='Requests per second throttle (default: 1.0)')
@click.pass_context
def abstracts(ctx, topic, mailto, limit, rps):
    """Fetch abstracts from Crossref for high-ranked entries (writes to papers.db)."""
    try:
        abstracts_cmd.run(ctx.obj['config_path'], topic, mailto=mailto, max_per_topic=limit, rps=rps)
        if topic:
            click.echo(f"✅ Abstracts fetched for topic '{topic}'")
        else:
            click.echo("✅ Abstract fetching completed for eligible topics")
    except Exception as e:
        click.echo(f"❌ Abstract fetching failed: {e}", err=True)
        sys.exit(1)


@cli.command('summarize')
@click.option('--topic', help='Summarize a specific topic only')
@click.option('--rps', type=float, help='Requests per second throttle (overrides config)')
@click.pass_context
def summarize(ctx, topic, rps):
    """Run LLM summarization for top-ranked entries and write into llm_summary (always overwrites)."""
    try:
        summarize_cmd.run(ctx.obj['config_path'], topic, rps=rps)
        if topic:
            click.echo(f"✅ Summarization completed for topic '{topic}'")
        else:
            click.echo("✅ Summarization completed for eligible topics")
    except Exception as e:
        click.echo(f"❌ Summarization failed: {e}", err=True)
        sys.exit(1)


@cli.command('pqa_summary')
@click.option('--topic', help='Download arXiv PDFs for a specific topic only')
@click.option('--rps', type=float, help='Requests per second throttle (polite; overrides config)')
@click.option('--limit', type=int, help='Limit number of entries per topic (optional)')
@click.option('--arxiv', 'arxiv_ids', multiple=True, help='ArXiv IDs or URLs to download directly (skip DB selection). Can be specified multiple times.')
@click.option('--entry-id', 'entry_ids', multiple=True, help='Entry IDs to look up (prefer history DB for weekend testing). Multiple allowed.')
@click.option('--use-history', is_flag=True, help='Resolve --entry-id lookups against matched_entries_history.db (default)')
@click.option('--history-date', type=str, help='Restrict history lookup to matched_date (YYYY-MM-DD)')
@click.option('--history-feed-like', type=str, help='Restrict history lookup to feeds whose name contains this substring (case-insensitive), e.g., "cond-mat"')
@click.option('--summarize', is_flag=True, help='Run paper-qa summarization on the (archived) PDFs after download')
@click.pass_context
def pqa_summary(ctx, topic, rps, limit, arxiv_ids, entry_ids, use_history, history_date, history_feed_like, summarize):
    """Download arXiv PDFs for ranked entries or specific arXiv IDs/URLs (then archives)."""
    try:
        # Default to history when entry_ids are provided unless explicitly not requested
        effective_use_history = use_history or (len(entry_ids) > 0)
        # Gate summarization via env var to avoid widening the function signature further
        if summarize:
            os.environ['PAPERQA_SUMMARIZE'] = '1'
        pqa_cmd.run(
            ctx.obj['config_path'],
            topic,
            rps=rps,
            limit=limit,
            arxiv=list(arxiv_ids) or None,
            entry_ids=list(entry_ids) or None,
            use_history=effective_use_history,
            history_date=history_date,
            history_feed_like=history_feed_like,
        )
        if arxiv_ids:
            click.echo("✅ pqa_summary completed for provided arXiv IDs/URLs")
        elif entry_ids:
            click.echo("✅ pqa_summary completed for provided entry IDs via history lookup")
        elif topic:
            click.echo(f"✅ pqa_summary completed for topic '{topic}'")
        else:
            click.echo("✅ pqa_summary completed for all topics")
    except Exception as e:
        click.echo(f"❌ pqa_summary failed: {e}", err=True)
        sys.exit(1)

@cli.command('email')
@click.option('--topic', help='Send for a specific topic only (default: all topics)')
@click.option('--mode', type=click.Choice(['auto', 'ranked']), default='auto', help='Content mode: auto (from DB) or ranked (embed ranked HTML if available)')
@click.option('--limit', type=int, help='Limit number of entries per topic')
@click.option('--recipients', 'recipients_file', type=str, help='Path to recipients YAML (overrides config.email.recipients_file)')
@click.option('--dry-run', is_flag=True, help='Do not send; write preview HTML to assets/')
@click.pass_context
def email(ctx, topic, mode, limit, recipients_file, dry_run):
    """Send an HTML digest email generated from papers.db via SMTP."""
    try:
        email_cmd.run(ctx.obj['config_path'], topic, mode=mode, limit=limit, dry_run=dry_run, recipients_file=recipients_file)
        if dry_run:
            click.echo("📝 Email dry-run completed (preview written to assets/)")
        else:
            click.echo("✅ Email sent successfully")
    except Exception as e:
        click.echo(f"❌ Email send failed: {e}", err=True)
        sys.exit(1)


@cli.command('purge')
@click.option('--days', type=int, help='Remove entries from the most recent DAYS days (including today)')
@click.option('--all', 'all_data', is_flag=True, help='Clear all databases')
@click.pass_context
def purge(ctx, days, all_data):
    """Remove entries from databases based on publication date"""
    if not days and not all_data:
        click.echo("Error: Must specify either --days X or --all", err=True)
        sys.exit(1)
    
    try:
        filter_cmd.purge(ctx.obj['config_path'], days, all_data)
        if all_data:
            click.echo("✅ All data purged successfully")
        else:
            click.echo(f"✅ Entries from the most recent {days} days purged successfully")
    except Exception as e:
        click.echo(f"❌ Purge command failed: {e}", err=True)
        sys.exit(1)


@cli.command('status')
@click.pass_context
def status(ctx):
    """Show system status and configuration"""
    config_path = ctx.obj['config_path']
    
    # Check if config file exists
    if not os.path.exists(config_path):
        click.echo(f"❌ Config file not found: {config_path}", err=True)
        return
    
    click.echo(f"📄 Config file: {config_path}")
    
    try:
        from core.config import ConfigManager
        config_manager = ConfigManager(config_path)
        
        # Validate configuration
        if config_manager.validate_config():
            click.echo("✅ Configuration is valid")
        else:
            click.echo("❌ Configuration validation failed")
            return
        
        # Show available topics
        topics = config_manager.get_available_topics()
        click.echo(f"📚 Available topics: {', '.join(topics)}")
        
        # Show enabled feeds
        feeds = config_manager.get_enabled_feeds()
        click.echo(f"📡 Enabled feeds: {len(feeds)}")
        
        # Show database paths
        config = config_manager.load_config()
        db_config = config['database']
        click.echo(f"🗄️  Database paths:")
        click.echo(f"   Current run: {db_config['path']}")
        click.echo(f"   All feeds: {db_config['all_feeds_path']}")
        click.echo(f"   History: {db_config['history_path']}")
        
    except Exception as e:
        click.echo(f"❌ Error checking status: {e}", err=True)


if __name__ == '__main__':
    cli()
