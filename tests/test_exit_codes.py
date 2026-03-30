"""Tests for consistent CLI exit codes."""

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paper_firehose.cli import cli
from paper_firehose.core.exit_codes import ERR_CONFIG, ERR_RUNTIME, ERR_USAGE


@pytest.fixture
def runner():
    return CliRunner()


def test_purge_missing_flags_exits_usage(runner):
    result = runner.invoke(cli, ["purge"])
    assert result.exit_code == ERR_USAGE


def test_filter_bad_config_exits_config(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not_a_valid_key: true\n")
    result = runner.invoke(cli, ["--config", str(bad_config), "filter"])
    assert result.exit_code in (ERR_CONFIG, ERR_RUNTIME)


def test_rank_bad_config_exits_config(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_FIREHOSE_DATA_DIR", str(tmp_path / "data"))
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text("not_a_valid_key: true\n")
    result = runner.invoke(cli, ["--config", str(bad_config), "rank"])
    assert result.exit_code in (ERR_CONFIG, ERR_RUNTIME)


def test_query_invalid_fuzzy_exits_usage(runner):
    result = runner.invoke(cli, ["query", "--fuzzy", "ab"])
    assert result.exit_code == ERR_USAGE


def test_exit_code_constants():
    assert ERR_USAGE == 2
    assert ERR_CONFIG == 3
    assert ERR_RUNTIME == 1
