# Paper Firehose - RSS Feed Filtering and Ranking

A modular, CLI-based system for fetching research papers from RSS feeds, filtering them using regular expressions, and generating organized HTML summaries. Features a three-database architecture for efficient deduplication and historical tracking.

## Architecture

- **Modular Design**: Extensible processor-based architecture
- **CLI Interface**: Simple commands for filtering, ranking, and summarization
- **YAML Configuration**: Topic-specific configurations with regex patterns
- **Three-Database System**: Efficient deduplication and historical tracking
- **RSS Feed Processing**: Supports 33+ academic journal feeds

## Installation

Developed with **Python 3.11**. Install dependencies:

```bash
pip install -r requirements.txt
```

Key dependencies:
- [feedparser](https://pypi.org/project/feedparser/) - RSS parsing
- [PyYAML](https://pyyaml.org/) - Configuration management  
- [click](https://click.palletsprojects.com/) - CLI framework

## Quick Start

```bash
# Check system status
python cli/main.py status

# Filter all topics
python cli/main.py filter

# Filter specific topic
python cli/main.py filter --topic primary

# Database cleanup
python cli/main.py purge --days 30
```

## Configuration

### Main Configuration (`config/config.yaml`)

```yaml
database:
  path: "assets/papers.db"
  all_feeds_path: "assets/all_feed_entries.db" 
  history_path: "assets/matched_entries_history.db"

feeds:
  cond-mat:
    name: "arXiv Condensed Matter"
    url: "https://rss.arxiv.org/rss/cond-mat"
    enabled: true
  nature:
    name: "Nature"
    url: "https://www.nature.com/nature.rss"
    enabled: true

priority_journals:
  - "nature"
  - "science"
  - "nat-mat"
```

### Topic Configuration (`config/topics/primary.yaml`)

```yaml
name: "Primary Research"
description: "Topological materials and condensed matter physics"

feeds:
  - "cond-mat"
  - "nature"
  - "science"

filter:
  pattern: "(topolog[a-z]+)|(graphit[a-z]+)|(weyl)|(dirac)"
  fields: ["title", "summary"]

output:
  filename: "results_primary.html"
```

## CLI Commands

### Core Commands

```bash
# Check system status and configuration
python cli/main.py status

# Filter RSS feeds and apply regex patterns
python cli/main.py filter [--topic TOPIC]

# Database management
python cli/main.py purge --days 30     # Remove entries older than 30 days
python cli/main.py purge --all         # Clear all databases
```

### Command Details

#### `filter`
Fetches RSS feeds, applies regex filters, and generates HTML output:
1. Fetches new entries from configured RSS feeds
2. Applies topic-specific regex patterns to titles and summaries
3. Includes entries from priority journals regardless of regex match
4. Stores filtered entries in three databases for efficient processing
5. Generates HTML output files organized by feed

Example output: `results_primary.html` with 403 filtered entries

#### `status`
Shows system configuration and health:
- Configuration file validation
- Available topics and enabled feeds
- Database paths and status

#### `purge`
Database cleanup and management:
- Remove entries older than specified days
- Complete database reset for testing
- Maintains deduplication efficiency


## GitHub Actions and Pages

To add...

## Database Architecture

The system uses a three-database approach for efficient processing and historical tracking:

### 1. `all_feed_entries.db` - Deduplication Database
- **Purpose**: Tracks all RSS entries ever fetched to prevent reprocessing
- **Contents**: Entry ID, feed name, title, link, authors, publication date
- **Retention**: Entries older than 4 months are automatically purged
- **Key Feature**: Only new entries (not in this database) are processed

### 2. `matched_entries_history.db` - Historical Matches
- **Purpose**: Permanent record of all entries that matched regex filters
- **Contents**: Entry metadata for all matched entries across all topics and runs
- **Retention**: Never automatically purged - accumulates all matches for historical analysis
- **Use Case**: Research trends, pattern analysis, long-term statistics

### 3. `papers.db` - Current Run Processing
- **Purpose**: Working database for current processing session
- **Contents**: Filtered entries with processing status (filtered → ranked → summarized)
- **Lifecycle**: Cleared at start of each run, populated during processing
- **Features**: Supports workflow tracking and pause/resume functionality

### Entry ID Generation
- **Primary**: SHA-1 hash of cleaned URL (removes query parameters)  
- **Fallback**: SHA-1 hash of title + publication date combination
- **Ensures**: Stable IDs across feeds and consistent deduplication

## Current Implementation Status

✅ **Phase 1 Complete** - Basic CLI and Filter Command
- Modular directory structure with extensible architecture
- YAML-based configuration system for feeds and topics  
- Three-database approach for deduplication and historical tracking
- RSS feed processing with regex filtering
- HTML output generation with proper LaTeX support
- CLI interface with `filter`, `status`, and `purge` commands

🚧 **Phase 2** - LLM Ranking (Planned)
- LLM-based entry ranking and importance scoring
- Integration with existing topic-specific prompts
- Priority journal handling in ranking process

🚧 **Phase 3** - Enhanced Summarization (Planned)  
- LLM summarization of top-ranked entries
- Integration with topic-ranked HTML output
- Advanced PaperQA analysis for PDF processing

## Future Development

- **Two-Stage PaperQA Ranking**: Enhanced paper analysis using full PDF content
- **User Profiles**: Individual YAML configurations for different research groups
- **Web Interface**: Configuration management and result browsing
- **API Layer**: REST interface for programmatic access
- **Enhanced Analytics**: Trend analysis and research pattern detection

## Legacy Support

The previous monolithic scripts are preserved in the `old/` directory:
- `old/rssparser.py` - Original entry point  
- `old/feedfilter.py` - Feed processing logic
- `old/llmsummary.py` - LLM summarization
- `old/*.json` - Original JSON configurations

These remain functional for backward compatibility during the transition period.

## Python API (Jupyter/Programmatic)

You can use the core functionality directly from Python (e.g., in notebooks):

```python
import paper_firehose as pf

# Inspect configuration status
pf.status()

# Run filtering for a specific topic (defaults to config/config.yaml)
pf.filter(topic="primary")

# Purge all data (CAUTION)
# pf.purge(all_data=True)
```

Notes:
- Ensure you run Python from the repository root (so the package can locate `src/` and `config/`).
- Alternatively, add the repository root to `PYTHONPATH` before importing.
