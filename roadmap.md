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

# Ranking Plan (Title-Only, Regex-First)

Objective
- Implement a ranking command that reads `assets/papers.db` (status='filtered'), ranks entries using title-only signals centered on topic regex patterns, writes `rank_score` and `rank_reasoning`, and optionally marks top-N as `status='ranked'` for LLM summarization.

Out-of-the-box packages (optional)
- sentence-transformers: Dual-encoder embeddings and cross-encoders for hybrid ranking later (e.g., `all-MiniLM-L6-v2`, `intfloat/e5-small-v2`, `BAAI/bge-small-en`, `cross-encoder/ms-marco-MiniLM-L-6-v2`).
- rank-bm25: Lightweight lexical baseline if needed.
- rapidfuzz: Fuzzy/partial string matching to soften strict regex where useful.
- faiss-cpu / hnswlib: ANN for dense retrieval at scale (later).
- ranx / pytrec_eval: Offline eval on a small labeled set.

Configuration design (YAML)
- Place ranking settings under each topic YAML (`config/topics/*.yaml`) in a `ranking:` section.
- For now, use identical parameters across topics by default (global semantics), while allowing per-topic overrides in the future.
- Synonyms remain baked into the `filter.pattern` regex.
- Keys (initial set):
  - `ranking.top_n`: integer (selection size for summarization).
  - `ranking.method`: "regex" (default), leave room for "hybrid", "bi", "cross" later.
  - `ranking.discouraged_terms`: list of lowercased terms to penalize (e.g., ["comment", "reply", "corrigendum", "erratum", "editorial"]).
  - Optional future keys (forward-compatible): `phrases`, `weights`, `synonyms`, `hybrid` block (alpha, model name).

Title-only scoring signals (regex-first)
- Base match: +1.0 if the topic `filter.pattern` matches anywhere in the title.
- Match count: +0.75 per non-overlapping regex match.
- Coverage: +2.0 × (longest matched span length / max(10, len(title))).
- Position: +0.25 if a match begins within the first 10 characters.
- Priority feed: +0.50 if `feed_name` is listed under `priority_journals` in main config.
- Recency: +0.5 × exp(-2 × days_since_pub/365) to lightly prefer fresh papers.
- Discouraged terms penalty: -0.50 if any discouraged term appears in title (configurable).
- Reasoning: store a concise trace, e.g., `"base=1.0, matches=2, longest_ratio=0.31, priority=+0.50, recency=+0.26, penalty=-0.50"`.

Module and CLI (no implementation yet)
- Module: `src/commands/ranking.py`
  - API: `run(config_path: str, topic: str | None = None, method: str = "regex", mark_top_as_ranked: bool = False) -> None`.
  - Reads `papers.db` entries for the topic(s) with `status='filtered'`.
  - Computes scores using the above signals and updates `rank_score` and `rank_reasoning`.
  - By default, keep `status='filtered'` to preserve behavior; optionally set `status='ranked'` for the top `ranking.top_n` when `mark_top_as_ranked=True`.
- CLI: add `rank` subcommand in `cli/main.py` (e.g., `paper-firehose rank --topic primary --method regex --mark-top`).

Database updates (no implementation yet)
- Add helper to `src/core/database.py`:
  - `update_entry_rank(entry_id: str, topic: str, score: float | None, reasoning: str | None, new_status: str | None = None)` to atomically set `rank_score`, `rank_reasoning`, and optionally `status`.
- Future (optional): embeddings cache table for hybrid ranking, keyed by `(id, topic, model)`.

Selection and diversity (later)
- After scoring, select top `ranking.top_n` per topic for summarization.
- Optionally apply MMR with title embeddings to ensure diversity; use a small sentence-transformers model for cosine similarity if enabled.

Evaluation and tuning (optional)
- Create a tiny labeled set (20–50 titles) per topic.
- Compare regex-only vs hybrid; tune weights (e.g., coverage coefficient, recency strength, penalty magnitude) and blend factor for hybrid using `ranx`/`pytrec_eval`.

Notes and decisions
- Synonyms are primarily handled inside the topic regex patterns; we will not introduce separate synonym expansion by default.
- Ranking parameters live in topic YAML files to allow future per-topic overrides, while defaults can be shared across topics.
- `ranking.top_n` is the authoritative YAML option for how many ranked items to pass to LLM summarization.
- Initial scope is title-only to avoid dependency on abstracts; can later enrich with abstracts (e.g., Crossref) for improved ranking.
