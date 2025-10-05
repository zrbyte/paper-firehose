"""Tests for runtime data directory resolution helpers."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Ensure the repository's src/ directory is importable without installation.
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from paper_firehose.core.paths import ensure_data_dir, get_data_dir  # noqa: E402


class DataDirEnvironmentOverrideTests(unittest.TestCase):
    """Verify that PAPER_FIREHOSE_DATA_DIR overrides the runtime data dir."""

    def test_get_data_dir_honors_environment_override(self) -> None:
        """get_data_dir should return the directory specified by the env var."""
        with TemporaryDirectory() as tmp:
            override = Path(tmp) / "custom-location"
            with mock.patch.dict(os.environ, {"PAPER_FIREHOSE_DATA_DIR": str(override)}, clear=False):
                data_dir = get_data_dir()
        self.assertEqual(data_dir, override.resolve())

    def test_ensure_data_dir_creates_environment_override_directory(self) -> None:
        """ensure_data_dir should create the directory specified by the env var."""
        with TemporaryDirectory() as tmp:
            override = Path(tmp) / "nested" / "override"
            with mock.patch.dict(os.environ, {"PAPER_FIREHOSE_DATA_DIR": str(override)}, clear=False):
                data_dir = ensure_data_dir()
                # Directory should be created and resolved when ensure_data_dir runs.
                self.assertTrue(override.exists(), "override directory was not created")
        self.assertEqual(data_dir, override.resolve())


if __name__ == "__main__":
    unittest.main()
