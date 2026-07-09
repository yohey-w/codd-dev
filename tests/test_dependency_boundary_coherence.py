"""Deterministic implement-oracle dependency-conformance gate (Increment 1).

Fable5-authorized (see ``dogfood/fable5_reply_2026-07-09_verify-coherence.md``,
"Increment 1", Q2/Q3/Q5). A language-free check: for every generated SOURCE file
owned by a derived task, resolve its INTERNAL import edges to their owning design
doc and assert each edge lands in {the same doc} ∪ {the transitive ``depends_on``
closure of the owning doc}. A resolved internal import to a doc PROVABLY OUTSIDE
that closure is a boundary violation; an unresolvable specifier is logged residue
(never a failure); test-tree artifacts are excluded from v1 scope (logged).

Red-first: these fixtures pin the exact contract — one violation → exactly one
finding naming the file/specifier/owning-doc/missing-edge; unresolvable → zero
findings + residue; same-doc + transitive-closure → pass; and the gate-integration
path feeds the rerun loop's contract feedback with the DUAL (declared dependency
vs. design-level gap; never inline/duplicate).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dependency_boundary_coherence import check_dependency_boundary_coherence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_config(project: Path, *, language: str = "typescript", name: str = "Demo") -> dict:
    (project / "codd").mkdir(parents=True, exist_ok=True)
    config = {"project": {"name": name, "language": language}}
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    return config


def _write_design_doc(
    project: Path, rel: str, *, node_id: str, depends_on: list[str] | None = None
) -> None:
    codd: dict = {"node_id": node_id, "type": "design"}
    if depends_on:
        codd["depends_on"] = [{"id": dep} for dep in depends_on]
    front = yaml.safe_dump({"codd": codd}, sort_keys=False)
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n\n# {node_id}\n", encoding="utf-8")


def _write_derived_cache(
    project: Path, cache_name: str, design_docs: list[str], tasks: list[dict]
) -> None:
    from codd.llm.plan_deriver import (
        DerivedTask,
        DerivedTaskCacheRecord,
        write_derived_task_cache,
    )

    records = [DerivedTask.from_dict(task) for task in tasks]
    write_derived_task_cache(
        project / ".codd" / "derived_tasks" / cache_name,
        DerivedTaskCacheRecord("stub", "key", "sha", "tmpl", "now", design_docs, records),
    )


def _task(
    task_id: str,
    source_design_doc: str,
    *,
    expected_outputs: list[str] | None = None,
    test_kinds: list[str] | None = None,
    layer: str = "detailed",
) -> dict:
    return {
        "id": task_id,
        "title": task_id.replace("_", " "),
        "description": "Derived task.",
        "source_design_doc": source_design_doc,
        "v_model_layer": layer,
        "expected_outputs": expected_outputs or [],
        "test_kinds": test_kinds or [],
        "approved": True,
    }


def _write_source(project: Path, rel: str, content: str) -> None:
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _check(project: Path, config: dict):
    return check_dependency_boundary_coherence(
        project, language="typescript", project_name="Demo", config=config
    )


# ---------------------------------------------------------------------------
# RED #1 — a boundary violation: a→b declared, a's file imports c (outside).
# ---------------------------------------------------------------------------


def test_import_outside_closure_is_one_boundary_violation(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project)

    # a depends_on b (a→b). c is a SEPARATE doc, NOT in a's closure.
    _write_design_doc(project, "docs/design/a.md", node_id="design:a", depends_on=["docs/design/b.md"])
    _write_design_doc(project, "docs/design/b.md", node_id="design:b")
    _write_design_doc(project, "docs/design/c.md", node_id="design:c")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md", "docs/design/b.md", "docs/design/c.md"],
        [
            _task("a_impl", "docs/design/a.md", expected_outputs=["src/a.ts"]),
            _task("b_impl", "docs/design/b.md", expected_outputs=["src/b.ts"]),
            _task("c_impl", "docs/design/c.md", expected_outputs=["src/c.ts"]),
        ],
    )

    # a imports c — an internal edge that lands PROVABLY outside a's closure {a, b}.
    _write_source(project, "src/a.ts", 'import { cThing } from "./c.js";\nexport const aThing = cThing;\n')
    _write_source(project, "src/b.ts", "export const bThing = 1;\n")
    _write_source(project, "src/c.ts", "export const cThing = 2;\n")

    result = _check(project, config)

    assert result.passed is False
    assert len(result.findings) == 1, result.findings
    finding = result.findings[0]
    # Names the file, the specifier, the owning doc, and the missing edge.
    assert finding.path == "src/a.ts"
    assert finding.specifier == "./c.js"
    assert finding.owning_doc == "docs/design/a.md"
    assert finding.target_doc == "docs/design/c.md"
    assert "src/a.ts" in finding.message
    assert "./c.js" in finding.message
    assert "docs/design/a.md" in finding.message
    assert "docs/design/c.md" in finding.message


# ---------------------------------------------------------------------------
# RED #2 — an UNRESOLVABLE internal specifier → zero findings + residue logged.
# ---------------------------------------------------------------------------


def test_unresolvable_specifier_is_residue_not_a_failure(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project)

    _write_design_doc(project, "docs/design/a.md", node_id="design:a", depends_on=["docs/design/b.md"])
    _write_design_doc(project, "docs/design/b.md", node_id="design:b")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md", "docs/design/b.md"],
        [
            _task("a_impl", "docs/design/a.md", expected_outputs=["src/a.ts"]),
            _task("b_impl", "docs/design/b.md", expected_outputs=["src/b.ts"]),
        ],
    )

    # ``./missing.js`` is internal-looking but resolves to NOTHING → residue.
    _write_source(project, "src/a.ts", 'import { x } from "./missing.js";\nexport const aThing = x;\n')
    _write_source(project, "src/b.ts", "export const bThing = 1;\n")

    result = _check(project, config)

    assert result.passed is True
    assert result.findings == []
    assert any("./missing.js" in entry for entry in result.residue), result.residue


# ---------------------------------------------------------------------------
# RED #3 — a same-doc import is always allowed (no finding).
# ---------------------------------------------------------------------------


def test_same_doc_import_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project)

    _write_design_doc(project, "docs/design/a.md", node_id="design:a")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md"],
        [
            _task("a_impl", "docs/design/a.md", expected_outputs=["src/a1.ts", "src/a2.ts"]),
        ],
    )

    _write_source(project, "src/a1.ts", 'import { two } from "./a2.js";\nexport const one = two;\n')
    _write_source(project, "src/a2.ts", "export const two = 2;\n")

    result = _check(project, config)

    assert result.passed is True
    assert result.findings == []


# ---------------------------------------------------------------------------
# RED #4 — a transitive-closure import (a→b→c, a imports c) is allowed.
# ---------------------------------------------------------------------------


def test_transitive_closure_import_passes(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project)

    # a→b→c: c IS reachable through the transitive depends_on closure of a.
    _write_design_doc(project, "docs/design/a.md", node_id="design:a", depends_on=["docs/design/b.md"])
    _write_design_doc(project, "docs/design/b.md", node_id="design:b", depends_on=["docs/design/c.md"])
    _write_design_doc(project, "docs/design/c.md", node_id="design:c")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md", "docs/design/b.md", "docs/design/c.md"],
        [
            _task("a_impl", "docs/design/a.md", expected_outputs=["src/a.ts"]),
            _task("b_impl", "docs/design/b.md", expected_outputs=["src/b.ts"]),
            _task("c_impl", "docs/design/c.md", expected_outputs=["src/c.ts"]),
        ],
    )

    _write_source(project, "src/a.ts", 'import { cThing } from "./c.js";\nexport const aThing = cThing;\n')
    _write_source(project, "src/b.ts", "export const bThing = 1;\n")
    _write_source(project, "src/c.ts", "export const cThing = 2;\n")

    result = _check(project, config)

    assert result.passed is True
    assert result.findings == []


# ---------------------------------------------------------------------------
# RED #5 — v1 scope is SOURCE-only: a TEST-tree artifact is excluded + logged.
# ---------------------------------------------------------------------------


def test_test_tree_artifact_is_excluded_and_logged(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    config = _write_config(project)

    _write_design_doc(project, "docs/design/a.md", node_id="design:a", depends_on=["docs/design/b.md"])
    _write_design_doc(project, "docs/design/b.md", node_id="design:b")
    _write_design_doc(project, "docs/design/c.md", node_id="design:c")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md", "docs/design/b.md", "docs/design/c.md"],
        [
            # The importer lives under the TEST root ("tests") → out of v1 scope.
            _task("a_test", "docs/design/a.md", expected_outputs=["tests/a.test.ts"], test_kinds=["unit"]),
            _task("c_impl", "docs/design/c.md", expected_outputs=["src/c.ts"]),
        ],
    )

    _write_source(project, "tests/a.test.ts", 'import { cThing } from "../src/c.js";\nexport const t = cThing;\n')
    _write_source(project, "src/c.ts", "export const cThing = 2;\n")

    result = _check(project, config)

    # A cross-boundary import in the TEST tree is NOT flagged here (covered by
    # test_import_coherence); the exclusion is LOGGED, never a silent cap.
    assert result.passed is True
    assert result.findings == []
    assert any("tests/a.test.ts" in entry for entry in result.excluded_test_artifacts), (
        result.excluded_test_artifacts
    )


# ---------------------------------------------------------------------------
# RED #6 — GATE INTEGRATION: a boundary violation feeds the rerun loop's
# contract feedback (with the Fable5 Q2 DUAL), and the loop repairs it.
# ---------------------------------------------------------------------------


def test_boundary_violation_feeds_rerun_loop_contract_feedback(tmp_path: Path, monkeypatch) -> None:
    import codd.implement_oracle as impl_oracle
    from codd.implement_oracle import run_implement_oracle_gate
    from codd.implement_oracle_types import (
        EVIDENCE_BOUNDARY_VIOLATION,
        ImplementOracleResult,
    )

    project = tmp_path / "proj"
    _write_config(project, language="python", name="app")

    # Minimal src-layout python project so resolve + certify succeed; the composite
    # oracle itself is stubbed out (we test the boundary wiring, not the oracle).
    _write_source(project, "src/app/__init__.py", "")
    _write_source(project, "tests/__init__.py", "")

    _write_design_doc(project, "docs/design/a.md", node_id="design:a", depends_on=["docs/design/b.md"])
    _write_design_doc(project, "docs/design/b.md", node_id="design:b")
    _write_design_doc(project, "docs/design/c.md", node_id="design:c")

    _write_derived_cache(
        project,
        "plan.yaml",
        ["docs/design/a.md", "docs/design/b.md", "docs/design/c.md"],
        [
            _task("a_impl", "docs/design/a.md", expected_outputs=["src/app/a.py"]),
            _task("b_impl", "docs/design/b.md", expected_outputs=["src/app/b.py"]),
            _task("c_impl", "docs/design/c.md", expected_outputs=["src/app/c.py"]),
        ],
    )

    # a imports c via a relative import; c is outside a's closure {a, b}.
    _write_source(project, "src/app/a.py", "from .c import c_thing\n\n\ndef a_use():\n    return c_thing\n")
    _write_source(project, "src/app/b.py", "b_thing = 1\n")
    _write_source(project, "src/app/c.py", "c_thing = 2\n")

    # Stub the oracle command so it always reports GREEN — the ONLY red must come
    # from the dependency-boundary gate we are wiring in.
    def _fake_run(_root, _profile, _spec, _config):
        return ImplementOracleResult(
            passed=True, executed=True, command="fake-oracle", detail="stub pass"
        )

    monkeypatch.setattr(impl_oracle, "_run_oracle_command", _fake_run)

    captured: list[str] = []

    def rerun(feedback: str, scope=None) -> None:
        captured.append(feedback)
        # DESIGN-level fix: declare the previously-missing dependency a→c.
        _write_design_doc(
            project,
            "docs/design/a.md",
            node_id="design:a",
            depends_on=["docs/design/b.md", "docs/design/c.md"],
        )

    result = run_implement_oracle_gate(
        project,
        language="python",
        project_name="app",
        config={"implement": {"oracle_max_attempts": 5}},
        rerun=rerun,
        echo=lambda _m: None,
    )

    # Exactly one corrective rerun was needed, and the feedback carried the DUAL.
    assert len(captured) == 1, captured
    feedback = captured[0]
    assert "src/app/a.py" in feedback
    assert "docs/design/c.md" in feedback  # names the missing edge target
    # The Fable5 Q2 dual: use a declared dependency OR treat as a design gap;
    # never inline/duplicate to dodge the boundary.
    assert "DESIGN-level gap" in feedback
    assert "duplicate" in feedback

    # After the design-level fix, the boundary gate passes and the gate is GREEN.
    assert result.passed is True
    # And the boundary category is the reserved EVIDENCE_BOUNDARY_VIOLATION.
    assert EVIDENCE_BOUNDARY_VIOLATION == "boundary_violation"
