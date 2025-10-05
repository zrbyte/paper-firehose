"""Tests for API key resolution helpers used by summarize command."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

# Ensure the repository's src/ directory is importable without installation.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from paper_firehose.commands import summarize  # noqa: E402


def test_resolve_api_key_prefers_default_config_dir_when_base_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relative api_key_file paths should resolve against the managed config directory."""

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    key_path = secrets_dir / "openaikulcs.env"
    key_path.write_text("sk-test-123\n", encoding="utf-8")

    monkeypatch.setattr(summarize, "DEFAULT_CONFIG_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = {"llm": {"api_key_file": "secrets/openaikulcs.env"}}

    key = summarize._resolve_api_key(config, config_base_dir=None)

    assert key == "sk-test-123"
