"""Utilities for locating the paper-firehose runtime data directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

_ENV_VAR = "PAPER_FIREHOSE_DATA_DIR"
_DEFAULT_DIRNAME = ".paper_firehose"


def _normalize_relative(parts: Iterable[str]) -> Path:
    """Normalize relative path components, stripping legacy prefixes."""
    path = Path(*parts)
    if not path.parts:
        return Path()
    first = path.parts[0]
    if first in {"assets", _DEFAULT_DIRNAME}:
        path = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
    return path


def get_data_dir() -> Path:
    """Return the configured runtime data directory.

    Honors the PAPER_FIREHOSE_DATA_DIR environment variable; otherwise defaults
    to ~/.paper_firehose on the current platform.
    """
    override = os.getenv(_ENV_VAR)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / _DEFAULT_DIRNAME).resolve()


def ensure_data_dir() -> Path:
    """Ensure the data directory exists on disk and return it."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def resolve_data_path(*relative: str, ensure_parent: bool = False) -> Path:
    """Resolve a path underneath the runtime data directory.

    Accepts legacy prefixes such as "assets/" to ease migration of existing
    configuration values.
    """
    data_dir = ensure_data_dir()
    relative_path = _normalize_relative(relative)
    full_path = data_dir / relative_path
    if ensure_parent:
        full_path.parent.mkdir(parents=True, exist_ok=True)
    return full_path


def resolve_data_file(path: str, ensure_parent: bool = False) -> Path:
    """Resolve a configured file path against the data directory.

    Absolute paths (or explicit ones containing a drive letter on Windows) are
    used as-is. Relative paths are interpreted relative to the runtime data dir,
    with legacy "assets/" prefixes stripped for backward compatibility.
    """
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        if ensure_parent:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate
    resolved = resolve_data_path(*candidate.parts, ensure_parent=ensure_parent)
    return resolved

__all__ = [
    "get_data_dir",
    "ensure_data_dir",
    "resolve_data_path",
    "resolve_data_file",
]
