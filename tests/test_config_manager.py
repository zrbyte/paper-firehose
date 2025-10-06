"""Tests for configuration management defaults."""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure the repository's src/ directory is importable without installation.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from paper_firehose.core.config import ConfigManager  # noqa: E402


def test_config_manager_creates_defaults(tmp_path):
    """When pointed at an empty directory, default config and topic files are created."""

    config_path = tmp_path / "config.yaml"
    assert not config_path.exists()

    cfg = ConfigManager(str(config_path))

    assert config_path.exists(), "config.yaml should be created on first run"

    topics_dir = tmp_path / "topics"
    assert topics_dir.is_dir(), "topics directory should be created"

    topic_files = list(topics_dir.glob("*.yml")) + list(topics_dir.glob("*.yaml"))
    assert topic_files, "at least one topic YAML should be bootstrapped"

    secrets_dir = tmp_path / "secrets"
    assert secrets_dir.is_dir(), "secrets directory should be created for credential storage"
    assert (secrets_dir / "email_password.env").exists(), "default SMTP password placeholder should be copied"
    assert (secrets_dir / "mailing_lists.yaml").exists(), "sample recipients file should be copied"
    assert (secrets_dir / "openaikulcs.env").exists(), "OpenAI API key placeholder should be copied"

    # Ensure the manager can load the generated configuration without errors
    data = cfg.load_config()
    assert isinstance(data, dict)


def test_load_topic_config_supports_yml_extension(tmp_path):
    """Topics saved with a .yml suffix should load just like .yaml files."""

    config_dir = tmp_path / "custom"
    config_dir.mkdir()

    config_path = config_dir / "config.yaml"
    config_path.write_text(
        (
            "database:\n"
            "  path: \"papers.db\"\n"
            "  all_feeds_path: \"all_feed_entries.db\"\n"
            "  history_path: \"matched_entries_history.db\"\n"
            "feeds: {}\n"
        ),
        encoding="utf-8",
    )

    topics_dir = config_dir / "topics"
    topics_dir.mkdir()
    topic_path = topics_dir / "my_topic.yml"
    topic_path.write_text(
        (
            "name: \"My Topic\"\n"
            "feeds:\n"
            "  - test-feed\n"
            "filter:\n"
            "  pattern: \"graphene\"\n"
        ),
        encoding="utf-8",
    )

    cfg = ConfigManager(str(config_path))
    data = cfg.load_topic_config("my_topic")

    assert data["name"] == "My Topic"
    assert data["feeds"] == ["test-feed"]
