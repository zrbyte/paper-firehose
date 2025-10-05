"""Utilities for locating runtime data and built-in system assets."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Iterable

_ENV_VAR = "PAPER_FIREHOSE_DATA_DIR"
_DEFAULT_DIRNAME = ".paper_firehose"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_DIR = _PACKAGE_ROOT / "system"


def _normalize_relative(parts: Iterable[str]) -> Path:
    """Normalize relative path components, stripping legacy prefixes."""
    path = Path(*parts)
    if not path.parts:
        return Path()
    first = path.parts[0]
    if first in {"assets", _DEFAULT_DIRNAME, "system"}:
        path = Path(*path.parts[1:]) if len(path.parts) > 1 else Path()
    return path


def get_data_dir() -> Path:
    """Return the configured runtime data directory.

    Honors the PAPER_FIREHOSE_DATA_DIR environment variable; otherwise defaults
    to ~/.paper_firehose on the current platform.
    """
    override = os.getenv(_ENV_VAR)
    if override is not None:
        cleaned = override.strip()
        if not cleaned:
            return (_REPO_ROOT / _DEFAULT_DIRNAME).resolve()
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            candidate = (_REPO_ROOT / candidate).resolve()
        else:
            candidate = candidate.resolve()
        return candidate
    return (Path.home() / _DEFAULT_DIRNAME).resolve()


def ensure_data_dir() -> Path:
    """Ensure the data directory exists on disk and return it."""
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _seed_from_system(data_dir)
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


def resolve_data_dir(*relative: str, ensure_exists: bool = False) -> Path:
    """Resolve a directory inside the runtime data directory."""
    directory = resolve_data_path(*relative)
    if ensure_exists:
        directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_system_dir() -> Path:
    """Return the repository's bundled system directory."""
    return _SYSTEM_DIR


def get_system_path(*relative: str) -> Path:
    """Return a path inside the repository's system directory."""
    return _SYSTEM_DIR.joinpath(*relative)


def _seed_from_system(target: Path) -> None:
    """Copy selected folders from system assets into *target* when missing."""
    if target.resolve() == _SYSTEM_DIR.resolve():
        return

    seeds = [
        ("config", False),
        ("templates", False),
        ("models", True),
    ]
    for name, is_heavy in seeds:
        src = _SYSTEM_DIR / name
        if not src.exists():
            continue
        dest = target / name
        if dest.exists():
            continue
        try:
            shutil.copytree(src, dest)
        except FileExistsError:
            continue
        except Exception:
            if not is_heavy:
                raise

__all__ = [
    "get_data_dir",
    "ensure_data_dir",
    "resolve_data_path",
    "resolve_data_file",
    "resolve_data_dir",
    "get_system_dir",
    "get_system_path",
]
