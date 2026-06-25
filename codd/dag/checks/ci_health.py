"""C8 CI health check with deterministic static validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from glob import has_magic
import json
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

from codd.dag.checks import DagCheck, register_dag_check
from codd.path_safety import project_relative_path, resolve_project_path
from codd.dag.checks.opt_out import (
    OPT_OUT_STATUS,
    OptOutDeclaration,
    OptOutSignal,
)


_DEFAULT_PROVIDER = "github" + "_actions"
_OPT_OUT_PROVIDER = "none"


# GitHub Actions officially loads workflow files with EITHER ``.yml`` or
# ``.yaml``. The default glob therefore matches both extensions (comma-separated
# union — see ``_locate_workflows``). A user who narrows ``workflow_glob`` to a
# single pattern keeps exact control (an explicit ``*.yaml`` pulls in only
# ``.yaml``); only the DEFAULT spans both so the common ``.yaml``-only project
# (e.g. Flask) is not mis-RED-flagged ``ci_workflow_missing``.
_DEFAULT_WORKFLOW_GLOB = ".github/workflows/*.yml,.github/workflows/*.yaml"


@dataclass
class CiConfig:
    provider: str = _DEFAULT_PROVIDER
    workflow_glob: str = _DEFAULT_WORKFLOW_GLOB
    required_triggers: list[str] = field(default_factory=lambda: ["push", "pull_request"])
    runtime_check: bool = False
    staleness_days: int = 14
    default_branch: str = "main"
    trigger_key: str = "on"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "CiConfig":
        if not isinstance(value, Mapping):
            # Missing ``ci:`` section is no longer treated as a silent opt-out.
            # The default provider applies; if no workflow files exist this
            # surfaces as a normal red ``ci_workflow_missing`` finding rather
            # than a free PASS.
            return cls()

        config = cls()
        return cls(
            provider=_string_value(value.get("provider"), config.provider),
            workflow_glob=_string_value(value.get("workflow_glob"), config.workflow_glob),
            required_triggers=_string_list(value.get("required_triggers"), config.required_triggers),
            runtime_check=bool(value.get("runtime_check", config.runtime_check)),
            staleness_days=_int_value(value.get("staleness_days"), config.staleness_days),
            default_branch=_string_value(value.get("default_branch"), config.default_branch),
            trigger_key=_string_value(value.get("trigger_key"), config.trigger_key),
        )


@dataclass
class CiHealthFinding:
    violation_type: str
    severity: str
    block_deploy: bool
    message: str
    details: list[str] = field(default_factory=list)


@dataclass
class CiHealthResult:
    check_name: str = "ci_health"
    status: str = "pass"
    severity: str = "info"
    block_deploy: bool = False
    message: str = "C8 ci_health PASS"
    findings: list[CiHealthFinding] = field(default_factory=list)
    workflow_files: list[str] = field(default_factory=list)
    passed: bool = True


@register_dag_check("ci_health")
class CiHealthCheck(DagCheck):
    """C8 CI workflow presence and trigger validation.

    Runtime provider polling is intentionally deferred. The shipped path is
    static, deterministic, and driven by config values.
    """

    check_name = "ci_health"
    severity = "red"
    block_deploy = True

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> CiHealthResult:
        del dag
        root = Path(project_root or self.project_root or ".").resolve()
        active_settings = codd_config or settings or self.settings
        config = CiConfig.from_mapping(_mapping_value(active_settings, "ci"))
        return self.check(root, config)

    def detect_opt_out(self, codd_config: dict[str, Any]) -> OptOutSignal | None:
        config = CiConfig.from_mapping(_mapping_value(codd_config, "ci"))
        if self._is_opt_out_provider(config):
            return OptOutSignal(
                check_name=self.check_name,
                source="ci.provider=none",
            )
        return None

    @staticmethod
    def _is_opt_out_provider(config: CiConfig) -> bool:
        return config.provider.strip().lower() == _OPT_OUT_PROVIDER

    def _make_opt_out_result(self, declaration: OptOutDeclaration | None) -> CiHealthResult:
        today = self.today
        if declaration is None:
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=(
                    "C8 ci_health: ci.provider=none requires an opt_outs declaration "
                    "in codd.yaml (check: ci_health, reason: ..., expires_at: "
                    "YYYY-MM-DD)."
                ),
                passed=False,
            )
        if declaration.is_expired(today):
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=(
                    f"C8 ci_health: opt-out expired on "
                    f"{declaration.expires_at.isoformat()} "
                    f"(reason: {declaration.reason}); renew the entry or remove it."
                ),
                passed=False,
            )
        return CiHealthResult(
            status=OPT_OUT_STATUS,
            severity=self.severity,
            block_deploy=False,
            message=(
                f"C8 ci_health opt-out active "
                f"(reason: {declaration.reason}, "
                f"expires: {declaration.expires_at.isoformat()})"
            ),
            passed=False,
        )

    def check(self, project_root: Path, config: CiConfig) -> CiHealthResult:
        project_root = Path(project_root).resolve()
        if self._is_opt_out_provider(config):
            declaration = (
                self.opt_out_policy.lookup(self.check_name) if self.opt_out_policy else None
            )
            return self._make_opt_out_result(declaration)
        workflow_files, out_of_root = self._locate_workflows(project_root, config.workflow_glob)
        # Out-of-root workflow paths are surfaced as a structured red finding,
        # never a crash. The escape can only come from a configured absolute
        # ``ci.workflow_glob`` that points outside ``project_root``; reading or
        # serializing such a path would either raise ``ValueError`` (relative_to)
        # or read files outside the project. We refuse to read them and report.
        out_of_root_finding = self._out_of_root_finding(out_of_root, project_root)
        if not workflow_files:
            if out_of_root_finding is not None:
                return CiHealthResult(
                    status="fail",
                    severity="red",
                    block_deploy=True,
                    message=out_of_root_finding.message,
                    findings=[out_of_root_finding],
                    passed=False,
                )
            finding = CiHealthFinding(
                violation_type="ci_workflow_missing",
                severity="red",
                block_deploy=True,
                message="No CI workflow files found matching glob.",
                details=[config.workflow_glob],
            )
            return CiHealthResult(
                status="fail",
                severity="red",
                block_deploy=True,
                message=finding.message,
                findings=[finding],
                passed=False,
            )

        findings = [
            *([out_of_root_finding] if out_of_root_finding is not None else []),
            *self._check_triggers(workflow_files, config.required_triggers, config.trigger_key),
            *self._check_verification_coverage(workflow_files, project_root),
        ]
        block_deploy = any(finding.block_deploy for finding in findings)
        severity = _max_severity(finding.severity for finding in findings)
        status = "fail" if block_deploy else ("warn" if findings else "pass")
        message = (
            "C8 ci_health PASS"
            if not findings
            else f"C8 ci_health found {len(findings)} static finding(s)"
        )
        return CiHealthResult(
            status=status,
            severity=severity,
            block_deploy=block_deploy,
            message=message,
            findings=findings,
            workflow_files=[_relative_to_root(path, project_root) for path in workflow_files],
            passed=not block_deploy,
        )

    @staticmethod
    def _out_of_root_finding(
        out_of_root: list[str],
        project_root: Path,
    ) -> CiHealthFinding | None:
        if not out_of_root:
            return None
        return CiHealthFinding(
            violation_type="ci_workflow_out_of_root",
            severity="red",
            block_deploy=True,
            message=(
                "CI workflow_glob resolves outside the project root; "
                "out-of-root workflows are not enumerated or read."
            ),
            details=list(out_of_root),
        )

    def format_report(self, result: CiHealthResult) -> str:
        return json.dumps({"ci_health_report": asdict(result)}, ensure_ascii=False, indent=2)

    def _locate_workflows(
        self,
        project_root: Path,
        workflow_glob: str,
    ) -> tuple[list[Path], list[str]]:
        """Resolve in-root workflow files for one OR MORE comma-separated globs.

        ``workflow_glob`` may carry several patterns separated by commas (the
        default spans both ``*.yml`` and ``*.yaml`` because GitHub Actions loads
        either extension). Each sub-glob is resolved independently through the
        shared path_safety jail (see :meth:`_locate_one_glob`); the in-root
        matches are unioned + de-duplicated, and any out-of-root markers are
        accumulated so a single escaping sub-glob still surfaces a structured red
        finding. A single (comma-free) pattern behaves exactly as before, so a
        user who narrows ``workflow_glob`` to one pattern keeps exact control.
        """

        sub_globs = [part.strip() for part in workflow_glob.split(",") if part.strip()]
        if not sub_globs:
            return [], []

        seen: set[Path] = set()
        in_root: list[Path] = []
        out_of_root: list[str] = []
        for sub in sub_globs:
            files, escaped = self._locate_one_glob(project_root, sub)
            for path in files:
                if path not in seen:
                    seen.add(path)
                    in_root.append(path)
            for marker in escaped:
                if marker not in out_of_root:
                    out_of_root.append(marker)
        in_root.sort()
        return in_root, out_of_root

    def _locate_one_glob(
        self,
        project_root: Path,
        workflow_glob: str,
    ) -> tuple[list[Path], list[str]]:
        """Resolve in-root workflow files for ONE glob via the path_safety jail.

        The glob root is validated *before* enumeration. An absolute
        ``workflow_glob`` whose static (non-magic) base escapes the project root
        is rejected without enumerating any path — the out-of-root tree is never
        listed (its mere existence/contents could otherwise change the finding
        shape) and is returned as an ``out_of_root`` marker for a structured red
        finding. An absolute base that genuinely lives under the root is rebased
        onto the root so ``Path.glob`` (which rejects absolute patterns) can
        enumerate it. Each enumerated match is then re-confined through the jail,
        so an in-root symlink whose target escapes the tree cannot smuggle an
        off-root workflow into the in-root list.
        """

        pattern = workflow_glob.strip()
        if not pattern:
            return [], []

        base = _glob_static_base(pattern)
        # Validate the glob ROOT before enumerating anything. An out-of-root
        # absolute base (or a ``../`` traversal that escapes) is rejected up
        # front: we never call glob() against an off-tree directory.
        if base and resolve_project_path(project_root, base) is None:
            return [], [base]

        if Path(pattern).is_absolute():
            rel_pattern = _absolute_to_relative_glob(project_root, pattern, base)
            if rel_pattern is None:
                # Defensive: base confined above; treat as nothing to enumerate.
                return [], []
            candidates = list(project_root.glob(rel_pattern))
        else:
            try:
                candidates = list(project_root.glob(pattern))
            except (ValueError, OSError):
                candidates = []

        in_root: list[Path] = []
        for path in sorted(candidates):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            # Re-confine each match (per-file symlink escape defense).
            if resolve_project_path(project_root, path) is None:
                continue
            in_root.append(path)
        return in_root, []

    def _check_triggers(
        self,
        workflow_files: list[Path],
        required_triggers: list[str],
        trigger_key: str,
    ) -> list[CiHealthFinding]:
        required = {trigger.strip() for trigger in required_triggers if trigger.strip()}
        if not required:
            return []

        actual: set[str] = set()
        for path in workflow_files:
            actual.update(self._workflow_triggers(path, trigger_key))

        missing = sorted(required - actual)
        if not missing:
            return []
        return [
            CiHealthFinding(
                violation_type="ci_trigger_incomplete",
                severity="amber",
                block_deploy=False,
                message="CI workflow does not include all required triggers.",
                details=[f"missing: {', '.join(missing)}"],
            )
        ]

    def _workflow_triggers(self, path: Path, trigger_key: str) -> set[str]:
        payload = _read_yaml_mapping(path)
        value = payload.get(trigger_key)
        if value is None and trigger_key == "on":
            value = payload.get(True)
        return _trigger_names(value)

    def _check_verification_coverage(
        self,
        workflow_files: list[Path],
        project_root: Path,
    ) -> list[CiHealthFinding]:
        verification_commands = self._deploy_verification_commands(project_root)
        if not verification_commands:
            return []

        workflow_commands = {
            _normalize_command(command)
            for path in workflow_files
            for command in self._workflow_commands(path)
            if _normalize_command(command)
        }
        missing = [
            command
            for command in verification_commands
            if not self._command_appears_in_workflow(command, workflow_commands)
        ]
        if not missing:
            return []
        return [
            CiHealthFinding(
                violation_type="ci_verification_not_in_workflow",
                severity="amber",
                block_deploy=False,
                message="Deployment verification command is not invoked by CI workflow.",
                details=missing,
            )
        ]

    def _deploy_verification_commands(self, project_root: Path) -> list[str]:
        commands: list[str] = []
        for path in self._deploy_yaml_candidates(project_root):
            # Re-confine each fixed-name candidate before reading: an in-root
            # ``deploy.yaml`` (or ``.codd``/``codd`` variant) that is a symlink
            # whose target escapes the project tree would otherwise leak its
            # off-root post_deploy commands into ``ci_verification_not_in_workflow``
            # (a path-escape false amber). The escaping candidate is dropped; an
            # in-root → in-root symlink and plain absence are unaffected. The
            # workflow files were already re-confined in ``_locate_workflows``.
            if resolve_project_path(project_root, path) is None:
                continue
            if not path.is_file():
                continue
            payload = _read_yaml_mapping(path)
            commands.extend(_hook_commands(payload))
        return _dedupe(_normalize_command(command) for command in commands if _normalize_command(command))

    @staticmethod
    def _deploy_yaml_candidates(project_root: Path) -> list[Path]:
        return [
            project_root / "deploy.yaml",
            project_root / ".codd" / "deploy.yaml",
            project_root / "codd" / "deploy.yaml",
        ]

    def _workflow_commands(self, path: Path) -> list[str]:
        return _command_values(_read_yaml_mapping(path))

    @staticmethod
    def _command_appears_in_workflow(command: str, workflow_commands: set[str]) -> bool:
        return any(command == candidate or command in candidate for candidate in workflow_commands)


def _relative_to_root(path: Path, project_root: Path) -> str:
    """Serialize ``path`` relative to ``project_root`` without raising.

    Workflow paths reaching this helper are already confined to the root by
    :meth:`CiHealthCheck._locate_workflows` (each match is re-checked through the
    shared path_safety jail), but an absolute-glob match may be lexically outside
    the (unresolved) root while still resolving inside it (e.g. via a symlink).
    The shared :func:`project_relative_path` resolves + confines + relativizes;
    the lexical fallback keeps serialization total for the already-confined case.
    """

    relative = project_relative_path(project_root, path)
    if relative is not None:
        return relative
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.resolve().relative_to(project_root.resolve()).as_posix()


def _glob_static_base(pattern: str) -> str:
    """Return the leading prefix of ``pattern`` that contains no glob magic.

    e.g. ``/abs/.github/workflows/*.yml`` -> ``/abs/.github/workflows``;
    ``.github/workflows/*.yml`` -> ``.github/workflows``. The base is what
    determines whether a configured ``workflow_glob`` escapes the project root,
    independent of the wildcard tail — so it can be confined BEFORE enumeration.
    """
    base_parts: list[str] = []
    for part in Path(pattern).parts:
        if has_magic(part):
            break
        base_parts.append(part)
    if not base_parts:
        return ""
    return str(Path(*base_parts))


def _absolute_to_relative_glob(project_root: Path, pattern: str, base: str) -> str | None:
    """Rebase an absolute (in-root) ``workflow_glob`` to a root-relative pattern.

    ``Path.glob`` rejects absolute patterns, so an absolute glob whose base has
    already been confirmed in-root is re-expressed relative to ``project_root``
    (preserving its absolute semantics). Returns ``None`` when the pattern
    anchors exactly at the root (nothing to enumerate).
    """
    rel_base = project_relative_path(project_root, base) if base else ""
    if rel_base is None:
        return None
    magic_tail = (
        Path(pattern).parts[len(Path(base).parts):] if base else Path(pattern).parts[1:]
    )
    rel_parts = [p for p in (rel_base,) if p and p != "."] + list(magic_tail)
    return str(Path(*rel_parts)) if rel_parts else None


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def _mapping_value(mapping: Any, key: str) -> Mapping[str, Any] | None:
    if isinstance(mapping, Mapping) and isinstance(mapping.get(key), Mapping):
        return mapping[key]
    return None


def _trigger_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value if item}
    if isinstance(value, dict):
        return {str(key) for key in value if key}
    return set()


# Keys that carry the actual verification spec inside a hook mapping. When a
# mapping declares one of these alongside a string-valued ``verification:``
# entry, the ``verification`` value is a name/label for the hook (schema:
# ``- verification: <label>`` + ``command:``/``url:`` ...), not a command.
_VERIFICATION_SPEC_KEYS = ("command", "run", "script", "test", "url")


def _hook_commands(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_hook_commands(item))
        return commands
    if isinstance(value, dict):
        commands: list[str] = []
        for key in ("post_deploy", "post_deploy_steps", "post_deploy_hooks"):
            commands.extend(_hook_commands(value.get(key)))
        for key in ("verification", "verifications"):
            item = value.get(key)
            if _is_verification_label(item, value):
                continue
            commands.extend(_hook_commands(item))
        for key in ("command", "run", "script", "test"):
            commands.extend(_hook_commands(value.get(key)))
        targets = value.get("targets")
        if isinstance(targets, dict):
            for target_config in targets.values():
                commands.extend(_hook_commands(target_config))
        return commands
    return []


def _is_verification_label(value: Any, mapping: dict[str, Any]) -> bool:
    """True when a string-valued ``verification:``/``verifications:`` entry is
    a hook label rather than a command: the surrounding mapping carries the
    actual spec (an explicit command key or an endpoint url). Collecting the
    label as a command would misreport it as a CI-missing verification."""
    if not isinstance(value, str):
        return False
    return any(mapping.get(key) is not None for key in _VERIFICATION_SPEC_KEYS)


def _command_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return []
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_command_values(item))
        return commands
    if isinstance(value, dict):
        commands: list[str] = []
        for key, item in value.items():
            if key in {"run", "script", "command", "commands"}:
                commands.extend(_coerce_command_list(item))
            else:
                commands.extend(_command_values(item))
        return commands
    return []


def _coerce_command_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        commands: list[str] = []
        for item in value:
            commands.extend(_coerce_command_list(item))
        return commands
    return []


def _normalize_command(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _string_value(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _string_list(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    result = [str(item) for item in value if str(item).strip()]
    return result or list(default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _max_severity(values: Any) -> str:
    order = {"info": 0, "amber": 1, "red": 2}
    selected = "info"
    for value in values:
        text = str(value)
        if order.get(text, 0) > order[selected]:
            selected = text
    return selected
