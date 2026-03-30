"""Config management CLI subcommands."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from ..core.config import ConfigManager, _KNOWN_MAIN_KEYS

logger = logging.getLogger(__name__)


def _coerce_value(raw: str) -> Any:
    """Auto-detect type from a CLI string value."""
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _traverse(data: dict, keys: list[str]) -> Any:
    """Traverse a nested dict by dot-notation key parts."""
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            raise KeyError(f"Key not found: {'.'.join(keys)}")
        current = current[k]
    return current


def _set_nested(data: dict, keys: list[str], value: Any) -> None:
    """Set a value in a nested dict by dot-notation key parts."""
    current = data
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]
    current[keys[-1]] = value


def show(config_path: str) -> str:
    """Return the main config as formatted YAML."""
    cfg = ConfigManager(config_path)
    config = cfg.load_config()
    return yaml.safe_dump(config, default_flow_style=False, sort_keys=False)


def get_value(config_path: str, key: str) -> Any:
    """Get a value from the main config using dot-notation."""
    cfg = ConfigManager(config_path)
    config = cfg.load_config()
    parts = key.split(".")
    return _traverse(config, parts)


def set_value(config_path: str, key: str, raw_value: str) -> None:
    """Set a value in the main config using dot-notation.

    Writes the updated config back to disk.
    """
    cfg = ConfigManager(config_path)
    config = cfg.load_config()
    parts = key.split(".")
    value = _coerce_value(raw_value)

    # Warn on unknown top-level key
    if parts[0] not in _KNOWN_MAIN_KEYS:
        logger.warning("Unknown config key: %s", parts[0])

    _set_nested(config, parts, value)

    config_file = Path(cfg.config_path)
    config_file.write_text(
        yaml.safe_dump(config, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def validate(config_path: str) -> tuple[bool, list[str]]:
    """Run full config validation.

    Returns:
        ``(is_valid, unknown_keys)``
    """
    cfg = ConfigManager(config_path)
    valid = cfg.validate_config()
    unknown = cfg.check_unknown_keys()
    return valid, unknown
