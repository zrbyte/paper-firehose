- Topics in which we're accumulating knowledge, like "RG" is separate from paper search and ranking. They should be kept separate.
	- Set topic type, to **accumulating**, where we're constantly appending to a html. For this implement sqlite database of entries, alongside the html file. All accumulating topics should go to an sqlite database: `accumulating_papers.db`.
	- All **daily** topic result entries stemming from the regexp search still go to `matched_entries_history.db`.
- Moving the feed entries across python scripts and ranking will need sqlite. Switch over from using dictinary to sqlite.
- Pipeline for filtering papers:
	- Take RSS entries and filter title and abstract by regex.
	- The entries from this filtering get passed to ranking. There should be a `ranking.py` which attaches a score to each entry. This ranking can be done by title + abstract. Use paper-qa? 
		- Ranking gets done on all the entries irrespective of journal.
	- Top 20%? of entries gets summarised by LMM, passed to `llmsummary.py`. 
	- For the top 5? arXiv entries the pdf is downloaded and the whole entry is summarised by paper-qa. Appears in a dropdown menu in the `summary.html`.
		- The dictionary element of the entry gets another key, with the rank? Switch to sqlite?

-- Main entries table (replaces all_new_entries)
CREATE TABLE matched_entries (
    id TEXT PRIMARY KEY,
    feed_name TEXT,
    topic TEXT,
    topic_type TEXT DEFAULT 'daily', -- 'daily' or 'accumulating'
    title TEXT,
    link TEXT,
    summary TEXT,
    authors TEXT,
    published_date TEXT,
    discovered_date TEXT,
    raw_metadata TEXT, -- JSON blob of original entry
    
    -- Ranking fields
    rank_score REAL,
    rank_method TEXT, -- 'llm', 'paperqa', etc.
    ranked_date TEXT,
    
    -- Analysis fields  
    pdf_downloaded BOOLEAN DEFAULT 0,
    pdf_analyzed BOOLEAN DEFAULT 0,
    pdf_summary TEXT,
    
    UNIQUE(feed_name, topic, id)

Migration Path
Phase 1: Modify rssparser.py to write to the new schema while keeping current output. Add a main.py to act sa a cli interface.
Phase 2: Create ranking.py that reads from DB and adds rank scores
Phase 3: Modify llmsummary.py to read ranked entries from DB
Phase 4: Remove dictionary passing between scripts

# Proposed modular redesign
- rss-feed-search/
  - src/
    - core/
      - feed_fetcher.py — RSS feed fetching & parsing
      - search_engine.py — Search/filtering logic
      - content_processor.py — Text processing & deduplication
      - storage.py — Database operations
    - filters/
      - base.py — Base filter interface
      - regex_filter.py — Regex-based filtering
      - ranking_filter.py — paper-qa based ranking
      - semantic_filter.py — AI-powered summarization
    - outputs/
      - base.py — Base output interface
      - html_generator.py — HTML output
      - json_exporter.py — JSON/API output
    - publishers/
      - base.py — Base publisher interface
      - ftp_publisher.py — FTP upload
    - config/
      - manager.py — Configuration management
      - validators.py — Config validation
  - config/
    - feeds.yaml — Feed definitions
    - search_topics.yaml — Search configurations
    - app.yaml — Application settings
  - templates/
    - html/ — HTML templates
  - api/
    - app.py — REST API server
    - routes/ — API endpoints
  - cli/
    - main.py — Command-line interface

# YAML based config
```yaml
# feeds.yaml
feeds:
  arxiv_condensed_matter:
    name: "arXiv Condensed Matter"
    url: "https://rss.arxiv.org/rss/cond-mat"
    enabled: true
    fetch_interval: "1h"
    rate_limit: 
      requests_per_minute: 10
    
  nature:
    name: "Nature"
    url: "https://www.nature.com/nature.rss"
    enabled: true
    fetch_interval: "2h"

# search_topics.yaml
topics:
  topology:
    name: "Topological Materials"
    description: "Research on topological insulators, superconductors, and related materials"
    filters:
      - type: "regex"
        pattern: "(topolog[a-z]+)|(weyl)|(dirac)"
        fields: ["title", "summary", "authors"]
      - type: "keyword"
        keywords: ["topological insulator", "quantum spin hall"]
        boost: 1.2
    output:
      html_template: "topic_summary.html"
      archive: true
    notifications:
      - email: "researcher@university.edu"
      - webhook: "https://slack.com/webhook/..."
```

# Technical improvements
- Comprehensive testing strategy
