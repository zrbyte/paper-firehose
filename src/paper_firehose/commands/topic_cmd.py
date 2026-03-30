"""Topic management CLI subcommands."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..core.config import ConfigManager, _DEFAULT_TOPIC_TEMPLATE

logger = logging.getLogger(__name__)

_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def list_topics(config_path: str) -> List[Dict[str, str]]:
    """Return available topics with names and descriptions."""
    cfg = ConfigManager(config_path)
    topics = cfg.get_available_topics()
    result = []
    for t in sorted(topics):
        try:
            tcfg = cfg.load_topic_config(t)
            result.append({
                "key": t,
                "name": tcfg.get("name", t),
                "description": tcfg.get("description", ""),
            })
        except Exception:
            result.append({"key": t, "name": t, "description": "(failed to load)"})
    return result


def show_topic(config_path: str, name: str) -> str:
    """Return a topic config as formatted YAML."""
    cfg = ConfigManager(config_path)
    tcfg = cfg.load_topic_config(name)
    return yaml.safe_dump(tcfg, default_flow_style=False, sort_keys=False)


def add_topic(
    config_path: str,
    name: str,
    *,
    from_topic: Optional[str] = None,
) -> Path:
    """Create a new topic config file.

    Args:
        config_path: Path to the main config file.
        name: New topic name (used as filename and ``name:`` field).
        from_topic: Clone an existing topic instead of using the default template.

    Returns:
        Path to the created file.

    Raises:
        ValueError: If the name is invalid or the topic already exists.
    """
    if not _VALID_NAME.match(name):
        raise ValueError(
            f"Invalid topic name '{name}': must be alphanumeric with hyphens/underscores"
        )

    cfg = ConfigManager(config_path)
    topics_dir = Path(cfg.base_dir) / "topics"
    topics_dir.mkdir(parents=True, exist_ok=True)
    dest = topics_dir / f"{name}.yaml"

    if dest.exists():
        raise ValueError(f"Topic '{name}' already exists at {dest}")

    if from_topic:
        source_cfg = cfg.load_topic_config(from_topic)
        data = dict(source_cfg)
        data["name"] = name
        content = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    else:
        content = _DEFAULT_TOPIC_TEMPLATE.strip().replace('name: "example"', f'name: "{name}"')

    dest.write_text(content + "\n", encoding="utf-8")
    logger.info("Created topic '%s' at %s", name, dest)
    return dest
