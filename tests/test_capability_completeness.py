"""Generic tests for the actor-capability completeness check.

These scenarios are framework-agnostic: every ``operation_flow`` here is a
synthetic mapping with no UI-framework or project-specific vocabulary, so the
check is exercised exactly as a ``frameworks=[]`` project would experience it.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codd.capability_completeness import (
    DEFAULT_CONSUME_VERBS,
    DEFAULT_PRODUCE_VERBS,
    capability_completeness_settings,
    capability_completeness_warnings,
    detect_capability_gaps,
)
from codd.cli import main


def _flow(*operations: dict) -> dict:
    return {"operations": list(operations)}


# --- Case 1: produce + consume target -> no warning (balanced lifecycle) -----


def test_produce_and_consume_target_has_no_gap() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "create_record", "verb": "create", "target": "record", "actor": "operator"},
                {"id": "view_record", "verb": "view", "target": "record", "actor": "operator"},
            ),
        )
    ]

    assert detect_capability_gaps(flows) == ()


# --- Case 2: produce-only target -> no warning (nothing consumes it yet) ------


def test_produce_only_target_has_no_gap() -> None:
    flows = [
        ("synthetic", _flow({"id": "create_record", "verb": "create", "target": "record"}))
    ]

    assert detect_capability_gaps(flows) == ()


# --- Case 3: consume-only target -> warning naming the target ----------------


def test_consume_only_target_is_reported() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "submit_widget", "verb": "submit", "target": "widget", "actor": "member"},
            ),
        )
    ]

    gaps = detect_capability_gaps(flows)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.target == "widget"
    assert gap.consumers == ("submit_widget",)
    assert gap.consumer_actors == ("member",)
    # Message is actionable and names the target and the actor.
    assert "widget" in gap.message
    assert "member" in gap.message
    assert "actor_capability_completeness" in gap.message
    assert "no operation produces it" in gap.message


def test_warnings_helper_returns_messages_for_consume_only_targets() -> None:
    flows = [
        ("synthetic", _flow({"id": "read_report", "verb": "read", "target": "report"}))
    ]

    messages = capability_completeness_warnings(flows, config={})

    assert len(messages) == 1
    assert "report" in messages[0]


# --- Case 4: opt-out suppresses the warning ----------------------------------


def test_opt_out_suppresses_warnings() -> None:
    flows = [
        ("synthetic", _flow({"id": "read_report", "verb": "read", "target": "report"}))
    ]

    messages = capability_completeness_warnings(
        flows, config={"capability_completeness": {"enabled": False}}
    )

    assert messages == []


def test_settings_defaults_enabled_with_builtin_verbs() -> None:
    enabled, produce, consume = capability_completeness_settings({})

    assert enabled is True
    assert produce == DEFAULT_PRODUCE_VERBS
    assert consume == DEFAULT_CONSUME_VERBS


def test_verb_overrides_extend_defaults() -> None:
    enabled, produce, consume = capability_completeness_settings(
        {"capability_completeness": {"produce_verbs": ["forge"], "consume_verbs": ["peek"]}}
    )

    assert enabled is True
    assert "forge" in produce
    assert "peek" in consume
    # Defaults are preserved, not replaced.
    assert DEFAULT_PRODUCE_VERBS <= produce


def test_custom_produce_verb_clears_a_gap() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "forge_widget", "verb": "forge", "target": "widget"},
                {"id": "view_widget", "verb": "view", "target": "widget"},
            ),
        )
    ]

    # Default taxonomy does not know "forge" -> widget looks consume-only.
    assert len(detect_capability_gaps(flows)) == 1
    # Project extends produce verbs -> the gap clears.
    assert (
        capability_completeness_warnings(
            flows, config={"capability_completeness": {"produce_verbs": ["forge"]}}
        )
        == []
    )


# --- Classification edges ----------------------------------------------------


def test_produce_takes_precedence_when_verb_has_both_senses() -> None:
    # An operation that both writes and reads still proves producibility.
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "save_and_view", "verb": "save_view", "target": "widget"},
                {"id": "view_widget", "verb": "view", "target": "widget"},
            ),
        )
    ]

    assert detect_capability_gaps(flows) == ()


def test_unknown_verbs_and_blank_targets_are_ignored() -> None:
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "frobnicate", "verb": "frobnicate", "target": "widget"},
                {"id": "no_target", "verb": "view", "target": ""},
            ),
        )
    ]

    # "frobnicate" is neither produce nor consume; the blank target is skipped.
    assert detect_capability_gaps(flows) == ()


def test_delete_alone_does_not_count_as_produce() -> None:
    # Removing a resource presupposes it exists; it is not a producer.
    flows = [
        (
            "synthetic",
            _flow(
                {"id": "delete_widget", "verb": "delete", "target": "widget"},
                {"id": "view_widget", "verb": "view", "target": "widget"},
            ),
        )
    ]

    gaps = detect_capability_gaps(flows)
    assert [gap.target for gap in gaps] == ["widget"]


# --- codd doctor CLI integration ---------------------------------------------


def _doctor_project(tmp_path: Path, operation_flow_yaml: str, extra: str = "") -> Path:
    project = tmp_path / "app"
    codd_dir = project / "codd"
    codd_dir.mkdir(parents=True)
    (codd_dir / "codd.yaml").write_text(
        f"""
version: "0.1.0"
project:
  name: app
  language: python
scan:
  source_dirs:
    - src/
{operation_flow_yaml}
{extra}
""".lstrip(),
        encoding="utf-8",
    )
    return project


def test_doctor_warns_on_consume_only_target(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: submit_widget
      verb: submit
      target: widget
      actor: member
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "CoDD doctor: WARN" in result.output
    assert "actor_capability_completeness" in result.output
    assert "widget" in result.output


def test_doctor_silent_on_balanced_target(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: create_widget
      verb: create
      target: widget
      actor: operator
    - id: view_widget
      verb: view
      target: widget
      actor: operator
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "actor_capability_completeness" not in result.output


def test_doctor_opt_out_suppresses_capability_warning(tmp_path: Path) -> None:
    project = _doctor_project(
        tmp_path,
        """
operation_flow:
  operations:
    - id: submit_widget
      verb: submit
      target: widget
      actor: member
""",
        extra="""
capability_completeness:
  enabled: false
""",
    )

    result = CliRunner().invoke(main, ["doctor", "--path", str(project)])

    assert result.exit_code == 0
    assert "actor_capability_completeness" not in result.output
