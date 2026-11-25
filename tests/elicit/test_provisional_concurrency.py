"""Verify that concurrent opt-in elicit runs reconcile the shared review state."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from codd.elicit.engine import ElicitEngine
from codd.elicit.finding import Finding
from codd.elicit.formatters.md import MdFormatter
from codd.elicit.persistence import ElicitPersistence
from codd.elicit.workflow import run_provisional_elicit
from tests.elicit.test_provisional_decisions import (
    DECISIONS,
    DESK_LIMIT,
    decision_row,
    decisions,
    discovery,
    finding,
    make_project,
)


@pytest.mark.parametrize("delayed_id", ["desk-duration", "duration-rediscovered"])
def test_concurrent_acceptance_wins_over_unresolved_review(tmp_path, delayed_id):
    project = make_project(tmp_path)
    shared = finding("desk-duration")
    delayed_shared = finding(delayed_id, shared["question"])
    cutoff = finding("booking-cutoff", "When do bookings close?")
    release = finding("desk-release", "When is a desk released after a no-show?")
    close_choice = "Bookings close ten minutes before the library closes."
    release_choice = "Unclaimed reservations expire after 15 minutes."
    delayed_started = Event()
    earlier_run_saved = Event()

    def earlier_ai(prompt):
        if "Requested findings:" not in prompt:
            return discovery(shared, cutoff)
        return decisions(decision_row(shared["id"], DESK_LIMIT), decision_row(cutoff["id"], close_choice))

    def delayed_ai(prompt):
        if "Requested findings:" not in prompt:
            return discovery(delayed_shared, release)
        delayed_started.set()
        assert earlier_run_saved.wait(timeout=5), "The other run did not finish"
        hold = decision_row(delayed_id, "", status="undecided", reason="Duration information missing.")
        if release["id"] in prompt.split("Requested findings:", 1)[1].split("Validation feedback", 1)[0]:
            return decisions(hold, decision_row(release["id"], release_choice))
        return decisions(hold)

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed_run = executor.submit(run_provisional_elicit, project, ElicitEngine(ai_command=delayed_ai))
        try:
            assert delayed_started.wait(timeout=5), "The delayed run did not collect its findings"
            run_provisional_elicit(project, ElicitEngine(ai_command=earlier_ai))
            saved = (project / DECISIONS).read_bytes()
        finally:
            earlier_run_saved.set()
        discovery_result, late_decisions = delayed_run.result(timeout=5)

    ledger = (project / DECISIONS).read_bytes()
    assert ledger.startswith(saved)
    for choice in (DESK_LIMIT, close_choice, release_choice):
        assert ledger.count(choice.encode()) == 1
    pending = ElicitPersistence(project).load_pending()
    report = project / "findings.md"
    if delayed_id == shared["id"]:
        assert not pending
        assert not report.exists() or delayed_id not in report.read_text(encoding="utf-8")
        assert not discovery_result.findings
        assert not late_decisions.unresolved
        assert delayed_id in late_decisions.already_decided_ids
    else:
        assert [item.id for item in pending] == [delayed_id]
        assert delayed_id in report.read_text(encoding="utf-8")
        assert [item.id for item in discovery_result.findings] == [delayed_id]
        assert list(late_decisions.unresolved) == [delayed_id]
        assert not late_decisions.already_decided_ids


@pytest.mark.parametrize("keep_deferred", [False, True])
def test_concurrent_unresolved_findings_merge_in_review(tmp_path, keep_deferred):
    project = make_project(tmp_path)
    first = finding("booking-cutoff", "When do bookings close?")
    second = finding("desk-release", "When is a desk released after a no-show?")
    report = project / "findings.md"
    delayed_started = Event()
    earlier_run_saved = Event()

    def undecided(item):
        return decisions(decision_row(item["id"], "", status="undecided", reason="Staff schedule needed."))

    def earlier_ai(prompt):
        return undecided(first) if "Requested findings:" in prompt else discovery(first)

    def delayed_ai(prompt):
        if "Requested findings:" not in prompt:
            return discovery(second)
        delayed_started.set()
        assert earlier_run_saved.wait(timeout=5), "The other run did not finish"
        return undecided(second)

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(run_provisional_elicit, project, ElicitEngine(ai_command=delayed_ai))
        try:
            assert delayed_started.wait(timeout=5), "The delayed run did not collect its findings"
            run_provisional_elicit(project, ElicitEngine(ai_command=earlier_ai))
            if keep_deferred:
                text = report.read_text(encoding="utf-8")
                report.write_text(text.replace("[ ] `booking-cutoff`", "[d] `booking-cutoff`"), encoding="utf-8")
        finally:
            earlier_run_saved.set()
        delayed.result(timeout=5)

    pending = ElicitPersistence(project).load_pending()
    assert {item.id for item in pending} == {first["id"], second["id"]}
    text = report.read_text(encoding="utf-8")
    assert all(item["id"] in text and item["question"] in text for item in (first, second))
    approval = "d" if keep_deferred else " "
    assert f"[{approval}] `booking-cutoff`" in text


@pytest.mark.parametrize("mark", ["r", "x", "d"])
def test_review_mark_set_during_ai_call_prevents_provisional_acceptance(tmp_path, mark):
    project = make_project(tmp_path)
    reviewed = finding("desk-duration")
    open_finding = finding("booking-cutoff", "When do bookings close?")
    cutoff = "Bookings close ten minutes before the library closes."
    review = project / "findings.md"
    review.write_text(MdFormatter().format([Finding.from_dict(reviewed)]), encoding="utf-8")

    def invoke(prompt):
        if "Requested findings:" not in prompt:
            return discovery(reviewed, open_finding)
        original = review.read_text(encoding="utf-8")
        assert "desk-duration" in prompt
        review.write_text(original.replace("[ ] `desk-duration`", f"[{mark}] `desk-duration`"), encoding="utf-8")
        return decisions(decision_row("desk-duration", DESK_LIMIT), decision_row("booking-cutoff", cutoff))

    report, result = run_provisional_elicit(project, ElicitEngine(ai_command=invoke))

    assert result.added_decisions == {"booking-cutoff": cutoff}
    assert report.metadata["provisional_decisions"]["decided_count"] == 1
    assert not result.unresolved
    written = (project / DECISIONS).read_text(encoding="utf-8")
    assert DESK_LIMIT not in written
    assert cutoff in written
    assert f"[{mark}] `desk-duration`" in review.read_text(encoding="utf-8")
