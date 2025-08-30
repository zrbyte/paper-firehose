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


@cli.command('purge')
@click.option('--days', type=int, help='Remove entries older than DAYS days')
@click.option('--all', 'all_data', is_flag=True, help='Clear all databases')
@click.pass_context
def purge(ctx, days, all_data):
    """Remove old entries from databases"""
    if not days and not all_data:
        click.echo("Error: Must specify either --days X or --all", err=True)
        sys.exit(1)
    
    if all_data:
        if not click.confirm('This will delete all data. Are you sure?'):
            click.echo("Aborted.")
            return
    
    try:
        filter_cmd.purge(ctx.obj['config_path'], days, all_data)
        if all_data:
            click.echo("✅ All data purged successfully")
        else:
            click.echo(f"✅ Entries older than {days} days purged successfully")
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
