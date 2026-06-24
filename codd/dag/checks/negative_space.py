"""DAG check: negative_space — declared forbidden-evidence scan.

This check deliberately does **not** attempt a general absence guarantee. It
never claims "no PII exists" or "the data is clean": proving a universal
negative over an unbounded corpus is outside what a static harness can honestly
assert, and a core that hard-coded what to forbid (PII shapes, domain literals)
would be both unsound and non-generic.

Instead it inspects only the forbidden evidence a project **explicitly
declares**. For each declaration it resolves the declared scope globs, scans the
matched files for the declared regex patterns, and reports hits. The honest
claim is bounded: "within the declared scan scope, N forbidden-pattern hits".

Optional project-side schema (no PII / domain literal lives in core — every
pattern is supplied by the project)::

    negative_space:
      forbidden_evidence:
        - id: no_secret_token_in_logs
          scope:
            paths:
              - logs/**/*.txt
              - src/**/*.py
          patterns:
            - name: secret_token
              regex: "SECRET_[A-Z]+"
          on_violation: warn   # warn | fail   (default: warn)

Severity model:

* hit(s) found AND ``on_violation: fail`` explicitly declared -> **red**
  (a deploy blocker the project itself logically declared). Otherwise hit(s)
  with the default or ``warn`` -> **amber** (visibility, not a blocker).
* scope declares no usable paths -> amber ``malformed_negative_space``.
* scope is usable but no usable pattern is declared (``patterns: []`` or every
  pattern missing/empty regex) -> amber ``no_usable_patterns`` (never a clean
  pass: a declaration that forbids nothing has verified nothing).
* scope resolves to 0 files -> amber ``vacuous`` (never a clean pass: a scope
  that matches nothing has verified nothing).
* the ``forbidden_evidence`` key is declared but malformed (a mapping instead
  of a list, or a list with no usable declaration — empty list, or non-mapping
  entries) -> amber ``malformed_negative_space``. The project opted in yet
  nothing is checked; this is never a silent skip (a vacuous false-green).
* the ``forbidden_evidence`` key is absent altogether -> ``skip``
  (checked_count=0, skipped=True): dormant by default, legacy/unrelated
  projects keep passing unchanged. "Key absent" is deliberately distinct from
  "declared but malformed".

Guards (none of these are silently swallowed):

* **path traversal** — every scoped path is resolved and rejected if it escapes
  the project root (``path_outside_root`` amber).
* **binary / unreadable files** — a file that cannot be decoded as text is
  skipped with a ``skipped`` diagnostic, not scanned (no false hit, no crash).
* **regex compile error** — surfaced as an ``invalid_regex`` amber diagnostic,
  mirroring ``extraction_diagnostics`` (never crash, never red).

API mirrors ``extraction_diagnostics.py`` / ``cardinality_coverage.py``
(``DagCheck`` + ``@register_dag_check`` + a result dataclass exposing
check_name/severity/status/passed/block_deploy/skipped/checked_count/warnings).
``codd/dag/runner.py`` is intentionally not edited here; the parent registers
the module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from glob import has_magic
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from codd.dag.checks import DagCheck, register_dag_check
from codd.path_safety import (
    PathEscapeError,
    project_relative_path,
    require_project_path,
    resolve_project_path,
)


_ON_VIOLATION_FAIL = "fail"
_ON_VIOLATION_WARN = "warn"


@dataclass
class NegativeSpaceResult:
    check_name: str = "negative_space"
    severity: str = "amber"
    status: str = "pass"
    message: str = ""
    block_deploy: bool = False
    passed: bool = True
    skipped: bool = False
    # files actually scanned across every declaration; 0 on a pass = vacuous
    checked_count: int = 0
    declarations_total: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)


@register_dag_check("negative_space")
class NegativeSpaceCheck(DagCheck):
    """Scan declared scopes for declared forbidden patterns; report hits only."""

    check_name = "negative_space"
    severity = "amber"
    block_deploy = False

    def run(
        self,
        dag: Any | None = None,
        project_root: str | Path | None = None,
        settings: dict[str, Any] | None = None,
        codd_config: dict[str, Any] | None = None,
    ) -> NegativeSpaceResult:
        if project_root is not None:
            self.project_root = Path(project_root)
        if settings is not None:
            self.settings = settings

        config = codd_config if codd_config is not None else self.settings
        declared = _forbidden_evidence_declared(config)
        declarations = _forbidden_evidence(config)

        if not declarations:
            if declared:
                # The forbidden_evidence key IS present but yields no usable
                # declaration: it is a mapping instead of a list of declarations,
                # or a list with zero mapping entries (empty list, or scalars
                # only). The project explicitly opted in, so this is a malformed
                # declaration — never a silent SKIP (which would be a vacuous
                # false-green: declared yet nothing checked, no diagnostic).
                return _finalize(
                    warnings=[_malformed_container_diagnostic()],
                    checked_count=0,
                    declarations_total=0,
                    has_fail_hit=False,
                )
            # The key itself is absent (legacy / unrelated projects never opted
            # in): dormant by default, keep passing unchanged.
            return NegativeSpaceResult(
                status="skip",
                skipped=True,
                passed=True,
                block_deploy=False,
                message=(
                    "negative_space SKIP "
                    "(no negative_space.forbidden_evidence declared)"
                ),
            )

        root = (self.project_root or Path.cwd()).resolve()

        warnings: list[dict[str, Any]] = []
        checked_count = 0
        has_fail_hit = False
        has_scope_escape = False

        for index, declaration in enumerate(declarations):
            decl_id = _declaration_id(declaration, index)
            on_violation = _on_violation(declaration)
            paths = _scope_paths(declaration)

            if not paths:
                warnings.append(_malformed_diagnostic(decl_id))
                continue

            compiled, regex_errors = _compile_patterns(declaration, decl_id)
            warnings.extend(regex_errors)

            try:
                scanned_files, scope_warnings = _resolve_scope_files(paths, root)
            except PathEscapeError as exc:
                # A DECLARED scope (its raw glob) resolves OUTSIDE the project
                # root — an absolute path pointing off-tree, or a ``../``
                # traversal that escapes. This is fail-closed: the project
                # declared an evidence scope CoDD cannot honestly scan, so the
                # check is NOT-VALID rather than silently amber. (Distinct from
                # an in-root scope that merely matches a per-file symlink whose
                # target escapes — that match is dropped + amber, below.)
                has_scope_escape = True
                warnings.append(_scope_outside_root_diagnostic(decl_id, exc))
                continue
            warnings.extend(scope_warnings)

            if not compiled:
                # Nothing scannable. Two distinct causes, both vacuous:
                #   * the declared pattern(s) failed to compile -> already
                #     surfaced as amber invalid_regex above; do not double-report.
                #   * NO usable pattern was declared at all (patterns: [] or every
                #     entry missing/empty regex) -> the declaration claims to
                #     forbid evidence yet checks nothing; surface a dedicated
                #     amber so it is never a clean pass (malformed declaration).
                if not regex_errors:
                    warnings.append(_no_usable_patterns_diagnostic(decl_id))
                # Either way nothing was scanned; do not count files as checked.
                continue

            decl_checked = 0
            for file_path in scanned_files:
                text = _read_text(file_path)
                if text is None:
                    warnings.append(
                        _skipped_diagnostic(decl_id, file_path, root)
                    )
                    continue
                decl_checked += 1
                for pattern_name, regex in compiled:
                    hit_count = sum(1 for _ in regex.finditer(text))
                    if hit_count:
                        red = on_violation == _ON_VIOLATION_FAIL
                        has_fail_hit = has_fail_hit or red
                        warnings.append(
                            _hit_diagnostic(
                                decl_id,
                                pattern_name,
                                file_path,
                                root,
                                hit_count,
                                red=red,
                            )
                        )

            checked_count += decl_checked

            if decl_checked == 0 and not any(
                w.get("declaration_id") == decl_id
                and w.get("type") in {"path_outside_root", "invalid_regex"}
                for w in warnings
            ):
                # Scope was well-formed and patterns compiled, but matched no
                # readable in-root file: vacuous, never a clean pass.
                warnings.append(_vacuous_diagnostic(decl_id))

        return _finalize(
            warnings=warnings,
            checked_count=checked_count,
            declarations_total=len(declarations),
            has_fail_hit=has_fail_hit,
            has_scope_escape=has_scope_escape,
        )


# --- config resolution ------------------------------------------------------


_FORBIDDEN_EVIDENCE_KEY = "forbidden_evidence"


def _forbidden_evidence_declared(config: Any) -> bool:
    """Whether the project opted in by declaring the forbidden_evidence key.

    This is independent of whether the value is well-formed: it answers "did the
    project write the key at all?", which lets the caller tell "no declaration"
    (key absent -> skip) apart from "declared but malformed" (key present but the
    value is not a usable list of declarations -> amber).
    """
    if not isinstance(config, Mapping):
        return False
    section = config.get("negative_space")
    if not isinstance(section, Mapping):
        return False
    return _FORBIDDEN_EVIDENCE_KEY in section


def _forbidden_evidence(config: Any) -> list[Mapping[str, Any]]:
    if not isinstance(config, Mapping):
        return []
    section = config.get("negative_space")
    if not isinstance(section, Mapping):
        return []
    declarations = section.get(_FORBIDDEN_EVIDENCE_KEY)
    if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
        return []
    return [d for d in declarations if isinstance(d, Mapping)]


def _declaration_id(declaration: Mapping[str, Any], index: int) -> str:
    raw = declaration.get("id")
    if isinstance(raw, str) and raw:
        return raw
    return f"forbidden_evidence[{index}]"


def _on_violation(declaration: Mapping[str, Any]) -> str:
    raw = declaration.get("on_violation")
    if isinstance(raw, str) and raw.strip().lower() == _ON_VIOLATION_FAIL:
        return _ON_VIOLATION_FAIL
    return _ON_VIOLATION_WARN


def _scope_paths(declaration: Mapping[str, Any]) -> list[str]:
    scope = declaration.get("scope")
    if not isinstance(scope, Mapping):
        return []
    paths = scope.get("paths")
    if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
        return []
    return [p for p in paths if isinstance(p, str) and p.strip()]


def _compile_patterns(
    declaration: Mapping[str, Any], decl_id: str
) -> tuple[list[tuple[str, re.Pattern[str]]], list[dict[str, Any]]]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    errors: list[dict[str, Any]] = []
    patterns = declaration.get("patterns")
    if not isinstance(patterns, Sequence) or isinstance(patterns, (str, bytes)):
        return compiled, errors
    for p_index, pattern in enumerate(patterns):
        if not isinstance(pattern, Mapping):
            continue
        regex_text = pattern.get("regex")
        if not isinstance(regex_text, str) or not regex_text:
            continue
        name = pattern.get("name")
        pattern_name = name if isinstance(name, str) and name else f"pattern[{p_index}]"
        try:
            compiled.append((pattern_name, re.compile(regex_text)))
        except re.error as exc:
            errors.append(_invalid_regex_diagnostic(decl_id, pattern_name, regex_text, exc))
    return compiled, errors


# --- scope resolution (with traversal guard) --------------------------------


def _resolve_scope_files(
    paths: list[str], root: Path
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Resolve scope globs to in-root files.

    Two distinct path-escape cases, kept separate on purpose:

    * a DECLARED scope whose raw glob escapes the project root (an absolute
      off-tree path, or a ``../`` traversal) -> raise :class:`PathEscapeError`
      (fail-closed). The project declared an evidence scope CoDD cannot honestly
      scan; a silent amber there is a false-green. ``lstrip("/")`` is *not*
      applied, so an absolute scope keeps its absolute semantics (it is honoured
      only when it genuinely lives under the root) and is never re-rooted onto a
      same-named in-root tree.
    * an in-root scope glob that merely *matches* a per-file symlink whose target
      escapes the root -> that single match is dropped + amber ``path_outside_root``
      (visibility), not fail-closed.
    """
    files: dict[Path, None] = {}
    warnings: list[dict[str, Any]] = []
    flagged_escape = False
    for raw in paths:
        # Fail-closed on a declared scope that escapes the root (may raise
        # PathEscapeError, which the caller turns into a red scope_outside_root).
        for match in _glob(root, raw):
            # Confinement decision via the shared path_safety jail: an in-root
            # scope glob may still match an in-root symlink whose target escapes
            # the root. The shared closure silently drops escapes; negative_space
            # surfaces the first such per-file escape as amber visibility.
            resolved = resolve_project_path(root, match)
            if resolved is None:
                if not flagged_escape:
                    warnings.append(_path_outside_root_diagnostic(raw, root))
                    flagged_escape = True
                continue
            try:
                if not resolved.is_file():
                    continue
            except OSError:
                continue
            files[resolved] = None
    return list(files), warnings


def _glob(root: Path, raw: str) -> list[Path]:
    """Enumerate a single declared scope glob, anchored inside ``root``.

    The declared scope's static (non-magic) base is confined via the shared
    path_safety jail *before* any enumeration: an absolute base pointing off-tree
    or a ``../`` traversal that escapes raises :class:`PathEscapeError`
    (fail-closed). An absolute base that genuinely lives under the root is
    re-expressed as a root-relative pattern so ``Path.glob`` (which rejects
    absolute patterns) can enumerate it while preserving absolute semantics.
    """
    pattern = raw.strip()
    if not pattern:
        return []

    rel_pattern = _project_relative_glob(root, pattern)
    if rel_pattern is None:
        # Empty after anchoring (e.g. the scope was exactly the project root).
        return []
    try:
        return list(root.glob(rel_pattern))
    except (ValueError, OSError):
        return []


def _glob_static_base(pattern: str) -> str:
    """Return the leading prefix of ``pattern`` that contains no glob magic.

    e.g. ``/tmp/secret/**/*.py`` -> ``/tmp/secret``; ``src/**/*.py`` -> ``src``;
    ``../outside.py`` -> ``../outside.py`` (no magic at all -> whole pattern).
    The base is what determines whether a *declared scope* escapes the root,
    independent of the wildcard tail.
    """
    parts = Path(pattern).parts
    base_parts: list[str] = []
    for part in parts:
        if has_magic(part):
            break
        base_parts.append(part)
    if not base_parts:
        return ""
    return str(Path(*base_parts))


def _project_relative_glob(root: Path, pattern: str) -> str | None:
    """Anchor a declared scope glob under ``root`` as a root-relative pattern.

    Fail-closed (raises :class:`PathEscapeError`) when the scope's static base
    escapes the project root. Returns ``None`` when the pattern anchors exactly
    at the root (nothing to enumerate). Relative in-root patterns are returned
    unchanged; absolute in-root patterns are rebased onto the root.
    """
    base = _glob_static_base(pattern)
    # Confine the static base (fail-closed on escape). ``require_project_path``
    # resolves + follows symlinks + confines, raising PathEscapeError otherwise.
    # An empty base means the magic starts at the first segment (a relative
    # pattern like ``**/*.py``); that is trivially in-root, so skip the check.
    if base:
        require_project_path(root, base, context="negative_space scope path")

    if Path(pattern).is_absolute():
        # Re-express the absolute (and now confirmed in-root) pattern relative to
        # the root so Path.glob accepts it, preserving its absolute semantics.
        rel_base = project_relative_path(root, base) if base else ""
        if rel_base is None:
            # Defensive: base confined above, so this should not happen.
            raise PathEscapeError(
                f"negative_space scope path resolves outside the project root: {pattern!r}",
                path=pattern,
            )
        magic_tail = Path(pattern).parts[len(Path(base).parts):] if base else Path(pattern).parts[1:]
        rel_parts = [p for p in (rel_base,) if p and p != "."] + list(magic_tail)
        return str(Path(*rel_parts)) if rel_parts else None

    # Relative pattern: already root-relative; enumerate as-is.
    return pattern


def _read_text(path: Path) -> str | None:
    """Return file text, or ``None`` for binary/unreadable files (skip, no hit)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --- result assembly --------------------------------------------------------


def _finalize(
    *,
    warnings: list[dict[str, Any]],
    checked_count: int,
    declarations_total: int,
    has_fail_hit: bool,
    has_scope_escape: bool = False,
) -> NegativeSpaceResult:
    if has_scope_escape:
        # A declared scope escaped the project root: fail-closed (red/error),
        # never a silent amber. CoDD cannot honestly scan an off-tree evidence
        # scope, so the check is NOT-VALID. This takes precedence — the gate must
        # not green/amber while a declared scope could not be examined at all.
        return NegativeSpaceResult(
            status="error",
            severity="red",
            passed=False,
            block_deploy=True,
            checked_count=checked_count,
            declarations_total=declarations_total,
            warnings=warnings,
            message=(
                "negative_space ERROR — a declared scope path resolves outside "
                "the project root and cannot be scanned (fail-closed); keep "
                "scope paths within the project tree "
                f"({checked_count} file(s) scanned across "
                f"{declarations_total} declaration(s))"
            ),
        )
    if has_fail_hit:
        return NegativeSpaceResult(
            status="fail",
            severity="red",
            passed=False,
            block_deploy=True,
            checked_count=checked_count,
            declarations_total=declarations_total,
            warnings=warnings,
            message=(
                "negative_space FAIL — forbidden evidence found in a scope "
                "declared on_violation: fail "
                f"({checked_count} file(s) scanned across "
                f"{declarations_total} declaration(s))"
            ),
        )
    if warnings:
        return NegativeSpaceResult(
            status="warn",
            severity="amber",
            passed=True,
            block_deploy=False,
            checked_count=checked_count,
            declarations_total=declarations_total,
            warnings=warnings,
            message=(
                f"negative_space found {len(warnings)} diagnostic(s) "
                f"({checked_count} file(s) scanned across "
                f"{declarations_total} declaration(s))"
            ),
        )
    return NegativeSpaceResult(
        status="pass",
        severity="amber",
        passed=True,
        block_deploy=False,
        checked_count=checked_count,
        declarations_total=declarations_total,
        message=(
            "negative_space PASS — no forbidden-pattern hits within the "
            f"declared scan scope ({checked_count} file(s) scanned across "
            f"{declarations_total} declaration(s))"
        ),
    )


# --- diagnostics ------------------------------------------------------------


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _hit_diagnostic(
    decl_id: str,
    pattern_name: str,
    file_path: Path,
    root: Path,
    hit_count: int,
    *,
    red: bool,
) -> dict[str, Any]:
    severity = "red" if red else "amber"
    return {
        "type": "forbidden_evidence_hit",
        "declaration_id": decl_id,
        "pattern": pattern_name,
        "path": _rel(file_path, root),
        "hit_count": hit_count,
        "severity": severity,
        "remediation": (
            f"Forbidden pattern '{pattern_name}' (declaration '{decl_id}') "
            f"matched {hit_count} time(s) within the declared scan scope. "
            "Remove the offending content or narrow the declared scope."
        ),
    }


def _malformed_diagnostic(decl_id: str) -> dict[str, Any]:
    return {
        "type": "malformed_negative_space",
        "declaration_id": decl_id,
        "severity": "amber",
        "remediation": (
            f"negative_space declaration '{decl_id}' has no scope.paths; "
            "declare at least one glob under scope.paths or remove the entry."
        ),
    }


def _malformed_container_diagnostic() -> dict[str, Any]:
    return {
        "type": "malformed_negative_space",
        "declaration_id": None,
        "severity": "amber",
        "remediation": (
            "negative_space.forbidden_evidence is declared but malformed: it "
            "must be a list of declaration mappings. It is either a mapping "
            "(wrap the single declaration in a list) or a list with no usable "
            "declaration (empty list, or non-mapping entries). Declare at least "
            "one declaration, or remove the forbidden_evidence key entirely "
            "(this is not a clean skip — the key was declared but checks "
            "nothing)."
        ),
    }


def _no_usable_patterns_diagnostic(decl_id: str) -> dict[str, Any]:
    return {
        "type": "no_usable_patterns",
        "declaration_id": decl_id,
        "severity": "amber",
        "remediation": (
            f"negative_space declaration '{decl_id}' has a scope but no usable "
            "pattern (patterns is empty, or every pattern is missing a non-empty "
            "regex), so nothing was scanned. Declare at least one pattern with a "
            "regex, or remove the entry (this is not a clean pass)."
        ),
    }


def _vacuous_diagnostic(decl_id: str) -> dict[str, Any]:
    return {
        "type": "vacuous",
        "declaration_id": decl_id,
        "severity": "amber",
        "remediation": (
            f"negative_space declaration '{decl_id}' scope matched no readable "
            "in-root file, so nothing was verified. Fix the scope globs (this "
            "is not a clean pass)."
        ),
    }


def _path_outside_root_diagnostic(raw_path: str, root: Path) -> dict[str, Any]:
    return {
        "type": "path_outside_root",
        "declaration_id": None,
        "path": raw_path,
        "severity": "amber",
        "remediation": (
            f"negative_space scope path '{raw_path}' resolves outside the "
            f"project root ({root}); out-of-root files are not scanned. "
            "Keep scope paths within the project tree."
        ),
    }


def _scope_outside_root_diagnostic(decl_id: str, exc: PathEscapeError) -> dict[str, Any]:
    """Red diagnostic for a DECLARED scope whose glob escapes the project root.

    This is the fail-closed twin of ``path_outside_root`` (which is a per-file
    symlink escape inside an in-root scope, amber). Here the *declared scope
    itself* points off-tree, so the declaration cannot be honestly scanned and
    the check is NOT-VALID — never a silent amber/green false-green.
    """
    return {
        "type": "scope_outside_root",
        "declaration_id": decl_id,
        "path": exc.path,
        "severity": "red",
        "remediation": (
            f"negative_space declaration '{decl_id}' declares a scope path that "
            f"resolves outside the project root ({exc.path!r}); CoDD will not "
            "scan off-tree evidence. Keep every scope path within the project "
            "tree (this is fail-closed, not a clean pass)."
        ),
    }


def _skipped_diagnostic(decl_id: str, file_path: Path, root: Path) -> dict[str, Any]:
    return {
        "type": "skipped",
        "declaration_id": decl_id,
        "path": _rel(file_path, root),
        "severity": "amber",
        "remediation": (
            f"negative_space skipped '{_rel(file_path, root)}' "
            "(binary or undecodable as UTF-8); it was not scanned."
        ),
    }


def _invalid_regex_diagnostic(
    decl_id: str, pattern_name: str, regex_text: str, exc: re.error
) -> dict[str, Any]:
    return {
        "type": "invalid_regex",
        "declaration_id": decl_id,
        "pattern": pattern_name,
        "regex": regex_text,
        "error": str(exc),
        "severity": "amber",
        "remediation": (
            f"Fix the regex for pattern '{pattern_name}' in declaration "
            f"'{decl_id}' (cannot compile: {exc}), or remove the pattern."
        ),
    }


# --- diagnostic helpers reused by the patterns assembly ---------------------

__all__ = ["NegativeSpaceCheck", "NegativeSpaceResult"]
