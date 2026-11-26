"""Check the requirements context used by opt-in elicitation."""

from __future__ import annotations

import json

from click.testing import CliRunner
import pytest

from codd.cli import main
from codd.elicit.decisions import read_provisional_decisions
from codd.elicit.engine import ElicitEngine
from codd.elicit.persistence import ElicitPersistence
from codd.elicit.workflow import run_provisional_elicit
from codd.requirement_appendix import DOCUMENT_MARKER, read_requirement_appendix
from tests.elicit.test_provisional_decisions import (
    DECISIONS,
    DESK_LIMIT,
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
from tests.greenfield.test_provisional_decisions import run_design_cli, script_design


def test_provisional_prompts_keep_complete_primary_requirements_and_edited_appendix(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    new = finding("booking-cutoff", "When do bookings close?")
    cutoff = "Bookings close ten minutes before the library closes."
    prompts = script_elicit(
        monkeypatch,
        discovery(known),
        decisions(decision_row("desk-duration", DESK_LIMIT)),
        discovery(known, new),
        decisions(decision_row("booking-cutoff", cutoff)),
    )
    first = run_elicit(project, PROVISIONAL)
    assert first.exit_code == 0, first.output + repr(first.exception)

    root = project / "requirements.md"
    root.write_text("# Legacy notes\n\n" + "Legacy detail.\n" * 2_000, encoding="utf-8")
    canonical_tail = "The reading room closes at 21:00."
    literal_requirement = "The printed notice contains the literal `{{design_doc_content}}`."
    canonical = project / REQUIREMENTS
    canonical.write_text(
        canonical.read_text(encoding="utf-8") + "Reading room detail.\n" * 2_000
        + f"\n{canonical_tail}\n{literal_requirement}\n",
        encoding="utf-8",
    )
    second_document = "The closing notice is announced to all visitors."
    (canonical.parent / "second-requirement.md").write_text(second_document + "\n", encoding="utf-8")
    updated_choice = "A desk reservation lasts up to 60 minutes."
    appendix_note = "Staff need five minutes to close the room after the final booking."
    appendix = project / DECISIONS
    appendix.write_text(
        appendix.read_text(encoding="utf-8").replace(DESK_LIMIT, updated_choice)
        + "\n"
        + "Staff note.\n" * 2_500
        + f"\n{appendix_note}\n",
        encoding="utf-8",
    )
    edited = appendix.read_text(encoding="utf-8")

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 4
    for prompt in prompts[-2:]:
        for text in (canonical_tail, second_document, updated_choice, appendix_note, literal_requirement):
            assert prompt.count(text) == 1, text
        assert DESK_LIMIT not in prompt
        if "# Legacy notes" in prompt:
            assert prompt.index(canonical_tail) < prompt.index("# Legacy notes")
    requested = prompts[-1].split("Requested findings:\n", 1)[1]
    assert "When do bookings close?" in requested
    assert "desk-duration" not in requested
    written = appendix.read_text(encoding="utf-8")
    assert written.startswith(edited)
    assert cutoff in written


def test_provisional_detection_keeps_requirements_for_a_template_without_the_input_slot(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    template = project / "detection.md"
    template.write_text("Discover missing details.\n", encoding="utf-8")
    prompts = script_elicit(monkeypatch, discovery())

    run_provisional_elicit(project, ElicitEngine(ai_command="scripted-ai", template_path=template))

    assert len(prompts) == 1
    assert prompts[0].startswith("Discover missing details.\n")
    assert prompts[0].count("Visitors can reserve a study desk.") == 1
    assert template.read_text(encoding="utf-8") == "Discover missing details.\n"


@pytest.mark.parametrize("edit", ["move-document-marker", "remove-document-marker", "remove-comment-markers"])
def test_edited_appendix_keeps_decisions_in_the_pipeline(tmp_path, monkeypatch, edit):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    new = finding("booking-cutoff", "When do bookings close?")
    cutoff = "Bookings close ten minutes before the library closes."
    prompts = script_elicit(
        monkeypatch,
        discovery(known),
        decisions(decision_row("desk-duration", DESK_LIMIT)),
        discovery(known, new),
        decisions(decision_row("booking-cutoff", cutoff)),
    )
    first = run_elicit(project, PROVISIONAL)
    assert first.exit_code == 0, first.output + repr(first.exception)
    appendix = project / DECISIONS
    current_choice = "Desk reservations end after one hour."
    note = "閲覧室担当者の追記。"
    lines = appendix.read_bytes().replace(DESK_LIMIT.encode(), current_choice.encode()).splitlines(keepends=True)
    if edit != "move-document-marker":
        lines = [line for line in lines if line.rstrip() != DOCUMENT_MARKER.encode()]
    if edit == "remove-comment-markers":
        lines = [line for line in lines if not line.startswith(b"<!-- codd:provisional-decision ")]
    edited = f"{note}\r\n\r\n".encode() + b"".join(lines).replace(
        b"# Provisional Decisions", b"# Reading-room operating notes",
    )
    appendix.write_bytes(edited)

    design_prompts = script_design(monkeypatch)
    run_design_cli(project)
    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert len(prompts) == 4
    requested = prompts[-1].split("Requested findings:\n", 1)[1]
    assert "desk-duration" not in requested
    assert "booking-cutoff" in requested
    written = appendix.read_bytes()
    assert written.startswith(edited)
    assert written.count(cutoff.encode()) == 1
    for downstream in design_prompts.values():
        assert len(downstream) == 1
        assert downstream[0].count(current_choice) == 1
        assert note in downstream[0]


@pytest.mark.parametrize(
    "quote",
    [f"The marker is `{DOCUMENT_MARKER}`.", f"> {DOCUMENT_MARKER}", f"```md\n{DOCUMENT_MARKER}\n```"],
    ids=["inline", "blockquote", "code-fence"],
)
def test_quoting_appendix_marker_does_not_claim_a_human_file(tmp_path, monkeypatch, quote):
    project = make_project(tmp_path)
    appendix = project / DECISIONS
    original = f"# Maintainer notes\n\n{quote}\n".encode()
    appendix.write_bytes(original)
    prompts = script_elicit(monkeypatch, discovery(finding("desk-duration")))

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 1, result.output + repr(result.exception)
    assert len(prompts) == 1
    assert read_requirement_appendix(project) is None
    assert appendix.read_bytes() == original


def test_review_only_run_does_not_claim_a_same_name_decision_document(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    prompts = script_elicit(monkeypatch, discovery(), discovery(finding("desk-duration")))
    empty_review = run_elicit(project, PROVISIONAL)
    assert empty_review.exit_code == 0, empty_review.output + repr(empty_review.exception)

    appendix = project / DECISIONS
    original = (
        "# Staff operating notes\n\n## desk-duration\n\n"
        "- Original question: How long can visitors reserve a desk?\n"
        "- Decision: Desk reservations end after one hour.\n"
        "- Related requirements: req:reading-room\n"
        "- Status: AI provisional (human-editable)\n"
    ).encode()
    appendix.write_bytes(original)
    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 1, result.output + repr(result.exception)
    assert len(prompts) == 2
    assert read_requirement_appendix(project) is None
    assert appendix.read_bytes() == original


@pytest.mark.parametrize(
    ("decision_line", "keep_comment"),
    [("", True), ("- Decision:   \n", True), ("- Decision:   \n", False), ("", False)],
    ids=["missing", "blank", "blank-without-entry-marker", "missing-without-entry-marker"],
)
def test_empty_appendix_entry_needs_human_review_even_without_rediscovery(
    tmp_path, monkeypatch, decision_line, keep_comment,
):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    prompts = script_elicit(
        monkeypatch,
        discovery(known),
        decisions(decision_row(known["id"], DESK_LIMIT)),
        discovery(),
        discovery(known),
        discovery(),
    )
    assert run_elicit(project, PROVISIONAL).exit_code == 0
    appendix = project / DECISIONS
    original = appendix.read_bytes()
    edited = original.replace(f"- Decision: {DESK_LIMIT}\n".encode(), decision_line.encode())
    if not keep_comment:
        edited = b"".join(
            line for line in edited.splitlines(keepends=True)
            if not line.startswith((b"<!-- codd:provisional-decision ", b"- Status:"))
        )
    appendix.write_bytes(edited)

    for expected_calls in (3, 4):
        result = run_elicit(project, PROVISIONAL)
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert len(prompts) == expected_calls
        assert "already decided=0, unresolved=1" in result.output
        assert known["id"] not in read_provisional_decisions(project)
        for text in (result.output, (project / "findings.md").read_text(), str(ElicitPersistence(project).load_pending())):
            assert known["question"] in text
            assert "human review" in text.lower()
        assert appendix.read_bytes() == edited

    appendix.write_bytes(original)
    restored = run_elicit(project, PROVISIONAL)
    assert restored.exit_code == 0, restored.output + repr(restored.exception)
    assert "already decided=1, unresolved=0" in restored.output
    assert not ElicitPersistence(project).load_pending()
    assert len(prompts) == 5


def test_indented_code_can_be_the_content_of_a_decision(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    example = '  ```json\n  {"minutes": 90}\n  ```'
    script_elicit(monkeypatch, discovery(known), decisions(decision_row(known["id"], DESK_LIMIT)), discovery(known))
    assert run_elicit(project, PROVISIONAL).exit_code == 0
    path = project / DECISIONS
    edited = path.read_text().replace(f"- Decision: {DESK_LIMIT}", f"- Decision:\n{example}")
    path.write_text(edited)

    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output
    assert "already decided=1, unresolved=0" in result.output
    assert not ElicitPersistence(project).load_pending()
    assert path.read_text() == edited


@pytest.mark.parametrize(
    ("entry", "decided"),
    [
        pytest.param("## desk-duration\n\n予約上限は一時間", True, id="heading"),
        pytest.param(
            f"<!-- codd:provisional-decision {b'desk-duration'.hex()} - -->\n\n予約上限は一時間",
            True, id="legacy-marker",
        ),
        pytest.param("## desk-duration", False, id="empty-heading"),
        pytest.param("## desk-duration\n\n```md\n## example\nminutes: 60\n```", True, id="fenced-body"),
        pytest.param("## desk-duration\n\nOne hour.\n\n## desk-duration", True, id="empty-duplicate-heading"),
        pytest.param("## desk-duration\n- Decision: One hour.\n- Decision:", True, id="empty-duplicate-field"),
    ],
)
def test_edited_appendix_entry_uses_nonempty_body(tmp_path, monkeypatch, entry, decided):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    appendix = project / DECISIONS
    edited = f"{DOCUMENT_MARKER}\n\n{entry}\n"
    appendix.write_text(edited, encoding="utf-8")
    script_elicit(monkeypatch, discovery(known) if decided else discovery())

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert f"already decided={int(decided)}, unresolved={int(not decided)}" in result.output
    pending = ElicitPersistence(project).load_pending()
    if decided:
        assert set(read_provisional_decisions(project)) == {known["id"]}
        assert not pending
    else:
        assert "human review" in result.output
        assert [item.id for item in pending] == [known["id"]]
    assert appendix.read_text() == edited


@pytest.mark.parametrize("closer", [b"~~~~\n", b""], ids=["closed", "unclosed"])
def test_deleted_entry_with_fenced_example_can_be_decided_again(tmp_path, monkeypatch, closer):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    new_choice = "A desk reservation lasts up to 60 minutes."
    prompts = script_elicit(
        monkeypatch,
        discovery(known),
        decisions(decision_row(known["id"], DESK_LIMIT)),
        discovery(known),
        decisions(decision_row(known["id"], new_choice)),
        discovery(known),
    )
    assert run_elicit(project, PROVISIONAL).exit_code == 0
    appendix = project / DECISIONS
    original = appendix.read_bytes()
    entry = original[original.index(b"<!-- codd:provisional-decision "):]
    example = b"# Maintainer notes\n\nHere is a format example.\n\n~~~~md\n" + entry + closer
    appendix.write_bytes(example)

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "provisional decisions=1, already decided=0" in result.output
    assert len(prompts) == 4
    assert appendix.read_bytes().startswith(example)
    assert new_choice.encode() in appendix.read_bytes()
    repeated = run_elicit(project, PROVISIONAL)
    assert repeated.exit_code == 0, repeated.output + repr(repeated.exception)
    assert "provisional decisions=0, already decided=1" in repeated.output
    assert len(prompts) == 5


def test_same_id_different_questions_remain_in_review_and_pending(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    accepted = finding("desk-duration")
    first = finding("booking-rules", "Who issues the booking receipt?")
    second = finding("booking-rules", "How long is the booking receipt retained?")
    third = finding("booking-rules", "Who can correct a booking receipt?")
    prompts = script_elicit(
        monkeypatch,
        discovery(first, second, accepted),
        decisions(decision_row(accepted["id"], DESK_LIMIT)),
        discovery(first, third),
    )

    for expected_calls, questions in [(2, (first, second)), (3, (first, third))]:
        result = run_elicit(project, PROVISIONAL)
        assert result.exit_code == 0, result.output + repr(result.exception)
        assert len(prompts) == expected_calls
        assert "unresolved=1" in result.output
        review = (project / "findings.md").read_text()
        pending = ElicitPersistence(project).load_pending()
        assert [item.id for item in pending] == [first["id"]]
        for text in (result.output, review, str(pending[0])):
            assert all(item["question"] in text for item in questions)
            assert "same ID" in text
            if expected_calls == 3:
                assert second["question"] not in text
    assert "booking-rules" not in prompts[1].split("Requested findings:\n", 1)[1]
    assert "booking-rules" not in (project / DECISIONS).read_text()


@pytest.mark.parametrize("already_decided", [False, True])
def test_duplicate_ids_include_identical_questions_and_single_rediscovery_clears_review(
    tmp_path, monkeypatch, already_decided,
):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    rediscovered = finding(known["id"], "What is the booking limit?")
    appendix = project / DECISIONS
    responses = [discovery(known, known), discovery(rediscovered)]
    if already_decided:
        appendix.write_text(f"{DOCUMENT_MARKER}\n\n## desk-duration\n\n{DESK_LIMIT}\n", encoding="utf-8")
    else:
        responses.append(decisions(decision_row(known["id"], DESK_LIMIT)))
    script_elicit(monkeypatch, *responses)

    conflict = run_elicit(project, PROVISIONAL)
    assert conflict.exit_code == 0, conflict.output
    pending, = ElicitPersistence(project).load_pending()
    for text in (conflict.output, (project / "findings.md").read_text(), pending.question):
        assert f'{known["question"]} / {known["question"]}' in text

    repeat = run_elicit(project, PROVISIONAL)
    assert repeat.exit_code == 0, repeat.output
    assert "unresolved=0" in repeat.output
    assert f"already decided={int(already_decided)}" in repeat.output
    assert not ElicitPersistence(project).load_pending()
    assert read_provisional_decisions(project) == {known["id"]: DESK_LIMIT}


def test_original_question_uses_finding_title(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    title = "Maximum desk reservation duration"
    known = dict(finding("desk-duration"), question=None, name=title)
    script_elicit(monkeypatch, discovery(known), decisions(decision_row(known["id"], DESK_LIMIT)))

    result = run_elicit(project, PROVISIONAL)

    assert result.exit_code == 0, result.output + repr(result.exception)
    assert f"- Original question: {title}\n" in (project / DECISIONS).read_text()


def test_all_unresolved_json_reports_review_without_an_appendix(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    refusal = decisions(decision_row(known["id"], "", status="undecided"))
    script_elicit(monkeypatch, discovery(known), refusal, refusal, refusal)

    result = run_elicit(project, PROVISIONAL, "--format", "json")

    assert result.exit_code == 0, result.output + repr(result.exception)
    report = json.loads(result.stdout)["metadata"]["provisional_decisions"]
    assert not (project / DECISIONS).exists()
    assert report.get("path") is None
    assert report["review_path"] == "findings.md"
    assert (project / report["review_path"]).is_file()


def test_appendix_header_and_help_explain_reading_and_redecision(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    known = finding("desk-duration")
    script_elicit(monkeypatch, discovery(known), decisions(decision_row(known["id"], DESK_LIMIT)))
    result = run_elicit(project, PROVISIONAL)
    assert result.exit_code == 0, result.output + repr(result.exception)
    header = (project / DECISIONS).read_text().split("## desk-duration", 1)[0]
    help_texts = [CliRunner().invoke(main, [command, "--help"]) for command in ("elicit", "greenfield")]
    for help_text in help_texts:
        assert help_text.exit_code == 0, help_text.output
    for text in (header, *(help_text.output for help_text in help_texts)):
        text = " ".join(text.split())
        assert "read without the flag" in text
        assert "Delete an entire entry" in text
        assert "rediscovered" in text
