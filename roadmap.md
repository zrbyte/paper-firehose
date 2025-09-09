# Core Pipeline (Minimal Implementation)

**Bare bones functionality with extensible architecture:**

- **Filter feeds by regex**: CLI command `paper-firehose filter`
- **Rank by LLM**: CLI command `paper-firehose rank` 
- **Summarize top ranked**: CLI command `paper-firehose summarize`
- **Complete pipeline**: CLI command `paper-firehose run`

**Architecture principles:**
- Three SQLite databases for feed management and processing
- YAML-based configuration with topic-specific files
- Modular processors that can be extended
- Database-driven workflow enabling pause/resume
- Only new RSS entries are processed to avoid duplicate work

**Database strategy:**
- `all_feed_entries.db`: All RSS entries ever fetched (unfiltered) - for deduplication. Purge entries that are older than 4 months from the database.
- `matched_entries_history.db`: All entries that matched regex filters across all topics - for historical reference, never gets purged, accumulating.
- `papers.db`: Current run's processing data (filtered → ranked → summarized). Each runs database kept for future reference, with an altered filename, with a date.

# Database Schema

## all_feed_entries.db (All RSS entries for deduplication)
```sql
-- All RSS entries ever fetched from any feed
CREATE TABLE feed_entries (
    entry_id TEXT PRIMARY KEY,  -- SHA1 hash of id/link
    feed_name TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT,
    authors TEXT,
    published_date TEXT,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    raw_data TEXT,  -- JSON blob of original entry
    
    UNIQUE(feed_name, entry_id)
);
```

## matched_entries_history.db (Historical record of all matches)
```sql
-- All entries that have ever matched regex filters across all topics
CREATE TABLE matched_entries (
    entry_id TEXT NOT NULL,
    feed_name TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT,
    authors TEXT,
    published_date TEXT,
    matched_date TEXT DEFAULT (datetime('now')),
    raw_data TEXT,  -- JSON blob
    
    PRIMARY KEY (entry_id, feed_name, topic)
);
```

## papers.db (Current run processing data)
```sql
-- Current run's entries being processed
CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    feed_name TEXT NOT NULL,
    
    -- Content
    title TEXT NOT NULL,
    link TEXT NOT NULL,
    summary TEXT,
    authors TEXT,
    published_date TEXT,
    discovered_date TEXT DEFAULT (datetime('now')),
    
    -- Processing status
    status TEXT DEFAULT 'new' CHECK(status IN ('new', 'filtered', 'ranked', 'summarized')),
    
    -- Ranking (nullable until ranked)
    rank_score REAL,
    rank_reasoning TEXT,
    
    -- Summary (nullable until summarized)
    llm_summary TEXT,
    
    -- Raw data for extensibility
    raw_data TEXT,  -- JSON blob
    
    UNIQUE(feed_name, topic, id)
);
```

# Directory Structure

```
paper-firehose/
├── src/
│   ├── core/
│   │   ├── database.py          # SQLite operations
│   │   ├── config.py            # YAML config loading
│   │   └── models.py            # Data classes
│   ├── commands/
│   │   ├── filter.py            # Filter command
│   │   ├── rank.py              # Rank command
│   │   └── summarize.py         # Summarize command
│   └── processors/
│       ├── feed_processor.py    # RSS processing
│       ├── llm_ranker.py        # LLM ranking
│       └── llm_summarizer.py    # LLM summarization
├── config/
│   ├── config.yaml              # Main config
│   └── topics/
│       ├── primary.yaml
│       └── rg.yaml
├── cli/
│   └── main.py                  # CLI entry point
└── assets/
    ├── all_feed_entries.db     # All RSS entries (deduplication)
    ├── matched_entries_history.db  # Historical matches
    └── papers.db               # Current run processing
```

# Migration Path

- **Phase 1**: Create minimal CLI structure with `paper-firehose` command
- **Phase 2**: Implement basic filter → rank → summarize pipeline  
- **Phase 3**: Add YAML configuration system
- **Phase 4**: Migrate existing functionality to new structure
- **Phase 5**: Update GitHub Actions workflow (pages.yaml) for new structure
- **Phase 6**: Future features

# Configuration Examples

## config/config.yaml
```yaml
database:
  path: "assets/papers.db"

llm:
  model: "gpt-4o-mini"
  api_key_env: "OPENAI_API_KEY"
  
feeds:
  cond-mat:
    name: "arXiv Condensed Matter"
    url: "https://rss.arxiv.org/rss/cond-mat"
    enabled: true
  nature:
    name: "Nature"
    url: "https://www.nature.com/nature.rss"
    enabled: true

defaults:
  top_n_per_topic: 5
```

## config/topics/primary.yaml
```yaml
name: "Primary Research"
description: "Topological materials and condensed matter physics"

feeds:
  - "cond-mat"
  - "nature"

filter:
  pattern: "(topolog[a-z]+)|(graphit[a-z]+)|(weyl)|(dirac)"
  fields: ["title", "summary"]

ranking:
  prompt: "Rank papers by relevance to topological materials and quantum phenomena."
  top_n: 5

output:
  filename: "primary_summary.html"
```

# CLI Usage

```bash
# Run complete pipeline
paper-firehose run

# Run individual steps
paper-firehose filter --topic primary
paper-firehose rank --topic primary
paper-firehose summarize --topic primary

# Process specific topic only
paper-firehose run --topic primary

# Database management (for testing)
paper-firehose purge --days 30        # Remove entries older than 30 days
paper-firehose purge --all            # Clear all databases
```

# Processing Workflow

1. **Feed fetching**: All RSS entries stored in `all_feed_entries.db` for deduplication
2. **New entry detection**: Only entries not in `all_feed_entries.db` are processed
3. **Regex filtering**: New entries tested against topic patterns
4. **Historical storage**: Matched entries stored in `matched_entries_history.db` 
5. **Current run processing**: Matched entries copied to `papers.db` for ranking/summarization
6. **Preservation**: Historical databases never auto-purged, only via manual `purge` commands

# Future Development
- User profiles with individual YAML configs
- Web-based configuration tool
- LLM-assisted regex generation from keywords
- Extensible processor plugins

---

# Ranking Status (Sentence-Transformers)

Implemented
- CLI: `paper-firehose rank` writes cosine `rank_score` for entries with `status='filtered'` using `ranking.query` and `ranking.model` from topic YAMLs.
- Processor: minimal ST ranker (`src/processors/st_ranker.py`) with title-only scoring.
- DB: `update_entry_rank` helper in `src/core/database.py`.

Next
- Apply cutoffs: `ranking.score_cutoff` and `ranking.percentile_cutoff` (use the strongest; fewest entries kept).
- Support `ranking.negative_queries` to downweight off-topic senses.
- Optional: multi-query aggregation (max/mean), mark `top_n` as `status='ranked'`, cache embeddings.
