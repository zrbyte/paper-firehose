# Core Pipeline (Minimal Implementation)

**Bare bones functionality with extensible architecture:**

- **Filter feeds by regex**: CLI command `paper-firehose filter`
- **Rank by LLM**: CLI command `paper-firehose rank` 
- **Summarize top ranked**: CLI command `paper-firehose summarize`
- **Complete pipeline**: CLI command `paper-firehose run`

**Architecture principles:**
- Single SQLite database for all data (`assets/papers.db`)
- YAML-based configuration with topic-specific files
- Modular processors that can be extended
- Database-driven workflow enabling pause/resume

# Minimal Database Schema

```sql
-- Core entries table - minimal but extensible
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

-- Simple deduplication
CREATE TABLE seen_entries (
    feed_name TEXT,
    topic TEXT,
    entry_id TEXT,
    title TEXT,
    link TEXT,
    first_seen TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (feed_name, topic, entry_id)
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
    └── papers.db               # SQLite database
```

# Migration Path

- **Phase 1**: Create minimal CLI structure with `paper-firehose` command
- **Phase 2**: Implement basic filter → rank → summarize pipeline
- **Phase 3**: Add YAML configuration system
- **Phase 4**: Migrate existing functionality to new structure
- **Phase 5**: Add extensibility points for future features

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
```

# Future Development
- User profiles with individual YAML configs
- Web-based configuration tool
- LLM-assisted regex generation from keywords
- Extensible processor plugins
