#!/usr/bin/env python3
"""
Copy the GitHub Actions configuration into a local paper-firehose data directory.

This makes it easy to iterate locally with the same YAML files that the CI
workflow uses. If PAPER_FIREHOSE_DATA_DIR is set, that directory is used;
otherwise the default (~/.paper_firehose) is targeted.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a local paper-firehose config from github_actions_config."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Target data directory (defaults to $PAPER_FIREHOSE_DATA_DIR or ~/.paper_firehose).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite any existing files under the target config directory.",
    )
    return parser.parse_args()


def resolve_data_dir(user_override: Path | None) -> Path:
    if user_override is not None:
        return user_override.expanduser().resolve()

    env_override = os.environ.get("PAPER_FIREHOSE_DATA_DIR")
    if env_override:
        return Path(env_override).expanduser().resolve()

    return Path.home() / ".paper_firehose"


def main() -> int:
    args = parse_args()
    data_dir = resolve_data_dir(args.data_dir)
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "github_actions_config"
    dest = data_dir / "config"

    if not src.exists():
        print(f"error: expected configuration directory {src} not found", file=sys.stderr)
        return 1

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not args.overwrite:
        print(
            f"error: destination {dest} already exists. "
            "Use --overwrite to replace its contents or specify --data-dir.",
            file=sys.stderr,
        )
        return 2

    shutil.copytree(src, dest, dirs_exist_ok=args.overwrite)
    print(f"Copied {src} -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
