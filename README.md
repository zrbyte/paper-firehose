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

# Generate HTML for all topics from papers.db (no fetching)
python cli/main.py html

# Generate HTML for a single topic from papers.db
python cli/main.py html --topic primary

# Database cleanup
python cli/main.py purge --days 30     # Remove entries from last 30 days (including today)
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
python cli/main.py html [--topic TOPIC]

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
5. Regenerates topic HTML for all topics from `papers.db` (DB-first)

Example output: `results_primary.html` with 403 filtered entries

#### `status`
Shows system configuration and health:
- Configuration file validation
- Available topics and enabled feeds
- Database paths and status

#### `purge`
Database cleanup and management:
- Remove entries from the most recent N days (including today) based on publication date
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
- **Key Feature**: **Topic Merging** - If an entry matches multiple topics, all topics are stored in a single record (e.g., "primary, rg")

### 3. `papers.db` - Current Run Processing
- **Purpose**: Working database for current processing session
- **Contents**: Filtered entries with processing status (filtered → ranked → summarized)
- **Lifecycle**: Cleared at start of each run, populated during processing
- **Features**: Composite primary key `PRIMARY KEY(id, topic)` stores one row per (entry, topic) so entries that match multiple topics are tracked independently. Supports workflow tracking and pause/resume functionality.

### Entry ID Generation
- **Primary**: SHA-1 hash of cleaned URL (removes query parameters)  
- **Fallback**: SHA-1 hash of title + publication date combination
- **Ensures**: Stable IDs across feeds and consistent deduplication

### Topic Merging and Deduplication
Each unique entry is stored only once in `matched_entries_history.db`
Note: In `papers.db` (current-run DB), matching multiple topics creates separate rows (one per topic) to allow per-topic status, ranking, and summaries.

## Current Implementation Status

✅ **Phase 1 Complete** - Basic CLI and Filter Command
- Modular directory structure with extensible architecture
- YAML-based configuration system for feeds and topics  
- Three-database approach for deduplication and historical tracking
- RSS feed processing with regex filtering
- **Improved HTML Generation**: Database-first approach with standalone capability
- CLI interface with `filter`, `status`, and `purge` commands
- **Topic Merging**: Intelligent deduplication across multiple topics

🚧 **Phase 2** - LLM Ranking (Planned)
- LLM-based entry ranking and importance scoring
- Integration with existing topic-specific prompts
- Priority journal handling in ranking process

🚧 **Phase 3** - Enhanced Summarization (Planned)  
- LLM summarization of top-ranked entries
- Integration with topic-ranked HTML output
- Advanced PaperQA analysis for PDF processing

## HTML Generation

The system now features an improved HTML generation system that works directly from the database:

### **Database-First Approach**
- **Direct Database Access**: HTML is generated directly from `papers.db` content
- **Standalone Capability**: Can generate HTML without running the full filter pipeline
- **Consistent Data Source**: Always reflects the current state of the database
- **Efficient Processing**: No need to re-parse RSS data for HTML generation

### **Usage Examples**
```python
# Generate HTML from current database state
pf.generate_html(topic="primary")

# Standalone HTML generation
from processors.html_generator import HTMLGenerator
html_gen = HTMLGenerator()
html_gen.generate_html_for_topic_from_database(db_manager, topic_name, output_path)
```

CLI usage:

```bash
# All topics
python cli/main.py html

# Specific topic
python cli/main.py html --topic primary
```

The `filter` command also regenerates HTML for all topics at the end of a run using the same DB-backed pathway to ensure consistency.

Feed headers in DB-backed HTML use the `feed_name` (e.g., `prl`). If you prefer human-readable titles, we can map them from `config.yaml`.

## Schema Change & Migration

As of this version, `papers.db` uses a composite primary key `PRIMARY KEY(id, topic)` so an entry can exist once per topic. A safe, automatic migration runs on startup:

- Detects the legacy schema (`id` as the sole primary key), creates a new table with the composite key, copies rows, drops the old table, and recreates the index.
- No manual action required; existing data are preserved.


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
#### `html`
Generate topic HTML directly from `papers.db` without fetching or filtering. Useful after a previous filter run or when only presentation needs updating.

```bash
# All topics
python cli/main.py html

# Single topic
python cli/main.py html --topic testtopic2
```

Uses entries with `status='filtered'` scoped to each topic.

```python
import paper_firehose as pf

# Inspect configuration status
pf.status()

# Run filtering for a specific topic (defaults to config/config.yaml)
pf.filter(topic="primary")

# Generate HTML for a topic directly from papers.db
pf.generate_html(topic="primary")

# Purge entries from the last N days (based on publication date)
# pf.purge(days=30)

# Purge all data (CAUTION)
# pf.purge(all_data=True)
```

Notes:
- Ensure you run Python from the repository root (so the package can locate `src/` and `config/`).
- Alternatively, add the repository root to `PYTHONPATH` before importing.
- **Testing Efficiency**: The new topic merging system eliminates duplicate database operations, making testing and rerunning much more efficient.
# Local backups (automatic)
# Up to 3 timestamped backups of the two important databases are written to assets/ at run start.
# Examples:
#   assets/all_feed_entries.20250906-164500.backup.db
#   assets/matched_entries_history.20250906-164500.backup.db
# The oldest backups beyond 3 are pruned automatically.
## Backups & CI Archiving

### Local Backups (Implemented)
- On each `filter` run (and before `purge`), the app writes timestamped backups of the two important databases into `assets/` and keeps only the latest 3 per database:
  - `assets/all_feed_entries.YYYYMMDD-HHMMSS.backup.db`
  - `assets/matched_entries_history.YYYYMMDD-HHMMSS.backup.db`
- This safeguards against accidental corruption during a run while avoiding unbounded growth.

### GitHub Actions Archiving (TODO)
- For GitHub Actions runs, persist `assets/all_feed_entries.db` and `assets/matched_entries_history.db` to a `data` branch so history and dedup state survive between CI runs.
- TODO:
  - Add a workflow that checks out the `data` branch, copies updated DBs from `assets/`, commits, and pushes.
  - Gate writes to `data` branch to CI context only.
  - Consider attaching DB artifacts to releases for manual recovery.
