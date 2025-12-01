"""
Model management utilities for Sentence-Transformers models.

Handles vendoring (downloading and caching) of transformer models locally
to avoid runtime network dependencies in production environments.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from .paths import get_system_path, resolve_data_dir


logger = logging.getLogger(__name__)


def has_model_files(path: str) -> bool:
    """Heuristic check that a local Sentence-Transformers model folder is valid.

    Args:
        path: Path to model directory

    Returns:
        True if the directory appears to contain a valid model
    """
    p = Path(path)
    if not p.exists() or not p.is_dir():
        return False
    # Common files for ST models
    candidates = [p / "config.json", p / "modules.json"]
    return any(c.exists() for c in candidates)


def ensure_local_model(model_spec: str) -> str:
    """Ensure a local model directory exists for the given spec and return the path or original spec.

    Behavior:
    - If spec is the default alias 'all-MiniLM-L6-v2':
        Use 'models/all-MiniLM-L6-v2'. If missing or empty, download
        'sentence-transformers/all-MiniLM-L6-v2' into that folder.
    - If spec looks like a repo id (e.g., 'sentence-transformers/x' or 'intfloat/e5-small'):
        Vendor to 'models/<last-segment>' when not present or empty.
    - If spec is a local path and valid, return it. If it exists but appears empty,
      try to infer repo id from the folder name and download into it.
    - On any failure (e.g., no network), return the original spec and let STRanker handle it.

    Args:
        model_spec: Model specification (repo ID, alias, or local path)

    Returns:
        Local path to model directory, or original spec if vendoring failed
    """
    # Try local path directly if it's already valid
    if Path(model_spec).exists() and has_model_files(model_spec):
        return model_spec

    models_root = resolve_data_dir('models', ensure_exists=True)
    system_models_root = get_system_path('models')

    repo_id: str | None = None
    target_dir: Path | None = None

    # Case 1: default alias
    if model_spec == "all-MiniLM-L6-v2":
        repo_id = "sentence-transformers/all-MiniLM-L6-v2"
        target_dir = models_root / "all-MiniLM-L6-v2"

    # Case 2: looks like HF repo id "org/name"
    elif "/" in model_spec and not Path(model_spec).exists():
        repo_id = model_spec
        last = model_spec.rsplit("/", 1)[-1]
        # sanitize last segment for filesystem safety just in case
        last = re.sub(r"[^A-Za-z0-9._\-]", "_", last)
        target_dir = models_root / last

    # Case 3: non-default spec that may be a local folder name or alias
    else:
        # If spec is a path but empty, try infer repo as sentence-transformers/<name>
        p = Path(model_spec)
        name = p.name if p.name else str(model_spec)
        repo_id = f"sentence-transformers/{name}"
        target_dir = p if p.is_absolute() else models_root / name

    assert target_dir is not None and repo_id is not None

    # If the target already looks valid, use it
    if has_model_files(str(target_dir)):
        return str(target_dir)

    # If the system bundle ships the model, copy it into the runtime directory
    if system_models_root.exists():
        system_candidate = system_models_root / target_dir.name
        try:
            if system_candidate.exists() and system_candidate.resolve() != target_dir.resolve():
                shutil.copytree(system_candidate, target_dir)
                if has_model_files(str(target_dir)):
                    return str(target_dir)
        except FileExistsError:
            pass
        except Exception as e:
            logger.debug("Model seed copy failed for %s -> %s: %s", system_candidate, target_dir, e)

    # Attempt download (best-effort)
    try:
        from huggingface_hub import snapshot_download  # type: ignore
        target_dir.mkdir(parents=True, exist_ok=True)
        snapshot_download(
            repo_id=repo_id,
            local_dir=str(target_dir),
            local_dir_use_symlinks=False,
        )
        return str(target_dir)
    except Exception as e:  # pragma: no cover - network optional
        logger.warning("Model vendor failed for '%s' -> %s: %s", repo_id, target_dir, e)
        # Fall back to original spec; STRanker will try to resolve
        return model_spec
