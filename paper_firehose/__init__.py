import os
import sys
from typing import Optional, Dict, Any

# Ensure the repository's src/ directory is on sys.path so we can reuse
# the existing implementation without changing its structure.
_PACKAGE_DIR = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(_PACKAGE_DIR)
_SRC_PATH = os.path.join(_REPO_ROOT, 'src')
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

from commands import filter as filter_cmd  # type: ignore
from core.config import ConfigManager  # type: ignore

_DEFAULT_CONFIG = os.path.join(_REPO_ROOT, 'config', 'config.yaml')

__all__ = [
    'filter',
    'purge',
    'status',
]


def filter(topic: Optional[str] = None, config_path: Optional[str] = None) -> None:
    """Run the filter step programmatically.

    Args:
        topic: Optional topic name to process; if None, process all topics.
        config_path: Path to main YAML config; defaults to repo config.
    """
    cfg_path = config_path or _DEFAULT_CONFIG
    filter_cmd.run(cfg_path, topic)


def purge(days: Optional[int] = None, all_data: bool = False, config_path: Optional[str] = None) -> None:
    """Purge entries from databases.

    Args:
        days: Remove entries older than this many days (see implementation semantics).
        all_data: If True, clear all databases.
        config_path: Path to main YAML config; defaults to repo config.
    """
    if days is None and not all_data:
        raise ValueError("Specify days or all_data=True")
    cfg_path = config_path or _DEFAULT_CONFIG
    filter_cmd.purge(cfg_path, days, all_data)


def status(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Return configuration and environment status for programmatic use."""
    cfg_path = config_path or _DEFAULT_CONFIG
    info: Dict[str, Any] = {'config_path': cfg_path}
    if not os.path.exists(cfg_path):
        info.update({'valid': False, 'error': f'Config file not found: {cfg_path}'})
        return info
    try:
        cm = ConfigManager(cfg_path)
        valid = cm.validate_config()
        topics = cm.get_available_topics()
        feeds = cm.get_enabled_feeds() if valid else {}
        cfg = cm.load_config()
        db_cfg = cfg.get('database', {}) if isinstance(cfg, dict) else {}
        info.update({
            'valid': bool(valid),
            'topics': topics,
            'enabled_feeds_count': len(feeds) if isinstance(feeds, dict) else 0,
            'db_paths': db_cfg,
        })
        return info
    except Exception as e:
        info.update({'valid': False, 'error': str(e)})
        return info


