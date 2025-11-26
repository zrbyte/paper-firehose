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


def test_config_manager_no_reseed_if_dir_exists(tmp_path):
    """If topics/secrets directories already exist, no new template files should be added."""

    # Step 1: Create initial config with empty topics directory
    config_dir = tmp_path / "config_test"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"

    # Create empty topics and secrets directories
    topics_dir = config_dir / "topics"
    topics_dir.mkdir()
    secrets_dir = config_dir / "secrets"
    secrets_dir.mkdir()

    # Add one custom topic file
    custom_topic = topics_dir / "my_custom_topic.yaml"
    custom_topic.write_text(
        (
            "name: \"Custom Topic\"\n"
            "feeds:\n"
            "  - test-feed\n"
            "filter:\n"
            "  pattern: \"test\"\n"
        ),
        encoding="utf-8",
    )

    # Record initial files
    initial_topic_files = set(f.name for f in topics_dir.iterdir())
    initial_secret_files = set(f.name for f in secrets_dir.iterdir()) if secrets_dir.exists() else set()

    # Step 2: Create ConfigManager (which runs _ensure_default_config)
    cfg = ConfigManager(str(config_path))

    # Step 3: Verify no new files were added to topics
    final_topic_files = set(f.name for f in topics_dir.iterdir())
    assert final_topic_files == initial_topic_files, (
        f"No new topic files should be added. "
        f"Initial: {initial_topic_files}, Final: {final_topic_files}"
    )

    # Step 4: Verify no new files were added to secrets
    final_secret_files = set(f.name for f in secrets_dir.iterdir()) if secrets_dir.exists() else set()
    assert final_secret_files == initial_secret_files, (
        f"No new secret files should be added. "
        f"Initial: {initial_secret_files}, Final: {final_secret_files}"
    )

    # Step 5: Verify the custom topic still exists and is loadable
    assert custom_topic.exists()
    assert "my_custom_topic" in cfg.get_available_topics()
