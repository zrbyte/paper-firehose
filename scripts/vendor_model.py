#!/usr/bin/env python3
"""
Download and vendor a Sentence-Transformers model into the repo.

Default: sentence-transformers/all-MiniLM-L6-v2 -> models/all-MiniLM-L6-v2

Usage:
  python scripts/vendor_model.py [huggingface_repo_id] [target_dir]

Examples:
  python scripts/vendor_model.py
  python scripts/vendor_model.py sentence-transformers/all-MiniLM-L6-v2 models/all-MiniLM-L6-v2

Notes:
  - Requires network access and huggingface-hub installed (comes with sentence-transformers).
  - Sets a friendly User-Agent automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

def main() -> int:
    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception as e:
        print("huggingface_hub not available (install sentence-transformers).", e, file=sys.stderr)
        return 2

    repo_id = sys.argv[1] if len(sys.argv) > 1 else "sentence-transformers/all-MiniLM-L6-v2"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("models") / "all-MiniLM-L6-v2"
    target.parent.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {repo_id} -> {target}")
    # Download to a temp cache dir; snapshot_download returns local path
    local_path = snapshot_download(repo_id=repo_id, local_dir=str(target), local_dir_use_symlinks=False)
    print(f"Model available at: {local_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

