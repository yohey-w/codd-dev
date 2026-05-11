"""Unit tests for codd.fix.phenomenon_parser."""

from __future__ import annotations

import json

import pytest

from codd.fix.phenomenon_parser import (
    PhenomenonAnalysis,
    parse_phenomenon,
)


def _make_invoker(response: str):
    def invoke(_prompt: str) -> str:
        return response
    return invoke


def test_empty_phenomenon_returns_default_analysis():
    result = parse_phenomenon("", ai_invoke=_make_invoker("{}"))
    assert result.intent == "unknown"
    assert result.ambiguity_score == 1.0
    assert result.subject_terms == []


def test_well_formed_json_parses():
    payload = json.dumps({
        "intent": "improvement",
        "subject_terms": ["login form", "error message"],
        "lexicon_hits": ["login"],
        "ambiguity_score": 0.2,
        "acceptance_signal": "users understand why login failed",
    })
    result = parse_phenomenon(
        "login error wording is unclear",
        ai_invoke=_make_invoker(payload),
    )
    assert result.intent == "improvement"
    assert result.subject_terms == ["login form", "error message"]
    assert result.lexicon_hits == ["login"]
    assert result.ambiguity_score == pytest.approx(0.2)
    assert "users understand" in result.acceptance_signal


def test_invalid_intent_is_coerced_to_unknown():
    payload = json.dumps({"intent": "totally_made_up", "ambiguity_score": 0.1})
    result = parse_phenomenon("x", ai_invoke=_make_invoker(payload))
    assert result.intent == "unknown"


def test_score_is_clamped_to_unit_interval():
    payload = json.dumps({"intent": "bugfix", "ambiguity_score": 3.4})
    result = parse_phenomenon("x", ai_invoke=_make_invoker(payload))
    assert result.ambiguity_score == 1.0

    payload = json.dumps({"intent": "bugfix", "ambiguity_score": -1.0})
    result = parse_phenomenon("x", ai_invoke=_make_invoker(payload))
    assert result.ambiguity_score == 0.0


def test_llm_failure_falls_back_to_maximum_ambiguity():
    def bad_invoker(_prompt: str) -> str:
        raise RuntimeError("AI subprocess died")

    result = parse_phenomenon("x", ai_invoke=bad_invoker)
    assert result.intent == "unknown"
    assert result.ambiguity_score == 1.0
    assert "ai_invoke_failed" in result.raw_response


def test_malformed_response_falls_back():
    result = parse_phenomenon("x", ai_invoke=_make_invoker("this is not JSON"))
    assert result.intent == "unknown"
    assert result.ambiguity_score == 1.0


def test_fenced_json_is_extracted():
    payload = "```json\n{\"intent\": \"bugfix\", \"ambiguity_score\": 0.4}\n```"
    result = parse_phenomenon("x", ai_invoke=_make_invoker(payload))
    assert result.intent == "bugfix"
    assert result.ambiguity_score == pytest.approx(0.4)


def test_is_ambiguous_threshold():
    a = PhenomenonAnalysis(ambiguity_score=0.7)
    assert a.is_ambiguous()
    a.ambiguity_score = 0.4
    assert not a.is_ambiguous()


def test_design_summaries_appear_in_prompt():
    """Test that we pass design summaries into the prompt context."""
    captured: dict[str, str] = {}

    def capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return json.dumps({"intent": "improvement", "ambiguity_score": 0.1})

    parse_phenomenon(
        "x",
        ai_invoke=capture,
        design_summaries={"auth/login.md": "Login screen design"},
    )
    assert "auth/login.md" in captured["prompt"]
    assert "Login screen design" in captured["prompt"]
