Usage Guide
===========

Installation
------------

Install via pip:

.. code-block:: bash

   pip install paper-firehose

Runtime Data Directory
----------------------

The application stores runtime state (SQLite databases, generated HTML,
secrets, templates) beneath the path in ``$PAPER_FIREHOSE_DATA_DIR``.
When unset, a platform-appropriate directory is created automatically.
Within that tree you will find:

* ``config/`` – YAML configuration files for global defaults and per-topic
  overrides. The ``ConfigManager`` API is the authoritative source for
  reading/writing these files.
* ``papers.db`` – current pipeline database populated by the ``filter``,
  ``rank``, ``abstracts`` and ``summarize`` commands.
* ``matched_entries_history.db`` – append-only history of matched entries.
* ``all_feed_entries.db`` – deduplicated store of every seen RSS item.


Command Line Interface
----------------------

The :mod:`paper_firehose.cli` module exposes the main pipeline commands.
Examples:

.. code-block:: bash

   # Fetch feeds and filter by topic configuration
   paper-firehose filter --topic condensed_matter

   # Rank filtered entries using sentence-transformers
   paper-firehose rank --topic condensed_matter

   # Fetch abstracts for highly-ranked entries
   paper-firehose abstracts --topic condensed_matter --mailto you@example.com

   # Generate HTML output from the current databases
   paper-firehose html


Programmatic Entry Points
-------------------------

The package exports convenience functions at the top level. They are thin
wrappers around the command implementations and make orchestration from
Python straightforward:

.. code-block:: python

   from paper_firehose import filter, rank, abstracts, summarize

   filter(topic="quantum")
   rank(topic="quantum")
   abstracts(topic="quantum", mailto="you@example.com")
   summarize(topic="quantum")

For more granular control import the modules within :mod:`paper_firehose.commands`
and :mod:`paper_firehose.core`. They expose utilities for managing configuration,
databases, HTML rendering, and email delivery.
