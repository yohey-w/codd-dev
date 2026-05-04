"""Coherence Engine: DriftEvent, EventBus, and Orchestrator."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Literal

ArtifactKind = Literal[
    "requirements",
    "lexicon",
    "design_md",
    "design_doc",
    "screen_flow",
    "implementation",
    "unit_test",
    "e2e_test",
]
ChangeType = Literal["created", "modified", "deleted", "renamed"]
Severity = Literal["red", "amber", "green"]
FixStrategy = Literal["auto", "hitl", "manual"]
RoutingKey = Severity | tuple[Severity, str] | str


@dataclass
class DriftEvent:
    """Unified representation of cross-artifact drift."""

    source_artifact: ArtifactKind
    target_artifact: ArtifactKind
    change_type: ChangeType
    payload: dict[str, Any]
    severity: Severity
    fix_strategy: FixStrategy
    kind: str
    event_id: str = field(default_factory=lambda: f"de-{uuid.uuid4().hex[:8]}")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EventBus:
    """In-process pub/sub bus for DriftEvents."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[DriftEvent], None]]] = {}
        self._published: list[DriftEvent] = []

    def subscribe(self, event_kind: str, callback: Callable[[DriftEvent], None]) -> None:
        """Subscribe to a specific event kind, or '*' for all events."""
        self._subscribers.setdefault(event_kind, []).append(callback)

    def publish(self, event: DriftEvent) -> None:
        """Publish a DriftEvent to all matching subscribers."""
        self._published.append(event)
        for callback in self._subscribers.get("*", []):
            callback(event)
        if event.kind != "*":
            for callback in self._subscribers.get(event.kind, []):
                callback(event)

    def published_events(self) -> list[DriftEvent]:
        """Return published events for replay or tests."""
        return list(self._published)

    def clear(self) -> None:
        """Clear published event history."""
        self._published.clear()


def set_coherence_bus(bus: EventBus | None) -> None:
    """Set the opt-in coherence bus on detectors that publish DriftEvents."""
    for module_name in (
        "codd.drift",
        "codd.validator",
        "codd.screen_flow_validator",
    ):
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            continue
        setter = getattr(module, "set_coherence_bus", None)
        if setter is not None:
            setter(bus)


@contextmanager
def use_coherence_bus(bus: EventBus):
    """Temporarily install a coherence bus and always clear it on exit."""
    set_coherence_bus(bus)
    try:
        yield bus
    finally:
        set_coherence_bus(None)


DEFAULT_ROUTING: dict[Severity, FixStrategy] = {
    "red": "auto",
    "amber": "hitl",
    "green": "manual",
}


class Orchestrator:
    """Route DriftEvents to auto-fix, HITL review, or logs."""

    def __init__(
        self,
        bus: EventBus,
        routing: Mapping[RoutingKey, FixStrategy] | None = None,
        hitl_path: str = "docs/coherence/pending_hitl.md",
        ntfy_rate_limit_seconds: int = 60,
    ) -> None:
        self._bus = bus
        self._routing: dict[RoutingKey, FixStrategy] = dict(DEFAULT_ROUTING)
        if routing:
            self._routing.update(routing)
        self._hitl_path = hitl_path
        self._ntfy_rate_limit = ntfy_rate_limit_seconds
        self._last_ntfy_time: float = 0.0
        self._pending_amber: list[DriftEvent] = []
        bus.subscribe("*", self._handle)

    def resolve_fix_strategy(self, event: DriftEvent) -> FixStrategy:
        """Resolve the action strategy for an event.

        Routing can be keyed by severity, by ``(severity, kind)``, or by a
        ``"severity:kind"`` string for config-file friendly overrides. If an
        event carries a strategy that differs from the built-in severity
        default, it is treated as an explicit event-level override.
        """
        built_in_default = DEFAULT_ROUTING.get(event.severity, "manual")
        routed_default = self._lookup_routing(event)
        if event.fix_strategy != built_in_default:
            return event.fix_strategy
        return routed_default

    def _lookup_routing(self, event: DriftEvent) -> FixStrategy:
        kind_key: tuple[Severity, str] = (event.severity, event.kind)
        config_key = f"{event.severity}:{event.kind}"
        return self._routing.get(
            kind_key,
            self._routing.get(config_key, self._routing.get(event.severity, "manual")),
        )

    def _handle(self, event: DriftEvent) -> None:
        strategy = self.resolve_fix_strategy(event)
        if strategy == "auto":
            self._dispatch_auto(event)
        elif strategy == "hitl":
            self._record_hitl(event)
        else:
            self._log_event(event)

    def _dispatch_auto(self, event: DriftEvent) -> None:
        """Attempt auto-fix and downgrade to HITL on failure."""
        try:
            self._log_event(event, prefix="[AUTO]")
            if event.payload.get("simulate_failure"):
                raise RuntimeError("Simulated auto-fix failure")
        except Exception as exc:
            degraded = DriftEvent(
                source_artifact=event.source_artifact,
                target_artifact=event.target_artifact,
                change_type=event.change_type,
                payload={
                    **event.payload,
                    "auto_fix_error": str(exc),
                    "downgraded_from": event.severity,
                },
                severity="amber",
                fix_strategy="hitl",
                kind=event.kind,
            )
            self._record_hitl(degraded)

    def _record_hitl(self, event: DriftEvent) -> None:
        """Append a human-in-the-loop review entry and notify if allowed."""
        directory = os.path.dirname(self._hitl_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        should_write_heading = (
            not os.path.exists(self._hitl_path)
            or os.path.getsize(self._hitl_path) == 0
        )
        with open(self._hitl_path, "a", encoding="utf-8") as handle:
            if should_write_heading:
                handle.write("# Pending HITL Reviews\n")
            handle.write(self._format_hitl_entry(event))

        self._pending_amber.append(event)
        self._maybe_send_ntfy()

    def _format_hitl_entry(self, event: DriftEvent) -> str:
        original_severity = event.payload.get("downgraded_from", event.severity)
        description = event.payload.get("description", "(no description)")
        suggested_action = event.payload.get("suggested_action", "(none)")
        auto_fix_error = event.payload.get("auto_fix_error")
        error_line = f"- **Auto Fix Error**: {auto_fix_error}\n" if auto_fix_error else ""
        return (
            f"\n## [{event.event_id}] {event.created_at} - "
            f"{event.source_artifact} -> {event.target_artifact} ({event.kind})\n"
            f"- **Source**: {event.source_artifact}\n"
            f"- **Target**: {event.target_artifact}\n"
            f"- **Severity**: {event.severity} (original: {original_severity})\n"
            f"- **Change**: {event.change_type}\n"
            f"- **Description**: {description}\n"
            f"- **Suggested Action**: {suggested_action}\n"
            f"{error_line}"
            f"- **Status**: [ ] accepted  [ ] rejected\n"
            f"- **Reviewer**: (pending)\n"
        )

    def _maybe_send_ntfy(self) -> None:
        """Send at most one ntfy notification per rate-limit window."""
        now = time.time()
        if now - self._last_ntfy_time < self._ntfy_rate_limit:
            return
        count = len(self._pending_amber)
        if count == 0:
            return
        self._send_ntfy(f"CoDD Coherence: {count} HITL event(s) pending review")
        self._last_ntfy_time = now
        self._pending_amber.clear()

    def _send_ntfy(self, message: str) -> None:
        """Send an ntfy notification when NTFY_URL is configured."""
        ntfy_url = os.environ.get("NTFY_URL", "")
        if not ntfy_url:
            return
        try:
            subprocess.run(
                ["curl", "-s", "-d", message, ntfy_url],
                timeout=5,
                check=False,
                capture_output=True,
            )
        except Exception:
            return

    def _log_event(self, event: DriftEvent, prefix: str = "[LOG]") -> None:
        print(
            f"{prefix} [{event.event_id}] {event.kind}: "
            f"{event.source_artifact}->{event.target_artifact} "
            f"(severity={event.severity})"
        )
