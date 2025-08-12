- Topics in which we're accumulating knowledge, like "RG" is separate from paper search and ranking. They should be kept separate.
	- Set topic type, to **accumulating**, where we're constantly appending to a html. For this implement sqlite database of entries, alongside the html file. All accumulating topics should go to an sqlite database: `accumulating_papers.db`.
	- All **daily** topic result entries stemming from the regexp search still go to `matched_entries_history.db`.
- Moving the feed entries across python scripts and ranking will need sqlite. Switch over from using dictinary to sqlite.
- Pipeline for filtering papers:
	- Take RSS entries and filter title and abstract by regex.
	- The entries from thies get passed to ranking. There should be a `ranking.py` which attaches a score to each entry. This ranking can be done by title + abstract. Use paper-qa? 
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
);

CREATE INDEX idx_topic_rank ON matched_entries(topic, rank_score DESC);
CREATE INDEX idx_unranked ON matched_entries(topic) WHERE rank_score IS NULL;

Migration Path
Phase 1: Modify rssparser.py to write to the new schema while keeping current output
Phase 2: Create ranking.py that reads from DB and adds rank scores
Phase 3: Modify llmsummary.py to read ranked entries from DB
Phase 4: Remove dictionary passing between scripts