"""Regression tests for the opt-in required artifact decision report."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.greenfield.pipeline import new_session, save_session
from codd.lexicon import AskItem, AskOption, load_lexicon
from codd.requirement_appendix import DECISION_LOCK_PATH, PROVISIONAL_DECISIONS_PATH
from codd.required_artifact_requirements import number_requirements
import codd.required_artifacts_deriver as deriver_module
from codd.required_artifacts_deriver import DEFAULTS_DIR, RequiredArtifactsDeriver


SYSTEM = "design:system_design"
REQUIREMENTS = "design:requirements"
DIGEST_RULES = "design:digest_rules"
OMISSION_RATIONALE = "This local tool has no hosted runtime or deployment."
UNASSIGNED_TEXT = "Battery support is awaiting a hardware decision."
UNASSIGNED_RATIONALE = "The hardware choice is pending."


@pytest.fixture
def fake_ai(monkeypatch):
    prompts = []
    responses = []

    def run(command, *, input, capture_output, text, timeout, check):
        assert command == ["mock-ai"]
        assert capture_output and text and not check
        prompts.append(input)
        assert responses, "unexpected invocation of the AI command"
        response = responses.pop(0)
        stdout = response if isinstance(response, str) else json.dumps(response)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(deriver_module.subprocess, "run", run)
    return prompts, responses


def _project(root, *, catalog=None, timeout=None):
    config = {"project": {"type": "generic"}}
    if catalog is not None or timeout is not None:
        config["required_artifacts"] = {}
        if catalog is not None:
            config["required_artifacts"]["default_artifacts"] = catalog
        if timeout is not None:
            config["required_artifacts"]["ai_timeout_seconds"] = timeout
    codd = root / ".codd"
    codd.mkdir()
    (codd / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _requirement(root, path, text):
    doc = root / path
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")


def _artifact(artifact_id, evidence, *, title=None, depends_on=()):
    return {
        "id": artifact_id,
        "title": title or artifact_id.split(":", 1)[1].replace("_", " ").title(),
        "depends_on": list(depends_on),
        "scope": "Document the implementation decisions for this artifact.",
        "rationale": "The project requires an explicit specification here.",
        "source": "ai_derived",
        "requirement_ids": evidence,
    }


def _legacy_artifacts(response):
    return [
        {key: value for key, value in artifact.items() if key != "requirement_ids"}
        for artifact in response["required_artifacts"]
    ]


def _response(selected, catalog, *, unassigned=()):
    selected_ids = {artifact["id"] for artifact in selected}
    return {
        "required_artifacts": selected,
        "omitted_artifacts": [
            {"id": candidate["id"], "rationale": OMISSION_RATIONALE}
            for candidate in catalog
            if candidate["id"] not in selected_ids
        ],
        "unassigned_requirements": list(unassigned),
    }


def _generic_catalog():
    return yaml.safe_load((DEFAULTS_DIR / "generic.yaml").read_text(encoding="utf-8"))[
        "default_artifacts"
    ]


def _generic_response(*, tail_evidence=(), unassigned=()):
    return _response(
        [
            _artifact(REQUIREMENTS, ["R1"]),
            _artifact(SYSTEM, list(tail_evidence) or ["R1"], depends_on=[REQUIREMENTS]),
        ],
        _generic_catalog(),
        unassigned=unassigned,
    )


def _plan(root, *flags, output_format="json"):
    return CliRunner().invoke(
        main,
        [
            "plan", "--path", str(root), "--derive", "--account-decisions",
            "--format", output_format, "--ai-cmd", "mock-ai", *flags,
        ],
    )


def _assert_prompt_item(prompt, requirement_id, fragment):
    assert re.search(rf"\b{requirement_id}\b[^\n]*{re.escape(fragment)}", prompt)


@pytest.mark.parametrize("flags", [(), ("--account-decisions",)], ids=["derive", "accounted"])
@pytest.mark.parametrize("usage", ["unused", "managed-lock", "saved-opt-in"])
def test_plan_derive_with_unreadable_appendix_candidate(tmp_path, monkeypatch, fake_ai, flags, usage):
    _project(tmp_path)
    primary = "docs/requirements/requirements.md"
    _requirement(tmp_path, primary, "Keep a local log.\n")
    (tmp_path / "project_lexicon.yaml").write_text(
        yaml.safe_dump({"requirement_docs": [primary]}), encoding="utf-8",
    )
    appendix = tmp_path / PROVISIONAL_DECISIONS_PATH
    appendix.write_text("Private memo.\n", encoding="utf-8")
    if usage == "managed-lock":
        lock = tmp_path / DECISION_LOCK_PATH
        lock.parent.mkdir()
        lock.write_bytes(b"\n")
    elif usage == "saved-opt-in":
        save_session(tmp_path, new_session({"provisional_decisions": True}))

    # Simulate mode 000 even on systems where the test user can read any file.
    original_open = Path.open
    denied = f"Permission denied: {appendix}"

    def open_path(path, *args, **kwargs):
        if path == appendix:
            raise PermissionError(denied)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    prompts, responses = fake_ai
    response = _generic_response()
    expected = _legacy_artifacts(response)
    responses.append(response if flags else {"required_artifacts": expected})
    result = CliRunner().invoke(
        main, ["plan", "--path", str(tmp_path), "--derive", "--ai-cmd", "mock-ai", *flags],
    )

    if usage == "unused":
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert len(prompts) == 1
        assert "Keep a local log." in prompts[0] and "Private memo." not in prompts[0]
        assert load_lexicon(tmp_path).required_artifacts == expected
    else:
        assert result.exit_code == 1
        assert denied in result.output + str(result.exception)
        assert prompts == []


def test_cli_accounts_for_shipped_candidates_and_preserves_legacy_artifact_shape(tmp_path, fake_ai):
    _project(tmp_path)
    records_path = "docs/requirements/a-records.md"
    runtime_path = "docs/requirements/z-runtime.md"
    _requirement(tmp_path, runtime_path, "# Runtime\n\nA worker dispatches previews.\n")
    _requirement(
        tmp_path,
        records_path,
        "# Records\n\n"
        "| Feature | Rule |\n"
        "| --- | --- |\n"
        "| Logs | Persist logs to disk. |\n"
        "| Actors | Retain operator names. |\n\n"
        "- Export preview renders a digest.\n"
        "  Include its total count.\n\n"
        "---\n\n"
        f"{UNASSIGNED_TEXT}\n",
    )
    digest = _artifact(DIGEST_RULES, ["R3"], title="Digest Rules", depends_on=[SYSTEM])
    digest["derived_from"] = ["q_digest"]
    response = _response(
        [
            _artifact(REQUIREMENTS, ["R1", "R2"]),
            _artifact(SYSTEM, ["R5"], depends_on=[REQUIREMENTS]),
            _artifact("design:data_design", ["R1", "R2"], depends_on=[SYSTEM]),
            digest,
        ],
        _generic_catalog(),
        unassigned=[{"id": "R4", "rationale": UNASSIGNED_RATIONALE}],
    )
    prompts, responses = fake_ai
    responses.append(response)

    result = _plan(tmp_path)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["saved"] is False
    assert report["required_artifacts"] == _legacy_artifacts(response)
    assert report["artifact_evidence"] == {
        artifact["id"]: artifact["requirement_ids"] for artifact in response["required_artifacts"]
    }
    assert report["added_artifact_ids"] == [DIGEST_RULES]
    titles = {candidate["id"]: candidate["title"] for candidate in _generic_catalog()}
    assert report["omitted_artifacts"] == [
        {**item, "title": titles[item["id"]]} for item in response["omitted_artifacts"]
    ]
    assert report["unassigned_requirements"] == response["unassigned_requirements"]
    rows = report["requirement_items"]
    assert [(row["id"], row["path"], row["start_line"], row["end_line"]) for row in rows] == [
        ("R1", records_path, 5, 5), ("R2", records_path, 6, 6),
        ("R3", records_path, 8, 9), ("R4", records_path, 13, 13),
        ("R5", runtime_path, 3, 3),
    ]
    expected_fragments = [
        "Persist logs to disk.", "Retain operator names.", "Export preview renders a digest.",
        UNASSIGNED_TEXT, "A worker dispatches previews.",
    ]
    for row, fragment in zip(rows, expected_fragments):
        assert fragment in row["text"]
        _assert_prompt_item(prompts[0], row["id"], fragment)
    assert "Include its total count." in rows[2]["text"]
    assert "Include its total count." in prompts[0]
    assert "# Records" in prompts[0] and "| Feature | Rule |" in prompts[0]

    lexicon_path = tmp_path / "project_lexicon.yaml"
    saved = lexicon_path.read_bytes()
    data = yaml.safe_load(saved)
    assert load_lexicon(tmp_path).required_artifacts == report["required_artifacts"]
    decisions = data["required_artifact_decisions"]
    assert re.fullmatch(r"[a-f0-9]{64}", decisions.pop("artifacts_sha256"))
    assert decisions == {
        key: value for key, value in report.items() if key not in {"required_artifacts", "saved"}
    }

    reused = _plan(tmp_path)
    assert reused.exit_code == 0, reused.output
    assert json.loads(reused.output) == {**report, "saved": True}
    rendered = _plan(tmp_path, output_format="text")
    assert rendered.exit_code == 0, rendered.output
    for label in ["Required artifacts", "Omitted candidates", "Added artifacts", "Unassigned requirements"]:
        assert label.lower() in rendered.output.lower()
    for phrase in ["Digest Rules", "Interface Design", OMISSION_RATIONALE, "R4", UNASSIGNED_RATIONALE]:
        assert phrase in rendered.output
    assert lexicon_path.read_bytes() == saved
    assert len(prompts) == 1


def test_accounting_uses_alternate_candidate_scopes_and_conditions(tmp_path, fake_ai):
    screens = "design:screen_items"
    transitions = "design:screen_transitions"
    catalog = [
        {"id": SYSTEM, "title": "System Design", "scope": "Components only.", "always_required": True},
        {
            "id": screens, "title": "Screen Item Definitions", "depends_on": [SYSTEM],
            "scope": "Defines screen contents and field validation.",
            "condition": "Needed for a product with a screen.",
        },
        {
            "id": transitions, "title": "Screen Transition Diagram", "depends_on": [screens],
            "scope": "Defines navigation and form redirects.",
            "condition": "Needed for multiple screens or a submission redirect.",
        },
    ]
    _project(tmp_path, catalog=catalog)
    _requirement(tmp_path, "requirements.md", "The single screen validates a postcode.\n")
    response = _response(
        [
            _artifact(SYSTEM, ["R1"]),
            _artifact(screens, ["R1"], depends_on=[SYSTEM]),
            _artifact("design:interface_design", ["R1"], depends_on=[SYSTEM]),
        ],
        catalog,
    )
    response["omitted_artifacts"][0]["rationale"] = "One screen with no redirect."
    prompts, responses = fake_ai
    responses.append(response)

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(response)
    assert report["added_artifact_ids"] == ["design:interface_design"]
    assert report["omitted_artifacts"] == [
        {"id": transitions, "title": "Screen Transition Diagram", "rationale": "One screen with no redirect."}
    ]
    for candidate in catalog[1:]:
        assert candidate["scope"] in prompts[0]
        assert " ".join(candidate["condition"].split()) in " ".join(prompts[0].split())


@pytest.mark.parametrize(
    "violation", ["dependency", "unknown_evidence", "unallocated", "mandatory", "missing", "duplicate"],
)
def test_accounting_retries_with_the_specific_violation(tmp_path, fake_ai, violation):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", f"Keep a local log.\n\n{UNASSIGNED_TEXT}\n")
    valid = _generic_response(tail_evidence=["R2"])
    invalid = deepcopy(valid)
    if violation == "dependency":
        invalid["required_artifacts"][1]["depends_on"].append("design:missing_dependency")
        diagnostic = "design:missing_dependency"
    elif violation == "unknown_evidence":
        invalid["required_artifacts"][1]["requirement_ids"].append("R888")
        diagnostic = "R888"
    elif violation == "unallocated":
        invalid["required_artifacts"][1]["requirement_ids"] = ["R1"]
        diagnostic = "R2"
    elif violation == "mandatory":
        invalid["required_artifacts"].pop(1)
        invalid["required_artifacts"][0]["requirement_ids"].append("R2")
        invalid["omitted_artifacts"].append({"id": SYSTEM, "rationale": OMISSION_RATIONALE})
        diagnostic = SYSTEM
    else:
        optional = invalid["omitted_artifacts"].pop()
        if violation == "duplicate":
            invalid["omitted_artifacts"].extend([optional, optional])
        diagnostic = optional["id"]
    prompts, responses = fake_ai
    responses.extend([invalid, valid])

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(valid)
    assert len(prompts) == 2
    assert diagnostic in prompts[1]
    if violation in {"mandatory", "missing", "duplicate"}:
        assert prompts[1].count(diagnostic) > prompts[0].count(diagnostic)
    if violation == "unallocated":
        assert prompts[1].count(UNASSIGNED_TEXT) > prompts[0].count(UNASSIGNED_TEXT)
    if violation == "mandatory":
        retry = prompts[1].lower()
        assert retry.count(SYSTEM) > prompts[0].count(SYSTEM)
        assert "always_required" in retry or "mandatory" in retry


def test_empty_and_invalid_successful_responses_retry_with_diagnostics(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, responses = fake_ai
    valid = _generic_response()
    responses.extend(["", "{malformed", valid])

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(valid)
    assert len(prompts) == 3
    assert "empty" in prompts[1].lower()
    assert "valid json" in prompts[2].lower()


def test_accounted_prompt_exposes_requirements_beyond_legacy_character_cutoff(tmp_path, fake_ai):
    _project(tmp_path)
    tail = "Export inventory deltas only after the final reconciliation."
    _requirement(tmp_path, "requirements.md", "A" * 25_000 + "\n\n" + tail + "\n")
    prompts, responses = fake_ai
    responses.append(_generic_response(tail_evidence=["R2"]))

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert len(prompts) == 1
    _assert_prompt_item(prompts[0], "R2", tail)
    assert report["requirement_items"][1]["text"] == tail
    assert report["artifact_evidence"][SYSTEM] == ["R2"]


def test_markdown_item_boundaries_preserve_list_and_fence_containers(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(
        tmp_path, "requirements.md",
        "- Keep a log.\n\n"
        "  Store the actor's address.\n"
        "  ```\n"
        "  > literal\n"
        "  ```\n"
        "- Send a digest.\n\n"
        "> ~~~text\n"
        "> example\n\n"
        "Persist records.\n\n"
        "    ~~~\n\n"
        "Encrypt the data.\n\n"
        "| Feature |\n| --- |\n"
        "| Exports |\n"
        "# Exports | Limits\n\n"
        "Retain timestamps.\n",
    )
    _, responses = fake_ai
    responses.append(_generic_response(tail_evidence=[f"R{i}" for i in range(2, 9)]))

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    items = report["requirement_items"]
    assert [row["text"].splitlines()[0] for row in items] == [
        "- Keep a log.", "- Send a digest.", "> ~~~text", "Persist records.",
        "    ~~~", "Encrypt the data.", "| Exports |", "Retain timestamps.",
    ]
    assert items[0]["end_line"] == 6
    assert items[2]["end_line"] == 10


@pytest.mark.parametrize("existing", [[], [_artifact(REQUIREMENTS, ["R1"])]], ids=["empty", "populated"])
def test_saved_legacy_artifacts_are_reused_until_explicit_rederive(tmp_path, fake_ai, existing):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    existing = _legacy_artifacts({"required_artifacts": existing})
    lexicon = tmp_path / "project_lexicon.yaml"
    original = "# Hand-maintained lineup\n" + yaml.safe_dump({"required_artifacts": existing})
    lexicon.write_text(original, encoding="utf-8")
    prompts, responses = fake_ai
    responses.append(_generic_response())

    reused = _plan(tmp_path)

    assert reused.exit_code == 0, reused.output
    assert json.loads(reused.output) == {"required_artifacts": existing, "saved": True}
    assert lexicon.read_bytes() == original.encode("utf-8")
    assert not prompts

    replaced = _plan(tmp_path, "--rederive")

    assert replaced.exit_code == 0, replaced.output
    replacement = json.loads(replaced.output)
    assert replacement["saved"] is False
    assert replacement["required_artifacts"] == _legacy_artifacts(_generic_response())
    assert len(prompts) == 1


def test_exhaustion_reports_violations_without_writing_and_next_call_derives(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", f"Keep a local log.\n\n{UNASSIGNED_TEXT}\n")
    lexicon = tmp_path / "project_lexicon.yaml"
    original = b"# Preserve author formatting\nnode_vocabulary: []\n"
    lexicon.write_bytes(original)
    valid = _generic_response(tail_evidence=["R2"])
    invalid = deepcopy(valid)
    invalid["required_artifacts"][1]["requirement_ids"] = ["R888"]
    invalid["required_artifacts"][1]["depends_on"] = ["design:missing_dependency"]
    prompts, responses = fake_ai
    responses.extend([invalid, invalid, invalid, valid])

    failed = _plan(tmp_path)

    assert failed.exit_code != 0
    assert len(prompts) == 3
    for violation in ["design:missing_dependency", "R888", "R2"]:
        assert violation in failed.output
        assert all(violation in retry for retry in prompts[1:])
    assert lexicon.read_bytes() == original

    retried = _plan(tmp_path)

    assert retried.exit_code == 0, retried.output
    assert json.loads(retried.output)["saved"] is False
    assert len(prompts) == 4


def test_saved_decision_details_are_bound_to_the_artifact_selection(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, responses = fake_ai
    responses.append(_generic_response())
    assert _plan(tmp_path).exit_code == 0

    path = tmp_path / "project_lexicon.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["required_artifacts"][0]["rationale"] = "Owner revised the rationale."
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    reused = _plan(tmp_path)

    assert reused.exit_code == 0, reused.output
    assert json.loads(reused.output) == {"required_artifacts": data["required_artifacts"], "saved": True}
    assert len(prompts) == 1


def test_accounted_subprocess_timeout_surfaces_without_persisting(tmp_path, monkeypatch):
    _project(tmp_path, timeout=7)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    timeouts = []

    def timeout_run(command, *, timeout, **kwargs):
        assert command == ["mock-ai"]
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(deriver_module.subprocess, "run", timeout_run)
    result = _plan(tmp_path)

    assert result.exit_code != 0
    assert "timed out" in result.output.lower()
    assert timeouts == [7]
    assert not (tmp_path / "project_lexicon.yaml").exists()


def test_legacy_derive_prompt_matches_d943740_byte_for_byte(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(
        tmp_path, "requirements.md",
        "# V1 Requirements\n\nSupport café labels.\n\n" + "A" * 24_010 + "\nExisting truncation marker.\n",
    )
    decisions = [
        AskItem(
            id="q_labels", question="Which labels?", status="RECOMMENDED_PROCEEDING",
            recommended_id="unicode",
            options=[AskOption(id="unicode", label="Unicode", recommended=True)],
        )
    ]
    prompts, responses = fake_ai
    legacy_payload = {"required_artifacts": _legacy_artifacts(_generic_response())}
    responses.append(legacy_payload)

    artifacts = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive([], decisions)

    assert artifacts == legacy_payload["required_artifacts"]
    assert len(prompts) == 1
    assert sha256(prompts[0].encode("utf-8")).hexdigest() == (
        "330fa49ee974abb99f8ed1cbee03fece6f78a53d94c7e034d20c53e915d575db"
    )


@pytest.mark.parametrize(
    "violation",
    [
        "selected_and_omitted", "unknown_omitted", "empty_evidence", "assigned_and_unassigned",
        "unassigned_without_rationale", "omitted_without_rationale", "dependency_cycle", "repeated_json_key",
    ],
)
def test_accounting_rejects_each_inconsistent_decision_and_retries(tmp_path, fake_ai, violation):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", f"Keep a local log.\n\n{UNASSIGNED_TEXT}\n")
    valid = _generic_response(tail_evidence=["R2"])
    invalid = deepcopy(valid)
    requirements_row, system_row = invalid["required_artifacts"]
    if violation == "selected_and_omitted":
        invalid["omitted_artifacts"].append({"id": SYSTEM, "rationale": OMISSION_RATIONALE})
        diagnostic = "both selected and omitted"
    elif violation == "unknown_omitted":
        invalid["omitted_artifacts"].append({"id": "design:not_a_candidate", "rationale": OMISSION_RATIONALE})
        diagnostic = "design:not_a_candidate"
    elif violation == "empty_evidence":
        system_row["requirement_ids"] = ["R1", "R2"]
        requirements_row["requirement_ids"] = []
        diagnostic = "non-empty requirement_ids"
    elif violation == "assigned_and_unassigned":
        invalid["unassigned_requirements"] = [{"id": "R2", "rationale": UNASSIGNED_RATIONALE}]
        diagnostic = "already assigned"
    elif violation == "unassigned_without_rationale":
        system_row["requirement_ids"] = ["R1"]
        invalid["unassigned_requirements"] = [{"id": "R2"}]
        diagnostic = "non-empty rationale"
    elif violation == "omitted_without_rationale":
        invalid["omitted_artifacts"][0].pop("rationale")
        diagnostic = "non-empty rationale"
    elif violation == "dependency_cycle":
        requirements_row["depends_on"] = [SYSTEM]
        diagnostic = "depends_on cycle:"
    else:
        body = json.dumps(valid)
        invalid = '{"required_artifacts": [], ' + body[1:]
        diagnostic = "repeats JSON key"
    prompts, responses = fake_ai
    responses.extend([invalid, valid])

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(valid)
    assert len(prompts) == 2
    assert diagnostic not in prompts[0]
    assert diagnostic in prompts[1]


def test_accounting_rejects_a_candidate_list_that_repeats_an_id_before_calling_the_ai(tmp_path, fake_ai):
    row = {"id": DIGEST_RULES, "title": "Digest Rules", "depends_on": [], "scope": "Rules for the digest."}
    _project(tmp_path, catalog=[row, dict(row)])
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, _responses = fake_ai

    with pytest.raises(ValueError, match="duplicate default artifact id"):
        RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert prompts == []


@pytest.mark.parametrize("wrapper", ["```json\n{}\n```", "```\n{}\n```\n", "\n```json  \n{}\n```  \n"])
def test_accounting_accepts_a_selection_wrapped_in_one_markdown_fence_without_retrying(tmp_path, fake_ai, wrapper):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, responses = fake_ai
    valid = _generic_response()
    responses.append(wrapper.format(json.dumps(valid, indent=2)))

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(valid)
    assert len(prompts) == 1


def test_accounting_still_rejects_prose_around_the_json_object(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, responses = fake_ai
    valid = _generic_response()
    responses.extend(["Here is the selection:\n" + json.dumps(valid), valid])

    report = RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert report["required_artifacts"] == _legacy_artifacts(valid)
    assert len(prompts) == 2
    assert "valid json" in prompts[1].lower()


_FRONT_MATTER = (
    "---\n"
    "codd:\n"
    '  node_id: "req:sample"\n'
    "  depends_on:\n"
    '    - id: "req:other"\n'
    "      relation: derives_from\n"
    "\n"
    "  modules:\n"
    "    - core\n"
    "{closing}\n"
)


@pytest.mark.parametrize("closing", ["---", "..."])
def test_leading_front_matter_is_context_and_never_a_requirement_item(closing):
    text = _FRONT_MATTER.format(closing=closing) + "# Sample\n\nKeep a local log.\n\n- Export the log as CSV.\n"

    items, prompt = number_requirements([("docs/requirements/sample.md", text)])

    assert [(item["id"], item["start_line"], item["text"]) for item in items] == [
        ("R1", 13, "Keep a local log."),
        ("R2", 15, "- Export the log as CSV."),
    ]
    assert 'node_id: "req:sample"' in prompt
    assert not re.search(r"R\d+ \|[^\n]*(codd:|node_id|req:other|modules:|- core)", prompt)


@pytest.mark.parametrize(
    "opening",
    ["---\n\nKeep a local log.\n\n---\n", "---\n- Keep a local log.\n---\n", "---\n\ncodd: [unclosed\n\n---\n", "---\ncodd:\n  node_id: x\n"],
    ids=["paragraph", "list", "invalid-yaml", "unclosed"],
)
def test_a_leading_rule_that_is_not_front_matter_keeps_its_lines_as_items(opening):
    items, _prompt = number_requirements([("requirements.md", opening + "\nExport the log as CSV.\n")])

    texts = [item["text"] for item in items]
    assert texts[-1] == "Export the log as CSV."
    assert len(texts) >= 2


def test_accounted_prompt_asks_for_reasons_in_the_language_of_the_requirements(tmp_path, fake_ai):
    _project(tmp_path)
    _requirement(tmp_path, "requirements.md", "Keep a local log.\n")
    prompts, responses = fake_ai
    responses.append(_generic_response())

    RequiredArtifactsDeriver(tmp_path, ai_command="mock-ai").derive_accounted([])

    assert "in the language of the requirement documents" in prompts[0]
