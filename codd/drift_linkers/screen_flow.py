"""Screen-flow drift linker for deploy and coverage gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import warnings

from codd.coherence_engine import EventBus, use_coherence_bus
from codd.drift import ScreenFlowDriftResult, compute_screen_flow_drift
from codd.drift_linkers import register_linker


@dataclass(frozen=True)
class ScreenFlowGateResult:
    """Result of linking screen-flow drift into a gate decision."""

    passed: bool
    skipped: bool
    status: str
    drift: ScreenFlowDriftResult
    transitions_path: Path
    details: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return len(self.drift.design_only) + len(self.drift.impl_only) + len(self.drift.mismatch)


@register_linker("screen_flow")
class ScreenFlowGate:
    """Connect ScreenFlowDriftResult to deploy and coverage gate decisions."""

    def __init__(
        self,
        expected_catalog_path: str | Path | None = None,
        project_root: str | Path = ".",
        settings: dict[str, Any] | None = None,
        *,
        apply: bool | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self.expected_catalog_path = Path(expected_catalog_path) if expected_catalog_path else None
        self.project_root = Path(project_root)
        self.settings = settings or {}
        self.apply = apply
        self.event_bus = event_bus or _event_bus_from_settings(self.settings)

    def run(self) -> ScreenFlowGateResult:
        transitions_path = _resolve_transitions_path(
            self.project_root,
            self.expected_catalog_path,
            self.settings,
        )

        if not _is_apply_mode(self.settings, self.apply):
            return _skipped_result("dry_run", transitions_path)

        if not transitions_path.exists():
            message = f"{transitions_path.as_posix()} not found; skipping screen-flow drift gate."
            if bool(self.settings.get("warn_on_skip", True)):
                warnings.warn(message, UserWarning, stacklevel=2)
            return _skipped_result("missing_screen_transitions", transitions_path, message)

        if self.event_bus is None:
            drift = compute_screen_flow_drift(
                self.project_root,
                transitions_path,
                extractor_config=self.settings,
            )
        else:
            with use_coherence_bus(self.event_bus):
                drift = compute_screen_flow_drift(
                    self.project_root,
                    transitions_path,
                    extractor_config=self.settings,
                )

        result = ScreenFlowGateResult(
            passed=_drift_count(drift) == 0,
            skipped=False,
            status="passed" if _drift_count(drift) == 0 else "failed",
            drift=drift,
            transitions_path=transitions_path,
            details=_details_for_drift(drift),
        )
        return result


def _skipped_result(reason: str, transitions_path: Path, warning: str | None = None) -> ScreenFlowGateResult:
    return ScreenFlowGateResult(
        passed=True,
        skipped=True,
        status=f"skipped:{reason}",
        drift=ScreenFlowDriftResult(
            design_only=[],
            impl_only=[],
            mismatch=[],
            total_design=0,
            total_impl=0,
        ),
        transitions_path=transitions_path,
        details=[f"screen_flow_design_drift: skipped ({reason})"],
        warnings=[warning] if warning else [],
    )


def _resolve_transitions_path(
    project_root: Path,
    expected_catalog_path: Path | None,
    settings: dict[str, Any],
) -> Path:
    path_value = _screen_flow_path_setting(settings)
    if (
        path_value is None
        and expected_catalog_path is not None
        and expected_catalog_path.name == "screen-transitions.yaml"
    ):
        path_value = expected_catalog_path
    if path_value is None:
        path_value = "docs/extracted/screen-transitions.yaml"

    path = Path(path_value).expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _screen_flow_path_setting(settings: dict[str, Any]) -> Any:
    for section_name in ("screen_flow_drift", "e2e", "screen_flow"):
        section = settings.get(section_name, {})
        if isinstance(section, dict):
            value = section.get("screen_transitions_path")
            if value:
                return value
    return settings.get("screen_transitions_path")


def _is_apply_mode(settings: dict[str, Any], explicit_apply: bool | None) -> bool:
    if explicit_apply is not None:
        return explicit_apply
    if "apply" in settings:
        return bool(settings["apply"])
    if "apply_mode" in settings:
        return bool(settings["apply_mode"])
    if "dry_run" in settings:
        return not bool(settings["dry_run"])
    deploy_settings = settings.get("deploy", {})
    if isinstance(deploy_settings, dict) and "dry_run" in deploy_settings:
        return not bool(deploy_settings["dry_run"])
    return True


def _event_bus_from_settings(settings: dict[str, Any]) -> EventBus | None:
    bus = settings.get("coherence_bus") or settings.get("event_bus")
    return bus if isinstance(bus, EventBus) else None


def _drift_count(drift: ScreenFlowDriftResult) -> int:
    return len(drift.design_only) + len(drift.impl_only) + len(drift.mismatch)


def _details_for_drift(drift: ScreenFlowDriftResult) -> list[str]:
    return [
        f"screen_flow_design_drift: {_drift_count(drift)}",
        f"design_only: {len(drift.design_only)}",
        f"impl_only: {len(drift.impl_only)}",
        f"mismatch: {len(drift.mismatch)}",
    ]
