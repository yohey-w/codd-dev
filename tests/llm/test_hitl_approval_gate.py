from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.dag.checks.user_journey_coherence import UserJourneyCoherenceCheck
from codd.deployment.providers.llm_consideration import Consideration
from codd.llm.approval import (
    ApprovalCache,
    approval_mode_from_config,
    filter_approved,
    notify_pending_considerations,
)


def _consideration(item_id: str, status: str = "pending", **extra) -> dict:
    payload = {
        "id": item_id,
        "description": f"{item_id} description",
        "approval_status": status,
    }
    payload.update(extra)
    return payload


def _write_cache(project: Path, *items: dict) -> None:
    cache_dir = project / ".codd" / "consideration_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "generated.json").write_text(
        json.dumps(
            {
                "provider_id": "fake",
                "design_doc_sha": "abc",
                "generated_at": "2026-05-06T00:00:00Z",
                "considerations": list(items),
            }
        ),
        encoding="utf-8",
    )


def _journey(name: str = "llm_flow") -> dict:
    return {
        "name": name,
        "criticality": "critical",
        "steps": [{"action": "expect_url", "value": "/done"}],
        "expected_outcome_refs": ["lexicon:llm_expected"],
    }


def _dag_without_manual_journeys() -> DAG:
    dag = DAG()
    dag.add_node(
        Node(
            id="docs/design/auth.md",
            kind="design_doc",
            path="docs/design/auth.md",
            attributes={"user_journeys": []},
        )
    )
    dag.add_node(
        Node(
            id="lexicon:llm_expected",
            kind="expected",
            attributes={"path": "tests/e2e/llm.spec.ts", "journey": "llm_flow"},
        )
    )
    dag.add_node(
        Node(
            id="implementation_plan.md#LLM",
            kind="plan_task",
            attributes={"expected_outputs": ["lexicon:llm_expected"]},
        )
    )
    dag.add_edge(
        Edge(
            from_id="implementation_plan.md#LLM",
            to_id="lexicon:llm_expected",
            kind="produces",
            attributes={"journey": "llm_flow"},
        )
    )
    dag.add_node(
        Node(
            id="verification:e2e:tests/e2e/llm.spec.ts",
            kind="verification_test",
            path="tests/e2e/llm.spec.ts",
            attributes={
                "kind": "e2e",
                "expected_outcome": {"source": "tests/e2e/llm.spec.ts"},
                "in_deploy_flow": True,
            },
        )
    )
    return dag


def test_approval_cache_save_and_load_persists_under_codd(tmp_path: Path):
    path = ApprovalCache.save("runtime_contract", "approved", tmp_path)

    assert path == tmp_path / ".codd" / "consideration_approvals" / "runtime_contract.json"
    assert ApprovalCache.load("runtime_contract", tmp_path) == "approved"


def test_approval_cache_load_all_returns_statuses(tmp_path: Path):
    ApprovalCache.save("one", "approved", tmp_path)
    ApprovalCache.save("two", "skipped", tmp_path)

    assert ApprovalCache.load_all(tmp_path) == {"one": "approved", "two": "skipped"}


def test_filter_approved_required_excludes_pending():
    approved = Consideration("approved_one", "Approved.", approval_status="approved")
    pending = Consideration("pending_one", "Pending.")

    assert filter_approved([approved, pending], "required") == [approved]


def test_filter_approved_per_consideration_keeps_approved_only():
    approved = Consideration("approved_one", "Approved.", approval_status="approved")
    skipped = Consideration("skipped_one", "Skipped.", approval_status="skipped")

    assert filter_approved([approved, skipped], "per_consideration") == [approved]


def test_filter_approved_auto_with_explicit_optin_allows_all():
    items = [Consideration("one", "One."), Consideration("two", "Two.", approval_status="skipped")]

    assert filter_approved(items, "auto", require_explicit_optin=True) == items


def test_filter_approved_auto_without_optin_falls_back_to_required():
    approved = Consideration("approved_one", "Approved.", approval_status="approved")
    pending = Consideration("pending_one", "Pending.")

    assert filter_approved([approved, pending], "auto") == [approved]


def test_cli_approve_single_persists_status(tmp_path: Path):
    _write_cache(tmp_path, _consideration("one"))

    result = CliRunner().invoke(main, ["llm", "approve", "one", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert ApprovalCache.load("one", tmp_path) == "approved"


def test_cli_approve_all_updates_pending_considerations(tmp_path: Path):
    _write_cache(tmp_path, _consideration("one"), _consideration("two"), _consideration("skip_me", "skipped"))

    result = CliRunner().invoke(main, ["llm", "approve", "--all", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert ApprovalCache.load("one", tmp_path) == "approved"
    assert ApprovalCache.load("two", tmp_path) == "approved"
    assert ApprovalCache.load("skip_me", tmp_path) == "pending"


def test_cli_skip_persists_status(tmp_path: Path):
    _write_cache(tmp_path, _consideration("one"))

    result = CliRunner().invoke(main, ["llm", "skip", "one", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert ApprovalCache.load("one", tmp_path) == "skipped"


def test_cli_list_outputs_status_from_cache(tmp_path: Path):
    _write_cache(tmp_path, _consideration("one"), _consideration("two"))
    ApprovalCache.save("one", "approved", tmp_path)

    result = CliRunner().invoke(main, ["llm", "list", "--path", str(tmp_path)])

    assert result.exit_code == 0
    assert "one\tapproved" in result.output
    assert "two\tpending" in result.output


def test_c7_merges_approved_consideration_journey_with_strategy(tmp_path: Path):
    _write_cache(
        tmp_path,
        _consideration(
            "derived_one",
            source_design_doc="docs/design/auth.md",
            generated_user_journeys=[_journey()],
            verification_strategy={"engine": "registered", "layer": "contract"},
        ),
    )
    ApprovalCache.save("derived_one", "approved", tmp_path)
    dag = _dag_without_manual_journeys()

    result = UserJourneyCoherenceCheck().run(dag, tmp_path, {})
    journeys = UserJourneyCoherenceCheck(project_root=tmp_path)._journey_entries(dag.nodes["docs/design/auth.md"])

    assert result.passed is True
    assert result.journey_reports[0]["user_journey"] == "llm_flow"
    assert journeys[0]["verification_strategy"]["engine"] == "registered"


def test_c7_skips_unapproved_consideration_and_warns(tmp_path: Path, caplog):
    _write_cache(
        tmp_path,
        _consideration(
            "derived_one",
            source_design_doc="docs/design/auth.md",
            generated_user_journeys=[_journey()],
        ),
    )

    result = UserJourneyCoherenceCheck().run(_dag_without_manual_journeys(), tmp_path, {})

    assert result.passed is True
    assert "SKIP" in result.message
    assert "Skipping unapproved LLM consideration derived_one" in caplog.text


def test_notify_pending_considerations_runs_configured_command():
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    notified = notify_pending_considerations(
        [Consideration(str(index), "Pending.") for index in range(6)],
        {"notification": {"ntfy_command": "ntfy send"}},
        run_command=fake_run,
    )

    assert notified is True
    assert calls[0][:2] == ["ntfy", "send"]


def test_notify_pending_considerations_stdout_fallback_does_not_fail():
    messages: list[str] = []

    notified = notify_pending_considerations(
        [Consideration(str(index), "Pending.") for index in range(6)],
        {},
        output=messages.append,
    )

    assert notified is False
    assert "6 pending considerations" in messages[0]


def test_approval_mode_auto_requires_double_optin():
    assert approval_mode_from_config({"llm": {"approval_mode": "auto"}}) == "required"
    assert (
        approval_mode_from_config(
            {"llm": {"approval_mode": "auto", "allow_auto": {"require_explicit_optin": True}}}
        )
        == "auto"
    )


def test_cli_approve_missing_consideration_is_graceful_error(tmp_path: Path):
    result = CliRunner().invoke(main, ["llm", "approve", "missing", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "consideration not found: missing" in result.output


def test_cli_skip_missing_consideration_is_graceful_error(tmp_path: Path):
    result = CliRunner().invoke(main, ["llm", "skip", "missing", "--path", str(tmp_path)])

    assert result.exit_code == 1
    assert "consideration not found: missing" in result.output
