"""Check provisional decision intake across the greenfield stages."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest

from codd import generator
from codd.cli import main
from codd.elicit import apply
from codd.elicit.finding import Finding
from codd.elicit.persistence import ElicitPersistence
from codd.graph import CEG
from codd.greenfield import pipeline
from codd.implementer.chunked_runner import ChunkedRunner
from codd.llm.impl_step_deriver import ImplStep, ImplStepCacheRecord, impl_step_cache_path, write_impl_step_cache
from codd.planner import _load_requirement_documents
from codd.requirement_reconciliation import discover_requirement_docs
from codd.validator import validate_project
from tests.elicit.test_provisional_decisions import (
    DECISIONS,
    DESK_LIMIT,
    DESK_QUESTION,
    HUMAN_REQUIREMENTS,
    PROVISIONAL,
    REQUIREMENTS,
    decision_row,
    decisions,
    discovery,
    finding,
    make_project,
    run_elicit,
    script_elicit,
)


WAVE = """\
'1':
- node_id: design:reservations
  output: docs/design/reservations.md
  title: Reservations
  depends_on:
  - id: req:reading-room
    relation: implements
"""
DESIGN = Path("docs/design/reservations.md")
DESIGN_BODY = "# Reservations\n\n## Overview\n\nVisitors can reserve a desk.\n"
SURFACE_CLASSIFICATION = '{"runnable_entrypoint":{"excluded":false,"evidence":""}}'
APPENDIX = (
    "<!-- codd:provisional-decisions -->\n# Provisional decisions\n\n"
    "## desk-duration\n\n"
    f"- Original question: {DESK_QUESTION}\n"
    f"- Decision: {DESK_LIMIT}\n"
    "- Reason: Keep booking simple.\n"
    "- Related requirements: req:reading-room\n"
    "- Status: AI provisional (human-editable)\n"
)

BASELINE = {
    "prompt:elicit": "01e238f4a4d36403d89a6d845de6de58462fff331bcc289abe4d94d89c5a70cf",
    "prompt:plan": "e982a67e067fc109c3002e8d6129c634ea6973a714b8410353ab421b355ee237",
    "prompt:generate": "71889bc9e23bfd69d1f5fdb953006953156f12e4deef883418f003cedb1a5d89",
    "codd/codd.yaml": "6dd738eb313a1c7cca5e19b61a3dd1d8c1353becd90fe8c30d88663acf3146c7",
    "docs/design/reservations.md": "afe146d3aad8d394207af9cf75664ed531601a2cb1fbbaeeb1e26496276bbb7c",
    "docs/requirements/requirements.md": "b6284e2bf91f8758d9ae6ff08182872a9a2a6914e635806fae13d43b11382000",
    "findings.md": "26d0c57c5df1ff50f0f2ba77e06f42a2540e6dec5618fe262697f9a138546a3c",
    "project_lexicon.yaml": "ae527d6e16250512a04164b3b1d4ed9c15a77934357ab66836556b1e58c13f68",
}
GREENFIELD_BASELINE = {
    "prompt:classify": "8d20de2bd27b404573799b0848c114481aa434413e7d0cb32fa2fd23cbdcd75a",
    ".codd/elicit/elicit_history.yaml": "03b1a8d328619ba046e004da5cc9483b8f5fbf7c3035fce93b4270795a830a37",
    ".codd/elicit/ignored_findings.yaml": "314c8232dee331480eb5bd15597e0cbb2c29976de39bda807cca84c95872ed80",
    ".codd/elicit/pending_findings.yaml": "86a819e3d4dde393b059d60da12055868ff9d93c5c49e145c29de8dc35e72952",
    ".codd/greenfield_session.yaml": "eea6f6a46fc5283be5618da6fdcd955c6cb8cb068125fd6a8588b3cd7482d6e7",
}


def script_design(monkeypatch) -> dict[str, list[str]]:
    prompts: dict[str, list[str]] = {"plan": [], "generate": []}

    def invoke(command, prompt, *args, **kwargs):
        if "You are initializing CoDD wave_config" in prompt:
            prompts["plan"].append(prompt)
            return WAVE
        if "You are writing a CoDD design document" in prompt:
            prompts["generate"].append(prompt)
            return DESIGN_BODY
        pytest.fail("Unexpected plan or generate AI call")

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    return prompts


def run_design_cli(project: Path) -> None:
    for command, options in [("plan", ["--init"]), ("generate", ["--wave", "1"])]:
        result = CliRunner().invoke(main, [command, "--path", str(project), *options])
        assert result.exit_code == 0, result.output + repr(result.exception)


def run_greenfield(project: Path, *options: str):
    return CliRunner().invoke(main, ["greenfield", "--path", str(project), *options])


@pytest.fixture
def quiet_later_stages(monkeypatch):
    # Keep real elicit, plan, and generate stages, with no publish or test commands.
    def complete(self, root, record, options):
        record["detail"] = "completed by test fixture"

    for stage in ("implement", "verify", "ci_scaffold", "propagate", "check"):
        monkeypatch.setattr(pipeline.GreenfieldPipeline, f"_stage_{stage}", complete)


@pytest.mark.parametrize("entrypoint", ["individual", "greenfield"])
def test_without_opt_in_keeps_baseline(tmp_path, monkeypatch, quiet_later_stages, entrypoint):
    # Match the prompts and file bytes observed on main at d943740.
    project = make_project(tmp_path / "reading-room")
    monkeypatch.setattr(apply, "_utc_timestamp", lambda: "2026-01-02T03:04:05Z")
    monkeypatch.setattr(pipeline, "_utc_now", lambda: "2026-01-02T03:04:05Z")
    responses = [discovery(finding("desk-duration"))]
    if entrypoint == "greenfield":
        responses.append(SURFACE_CLASSIFICATION)
    elicit_prompts = script_elicit(monkeypatch, *responses)
    design_prompts = script_design(monkeypatch)
    if entrypoint == "individual":
        result = run_elicit(project)
        assert result.exit_code == 0, result.output + repr(result.exception)
        run_design_cli(project)
    else:
        result = run_greenfield(project)
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert "stage elicit: done" in result.output

    prompts = {"elicit": elicit_prompts[:1], **design_prompts}
    if entrypoint == "greenfield":
        assert "classifying" in elicit_prompts[1]
        prompts["classify"] = elicit_prompts[1:]
    else:
        assert len(elicit_prompts) == 1
    assert all(len(items) == 1 for items in prompts.values()), prompts.keys()
    assert (project / DESIGN).is_file()
    assert not (project / DECISIONS).exists()
    fingerprints = {
        f"prompt:{stage}": hashlib.sha256(items[0].encode("utf-8")).hexdigest()
        for stage, items in prompts.items()
    }
    fingerprints.update({
        path.relative_to(project).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(project.rglob("*"))
        if path.is_file()
    })
    expected = BASELINE if entrypoint == "individual" else {**BASELINE, **GREENFIELD_BASELINE}
    assert fingerprints == expected


@pytest.mark.parametrize("entrypoint", ["individual", "greenfield"])
def test_provisional_decision_reaches_downstream_prompts(tmp_path, monkeypatch, quiet_later_stages, entrypoint):
    project = make_project(tmp_path)
    prompts = script_design(monkeypatch)
    responses = [discovery(finding("desk-duration")), decisions(decision_row("desk-duration", DESK_LIMIT))]
    if entrypoint == "greenfield":
        responses.append(SURFACE_CLASSIFICATION)
    script_elicit(monkeypatch, *responses)

    if entrypoint == "individual":
        result = run_elicit(project, PROVISIONAL)
        assert result.exit_code == 0, result.output + repr(result.exception)
        run_design_cli(project)
    else:
        result = run_greenfield(project, PROVISIONAL)
        assert result.exit_code == 0, result.output + repr(result.exception)

    source = "src/reservations.py"
    prompts["implement"] = []

    def invoke_implementation(command, prompt, *args, **kwargs):
        assert "You are generating implementation code" in prompt
        prompts["implement"].append(prompt)
        return f"=== FILE: {source} ===\n```python\nMAX_RESERVATION_MINUTES = 90\n```\n"

    monkeypatch.setattr(generator, "_invoke_ai_command", invoke_implementation)
    implementation = CliRunner().invoke(main, [
        "implement", "--path", str(project), "--design", DESIGN.as_posix(),
        "--output", source, "--use-derived-steps", "false",
    ])
    assert implementation.exit_code == 0, implementation.output + repr(implementation.exception)

    for name, calls in prompts.items():
        assert len(calls) == 1, name
        assert "Visitors can reserve a study desk." in calls[0], name
        assert DESK_LIMIT in calls[0], name
        assert DECISIONS.as_posix() in calls[0], name
    assert (project / DESIGN).is_file()
    assert (project / source).is_file()
    assert (project / REQUIREMENTS).read_bytes() == HUMAN_REQUIREMENTS


def test_greenfield_resumes_after_partial_decisions(tmp_path, monkeypatch, quiet_later_stages):
    project = make_project(tmp_path)
    unresolved = "wheelchair-width"
    reason = "The reading room has no measured clearance."
    undecided = decision_row(unresolved, "", status="undecided", reason=reason)
    accepted = {**finding("desk-duration"), "details": {"approval": "approved"}}
    pending = {**finding(unresolved, "How wide is the access aisle?"), "details": {"approval": "approved"}}
    elicit_prompts = script_elicit(
        monkeypatch,
        discovery(accepted, pending),
        decisions(decision_row("desk-duration", DESK_LIMIT), undecided),
        decisions(undecided),
        decisions(undecided),
        SURFACE_CLASSIFICATION,
    )
    design_prompts = script_design(monkeypatch)
    verify_attempts = []

    def verify(self, root, record, options):
        verify_attempts.append(root)
        if len(verify_attempts) == 1:
            raise pipeline.StageError("verify interrupted by test fixture")

    monkeypatch.setattr(pipeline.GreenfieldPipeline, "_stage_verify", verify)
    result = run_greenfield(project, PROVISIONAL)

    assert result.exit_code == 1, result.output + repr(result.exception)
    assert "Failed stage: verify" in result.output
    assert DECISIONS.as_posix() in result.output
    assert unresolved in result.output and reason in result.output
    assert len(elicit_prompts) == 5
    for prompts in design_prompts.values():
        assert len(prompts) == 1
        assert DESK_LIMIT in prompts[0]
        assert reason not in prompts[0]
    assert (project / DESIGN).is_file()
    decisions_before = (project / DECISIONS).read_bytes()
    pending_before = (project / ".codd/elicit/pending_findings.yaml").read_bytes()

    resumed = run_greenfield(project, "--resume")

    assert resumed.exit_code == 0, resumed.output + repr(resumed.exception)
    assert DECISIONS.as_posix() in resumed.output
    assert len(elicit_prompts) == 5
    assert all(len(prompts) == 1 for prompts in design_prompts.values())
    assert (project / DECISIONS).read_bytes() == decisions_before
    assert (project / ".codd/elicit/pending_findings.yaml").read_bytes() == pending_before
    assert [item.id for item in ElicitPersistence(project).load_pending()] == [unresolved]
    assert (project / REQUIREMENTS).read_bytes() == HUMAN_REQUIREMENTS


def test_requirement_readers_include_appendix(tmp_path):
    project = make_project(tmp_path)
    (project / DECISIONS).write_text(APPENDIX, encoding="utf-8")
    config = {"scan": {"doc_dirs": ["docs"]}}
    assert project / DECISIONS in discover_requirement_docs(project, config)
    requirements = _load_requirement_documents(project, config)
    assert {doc.path for doc in requirements} == {REQUIREMENTS.as_posix(), DECISIONS.as_posix()}
    assert [doc.node_id for doc in requirements if doc.node_id] == ["req:reading-room"]
    assert sum(doc.content.count(DESK_LIMIT) for doc in requirements) == 1


@pytest.mark.parametrize("has_marker", [True, False], ids=["appendix", "ordinary-markdown"])
def test_appendix_scan_validation_and_pre_commit(tmp_path, has_marker):
    project = make_project(tmp_path)
    design = project / DESIGN
    design.parent.mkdir()
    design.write_text(
        "---\ncodd:\n  node_id: design:reservations\n  type: design\n"
        "  depends_on:\n  - id: req:reading-room\n    relation: implements\n---\n\n"
        + DESIGN_BODY,
        encoding="utf-8",
    )
    cli = CliRunner()

    def scan():
        result = cli.invoke(main, ["scan", "--path", str(project)])
        assert result.exit_code == 0, result.output + repr(result.exception)
        graph = CEG(project / "codd/scan")
        edges = {(edge["source_id"], edge["relation"], edge["target_id"]) for edge in graph.edges}
        return result.output, (graph.nodes, edges)

    _, original_graph = scan()
    assert set(original_graph[0]) == {"design:reservations", "req:reading-room"}
    assert original_graph[1] == {("design:reservations", "implements", "req:reading-room")}

    content = APPENDIX if has_marker else APPENDIX.split("\n", 1)[1]
    (project / DECISIONS).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "--", DECISIONS.as_posix()], cwd=project, check=True, capture_output=True)

    scan_output, current_graph = scan()
    validation = validate_project(project)
    commit_check = cli.invoke(main, ["hooks", "run-pre-commit", "--path", str(project)])
    complaints = {
        "scan": DECISIONS.as_posix() in scan_output,
        "validate": any(
            issue.location == DECISIONS.as_posix() and issue.code == "missing_frontmatter"
            for issue in validation.issues
        ),
        "pre-commit": DECISIONS.as_posix() in commit_check.output and commit_check.exit_code == 1,
    }
    assert current_graph == original_graph
    assert complaints == dict.fromkeys(complaints, not has_marker)
    assert validation.exit_code == commit_check.exit_code == int(not has_marker)


def test_chunked_implement_reads_provisional_decisions(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (project / DECISIONS).write_text(APPENDIX, encoding="utf-8")
    task = "design:reservations"
    steps = [
        ImplStep.from_dict({
            "id": step_id,
            "kind": "edit",
            "rationale": rationale,
            "source_design_section": "Reservations",
            "expected_outputs": ["src/reservations.py"],
            "approved": True,
        })
        for step_id, rationale in (
            ("booking_capacity", "Track open desks."),
            ("booking_expiry", "Release expired desk reservations."),
        )
    ]
    write_impl_step_cache(
        impl_step_cache_path(task, {"project_root": project}),
        ImplStepCacheRecord("scripted", "key", task, "design", "template", "now", [DESIGN.as_posix()], steps),
    )
    prompts = []

    def invoke(self, ai_command, prompt, project_root):
        prompts.append(prompt)
        return "Chunk complete.", 0

    monkeypatch.setattr(ChunkedRunner, "_invoke", invoke)
    result = CliRunner().invoke(main, [
        "implement", "run", "--path", str(project), "--task", task, "--chunk-size", "1",
    ])

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "2/2 chunks" in result.output
    assert len(prompts) == 2
    for prompt in prompts:
        assert DESK_LIMIT in prompt
        assert DECISIONS.as_posix() in prompt


def test_resume_before_discovery_uses_saved_flag_and_lists_accepted_decision(tmp_path, monkeypatch, quiet_later_stages):
    project = make_project(tmp_path)
    with monkeypatch.context() as first_run:
        def interrupt(*args):
            raise pipeline.StageError("interrupted before discovery")

        first_run.setattr(pipeline.GreenfieldPipeline, "_stage_elicit", interrupt)
        assert run_greenfield(project, PROVISIONAL).exit_code == 1
    session = pipeline.load_session(project)
    assert session["options"]["provisional_decisions"] is True
    assert session["stages"]["elicit"]["status"] == "failed"
    assert not (project / DECISIONS).exists()

    script_elicit(monkeypatch, discovery(finding("desk-duration")),
                  decisions(decision_row("desk-duration", DESK_LIMIT)), SURFACE_CLASSIFICATION)
    script_design(monkeypatch)
    resumed = run_greenfield(project, "--resume")

    assert resumed.exit_code == 0, resumed.output + repr(resumed.exception)
    assert DESK_LIMIT in (project / DECISIONS).read_text(encoding="utf-8")
    assert f"[greenfield] elicit: desk-duration: {DESK_LIMIT}" in resumed.output.splitlines()


@pytest.mark.parametrize("failed_write", ["pending", "findings"])
@pytest.mark.parametrize("entrypoint", ["greenfield", "individual"])
def test_review_write_failure_keeps_prior_pending_and_reports_appendix(
    tmp_path, monkeypatch, quiet_later_stages, failed_write, entrypoint,
):
    project = make_project(tmp_path)
    state = ElicitPersistence(project)
    state.save_pending([Finding.from_dict(finding("P", "Prior question?"))])
    prior_pending = state.pending_path.read_bytes()
    open_rows = [decision_row(key, "", status="undecided", reason="Needs review.") for key in ("P", "G")]
    responses = [
        discovery(finding("F"), finding("G", "New question?")),
        decisions(decision_row("F", DESK_LIMIT), *open_rows), decisions(*open_rows), decisions(*open_rows),
    ]
    if entrypoint == "greenfield":
        responses.append(SURFACE_CLASSIFICATION)
        script_design(monkeypatch)
    prompts = script_elicit(monkeypatch, *responses)
    write_text, write_bytes = Path.write_text, Path.write_bytes

    def short_write(path, text, *args, **kwargs):
        if path.parent == state.elicit_dir and text.startswith("pending:"):
            write_text(path, "pending: [", encoding="utf-8")
            raise OSError("interrupted pending write")
        return write_text(path, text, *args, **kwargs)

    def fail_findings(path, content):
        if path == project / "findings.md":
            raise OSError("interrupted findings write")
        return write_bytes(path, content)

    if failed_write == "pending":
        monkeypatch.setattr(Path, "write_text", short_write)
    else:
        monkeypatch.setattr(Path, "write_bytes", fail_findings)
    run = run_greenfield if entrypoint == "greenfield" else run_elicit
    result = run(project, PROVISIONAL)

    assert result.exit_code == int(entrypoint == "individual"), result.output + repr(result.exception)
    assert f"interrupted {failed_write} write" in result.output
    assert DESK_LIMIT in (project / DECISIONS).read_text(encoding="utf-8")
    if failed_write == "pending":
        assert state.pending_path.read_bytes() == prior_pending
    else:
        assert {item.id for item in state.load_pending()} == {"G", "P"}
    assert DECISIONS.as_posix() in result.output
    if entrypoint == "greenfield":
        resumed = run_greenfield(project, "--resume")
        assert resumed.exit_code == 0, resumed.output
        assert DECISIONS.as_posix() in resumed.output
        assert "G (unresolved)" in resumed.output
        assert len(prompts) == len(responses)


@pytest.mark.parametrize("first_options", [(PROVISIONAL,), ()], ids=["flag", "existing-appendix"])
def test_unreadable_appendix_stops_at_first_read(tmp_path, monkeypatch, first_options):
    project = make_project(tmp_path)
    (project / DECISIONS).write_bytes(b"<!-- codd:provisional-decisions -->\n\xff")
    prompts = script_elicit(monkeypatch)
    for options in [first_options, ("--resume",)]:
        result = run_greenfield(project, *options)
        assert result.exit_code == 1, result.output
        assert "Failed stage: elicit" in result.output
        assert DECISIONS.as_posix() in result.output
        assert "UTF-8" in result.output
    assert prompts == []


def test_failed_append_reports_and_preserves_existing_decisions(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    appendix = project / DECISIONS
    appendix.write_text(APPENDIX, encoding="utf-8")
    original = appendix.read_bytes()
    script_elicit(monkeypatch, discovery(finding("F", "Who may edit it?")),
                  decisions(decision_row("F", "Only the author may edit it.")))

    def disk_full(fd):
        raise OSError("disk full during append")

    monkeypatch.setattr(os, "fsync", disk_full)
    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 1
    assert "disk full during append" in result.output
    assert DECISIONS.as_posix() in result.output
    assert appendix.read_bytes() == original
    assert not list(appendix.parent.glob(f".{appendix.name}.*.tmp"))


def test_unknown_decision_id_warning_stays_in_greenfield_summary_and_resume(
    tmp_path, monkeypatch, quiet_later_stages,
):
    project = make_project(tmp_path)
    unknown = "unrequested-lockers"
    response = decisions(decision_row("desk-duration", DESK_LIMIT), decision_row(unknown, "Lockers are free."))
    prompts = script_elicit(
        monkeypatch, discovery(finding("desk-duration")), response, response, response, SURFACE_CLASSIFICATION,
    )
    script_design(monkeypatch)

    first = run_greenfield(project, PROVISIONAL)
    resumed = run_greenfield(project, "--resume")

    warning = f"Unknown finding ID: {unknown!r}"
    assert len(prompts) == 5
    assert warning in first.output.split("Greenfield autopilot:", 1)[0]
    for run in (first, resumed):
        assert run.exit_code == 0, run.output + repr(run.exception)
        summary = run.output.split("Greenfield autopilot:", 1)[1]
        assert any(
            "] elicit — " in line and "warning" in line.lower() and warning in line
            for line in summary.splitlines()
        )
