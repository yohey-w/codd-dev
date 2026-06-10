"""Tests for the Artifact Contract core (catalog + contract + verify).

All scenarios use synthetic, project-agnostic artifacts; no project-specific
paths or vocabulary appear.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import yaml

from codd.artifact_contract import (
    CATALOG_PATH,
    ArtifactCatalog,
    CatalogArtifact,
    CatalogError,
    ContractProposal,
    load_catalog,
    load_contract,
    load_proposal,
    merge_into_codd_yaml,
    plan_adopt,
    proposal_path,
    render_adopt,
    render_catalog,
    render_contract,
    render_suggestion,
    suggest_contract,
    verify_contract,
    write_proposal,
)


# ---------------------------------------------------------------------------
# Catalog loading + normalization invariants
# ---------------------------------------------------------------------------
def test_shipped_catalog_loads_and_is_normalized():
    catalog = load_catalog()
    assert isinstance(catalog, ArtifactCatalog)
    assert catalog.artifacts, "catalog must not be empty"
    # Every derived_view references only real ids, and ssot declare none.
    for art in catalog.artifacts:
        if art.is_ssot:
            assert not art.derived_from
        else:
            assert art.is_derived_view
            assert art.derived_from
            for ref in art.derived_from:
                assert catalog.get(ref) is not None
                assert catalog.get(ref).is_ssot, "derived_view must derive from ssot"


def test_shipped_catalog_path_exists():
    assert CATALOG_PATH.exists()


def _write_catalog(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "catalog.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_catalog_rejects_derived_view_without_derived_from(tmp_path):
    path = _write_catalog(
        tmp_path,
        "version: 1\nartifacts:\n"
        "  - {id: a, kind: ssot, produced_by: plan}\n"
        "  - {id: b, kind: derived_view, produced_by: verify}\n",
    )
    with pytest.raises(CatalogError, match="non-empty derived_from"):
        load_catalog(path)


def test_catalog_rejects_ssot_with_derived_from(tmp_path):
    path = _write_catalog(
        tmp_path,
        "version: 1\nartifacts:\n"
        "  - {id: a, kind: ssot, produced_by: plan}\n"
        "  - {id: b, kind: ssot, produced_by: plan, derived_from: [a]}\n",
    )
    with pytest.raises(CatalogError, match="must not declare derived_from"):
        load_catalog(path)


def test_catalog_rejects_unknown_derived_from(tmp_path):
    path = _write_catalog(
        tmp_path,
        "version: 1\nartifacts:\n"
        "  - {id: a, kind: ssot, produced_by: plan}\n"
        "  - {id: b, kind: derived_view, produced_by: verify, derived_from: [missing]}\n",
    )
    with pytest.raises(CatalogError, match="unknown derived_from id"):
        load_catalog(path)


def test_catalog_rejects_duplicate_ids(tmp_path):
    path = _write_catalog(
        tmp_path,
        "version: 1\nartifacts:\n"
        "  - {id: a, kind: ssot, produced_by: plan}\n"
        "  - {id: a, kind: ssot, produced_by: plan}\n",
    )
    with pytest.raises(CatalogError, match="duplicate"):
        load_catalog(path)


def test_catalog_rejects_invalid_kind(tmp_path):
    path = _write_catalog(
        tmp_path,
        "version: 1\nartifacts:\n  - {id: a, kind: bogus, produced_by: plan}\n",
    )
    with pytest.raises(CatalogError, match="invalid kind"):
        load_catalog(path)


# ---------------------------------------------------------------------------
# Contract resolution (opt-in)
# ---------------------------------------------------------------------------
def test_contract_absent_is_inactive():
    contract = load_contract({})
    assert contract.enabled is False
    assert contract.is_active is False
    assert contract.stages == {}


def test_contract_disabled_with_stages_is_inactive():
    contract = load_contract(
        {"artifact_contract": {"enabled": False, "stages": {"design": ["design_spec"]}}}
    )
    assert contract.is_active is False


def test_contract_enabled_with_stages_is_active():
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"design": ["design_spec", "lexicon"]}}}
    )
    assert contract.is_active is True
    assert contract.stages["design"] == ("design_spec", "lexicon")


def test_contract_scalar_stage_value_coerced_to_tuple():
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"implement": "source"}}}
    )
    assert contract.stages["implement"] == ("source",)


# ---------------------------------------------------------------------------
# verify_contract — deterministic existence gate
# ---------------------------------------------------------------------------
def _tiny_catalog() -> ArtifactCatalog:
    return ArtifactCatalog(
        version=1,
        artifacts=(
            CatalogArtifact(
                id="source",
                description="src",
                kind="ssot",
                produced_by="implement",
                default_path_globs=("src/**/*",),
            ),
            CatalogArtifact(
                id="design_spec",
                description="design",
                kind="ssot",
                produced_by="generate",
                default_path_globs=("docs/design/**/*.md",),
                validator="design_doc_frontmatter",
            ),
        ),
    )


def test_verify_pass_when_artifacts_present(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)\n", encoding="utf-8")
    catalog = _tiny_catalog()
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}}
    )
    report = verify_contract(catalog, contract, tmp_path)
    assert report.passed is True
    assert report.has_failures is False
    assert report.stages[0].checks[0].status == "pass"


def test_verify_reports_missing(tmp_path):
    catalog = _tiny_catalog()
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}}
    )
    report = verify_contract(catalog, contract, tmp_path)
    assert report.has_failures is True
    assert report.failure_count == 1
    assert report.stages[0].checks[0].status == "missing"


def test_verify_reports_invalid_empty_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("", encoding="utf-8")
    catalog = _tiny_catalog()
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"implement": ["source"]}}}
    )
    report = verify_contract(catalog, contract, tmp_path)
    assert report.has_failures is True
    assert report.stages[0].checks[0].status == "invalid"


def test_verify_design_doc_frontmatter_validator(tmp_path):
    design = tmp_path / "docs" / "design"
    design.mkdir(parents=True)
    catalog = _tiny_catalog()
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"design": ["design_spec"]}}}
    )

    # No frontmatter → invalid.
    (design / "spec.md").write_text("# No frontmatter here\n", encoding="utf-8")
    report = verify_contract(catalog, contract, tmp_path)
    assert report.stages[0].checks[0].status == "invalid"

    # With frontmatter → pass.
    (design / "spec.md").write_text("---\nnode_id: x\n---\n# Body\n", encoding="utf-8")
    report = verify_contract(catalog, contract, tmp_path)
    assert report.stages[0].checks[0].status == "pass"


def test_verify_unknown_artifact_id(tmp_path):
    catalog = _tiny_catalog()
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"design": ["nonexistent"]}}}
    )
    report = verify_contract(catalog, contract, tmp_path)
    assert report.stages[0].checks[0].status == "unknown"


def test_verify_stage_filter(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x=1\n", encoding="utf-8")
    catalog = _tiny_catalog()
    contract = load_contract(
        {
            "artifact_contract": {
                "enabled": True,
                "stages": {"design": ["design_spec"], "implement": ["source"]},
            }
        }
    )
    report = verify_contract(catalog, contract, tmp_path, stage="implement")
    assert len(report.stages) == 1
    assert report.stages[0].stage == "implement"
    assert report.passed is True


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_catalog_and_contract_smoke():
    catalog = load_catalog()
    text = render_catalog(catalog)
    assert "SSOT" in text and "Derived views" in text
    contract = load_contract(
        {"artifact_contract": {"enabled": True, "stages": {"design": ["design_spec"]}}}
    )
    out = render_contract(contract)
    assert "design" in out and "design_spec" in out


def test_render_contract_disabled():
    out = render_contract(load_contract({}))
    assert "disabled" in out or "no stages" in out


# ---------------------------------------------------------------------------
# Phase 2: suggest (requirement-driven selection) — all deterministic
# ---------------------------------------------------------------------------
def _scaffold_project(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "requirements").mkdir(parents=True)
    (tmp_path / "docs" / "design").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs" / "requirements" / "req.md").write_text("- must\n", encoding="utf-8")
    (tmp_path / "docs" / "design" / "d.md").write_text("---\nt: 1\n---\n# d\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_suggest_selects_present_ssot_artifacts(tmp_path):
    _scaffold_project(tmp_path)
    catalog = load_catalog()
    proposal = suggest_contract(catalog, tmp_path)
    stages = proposal.stages
    assert stages["generate"] == ("design_spec",) or "design_spec" in stages["generate"]
    assert "source" in stages["implement"]
    assert "test_suite" in stages["implement"]
    # requirements present via glob even without explicit discovery hint
    assert "requirements" in stages["plan"]


def test_suggest_excludes_derived_views(tmp_path):
    _scaffold_project(tmp_path)
    catalog = load_catalog()
    proposal = suggest_contract(catalog, tmp_path)
    selected_ids = {s.artifact_id for s in proposal.suggestions}
    # derived_view artifacts are machine-generated; never authored ⇒ never proposed
    for derived in ("dependency_graph", "coverage_report", "traceability_matrix", "reconciliation_ledger"):
        assert derived not in selected_ids
    for chosen in proposal.stages.values():
        assert "dependency_graph" not in chosen


def test_suggest_requirements_implied_by_discovery(tmp_path):
    catalog = load_catalog()
    # No requirement files on disk, but caller passes discovered docs.
    proposal = suggest_contract(
        catalog,
        tmp_path,
        requirement_docs=("docs/requirements/x.md",),
    )
    by_id = {s.artifact_id: s for s in proposal.suggestions}
    req = by_id["requirements"]
    assert req.present is False
    assert req.implied is True
    assert req.selected is True
    assert "discovered" in req.signal


def test_suggest_operation_flow_signal_from_config(tmp_path):
    catalog = load_catalog()
    config = {"operation_flow": {"operations": [{"id": "op1"}]}}
    proposal = suggest_contract(catalog, tmp_path, codd_config=config)
    by_id = {s.artifact_id: s for s in proposal.suggestions}
    assert by_id["operation_flow"].selected is True
    assert by_id["operation_flow"].present is True

    # No operations declared ⇒ not selected, even though codd.yaml glob "matches".
    proposal2 = suggest_contract(catalog, tmp_path, codd_config={"operation_flow": {}})
    by_id2 = {s.artifact_id: s for s in proposal2.suggestions}
    assert by_id2["operation_flow"].selected is False
    assert by_id2["operation_flow"].present is False


def test_suggest_empty_project_selects_nothing(tmp_path):
    catalog = load_catalog()
    proposal = suggest_contract(catalog, tmp_path)
    assert proposal.stages == {}
    assert all(not s.selected for s in proposal.suggestions)


def test_suggest_is_read_only(tmp_path):
    _scaffold_project(tmp_path)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    suggest_contract(load_catalog(), tmp_path)
    after = sorted(p.name for p in tmp_path.rglob("*"))
    assert before == after  # suggest writes nothing itself


def test_proposal_roundtrip(tmp_path):
    _scaffold_project(tmp_path)
    proposal = suggest_contract(load_catalog(), tmp_path)
    path = proposal_path(tmp_path / ".codd")
    write_proposal(path, proposal)
    reloaded = load_proposal(path)
    assert reloaded.stages == proposal.stages
    assert {s.artifact_id for s in reloaded.suggestions} == {
        s.artifact_id for s in proposal.suggestions
    }


def test_proposal_path_custom_output(tmp_path):
    rel = proposal_path(tmp_path / ".codd", "my_proposal.yaml")
    assert rel == tmp_path / ".codd" / "my_proposal.yaml"
    absolute = tmp_path / "elsewhere.yaml"
    assert proposal_path(tmp_path / ".codd", str(absolute)) == absolute


def test_render_suggestion_smoke(tmp_path):
    _scaffold_project(tmp_path)
    out = render_suggestion(suggest_contract(load_catalog(), tmp_path))
    assert "SELECT" in out
    assert "source" in out


# ---------------------------------------------------------------------------
# Phase 2: adopt (opt-in, non-destructive merge into codd.yaml)
# ---------------------------------------------------------------------------
def _proposal_for(stages: dict[str, list[str]]) -> ContractProposal:
    payload = {
        "stages": stages,
        "artifacts": [
            {"id": aid, "stage": stage, "present": True, "implied": True, "selected": True}
            for stage, ids in stages.items()
            for aid in ids
        ],
    }
    return ContractProposal.from_payload(payload)


def test_adopt_plan_merges_into_empty_contract():
    proposal = _proposal_for({"plan": ["requirements"], "implement": ["source"]})
    plan = plan_adopt(proposal, {})
    assert plan.has_changes is True
    assert plan.merged_stages["plan"] == ("requirements",)
    assert plan.merged_stages["implement"] == ("source",)
    assert plan.added["plan"] == ("requirements",)


def test_adopt_preserves_existing_and_appends():
    config = {
        "artifact_contract": {
            "enabled": False,
            "stages": {"plan": ["hand_added"], "custom": ["x"]},
        }
    }
    proposal = _proposal_for({"plan": ["requirements"], "implement": ["source"]})
    plan = plan_adopt(proposal, config)
    # existing entries kept, in order, new appended after
    assert plan.merged_stages["plan"] == ("hand_added", "requirements")
    assert plan.merged_stages["custom"] == ("x",)  # untouched stage preserved
    assert plan.merged_stages["implement"] == ("source",)
    assert plan.added["plan"] == ("requirements",)
    assert "custom" not in plan.added


def test_adopt_idempotent():
    config = {
        "artifact_contract": {
            "enabled": True,
            "stages": {"plan": ["requirements"], "implement": ["source"]},
        }
    }
    proposal = _proposal_for({"plan": ["requirements"], "implement": ["source"]})
    plan = plan_adopt(proposal, config)
    assert plan.has_changes is False
    assert all(not ids for ids in plan.added.values())


def test_adopt_enable_flag_is_a_change_even_without_new_ids():
    config = {
        "artifact_contract": {
            "enabled": False,
            "stages": {"plan": ["requirements"]},
        }
    }
    proposal = _proposal_for({"plan": ["requirements"]})
    plan = plan_adopt(proposal, config, enable=True)
    assert plan.has_changes is True
    assert not any(plan.added.values())  # no new ids, only enabled flips


def test_merge_into_codd_yaml_writes_and_preserves(tmp_path):
    codd_yaml = tmp_path / "codd.yaml"
    codd_yaml.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "operation_flow": {"operations": [{"id": "op1"}]},
                "artifact_contract": {"enabled": False, "stages": {"plan": ["hand_added"]}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    proposal = _proposal_for({"plan": ["requirements"], "implement": ["source"]})
    plan = plan_adopt(proposal, yaml.safe_load(codd_yaml.read_text()))
    count = merge_into_codd_yaml(codd_yaml, plan)
    assert count == 2

    data = yaml.safe_load(codd_yaml.read_text())
    # unrelated sections preserved
    assert data["version"] == 1
    assert data["operation_flow"]["operations"] == [{"id": "op1"}]
    # contract merged, enabled NOT flipped (no --enable)
    assert data["artifact_contract"]["enabled"] is False
    assert data["artifact_contract"]["stages"]["plan"] == ["hand_added", "requirements"]
    assert data["artifact_contract"]["stages"]["implement"] == ["source"]


def test_merge_enable_only_sets_enabled(tmp_path):
    codd_yaml = tmp_path / "codd.yaml"
    codd_yaml.write_text(
        yaml.safe_dump(
            {"artifact_contract": {"enabled": False, "stages": {"plan": ["requirements"]}}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    proposal = _proposal_for({"plan": ["requirements"]})
    plan = plan_adopt(proposal, yaml.safe_load(codd_yaml.read_text()), enable=True)
    count = merge_into_codd_yaml(codd_yaml, plan)
    assert count == 0  # no new ids
    data = yaml.safe_load(codd_yaml.read_text())
    assert data["artifact_contract"]["enabled"] is True
    assert data["artifact_contract"]["stages"]["plan"] == ["requirements"]


def test_merge_no_changes_does_not_write(tmp_path):
    codd_yaml = tmp_path / "codd.yaml"
    original = yaml.safe_dump(
        {"artifact_contract": {"enabled": True, "stages": {"plan": ["requirements"]}}},
        sort_keys=False,
    )
    codd_yaml.write_text(original, encoding="utf-8")
    proposal = _proposal_for({"plan": ["requirements"]})
    plan = plan_adopt(proposal, yaml.safe_load(codd_yaml.read_text()))
    count = merge_into_codd_yaml(codd_yaml, plan)
    assert count == 0
    assert codd_yaml.read_text() == original  # byte-identical, untouched


def test_render_adopt_smoke():
    proposal = _proposal_for({"plan": ["requirements"]})
    plan = plan_adopt(proposal, {})
    out = render_adopt(plan)
    assert "requirements" in out
    no_change = plan_adopt(
        proposal,
        {"artifact_contract": {"enabled": True, "stages": {"plan": ["requirements"]}}},
    )
    assert "No changes" in render_adopt(no_change)
