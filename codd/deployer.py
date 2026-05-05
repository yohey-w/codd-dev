"""Deployment orchestration for ``codd deploy``.

Phase 1 keeps deploy execution behind pluggable targets. Core code owns only
configuration validation, target dispatch, dry-run/apply flow, healthchecks,
rollback orchestration, and structured logging.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

from codd.cli import CoddCLIError
from codd.deploy_targets import get_target


def load_deploy_config(config_path: Path) -> dict[str, Any]:
    """Load and validate deploy.yaml."""
    config_path = Path(config_path).expanduser()
    if not config_path.exists():
        raise CoddCLIError(f"Deploy config not found: {config_path}")

    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise CoddCLIError("deploy.yaml must contain a YAML mapping")

    targets = payload.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise CoddCLIError("deploy.yaml must define at least one target under 'targets'")

    default_target = payload.get("default_target")
    if default_target is not None and default_target not in targets:
        raise CoddCLIError(f"default_target {default_target!r} is not defined in targets")

    for target_name, target_config in targets.items():
        _validate_target_config(str(target_name), target_config)

    global_config = payload.get("global", {})
    if global_config is not None and not isinstance(global_config, dict):
        raise CoddCLIError("'global' must be a mapping when provided")

    return payload


def run_healthcheck(
    url: str,
    expected_status: int,
    timeout_seconds: int,
    retries: int,
) -> bool:
    """HTTP GET healthcheck with retries."""
    for attempt in range(max(1, retries)):
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=timeout_seconds) as response:
                if response.status == expected_status:
                    return True
        except (OSError, URLError):
            pass

        if attempt < retries - 1:
            time.sleep(1)

    return False


def run_deploy(
    project_root: Path,
    target_name: str | None = None,
    config: dict[str, Any] | None = None,
    *,
    config_path: Path | None = None,
    dry_run: bool = True,
    rollback_flag: bool = False,
    healthcheck_timeout: int = 60,
    emit_output: bool = False,
) -> int:
    """Main deploy entry point. Returns exit code."""
    project_root = Path(project_root).resolve()
    deploy_config = config if config is not None else load_deploy_config(config_path or project_root / "deploy.yaml")
    selected_target = _select_target_name(deploy_config, target_name)
    target_config = deploy_config["targets"][selected_target]
    target_cls = get_target(target_config["type"])
    target = target_cls(target_config)

    log_context: dict[str, Any] = {
        "target": selected_target,
        "target_type": target_config["type"],
        "dry_run": dry_run,
        "rollback": rollback_flag,
        "actions": [],
        "status": "started",
        "errors": [],
    }

    try:
        if rollback_flag:
            log_context["status"] = "rollback_succeeded" if target.rollback({}) else "rollback_failed"
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 0 if log_context["status"] == "rollback_succeeded" else 1

        if dry_run:
            actions = target.dry_run()
            log_context["actions"] = actions
            log_context["status"] = "dry_run"
            if emit_output:
                _emit_dry_run_actions(selected_target, actions)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 0

        gate_result = _run_screen_flow_apply_gate(project_root)
        if not gate_result.passed:
            log_context["status"] = "screen_flow_gate_failed"
            log_context["errors"].extend(gate_result.details)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 1

        snapshot = target.snapshot()
        log_context["snapshot"] = snapshot
        if not target.deploy():
            log_context["status"] = "deploy_failed"
            _maybe_rollback(target, snapshot, deploy_config, log_context)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 1

        if not _run_target_healthcheck(target, target_config, healthcheck_timeout):
            log_context["status"] = "healthcheck_failed"
            _maybe_rollback(target, snapshot, deploy_config, log_context)
            _write_deploy_log(project_root, deploy_config, selected_target, log_context)
            return 1

        log_context["status"] = "deployed"
        _write_deploy_log(project_root, deploy_config, selected_target, log_context)
        return 0
    except Exception as exc:
        log_context["status"] = "failed"
        log_context["errors"].append(str(exc))
        _write_deploy_log(project_root, deploy_config, selected_target, log_context)
        raise


def _validate_target_config(target_name: str, target_config: Any) -> None:
    if not isinstance(target_config, dict):
        raise CoddCLIError(f"Deploy target {target_name!r} must be a mapping")
    target_type = target_config.get("type")
    if not isinstance(target_type, str) or not target_type.strip():
        raise CoddCLIError(f"Deploy target {target_name!r} must define a non-empty type")

    ssh_key = target_config.get("ssh_key")
    if isinstance(ssh_key, str) and ssh_key.lstrip().startswith("-----BEGIN"):
        raise CoddCLIError("ssh_key must be a path reference, not private key content")

    healthcheck = target_config.get("healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise CoddCLIError(f"Deploy target {target_name!r} healthcheck must be a mapping")


def _select_target_name(config: dict[str, Any], target_name: str | None) -> str:
    targets = config["targets"]
    selected = target_name or config.get("default_target")
    if selected:
        if selected not in targets:
            raise CoddCLIError(f"Deploy target {selected!r} is not defined")
        return selected
    if len(targets) == 1:
        return next(iter(targets))
    raise CoddCLIError("Target is required when deploy.yaml has multiple targets")


def _run_target_healthcheck(target: Any, target_config: dict[str, Any], timeout_seconds: int) -> bool:
    healthcheck = target_config.get("healthcheck") or {}
    url = healthcheck.get("url")
    if url:
        return run_healthcheck(
            url=str(url),
            expected_status=int(healthcheck.get("expected_status", 200)),
            timeout_seconds=int(timeout_seconds or healthcheck.get("timeout_seconds", 60)),
            retries=int(healthcheck.get("retries", 1)),
        )
    return bool(target.healthcheck())


def _run_screen_flow_apply_gate(project_root: Path) -> Any:
    """Run screen-flow drift gate only for apply deploys."""

    from codd.drift_linkers.screen_flow import ScreenFlowGate

    settings: dict[str, Any] = {"apply": True}
    try:
        from codd.config import load_project_config

        settings = {**load_project_config(project_root), "apply": True}
    except (FileNotFoundError, ValueError):
        pass
    return ScreenFlowGate(project_root=project_root, settings=settings).run()


def _maybe_rollback(
    target: Any,
    snapshot: dict[str, Any],
    deploy_config: dict[str, Any],
    log_context: dict[str, Any],
) -> None:
    global_config = deploy_config.get("global") or {}
    if not bool(global_config.get("rollback_on_healthcheck_fail", True)):
        log_context["rollback_attempted"] = False
        return
    log_context["rollback_attempted"] = True
    log_context["rollback_succeeded"] = bool(target.rollback(snapshot))


def _emit_dry_run_actions(target_name: str, actions: list[str]) -> None:
    print("Proposed actions:")
    print(f"Target: {target_name}")
    for action in actions:
        print(f"- {action}")


def _write_deploy_log(
    project_root: Path,
    deploy_config: dict[str, Any],
    target_name: str,
    context: dict[str, Any],
) -> Path:
    global_config = deploy_config.get("global") or {}
    log_dir = Path(global_config.get("log_dir", "docs/reports/deploy_logs/")).expanduser()
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    safe_target = re.sub(r"[^A-Za-z0-9_.-]+", "-", target_name).strip("-") or "target"
    log_path = log_dir / f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{safe_target}.yaml"
    payload = {
        "timestamp": timestamp.isoformat(),
        **context,
    }
    log_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return log_path
