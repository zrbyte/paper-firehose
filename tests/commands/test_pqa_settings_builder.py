"""Tests for pqa_summary._build_paperqa_settings_kwargs.

Exercises the settings builder that configures paper-qa with the correct
LLM models, temperature overrides, multimodal disable, and JSON disable.
"""

from unittest.mock import MagicMock, patch
from typing import Optional

import pytest

from paper_firehose.commands.pqa_summary import _build_paperqa_settings_kwargs


# ---------------------------------------------------------------------------
# Fake Settings classes simulating different paper-qa versions
# ---------------------------------------------------------------------------

class FakeSettingsModern:
    """Simulates paper-qa >=2026.x with parsing.multimodal and prompts."""
    model_fields = {
        "llm": ..., "summary_llm": ..., "temperature": ...,
        "parsing": ..., "prompts": ...,
    }


class FakeSettingsOld:
    """Simulates paper-qa 5.x with parsing but no prompts field."""
    model_fields = {
        "llm": ..., "summary_llm": ..., "temperature": ...,
        "parsing": ...,
    }


class FakeSettingsMinimal:
    """Simulates a minimal Settings with no parsing/prompts/temperature."""
    model_fields = {
        "llm": ..., "summary_llm": ...,
    }


# ---------------------------------------------------------------------------
# LLM model assignment
# ---------------------------------------------------------------------------

class TestLLMModels:
    def test_sets_llm(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsMinimal, llm="gpt-4o", summary_llm=None)
        assert kwargs["llm"] == "gpt-4o"
        assert "summary_llm" not in kwargs

    def test_sets_summary_llm(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsMinimal, llm=None, summary_llm="gpt-4o-mini")
        assert kwargs["summary_llm"] == "gpt-4o-mini"
        assert "llm" not in kwargs

    def test_sets_both(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsMinimal, llm="gpt-4o", summary_llm="gpt-4o-mini")
        assert kwargs["llm"] == "gpt-4o"
        assert kwargs["summary_llm"] == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# GPT-5 temperature override
# ---------------------------------------------------------------------------

class TestTemperatureOverride:
    def test_gpt5_llm_sets_temperature(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsModern, llm="gpt-5.2", summary_llm=None)
        assert kwargs["temperature"] == 1.0

    def test_gpt5_summary_llm_sets_temperature(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsModern, llm=None, summary_llm="gpt-5-mini")
        assert kwargs["temperature"] == 1.0

    def test_non_gpt5_no_temperature(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsModern, llm="gpt-4o", summary_llm="gpt-4o-mini")
        assert "temperature" not in kwargs

    def test_temperature_not_set_when_field_missing(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsMinimal, llm="gpt-5.2", summary_llm=None)
        assert "temperature" not in kwargs


# ---------------------------------------------------------------------------
# Parsing / multimodal disable
# ---------------------------------------------------------------------------

class TestParsingDisable:
    def test_modern_paperqa_disables_multimodal(self):
        """paper-qa >=2026.x: ParsingSettings has multimodal field."""
        class FakeParsingSettings:
            model_fields = {"multimodal": ..., "use_doc_details": ...}

        with patch("paper_firehose.commands.pqa_summary.logger"):
            with patch.dict("sys.modules", {"paperqa.settings": MagicMock()}):
                with patch("paperqa.settings.ParsingSettings", FakeParsingSettings, create=True):
                    kwargs = _build_paperqa_settings_kwargs(
                        FakeSettingsModern, llm="gpt-4o", summary_llm=None
                    )
        parsing = kwargs.get("parsing", {})
        assert parsing.get("multimodal") is False
        assert parsing.get("use_doc_details") is False

    def test_old_paperqa_disables_parse_pdf_tables(self):
        """paper-qa 5.x: ParsingSettings has parse_pdf_tables_and_figures."""
        class FakeParsingSettings:
            model_fields = {"parse_pdf_tables_and_figures": ..., "use_doc_details": ...}

        with patch("paper_firehose.commands.pqa_summary.logger"):
            with patch.dict("sys.modules", {"paperqa.settings": MagicMock()}):
                with patch("paperqa.settings.ParsingSettings", FakeParsingSettings, create=True):
                    kwargs = _build_paperqa_settings_kwargs(
                        FakeSettingsOld, llm="gpt-4o", summary_llm=None
                    )
        parsing = kwargs.get("parsing", {})
        assert parsing.get("parse_pdf_tables_and_figures") is False

    def test_no_parsing_field_skips_config(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsMinimal, llm="gpt-4o", summary_llm=None)
        assert "parsing" not in kwargs


# ---------------------------------------------------------------------------
# JSON chunk summary disable
# ---------------------------------------------------------------------------

class TestJsonDisable:
    def test_modern_disables_json(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsModern, llm="gpt-4o", summary_llm=None)
        assert kwargs.get("prompts") == {"use_json": False}

    def test_no_prompts_field_skips(self):
        kwargs = _build_paperqa_settings_kwargs(FakeSettingsOld, llm="gpt-4o", summary_llm=None)
        assert "prompts" not in kwargs
