"""Tests for the Coherence Engine."""

from __future__ import annotations

from pathlib import Path

from codd.coherence_engine import DriftEvent, EventBus, Orchestrator


def _event(
    *,
    severity="amber",
    fix_strategy="hitl",
    kind="url_drift",
    payload=None,
) -> DriftEvent:
    return DriftEvent(
        source_artifact="design_md",
        target_artifact="implementation",
        change_type="modified",
        payload=payload or {
            "description": "Route changed in DESIGN.md.",
            "suggested_action": "Update the implementation route.",
        },
        severity=severity,
        fix_strategy=fix_strategy,
        kind=kind,
    )


def _patch_ntfy(monkeypatch, orchestrator: Orchestrator) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(orchestrator, "_send_ntfy", messages.append)
    return messages


def test_drift_event_creation():
    event = _event()

    assert event.event_id.startswith("de-")
    assert len(event.event_id) == 11
    assert event.created_at
    assert event.kind == "url_drift"
    assert event.severity == "amber"
    assert event.fix_strategy == "hitl"


def test_drift_event_unique_ids():
    first = _event()
    second = _event()

    assert first.event_id != second.event_id


def test_drift_event_payload():
    payload = {"before": "/old", "after": "/new", "nested": {"key": "value"}}

    event = _event(payload=payload)

    assert event.payload == payload


def test_eventbus_subscribe_and_publish():
    bus = EventBus()
    received: list[DriftEvent] = []
    event = _event()

    bus.subscribe("url_drift", received.append)
    bus.publish(event)

    assert received == [event]


def test_eventbus_catch_all():
    bus = EventBus()
    received: list[DriftEvent] = []

    bus.subscribe("*", received.append)
    first = _event(kind="url_drift")
    second = _event(kind="lexicon_violation")
    bus.publish(first)
    bus.publish(second)

    assert received == [first, second]


def test_eventbus_specific_kind():
    bus = EventBus()
    received: list[DriftEvent] = []

    bus.subscribe("url_drift", received.append)
    event = _event(kind="lexicon_violation")
    bus.publish(event)

    assert received == []


def test_eventbus_published_events():
    bus = EventBus()
    first = _event(kind="url_drift")
    second = _event(kind="design_token_drift")

    bus.publish(first)
    bus.publish(second)

    assert bus.published_events() == [first, second]
    assert bus.published_events() is not bus.published_events()


def test_eventbus_clear():
    bus = EventBus()
    bus.publish(_event())

    bus.clear()

    assert bus.published_events() == []


def test_orchestrator_green_logs_only(tmp_path, capsys, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event(severity="green", fix_strategy="manual"))

    assert not hitl_path.exists()
    assert "[LOG]" in capsys.readouterr().out


def test_orchestrator_amber_records_hitl(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)
    event = _event()

    bus.publish(event)

    content = hitl_path.read_text(encoding="utf-8")
    assert "# Pending HITL Reviews" in content
    assert f"## [{event.event_id}]" in content
    assert "- **Severity**: amber (original: amber)" in content


def test_orchestrator_red_auto_dispatches(tmp_path, monkeypatch):
    bus = EventBus()
    orchestrator = Orchestrator(bus, hitl_path=str(tmp_path / "pending_hitl.md"))
    dispatched: list[DriftEvent] = []
    monkeypatch.setattr(orchestrator, "_dispatch_auto", dispatched.append)
    event = _event(severity="red", fix_strategy="auto")

    bus.publish(event)

    assert dispatched == [event]


def test_orchestrator_auto_fail_downgrades_to_amber(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event(severity="red", fix_strategy="auto", payload={"simulate_failure": True}))

    content = hitl_path.read_text(encoding="utf-8")
    assert "- **Severity**: amber (original: red)" in content
    assert "- **Auto Fix Error**: Simulated auto-fix failure" in content


def test_orchestrator_hitl_format(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)
    event = _event()

    bus.publish(event)

    content = hitl_path.read_text(encoding="utf-8")
    assert event.event_id in content
    assert "- **Status**: [ ] accepted  [ ] rejected" in content
    assert "- **Reviewer**: (pending)" in content
    assert "- **Suggested Action**: Update the implementation route." in content


def test_orchestrator_custom_routing(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, routing={"red": "hitl"}, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event(severity="red", fix_strategy="auto"))

    assert hitl_path.exists()
    assert "- **Severity**: red (original: red)" in hitl_path.read_text(encoding="utf-8")


def test_orchestrator_kind_specific_routing(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(
        bus,
        routing={("red", "design_token_drift"): "hitl"},
        hitl_path=str(hitl_path),
    )
    _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event(severity="red", fix_strategy="auto", kind="design_token_drift"))

    assert "design_token_drift" in hitl_path.read_text(encoding="utf-8")


def test_orchestrator_event_fix_strategy_overrides_routing(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "pending_hitl.md"
    orchestrator = Orchestrator(bus, routing={"amber": "hitl"}, hitl_path=str(hitl_path))
    dispatched: list[DriftEvent] = []
    monkeypatch.setattr(orchestrator, "_dispatch_auto", dispatched.append)
    _patch_ntfy(monkeypatch, orchestrator)
    event = _event(severity="amber", fix_strategy="auto")

    bus.publish(event)

    assert dispatched == [event]
    assert not hitl_path.exists()


def test_orchestrator_rate_limit(tmp_path, monkeypatch):
    bus = EventBus()
    orchestrator = Orchestrator(
        bus,
        hitl_path=str(tmp_path / "pending_hitl.md"),
        ntfy_rate_limit_seconds=60,
    )
    messages = _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event(severity="red", fix_strategy="hitl", kind="url_drift"))
    bus.publish(_event(severity="red", fix_strategy="hitl", kind="lexicon_violation"))

    assert messages == ["CoDD Coherence: 1 HITL event(s) pending review"]


def test_orchestrator_amber_hitl_does_not_send_ntfy(tmp_path, monkeypatch):
    bus = EventBus()
    orchestrator = Orchestrator(bus, hitl_path=str(tmp_path / "pending_hitl.md"))
    messages = _patch_ntfy(monkeypatch, orchestrator)

    bus.publish(_event())

    assert messages == []


def test_full_flow(tmp_path, monkeypatch):
    bus = EventBus()
    hitl_path = tmp_path / "docs" / "coherence" / "pending_hitl.md"
    orchestrator = Orchestrator(bus, hitl_path=str(hitl_path))
    _patch_ntfy(monkeypatch, orchestrator)
    event = _event(kind="design_token_drift")

    bus.publish(event)

    assert bus.published_events() == [event]
    content = Path(hitl_path).read_text(encoding="utf-8")
    assert "design_token_drift" in content
    assert "Route changed in DESIGN.md." in content
