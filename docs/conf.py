"""Sphinx configuration for the Paper Firehose documentation."""

from __future__ import annotations

import os
import sys
from datetime import datetime

try:
    from importlib.metadata import version as get_version
except ImportError:  # pragma: no cover -- Python <3.8 fallback
    from importlib_metadata import version as get_version  # type: ignore


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT, "src")

# Ensure the project sources are importable for autodoc
sys.path.insert(0, SRC_DIR)


project = "Paper Firehose"
author = "Peter Nemes-Incze"
copyright = f"{datetime.now():%Y}, {author}"

try:
    release = get_version("paper_firehose")
except Exception:  # pragma: no cover - local builds without install
    release = "0.0.0"
version = release


extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.todo",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]

autosummary_generate = True
autodoc_typehints = "description"
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}

napoleon_google_docstring = True
napoleon_numpy_docstring = False

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_static_path = ["_static"]

todo_include_todos = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}
