"""Tests for the generative operation-derivation flow (W-B).

Every fixture is framework- and project-agnostic: a synthetic inventory CLI
tool with a declared ``operation_flow`` plus Markdown requirement documents. The
single LLM slot is injected as a fake callable, so no process is ever spawned.
The invariants mirror the inverse check (requirement_reconciliation): a unit
already anchored to the declared universe is NOT proposed; an uncovered unit IS;
and merge is opt-in, idempotent, and non-destructive.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
import codd.operations_derive as opx


# --- fixtures -----------------------------------------------------------------


def _write_project(
    root: Path,
    *,
    operations: list[dict],
    requirements: str,
    extra_config: dict | None = None,
) -> Path:
    codd_dir = root / "codd"
    codd_dir.mkdir(parents=True, exist_ok=True)
    config: dict = {
        "operation_flow": {"operations": operations},
    }
    if extra_config:
        config.update(extra_config)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    req_dir = root / "docs" / "requirements"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "requirements.md").write_text(requirements, encoding="utf-8")
    return codd_dir


def _config(operations: list[dict], extra: dict | None = None) -> dict:
    cfg: dict = {
        "operation_flow": {"operations": operations},
        "requirement_reconciliation": {"enabled": True, "sections": ["functional"]},
    }
    if extra:
        cfg.update(extra)
    return cfg


def _unit(text: str, *, section: str = "Functional requirements", label: str = "R1", source: str = "req.md"):
    from codd.requirement_reconciliation import RequirementUnit

    return RequirementUnit(source=source, section=section, label=label, text=text)


# --- layer 1: deterministic uncovered detection -------------------------------


def test_uncovered_units_reuses_reconciliation(tmp_path: Path) -> None:
    requirements = (
        "## Functional requirements\n\n"
        "| Behaviour | Note |\n"
        "| --- | --- |\n"
        "| List existing records | declared via operation_flow.list_records |\n"
        "| Compress old snapshots offsite | new behaviour |\n"
    )
    _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements=requirements,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["functional"]}},
    )
    from codd.config import load_project_config

    config = load_project_config(tmp_path)
    uncovered = opx.uncovered_requirement_units(tmp_path, config)
    labels = {item.unit.text for item in uncovered}
    # The "list" row anchors via term overlap; only the unrelated row is uncovered.
    assert any("Compress" in text for text in labels)
    assert not any("List existing records" in text for text in labels)


def test_uncovered_units_empty_when_no_declared_operations(tmp_path: Path) -> None:
    config = _config([])  # no declared operations
    assert opx.uncovered_requirement_units(tmp_path, config) == []


def test_uncovered_units_disabled_returns_empty(tmp_path: Path) -> None:
    config = _config(
        [{"id": "list_records", "verb": "list", "target": "record"}],
        {"requirement_reconciliation": {"enabled": False}},
    )
    assert opx.uncovered_requirement_units(tmp_path, config) == []


# --- layer 2: LLM proposal parsing/validation ---------------------------------


def test_parse_ai_proposal_valid() -> None:
    raw = json.dumps(
        {
            "id": "Archive Record",
            "actor": "operator",
            "verb": "archive",
            "target": "record",
            "expected_outcomes": ["record is archived", "record disappears from list"],
        }
    )
    proposal = opx.parse_ai_proposal(raw, _unit("Archive a record"), existing_ids=set())
    assert proposal is not None
    assert proposal.id == "archive_record"  # normalized to snake_case
    assert proposal.actor == "operator"
    assert proposal.expected_outcomes == ["record is archived", "record disappears from list"]


def test_parse_ai_proposal_tolerates_fenced_and_prose() -> None:
    raw = (
        "Sure, here is the entry:\n```json\n"
        '{"id": "delete_record", "actor": "admin", "verb": "delete", '
        '"target": "record", "expected_outcomes": ["gone"]}\n```\nDone.'
    )
    proposal = opx.parse_ai_proposal(raw, _unit("Delete a record"), existing_ids=set())
    assert proposal is not None
    assert proposal.id == "delete_record"


def test_parse_ai_proposal_rejects_missing_required_fields() -> None:
    raw = json.dumps({"id": "x", "actor": "", "verb": "do", "target": "thing"})
    assert opx.parse_ai_proposal(raw, _unit("x"), existing_ids=set()) is None


def test_parse_ai_proposal_rejects_non_json() -> None:
    assert opx.parse_ai_proposal("not json at all", _unit("x"), existing_ids=set()) is None


def test_parse_ai_proposal_dedupes_id() -> None:
    raw = json.dumps({"id": "archive_record", "actor": "op", "verb": "archive", "target": "record"})
    proposal = opx.parse_ai_proposal(raw, _unit("x"), existing_ids={"archive_record"})
    assert proposal is not None
    assert proposal.id == "archive_record_2"


# --- derive end-to-end (fake AI) ----------------------------------------------


def _fake_ai(payload: dict):
    def invoke(_prompt: str) -> str:
        return json.dumps(payload)

    return invoke


def test_derive_operations_produces_proposal(tmp_path: Path) -> None:
    requirements = (
        "## Functional requirements\n\n"
        "| Behaviour | Note |\n"
        "| --- | --- |\n"
        "| List existing records | operation_flow.list_records |\n"
        "| Archive a snapshot bundle | new |\n"
    )
    _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements=requirements,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["functional"]}},
    )
    from codd.config import load_project_config

    config = load_project_config(tmp_path)
    ai = _fake_ai({"id": "archive_bundle", "actor": "operator", "verb": "archive", "target": "bundle",
                   "expected_outcomes": ["bundle archived"]})
    result = opx.derive_operations(tmp_path, config, ai_invoke=ai)

    assert len(result.uncovered_units) >= 1
    assert len(result.artifact.proposals) == len(result.uncovered_units)
    assert result.artifact.proposals[0].id == "archive_bundle"
    assert result.artifact.proposals[0].approved is False


def test_derive_records_skips_on_ai_error(tmp_path: Path) -> None:
    requirements = (
        "## Functional requirements\n\n"
        "| Behaviour |\n| --- |\n| Archive a snapshot bundle |\n"
    )
    _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements=requirements,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["functional"]}},
    )
    from codd.config import load_project_config

    config = load_project_config(tmp_path)

    def boom(_prompt: str) -> str:
        raise RuntimeError("ai down")

    result = opx.derive_operations(tmp_path, config, ai_invoke=boom)
    assert result.artifact.proposals == []
    assert len(result.skipped_units) == len(result.uncovered_units) >= 1


# --- layer 3: artifact + approve + merge --------------------------------------


def test_artifact_roundtrip(tmp_path: Path) -> None:
    artifact = opx.ProposalArtifact(
        generated_at="2026-06-11T00:00:00+00:00",
        proposals=[
            opx.ProposedOperation(id="a", actor="x", verb="do", target="t", expected_outcomes=["o"]),
        ],
    )
    path = tmp_path / "proposal.yaml"
    opx.write_proposal_artifact(path, artifact)
    loaded = opx.load_proposal_artifact(path)
    assert loaded.generated_at == artifact.generated_at
    assert loaded.proposals[0].id == "a"
    assert loaded.proposals[0].expected_outcomes == ["o"]


def test_load_missing_artifact_returns_empty(tmp_path: Path) -> None:
    loaded = opx.load_proposal_artifact(tmp_path / "nope.yaml")
    assert loaded.proposals == []


def test_approve_all_and_by_id() -> None:
    artifact = opx.ProposalArtifact(proposals=[
        opx.ProposedOperation(id="a", actor="x", verb="do", target="t"),
        opx.ProposedOperation(id="b", actor="x", verb="do", target="t"),
    ])
    approved = opx.approve_proposals(artifact, ids=["a"])
    assert approved == ["a"]
    assert artifact.proposals[0].approved is True
    assert artifact.proposals[1].approved is False

    approved_all = opx.approve_proposals(artifact, approve_all=True)
    assert set(approved_all) == {"a", "b"}
    assert all(p.approved for p in artifact.proposals)


def test_plan_merge_only_approved_non_duplicate() -> None:
    config = _config([{"id": "list_records", "verb": "list", "target": "record"}])
    artifact = opx.ProposalArtifact(proposals=[
        opx.ProposedOperation(id="archive_record", actor="op", verb="archive", target="record", approved=True),
        opx.ProposedOperation(id="list_records", actor="op", verb="list", target="record", approved=True),
        opx.ProposedOperation(id="delete_record", actor="op", verb="delete", target="record", approved=False),
    ])
    plan = opx.plan_merge(artifact, config)
    assert [op["id"] for op in plan.new_operations] == ["archive_record"]
    assert plan.skipped_existing == ["list_records"]
    assert plan.skipped_unapproved == ["delete_record"]
    assert plan.has_changes is True


def test_merge_into_codd_yaml_is_non_destructive(tmp_path: Path) -> None:
    codd_dir = _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements="## Functional requirements\n\n| x |\n| --- |\n| y |\n",
    )
    codd_yaml = codd_dir / "codd.yaml"
    plan = opx.MergePlan(new_operations=[
        {"id": "archive_record", "actor": "op", "verb": "archive", "target": "record",
         "expected_outcomes": ["archived"]},
    ])
    count = opx.merge_into_codd_yaml(codd_yaml, plan)
    assert count == 1

    reloaded = yaml.safe_load(codd_yaml.read_text(encoding="utf-8"))
    ids = [op["id"] for op in reloaded["operation_flow"]["operations"]]
    assert ids == ["list_records", "archive_record"]  # existing preserved, appended after


def test_merge_no_changes_does_not_write(tmp_path: Path) -> None:
    codd_dir = _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements="## Functional requirements\n\n| x |\n| --- |\n| y |\n",
    )
    codd_yaml = codd_dir / "codd.yaml"
    before = codd_yaml.read_text(encoding="utf-8")
    plan = opx.MergePlan()  # no changes
    assert opx.merge_into_codd_yaml(codd_yaml, plan) == 0
    assert codd_yaml.read_text(encoding="utf-8") == before


# --- CLI wiring ---------------------------------------------------------------


def test_cli_full_flow(tmp_path: Path, monkeypatch) -> None:
    requirements = (
        "## Functional requirements\n\n"
        "| Behaviour | Note |\n"
        "| --- | --- |\n"
        "| List existing records | operation_flow.list_records |\n"
        "| Archive a snapshot bundle | new |\n"
    )
    _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements=requirements,
        extra_config={"requirement_reconciliation": {"enabled": True, "sections": ["functional"]}},
    )

    # Inject a fake AI so the derive command never spawns a process.
    def fake_builder(_project_root, _config, _ai_cmd):
        return _fake_ai({"id": "archive_bundle", "actor": "operator", "verb": "archive",
                         "target": "bundle", "expected_outcomes": ["bundle archived"]})

    monkeypatch.setattr("codd.cli._build_operations_ai_invoke", fake_builder)

    runner = CliRunner()
    proj = str(tmp_path)

    derive = runner.invoke(main, ["operations", "derive", "--path", proj])
    assert derive.exit_code == 0, derive.output
    assert "Proposed operations: 1" in derive.output

    show = runner.invoke(main, ["operations", "show", "--path", proj])
    assert show.exit_code == 0, show.output
    assert "list_records" in show.output
    assert "archive_bundle" in show.output
    assert "[pending]" in show.output

    # merge before approve = nothing happens
    premerge = runner.invoke(main, ["operations", "merge", "--path", proj])
    assert premerge.exit_code == 0, premerge.output
    assert "No approved" in premerge.output

    approve = runner.invoke(main, ["operations", "approve", "--path", proj, "--all"])
    assert approve.exit_code == 0, approve.output
    assert "Approved 1" in approve.output

    dry = runner.invoke(main, ["operations", "merge", "--path", proj, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "dry-run" in dry.output
    # dry-run must not write
    cfg = yaml.safe_load((tmp_path / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert [o["id"] for o in cfg["operation_flow"]["operations"]] == ["list_records"]

    merged = runner.invoke(main, ["operations", "merge", "--path", proj])
    assert merged.exit_code == 0, merged.output
    assert "Merged 1 operation" in merged.output
    cfg = yaml.safe_load((tmp_path / "codd" / "codd.yaml").read_text(encoding="utf-8"))
    assert [o["id"] for o in cfg["operation_flow"]["operations"]] == ["list_records", "archive_bundle"]

    # idempotent: re-merge adds nothing (id now declared)
    again = runner.invoke(main, ["operations", "merge", "--path", proj])
    assert again.exit_code == 0, again.output
    assert "No approved" in again.output


def test_cli_approve_requires_selector(tmp_path: Path) -> None:
    _write_project(
        tmp_path,
        operations=[{"id": "list_records", "verb": "list", "target": "record"}],
        requirements="## Functional requirements\n\n| x |\n| --- |\n| y |\n",
    )
    runner = CliRunner()
    res = runner.invoke(main, ["operations", "approve", "--path", str(tmp_path)])
    assert res.exit_code == 1
    assert "--all or one or more --id" in res.output


def test_cli_missing_codd_dir(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(main, ["operations", "show", "--path", str(tmp_path)])
    assert res.exit_code == 1
    assert "no codd.yaml" in res.output
