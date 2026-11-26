"""Exercise provisional decisions through the normal elicit CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

from click.testing import CliRunner
import pytest
import yaml

from codd.cli import main
from codd.deployment.providers.ai_command import SubprocessAiCommand
from codd.elicit.apply import load_findings_from_file
from codd.elicit.decisions import read_provisional_decisions
from codd.elicit.finding import Finding
from codd.elicit.persistence import ElicitPersistence


REQUIREMENTS = Path("docs/requirements/requirements.md")
DECISIONS = Path("docs/requirements/provisional-decisions.md")
HUMAN_REQUIREMENTS = (
    "---\r\ncodd:\r\n  node_id: req:reading-room\r\n  type: requirement\r\n---\r\n"
    "\r\n# Reading room\r\n\r\nVisitors can reserve a study desk.\r\n参加者向け。\r\n"
).encode("utf-8")
DESK_LIMIT = "A desk reservation lasts up to 90 minutes."
DESK_QUESTION = "How long can visitors reserve a desk?"
PROVISIONAL = "--provisional-decisions"


def make_project(root: Path) -> Path:
    requirements = root / REQUIREMENTS
    requirements.parent.mkdir(parents=True)
    requirements.write_bytes(HUMAN_REQUIREMENTS)
    (root / "codd").mkdir()
    (root / "codd/codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "reading-room", "language": "python"},
                "ai_command": "scripted-ai",
                "scan": {"doc_dirs": ["docs/requirements", "docs/design"]},
                "test_coverage": {"gate": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "project_lexicon.yaml").write_text(
        "node_vocabulary: []\nnaming_conventions: []\ndesign_principles: []\n",
        encoding="utf-8",
    )
    return root


def finding(finding_id: str, question: str = DESK_QUESTION) -> dict:
    return {
        "id": finding_id,
        "kind": "coverage_gap",
        "severity": "medium",
        "question": question,
        "related_requirement_ids": ["req:reading-room"],
    }


def discovery(*findings: dict) -> str:
    return json.dumps({"findings": findings, "lexicon_coverage_report": {}})


def decision_row(finding_id: str, decision: str, *, status="decided", reason="Keep booking simple.") -> dict:
    return {"id": finding_id, "status": status, "decision": decision, "reason": reason}


def decisions(*rows: dict) -> str:
    return json.dumps({"decisions": rows})


def script_elicit(monkeypatch, *responses: str) -> list[str]:
    prompts = []
    remaining = iter(responses)

    def invoke(self, prompt, *args, **kwargs):
        prompts.append(prompt)
        try:
            return next(remaining)
        except StopIteration:
            pytest.fail(f"Unexpected elicit AI call {len(prompts)}")

    monkeypatch.setattr(SubprocessAiCommand, "invoke", invoke)
    return prompts


def run_elicit(root: Path, *options: str):
    return CliRunner().invoke(main, ["elicit", "--path", str(root), *options])


def decision_feedback(prompt: str) -> list[str]:
    return json.loads(prompt.rsplit("Validation feedback from the previous attempt:\n", 1)[1])


def test_elicit_writes_provisional_requirements(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    finding_id = "desk-duration"
    prompts = script_elicit(
        monkeypatch,
        discovery(finding(finding_id)),
        decisions(decision_row(finding_id, DESK_LIMIT)),
    )

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    assert DESK_QUESTION in prompts[1]
    written = (project / DECISIONS).read_text(encoding="utf-8")
    assert finding_id in written
    assert DESK_LIMIT in written
    assert "req:reading-room" in written
    assert "Keep booking simple." in written
    assert DECISIONS.as_posix() in result.output
    assert (project / REQUIREMENTS).read_bytes() == HUMAN_REQUIREMENTS


def test_elicit_markdown_reports_decisions_unresolved_reasons_and_count(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    unresolved = "wheelchair-width"
    reason = "The reading room has no measured clearance."
    undecided = decision_row(unresolved, "", status="undecided", reason=reason)
    script_elicit(
        monkeypatch,
        discovery(finding("desk-duration"), finding(unresolved, "How wide is the access aisle?")),
        decisions(decision_row("desk-duration", DESK_LIMIT), undecided),
        decisions(undecided),
        decisions(undecided),
    )

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    lines = result.stdout.splitlines()
    assert any("desk-duration" in line and DESK_LIMIT in line for line in lines)
    assert any(unresolved in line and "unresolved" in line and reason in line for line in lines)
    added_count = re.search(r"^Elicit provisional decisions=(\d+),", result.stdout, re.MULTILINE)
    assert added_count is not None
    assert int(added_count[1]) == len(read_provisional_decisions(project)) == 1


def test_elicit_requests_both_answer_fields_in_requirement_language(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    requirement = "来館者は自習席を予約できる。"
    (project / REQUIREMENTS).write_text(f"# 自習室\n\n{requirement}\n", encoding="utf-8")
    prompts = script_elicit(
        monkeypatch,
        discovery(finding("desk-duration", "席は何分まで予約できるか。")),
        decisions(decision_row("desk-duration", "予約は90分まで", reason="席の回転を保つため。")),
    )

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    instructions, context = prompts[1].split("Requirements and existing decisions:\n", 1)
    assert requirement in context
    assert "Use the language of the related requirements for decision and reason." in instructions
    assert (
        "Do not add features, screens, or integrations the requirements do not ask for" in instructions
    )
    assert "out of scope for this version" in instructions
    assert "full stop" not in instructions


def test_elicit_retries_a_decision_with_a_blank_reason(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    finding_id = "desk-duration"
    reason = "Keep booking simple."
    prompts = script_elicit(
        monkeypatch,
        discovery(finding(finding_id)),
        decisions(decision_row(finding_id, "Bookings last up to an hour.", reason=" \t ")),
        decisions(decision_row(finding_id, DESK_LIMIT, reason=reason)),
    )

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 3
    assert decision_feedback(prompts[2]) == [f"{finding_id}: Reason is empty or is not text"]
    assert read_provisional_decisions(project) == {finding_id: DESK_LIMIT}
    assert f"- Reason: {reason}" in (project / DECISIONS).read_text(encoding="utf-8")


@pytest.mark.parametrize("output_format", ["md", "json"])
def test_elicit_retries_and_reports_unexpected_row_with_all_known_ids_answered(
    tmp_path, monkeypatch, output_format,
):
    project = make_project(tmp_path)
    known = "desk-duration"
    unknown = "unrequested-lockers"
    unexpected = f"Unknown finding ID: {unknown!r}"
    response = decisions(decision_row(known, DESK_LIMIT), decision_row(unknown, "Lockers are free."))
    prompts = script_elicit(monkeypatch, discovery(finding(known)), response, response, response)

    result = run_elicit(project, PROVISIONAL, "--format", output_format)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 4
    assert decision_feedback(prompts[2]) == [unexpected]
    assert decision_feedback(prompts[3]) == [unexpected]
    assert read_provisional_decisions(project) == {known: DESK_LIMIT}
    assert not ElicitPersistence(project).load_pending()
    if output_format == "json":
        report = json.loads(result.stdout)["metadata"]["provisional_decisions"]
        assert report["issues"] == [unexpected]
    else:
        assert any("warning" in line.lower() and unexpected in line for line in result.stdout.splitlines())


def test_elicit_limits_ai_supplied_validation_diagnostics(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    finding_id = "desk-duration"
    reason = "Needs floor measurements. " + "Unneeded detail " * 100
    unexpected_id = "unknown-" + "too-long-" * 100
    response = decisions(
        decision_row(finding_id, "", status="undecided", reason=reason),
        decision_row(unexpected_id, "Lockers are free."),
    )
    prompts = script_elicit(monkeypatch, discovery(finding(finding_id)), response, response, response)

    result = run_elicit(project, PROVISIONAL, "--format", "json")

    assert result.exit_code == 0, result.output + repr(result.exception)
    report = json.loads(result.stdout)["metadata"]["provisional_decisions"]
    reason = report["unresolved"][finding_id]["reason"]
    issue, = report["issues"]
    assert reason.startswith("Undecided: Needs floor measurements.") and len(reason) <= 300
    assert issue.startswith("Unknown finding ID: 'unknown-") and len(issue) <= 300
    assert decision_feedback(prompts[-1]) == [issue, f"{finding_id}: {reason}"]
    pending, = ElicitPersistence(project).load_pending()
    assert pending.details["provisional_decision_reason"] == reason


@pytest.mark.parametrize("stage", ["discovery", "decision"])
def test_elicit_does_not_reuse_failed_command_output_as_a_reason(tmp_path, stage):
    project = make_project(tmp_path)
    private_requirement = "PRIVATE-DRAFT-COLLECTION-PLAN"
    (project / REQUIREMENTS).write_text(
        f"# Reading room\n\n{private_requirement}\n" + "body " * 850, encoding="utf-8",
    )
    fake_ai = project / "fake_ai.py"
    discovery_response = discovery(finding("desk-duration"), finding("desk-access", "Who can reserve a desk?"))
    fake_ai.write_text(
        "import json\nimport sys\nfrom pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        f"if {stage == 'discovery'} or prompt.startswith('Generate provisional decisions'):\n"
        "    with Path('failed_prompts.jsonl').open('a', encoding='utf-8') as log:\n"
        "        log.write(json.dumps(prompt) + '\\n')\n"
        "    sys.stderr.write(prompt)\n"
        "    sys.exit(2)\n"
        f"print({discovery_response!r})\n",
        encoding="utf-8",
    )

    result = run_elicit(project, PROVISIONAL, "--ai-cmd", shlex.join([sys.executable, str(fake_ai)]))

    prompts = [
        json.loads(line)
        for line in (project / "failed_prompts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert all(prompt.count(private_requirement) == 1 for prompt in prompts)
    assert private_requirement not in result.output
    assert "exit code 2" in result.output
    persistence = ElicitPersistence(project)
    pending = persistence.load_pending()
    if stage == "discovery":
        assert result.exit_code == 1
        assert len(prompts) == 1
        assert "AI command failed while discovering findings" in result.output
        assert not pending and not (project / "findings.md").exists()
        return

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 3
    assert private_requirement not in (project / "findings.md").read_text(encoding="utf-8")
    assert private_requirement not in persistence.pending_path.read_text(encoding="utf-8")
    assert {item.id for item in pending} == {"desk-duration", "desk-access"}
    for item in pending:
        reason = item.details["provisional_decision_reason"]
        assert 0 < len(reason) <= 300 and private_requirement not in reason
        assert reason in result.output
        for prompt in prompts[1:]:
            assert f"{item.id}: {reason}" in decision_feedback(prompt)


@pytest.mark.parametrize(
    ("invalid_rows", "diagnostic"),
    [
        ([], r"missing|omitted"),
        ([decision_row("unrequested-lockers", "Lockers are available for free.")], r"unknown|unexpected"),
        ([decision_row("desk-duration", DESK_LIMIT)] * 2, r"duplicate|multiple"),
        ([decision_row("desk-duration", "  ")], r"empty|blank"),
        ([decision_row("desk-duration", "上限は90分ですか？")], r"question|interrogative"),
        ([decision_row("desk-duration", DESK_QUESTION.rstrip("?"))], r"question|repeat"),
        ([decision_row("desk-duration", "90 minutes", status="question")], r"question"),
        ([decision_row("desk-duration", "90 minutes", status="undecided")], r"undecided"),
    ],
    ids=["missing", "unknown", "duplicate", "blank", "question", "repeated-question", "question-status", "undecided"],
)
def test_elicit_retries_with_validation_reason(tmp_path, monkeypatch, invalid_rows, diagnostic):
    project = make_project(tmp_path)
    cutoff = "Bookings close ten minutes before the library closes."
    prompts = script_elicit(
        monkeypatch,
        discovery(finding("desk-duration"), finding("booking-cutoff", "When do bookings close?")),
        decisions(decision_row("booking-cutoff", cutoff), *invalid_rows),
        decisions(decision_row("desk-duration", DESK_LIMIT)),
    )

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 3
    original_lines = set(prompts[1].splitlines())
    feedback = "\n".join(line for line in prompts[2].splitlines() if line not in original_lines)
    assert re.search(diagnostic, feedback, re.IGNORECASE), feedback
    written = (project / DECISIONS).read_text(encoding="utf-8")
    assert written.count(DESK_LIMIT) == 1
    assert written.count(cutoff) == 1
    assert "unrequested-lockers" not in written
    assert not ElicitPersistence(project).load_pending()


def test_elicit_keeps_human_edits_and_decides_only_new_findings(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    original = finding("desk-duration")
    same_id = finding("desk-duration", "What is the maximum desk booking duration?")
    cutoff = "Bookings close ten minutes before the library closes."
    prompts = script_elicit(
        monkeypatch,
        discovery(original),
        decisions(decision_row("desk-duration", DESK_LIMIT)),
        discovery(same_id),
        discovery(original, finding("booking-cutoff", "When do bookings close?")),
        decisions(decision_row("booking-cutoff", cutoff)),
    )
    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output + repr(result.exception)
    path = project / DECISIONS
    path.chmod(0o660)
    hand_edited = path.read_bytes().replace(DESK_LIMIT.encode(), "予約は60分まで。".encode())
    path.write_bytes(hand_edited + b"\nHuman note for the next review.\n")
    hand_edited = path.read_bytes()

    repeat = run_elicit(project, PROVISIONAL)
    assert repeat.exit_code == 0, repeat.output + repr(repeat.exception)
    assert len(prompts) == 3
    assert "予約は60分まで。" in prompts[2]
    assert path.read_bytes() == hand_edited
    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 5
    assert "When do bookings close?" in prompts[-1]
    written = path.read_bytes()
    assert written.startswith(hand_edited)
    assert DESK_LIMIT.encode() not in written
    assert written.count(cutoff.encode()) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    assert (project / REQUIREMENTS).read_bytes() == HUMAN_REQUIREMENTS


def test_elicit_json_reports_partial_decisions_and_keeps_review(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    unresolved = "wheelchair-width"
    reason = "The reading room has no measured clearance."
    undecided = decision_row(unresolved, "", status="undecided", reason=reason)
    prompts = script_elicit(
        monkeypatch,
        discovery(finding("desk-duration"), finding(unresolved, "How wide is the access aisle?")),
        decisions(decision_row("desk-duration", DESK_LIMIT), undecided),
        decisions(undecided),
        decisions(undecided),
    )

    result = run_elicit(project, PROVISIONAL, "--format", "json")

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 4
    payload = json.loads(result.stdout)
    report = payload["metadata"]["provisional_decisions"]
    assert report["path"] == DECISIONS.as_posix()
    assert report["decided_count"] == 1
    assert report["unresolved_count"] == 1
    assert report["decisions"] == {"desk-duration": DESK_LIMIT}
    assert report["unresolved"][unresolved]["question"] == "How wide is the access aisle?"
    assert reason in report["unresolved"][unresolved]["reason"]
    assert [item["id"] for item in payload["findings"]] == [unresolved]
    assert reason in json.dumps(payload["findings"])
    pending = ElicitPersistence(project).load_pending()
    assert [item.id for item in pending] == [unresolved]
    assert reason in json.dumps(pending[0].to_dict())
    review = (project / "findings.md").read_text(encoding="utf-8")
    assert unresolved in review and reason in review
    assert "desk-duration" not in review
    written = (project / DECISIONS).read_text(encoding="utf-8")
    assert DESK_LIMIT in written
    assert unresolved not in written
    assert (project / REQUIREMENTS).read_bytes() == HUMAN_REQUIREMENTS


def test_elicit_resolves_prior_pending_without_reopening_reviewed_findings(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    prior_pending = finding("desk-duration")
    approved = finding("notice-period", "How early can visitors reserve a desk?")
    rejected = finding("unlimited-desks", "Can visitors reserve more than one desk?")
    deferred = finding("late-hours", "Can visitors stay after closing?")
    input_file = project / "legacy-review.json"
    input_file.write_text(
        json.dumps([
            prior_pending,
            deferred,
            {**approved, "details": {"decision": "approve"}},
            {**rejected, "details": {"decision": "reject"}},
        ]),
        encoding="utf-8",
    )
    legacy = CliRunner().invoke(main, ["elicit", "apply", str(input_file), "--path", str(project)])
    assert legacy.exit_code == 0, legacy.output + repr(legacy.exception)
    assert {item.id for item in ElicitPersistence(project).load_pending()} == {"desk-duration", "late-hours"}
    review = project / "findings.md"
    review.write_text(
        review.read_text(encoding="utf-8").replace("[ ] `late-hours`", "[d] `late-hours`"),
        encoding="utf-8",
    )
    before = (project / REQUIREMENTS).read_bytes()

    prompts = script_elicit(
        monkeypatch,
        discovery(prior_pending, approved, rejected, deferred),
        decisions(decision_row("desk-duration", DESK_LIMIT)),
    )
    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 2
    assert DESK_QUESTION in prompts[1]
    assert [item.id for item in ElicitPersistence(project).load_pending()] == ["late-hours"]
    assert ElicitPersistence(project).load_ignored() == {"unlimited-desks"}
    assert (project / REQUIREMENTS).read_bytes() == before
    written = (project / DECISIONS).read_text(encoding="utf-8")
    assert DESK_LIMIT in written
    assert all(known not in written for known in ("notice-period", "unlimited-desks", "late-hours"))
    review_text = review.read_text(encoding="utf-8")
    assert "desk-duration" not in review_text
    assert "[d] `late-hours`" in review_text


def test_elicit_preserves_existing_notes_when_recording_and_updating_review(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    report = project / "findings.md"
    notes = "# Staff notes\r\n\r\n次の会議で閉館時刻を確認する。".encode("utf-8")
    report.write_bytes(notes)
    reason = "The regular closing time is missing."
    finding_id = "booking-cutoff"
    question = "When do bookings close?"
    item = finding(finding_id, question)
    unresolved = decisions(decision_row(finding_id, "", status="undecided", reason=reason))
    script_elicit(
        monkeypatch,
        discovery(item), unresolved, unresolved, unresolved,
        discovery(item), unresolved, unresolved, unresolved,
        discovery(item), decisions(decision_row(finding_id, "Bookings close at 20:30.")),
    )

    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output + repr(result.exception)
    first = report.read_bytes()
    assert first.startswith(notes)
    assert first.count(b"- approval: [ ] `booking-cutoff`") == 1
    assert question.encode() in first and reason.encode() in first
    assert [finding.id for finding in load_findings_from_file(report)] == [finding_id]

    followup = b"\nStaff meet on Wednesday.\n"
    report.write_bytes(first + followup)
    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert report.read_bytes() == first + followup

    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output + repr(result.exception)
    final = report.read_bytes()
    assert final.startswith(notes) and final.endswith(followup)
    assert finding_id.encode() not in final
    assert not ElicitPersistence(project).load_pending()


@pytest.mark.parametrize("linked", [False, True], ids=["new", "symlink"])
def test_unflagged_apply_uses_normal_pending_file_write(tmp_path, linked):
    project = make_project(tmp_path)
    pending = ElicitPersistence(project).pending_path
    if linked:
        target = project / "review.yaml"
        target.write_text("pending: []\n", encoding="utf-8")
        target.chmod(0o640)
        pending.parent.mkdir(parents=True)
        pending.symlink_to(target)
    else:
        target = pending
    input_file = project / "findings.json"
    input_file.write_text(json.dumps([finding("desk-duration")]), encoding="utf-8")
    mask = os.umask(0o022)
    try:
        result = CliRunner().invoke(main, ["elicit", "apply", str(input_file), "--path", str(project)])
    finally:
        os.umask(mask)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert pending.is_symlink() == linked
    assert stat.S_IMODE(target.stat().st_mode) == (0o640 if linked else 0o644)
    assert yaml.safe_load(target.read_text())["pending"][0]["finding"]["id"] == "desk-duration"


def test_flagged_pending_update_keeps_link_permissions_and_prior_bytes_on_failure(tmp_path, monkeypatch):
    persistence = ElicitPersistence(tmp_path)
    target = persistence.pending_path.with_name("review.yaml")
    target.parent.mkdir(parents=True)
    target.write_text("pending: []\n", encoding="utf-8")
    target.chmod(0o640)
    persistence.pending_path.symlink_to(target.name)
    prior = Finding.from_dict(finding("desk-duration"))

    persistence.reconcile_pending([prior], resolved_ids=set())
    assert persistence.pending_path.is_symlink()
    assert persistence.load_pending() == [prior]
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    original = target.read_bytes()
    write_text = Path.write_text

    def interrupt(path, content, *args, **kwargs):
        if path.parent == target.parent and content.startswith("pending:"):
            write_text(path, "pending: [", encoding="utf-8")
            raise OSError("interrupted pending write")
        return write_text(path, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupt)
    with pytest.raises(OSError, match="interrupted pending write"):
        persistence.reconcile_pending([], resolved_ids={prior.id})
    assert persistence.pending_path.is_symlink()
    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
