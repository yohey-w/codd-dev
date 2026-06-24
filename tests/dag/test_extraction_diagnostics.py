"""Tests for the extraction_diagnostics DAG check (invalid capability_pattern regex).

Self-contained amber diagnostic: ``capability_patterns`` whose regex fails to
compile is silently dropped by ``extractor._capability_matchers`` (``except
re.error: continue``), so the capability's detector never fires — a false-green
source. This check re-validates the declared patterns and surfaces the bad ones
as amber, never red (a config regex typo is advisory, not a deploy blocker).
"""

from __future__ import annotations

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.extraction_diagnostics import ExtractionDiagnosticsCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _settings(capability_patterns: dict | None) -> dict:
    """Mirror the builder.coherence.capability_patterns location."""
    coherence: dict = {}
    if capability_patterns is not None:
        coherence["capability_patterns"] = capability_patterns
    return {"coherence": coherence}


def _run(capability_patterns: dict | None, settings: dict | None = None):
    config = settings if settings is not None else _settings(capability_patterns)
    return ExtractionDiagnosticsCheck(
        dag=_dag(), project_root=None, settings=config
    ).run(codd_config=config)


def _diagnostics_of_type(result, diagnostic_type: str) -> list[dict]:
    return [w for w in result.warnings if w.get("type") == diagnostic_type]


def test_extraction_diagnostics_registered():
    assert get_registry()["extraction_diagnostics"] is ExtractionDiagnosticsCheck


# Fixture 1 — a single valid regex: pass, no diagnostics, checked_count == 1.
def test_valid_regex_passes_with_checked_count():
    result = _run(
        {
            "send_notification": {"regex": r"send_email\("},
        }
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.block_deploy is False
    assert result.checked_count == 1
    assert _diagnostics_of_type(result, "invalid_regex") == []


# Fixture 2 — an invalid regex "[" is surfaced as a single amber diagnostic.
def test_invalid_regex_warns_amber():
    result = _run(
        {
            "broken_detector": {"regex": "["},
        }
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 1
    diagnostics = _diagnostics_of_type(result, "invalid_regex")
    assert len(diagnostics) == 1
    entry = diagnostics[0]
    assert entry["capability"] == "broken_detector"
    assert entry["regex"] == "["
    assert entry["severity"] == "amber"
    assert entry["error"]
    assert entry["remediation"]


# Fixture 3 — no capability_patterns declared: skip (false-red guard).
def test_no_capability_patterns_skips():
    # empty dict
    result = _run({})
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.warnings == []

    # missing coherence section entirely
    result_missing = _run(None, settings={})
    assert result_missing.skipped is True
    assert result_missing.status == "skip"

    # None settings entirely
    none_result = ExtractionDiagnosticsCheck(
        dag=_dag(), project_root=None, settings=None
    ).run(codd_config=None)
    assert none_result.skipped is True
    assert none_result.status == "skip"


# Fixture 4 — valid + multiple invalid mixed: only the invalid ones surface,
# and all of them do (across dict/list/matches shapes mirroring the extractor).
def test_mixed_valid_and_invalid_surfaces_all_invalid():
    result = _run(
        {
            "ok_one": {"regex": r"valid_pattern"},
            "bad_dict": {"regex": "("},
            "bad_in_matches": {
                "matches": [
                    {"regex": r"fine_here"},
                    {"regex": "(?P<"},
                ]
            },
            "bad_list_shape": [
                {"regex": r"also_fine"},
                {"regex": "[a-"},
            ],
        }
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False

    invalid = _diagnostics_of_type(result, "invalid_regex")
    bad_regexes = {entry["regex"] for entry in invalid}
    assert bad_regexes == {"(", "(?P<", "[a-"}
    # checked_count counts every regex compile attempt (valid + invalid).
    assert result.checked_count == 6
    for entry in invalid:
        assert entry["severity"] == "amber"
        assert entry["error"]
        assert entry["remediation"]
        assert entry["capability"] in {"bad_dict", "bad_in_matches", "bad_list_shape"}


# Fixture 5 — capability_patterns declared but NO usable regex anywhere:
# vacuous false-green guard. Declaring a capability with no compilable regex
# means "I claim to verify X" while nothing is actually checked (checked_count
# would be 0 -> old code returned pass). Must amber, never pass, never red.
def test_declared_but_no_usable_regex_warns_amber():
    result = _run(
        {
            # regex key missing entirely
            "no_regex_key": {"value": True},
            # regex present but empty string -> dropped like the extractor does
            "empty_regex": {"regex": ""},
            # regex present but non-string -> dropped like the extractor does
            "non_string_regex": {"regex": 123},
            # matches list whose every entry lacks a usable regex
            "empty_matches": {"matches": [{"value": 1}, {"regex": ""}]},
        }
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.skipped is False  # distinct from "no patterns declared" skip
    assert result.block_deploy is False
    # No usable regex was compilable.
    assert result.checked_count == 0
    # invalid_regex must NOT fire (these regexes never reached re.compile).
    assert _diagnostics_of_type(result, "invalid_regex") == []
    no_usable = _diagnostics_of_type(result, "no_usable_pattern")
    assert len(no_usable) == 1
    entry = no_usable[0]
    assert entry["severity"] == "amber"
    assert entry["remediation"]


# Fixture 6 — at least one usable regex among unusable siblings: pass unchanged.
# A single compilable regex means the check is non-vacuous, so no no_usable_pattern.
def test_one_usable_regex_among_unusable_passes():
    result = _run(
        {
            "no_regex_key": {"value": True},
            "empty_regex": {"regex": ""},
            "has_regex": {"regex": r"do_thing\("},
        }
    )
    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.block_deploy is False
    assert result.checked_count == 1
    assert _diagnostics_of_type(result, "no_usable_pattern") == []
    assert _diagnostics_of_type(result, "invalid_regex") == []


# Fixture 7 — declared with an INVALID regex only (no usable one): the existing
# invalid_regex amber must still fire; this is NOT downgraded to no_usable_pattern
# and is NOT escalated to red. (A bad regex was attempted -> already diagnosed.)
def test_invalid_regex_only_keeps_invalid_regex_amber():
    result = _run(
        {
            "broken_only": {"regex": "["},
        }
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 1  # the invalid regex WAS a compile attempt
    invalid = _diagnostics_of_type(result, "invalid_regex")
    assert len(invalid) == 1
    assert invalid[0]["regex"] == "["
