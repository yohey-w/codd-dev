"""Protect written decisions and reject incomplete AI answers."""

import json
import re

import pytest

from codd.elicit.decisions import read_provisional_decisions, resolve_provisional_decisions
from codd.elicit.finding import Finding


def _finding(finding_id, question, *requirements):
    return Finding(
        id=finding_id, kind="unclear", severity="medium", question=question,
        related_requirement_ids=list(requirements),
    )


def _respond(finding_id, decision):
    return {
        "id": finding_id,
        "status": "decided",
        "decision": decision,
        "reason": "Makes the behavior predictable.",
    }


def _resolve(root, findings, invoke):
    return resolve_provisional_decisions(root, findings, invoke=invoke, requirements_context="")


@pytest.mark.parametrize(
    "placeholder", ["未定", "TBD", "...", "Later…", "–.", "未定。", "TBD.", "T.B.D.", "PENDING.", "Yes."],
)
def test_nonempty_answer_has_no_sentence_or_word_requirement(tmp_path, placeholder):
    item = _finding("retention", "How long are copies stored?")
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [_respond(item.id, placeholder)]})

    result = _resolve(tmp_path, [item], invoke)

    assert len(prompts) == 1
    assert result.added_decisions == {item.id: placeholder}


@pytest.mark.parametrize(
    "choice",
    [
        "Copies are stored for three days.",
        "複製は三日間保存する。",
        "تُحفظ النسخ ثلاثة أيام.",
        "प्रतियाँ तीन दिन रखी जाती हैं।",
        "The booking limit is 3.",
        "Return HTTP 429 after the fifth request.",
        "超過時はHTTP429を返す。",
        "禁止。",
        "50%。",
        "เก็บสำเนาไว้สามวัน",
        "- Keep copies for three days\n- Delete expired copies",
        "Evaluate a ? b : c for each item",
    ],
)
def test_complete_answers_with_local_punctuation_are_saved(tmp_path, choice):
    item = _finding("retention", "How long are copies stored?")
    result = _resolve(
        tmp_path, [item], lambda _: json.dumps({"decisions": [_respond(item.id, choice)]}),
    )

    assert result.added_decisions == {item.id: choice}


@pytest.mark.parametrize(
    "choice",
    [
        "Should users have a limit? The team will decide later.",
        "Should users have a limit?The team will decide later.",
        "What is the limit?\nThe team will decide later.",
        "上限を設定しますか？担当者が後で決めます。",
        "هل نضع حدًا؟ سيقرر الفريق لاحقًا.",
    ],
)
def test_internal_question_does_not_block_a_decision(tmp_path, choice):
    item = _finding("limits", "What is the maximum size?")
    result = _resolve(
        tmp_path, [item], lambda _: json.dumps({"decisions": [_respond(item.id, choice)]}),
    )

    assert result.added_decisions == {item.id: choice}


@pytest.mark.parametrize(
    ("choice", "diagnostic"),
    [
        ("Keep them for three days?", "Decision ends with a question"),
        ('“Keep them for three days?”', "Decision ends with a question"),
        ('„Keep them for three days?“', "Decision ends with a question"),
        ('Display “Continue?”', "Decision ends with a question"),
        ('“Start” is replaced by “Continue?”', "Decision ends with a question"),
        ("**Keep them for three days?**", "Decision ends with a question"),
        ("Confirm 【keep them for three days？】", "Decision ends with a question"),
        ("_「三日間ですか？」_", "Decision ends with a question"),
        ("保管は何日？\u0332", "Decision ends with a question"),
        ("`Keep them for three days?`", "Decision ends with a question"),
        ("How long are copies stored.", "Decision repeats the original question"),
    ],
)
def test_question_at_the_end_or_copied_from_finding_is_retried(tmp_path, choice, diagnostic):
    item = _finding("retention", "How long are copies stored?")
    accepted = "Copies are stored for three days"
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        decision = choice if len(prompts) == 1 else accepted
        return json.dumps({"decisions": [_respond(item.id, decision)]})

    result = _resolve(tmp_path, [item], invoke)

    assert len(prompts) == 2
    feedback = prompts[1].split("Validation feedback from the previous attempt:\n", 1)[1]
    assert json.loads(feedback) == [f"{item.id}: {diagnostic}"]
    assert result.added_decisions == {item.id: accepted}


def test_decision_cannot_repeat_the_name_used_as_the_original_question(tmp_path):
    item = Finding(id="retention", kind="unclear", severity="medium", name="How long are copies stored?")
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [_respond(item.id, "How long are copies stored.")]})

    result = _resolve(tmp_path, [item], invoke)

    assert len(prompts) == 3
    assert result.unresolved == {item.id: "Decision repeats the original question"}
    assert not result.path.exists()


@pytest.mark.parametrize(
    "choice",
    [
        "Use https://example.invalid/search?filter=active as the lookup URL.",
        "Use /?Filter=Active as the lookup URL.",
        "Print '?' for an unknown value.",
        'Display the literal "Continue? This destroys the draft." before deleting the draft.',
        "Show `? help` as the command example.",
        "表示に「続けますか？下書きが削除されます。」を使う。",
    ],
)
def test_question_marks_in_literal_text_are_allowed(tmp_path, choice):
    item = _finding("representation", "How is the value shown?")
    result = _resolve(
        tmp_path, [item], lambda _: json.dumps({"decisions": [_respond(item.id, choice)]}),
    )

    assert result.added_decisions == {item.id: choice}


@pytest.mark.parametrize("change", ["remove", "odd-hex", "invalid-utf8-hex"])
def test_editing_hidden_marker_preserves_decisions(tmp_path, change):
    first = _finding("retention", "How long are copies stored?")
    second = _finding("size", "What is the maximum size?")
    choices = {first.id: "Copies are stored for three days.", second.id: "Copies are at most 1 MB."}
    initial = _resolve(
        tmp_path,
        [first, second],
        lambda _: json.dumps({"decisions": [_respond(key, choice) for key, choice in choices.items()]}),
    )
    path = initial.path
    edited = path.read_bytes().replace(choices[first.id].encode(), "複製は一週間保存する。".encode())
    if change == "remove":
        edited = re.sub(rb"(?m)^<!-- codd:provisional-decision .* -->\n", b"", edited, count=1)
    else:
        hex_value = b"f" if change == "odd-hex" else b"ff"
        edited = re.sub(rb"(<!-- codd:provisional-decision )[^ ]+", rb"\g<1>" + hex_value, edited, count=1)
    edited = edited.replace(b"\n", b"\r\n").rstrip(b"\r\n")
    path.write_bytes(edited)

    existing = read_provisional_decisions(tmp_path)
    assert existing == {**choices, first.id: "複製は一週間保存する。"}
    repeated = _resolve(tmp_path, [first, second], lambda _: pytest.fail("Asked AI again"))
    assert repeated.already_decided_ids == [first.id, second.id]
    assert path.read_bytes() == edited


@pytest.mark.parametrize("second_requirement", ["req:backup", "req:restore"])
def test_request_an_answer_for_every_new_id_with_same_question(tmp_path, second_requirement):
    question = "What happens on failure?"
    first = _finding("backup-failure", question, "req:backup")
    second = _finding("another-finding", question, second_requirement)
    choices = {first.id: "Failed operations retry once.", second.id: "Failed operations report an error."}
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        return json.dumps({"decisions": [_respond(key, choice) for key, choice in choices.items()]})

    result = _resolve(tmp_path, [first, second], invoke)

    assert len(prompts) == 1
    assert first.id in prompts[0] and second.id in prompts[0]
    assert result.added_decisions == choices
    assert not result.already_decided_ids


def test_rediscovered_question_reuses_a_decision_only_for_the_same_id(tmp_path):
    question = "What happens on failure?"
    prior = _finding("backup-failure", question, "req:backup", "req:jobs")
    alias = _finding("backup-failure-again", question, "req:jobs", "req:backup", "req:jobs")
    fresh = _finding("restore-failure", question, "req:restore")
    prior_choice = "Failed backups retry once."
    alias_choice = "Backups report retry attempts."
    fresh_choice = "Failed restores report an error."
    initial = _resolve(
        tmp_path, [prior], lambda _: json.dumps({"decisions": [_respond(prior.id, prior_choice)]}),
    )
    original = initial.path.read_bytes()

    def invoke(prompt):
        assert fresh.id in prompt
        assert alias.id in prompt
        assert '"id": "backup-failure"' not in prompt
        return json.dumps({"decisions": [_respond(alias.id, alias_choice), _respond(fresh.id, fresh_choice)]})

    renamed = _finding(prior.id, "Who is notified after failure?", "req:backup")
    result = _resolve(tmp_path, [renamed, alias, fresh], invoke)

    assert result.already_decided_ids == [prior.id]
    assert result.added_decisions == {alias.id: alias_choice, fresh.id: fresh_choice}
    assert result.path.read_bytes().startswith(original)


def test_retry_can_use_validated_decisions_from_the_same_batch(tmp_path):
    cutoff = _finding("booking-cutoff", "When do bookings close?")
    buffer = _finding("booking-buffer", "What is the gap before closing time?")
    cutoff_choice = "Bookings stop 15 minutes before closing."
    buffer_choice = "Leave a 15-minute gap before closing."
    prompts = []

    def invoke(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"decisions": [_respond(cutoff.id, cutoff_choice)]})
        assert cutoff_choice in prompt
        return json.dumps({"decisions": [_respond(buffer.id, buffer_choice)]})

    result = _resolve(tmp_path, [cutoff, buffer], invoke)

    assert len(prompts) == 2
    assert result.added_decisions == {cutoff.id: cutoff_choice, buffer.id: buffer_choice}
