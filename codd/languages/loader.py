"""Load a ``LanguageProfile`` from a YAML file.

Phase 1, additive only. Parsing rules:

* Uses PyYAML's ``safe_load`` (the repo-wide convention; see ``codd/config.py``).
  No toml is needed — profiles are YAML.
* Placeholder templates (``{package_name}`` / ``{module_path}`` /
  ``{module_root}`` etc.) are kept as **literal strings**. They are NOT
  substituted here — that's PathPlanner's job in a later phase.
* Unknown / not-yet-modeled top-level keys (e.g. ``path_rules``,
  ``implement_oracle``) are preserved verbatim under
  ``LanguageProfile.extra`` and the whole document under ``.raw``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from .profile import (
    ArtifactsSpec,
    CiSpec,
    CommandSpec,
    DependencyIntegrityFile,
    Identity,
    ImportsSpec,
    LanguageProfile,
    LayoutSpec,
    ManifestSpec,
    PackageRoot,
    ReportSpec,
    ScaffoldSpec,
    ScopeSpec,
    SourceSet,
    TestSet,
    TestsSpec,
    ToolchainSpec,
    VerifySpec,
)


class LanguageProfileError(ValueError):
    """Raised when a language profile YAML is malformed or missing required keys."""


# ---------------------------------------------------------------------------
# small parsing helpers
# ---------------------------------------------------------------------------


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    # A scalar where a list was expected — wrap it, be forgiving.
    return (value,)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    return tuple(str(v) for v in _as_tuple(value))


def _as_mapping(value: Any, *, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise LanguageProfileError(f"{where}: expected a mapping, got {type(value).__name__}")


def _require(mapping: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise LanguageProfileError(f"{where}: missing required key '{key}'")
    return mapping[key]


# ---------------------------------------------------------------------------
# section parsers
# ---------------------------------------------------------------------------


def _parse_identity(doc: Mapping[str, Any]) -> Identity:
    where = "identity"
    lang_id = _require(doc, "id", where=where)
    display = doc.get("display_name") or str(lang_id)
    strictness = doc.get("strictness", "strict")
    if strictness not in ("strict", "legacy_compatible"):
        raise LanguageProfileError(
            f"{where}: strictness must be 'strict' or 'legacy_compatible', "
            f"got {strictness!r}"
        )
    return Identity(
        id=str(lang_id),
        display_name=str(display),
        aliases=_as_str_tuple(doc.get("aliases")),
        file_extensions=_as_str_tuple(doc.get("file_extensions")),
        strictness=strictness,
    )


def _parse_source_set(raw: Mapping[str, Any], *, idx: int) -> SourceSet:
    where = f"layout.source_sets[{idx}]"
    m = _as_mapping(raw, where=where)
    return SourceSet(
        id=str(_require(m, "id", where=where)),
        root=str(_require(m, "root", where=where)),
        file_globs=_as_str_tuple(m.get("file_globs")),
        role=(str(m["role"]) if m.get("role") is not None else None),
    )


def _parse_test_set(raw: Mapping[str, Any], *, idx: int) -> TestSet:
    where = f"layout.test_sets[{idx}]"
    m = _as_mapping(raw, where=where)
    return TestSet(
        id=str(_require(m, "id", where=where)),
        root=str(_require(m, "root", where=where)),
        file_globs=_as_str_tuple(m.get("file_globs")),
        role=(str(m["role"]) if m.get("role") is not None else None),
        colocated=bool(m.get("colocated", False)),
        optional=bool(m.get("optional", False)),
    )


def _parse_package_root(raw: Any) -> PackageRoot:
    m = _as_mapping(raw, where="layout.package_root")
    if not m:
        return PackageRoot(kind="none")
    kind = m.get("kind", "none")
    return PackageRoot(
        kind=str(kind),
        path=(str(m["path"]) if m.get("path") is not None else None),
    )


def _parse_layout(doc: Mapping[str, Any]) -> LayoutSpec:
    m = _as_mapping(doc.get("layout"), where="layout")
    source_sets = tuple(
        _parse_source_set(s, idx=i) for i, s in enumerate(_as_tuple(m.get("source_sets")))
    )
    test_sets = tuple(
        _parse_test_set(s, idx=i) for i, s in enumerate(_as_tuple(m.get("test_sets")))
    )
    default_cwd = m.get("default_command_cwd")
    return LayoutSpec(
        repo_root=str(m.get("repo_root", ".")),
        module_root=str(m.get("module_root", ".")),
        manifest_root=str(m.get("manifest_root", ".")),
        default_command_cwd=(str(default_cwd) if default_cwd is not None else None),
        source_sets=source_sets,
        test_sets=test_sets,
        package_root=_parse_package_root(m.get("package_root")),
    )


def _parse_report(raw: Any) -> ReportSpec | None:
    if raw is None:
        return None
    m = _as_mapping(raw, where="report")
    return ReportSpec(
        path=(str(m["path"]) if m.get("path") is not None else None),
        format=(str(m["format"]) if m.get("format") is not None else None),
        adapter=(str(m["adapter"]) if m.get("adapter") is not None else None),
        capture=(str(m["capture"]) if m.get("capture") is not None else None),
    )


def _parse_toolchain(doc: Mapping[str, Any]) -> ToolchainSpec | None:
    raw = doc.get("toolchain")
    if raw is None:
        return None
    m = _as_mapping(raw, where="toolchain")
    manifest_m = _as_mapping(m.get("manifest"), where="toolchain.manifest")
    if not manifest_m:
        raise LanguageProfileError("toolchain: missing required 'manifest' section")
    manifest = ManifestSpec(
        path=str(_require(manifest_m, "path", where="toolchain.manifest")),
        format=str(_require(manifest_m, "format", where="toolchain.manifest")),
        required=bool(manifest_m.get("required", True)),
    )
    integrity = []
    for i, raw_f in enumerate(_as_tuple(m.get("dependency_integrity_files"))):
        fm = _as_mapping(raw_f, where=f"toolchain.dependency_integrity_files[{i}]")
        integrity.append(
            DependencyIntegrityFile(
                path=str(
                    _require(
                        fm, "path", where=f"toolchain.dependency_integrity_files[{i}]"
                    )
                ),
                kind=str(fm.get("kind", "lock")),
                required=bool(fm.get("required", False)),
                generated_when=(
                    str(fm["generated_when"])
                    if fm.get("generated_when") is not None
                    else None
                ),
            )
        )
    # Everything not explicitly modeled is preserved (adapter-facing).
    known = {"manifest", "dependency_integrity_files", "package_manager", "module_identity"}
    extra = {k: v for k, v in m.items() if k not in known}
    return ToolchainSpec(
        manifest=manifest,
        dependency_integrity_files=tuple(integrity),
        package_manager=_as_mapping(m.get("package_manager"), where="toolchain.package_manager"),
        module_identity=_as_mapping(m.get("module_identity"), where="toolchain.module_identity"),
        extra=extra,
    )


def _parse_scope(raw: Any) -> ScopeSpec | None:
    if raw is None:
        return None
    m = _as_mapping(raw, where="command.scope")
    return ScopeSpec(
        must_include_source_sets=_as_str_tuple(m.get("must_include_source_sets")),
        must_include_test_sets=_as_str_tuple(m.get("must_include_test_sets")),
    )


def _parse_command(cmd_id: str, raw: Any) -> CommandSpec:
    where = f"commands.{cmd_id}"
    m = _as_mapping(raw, where=where)
    argv = _as_str_tuple(_require(m, "argv", where=where))
    if not argv:
        raise LanguageProfileError(f"{where}: 'argv' must be a non-empty list")
    env_raw = _as_mapping(m.get("env"), where=f"{where}.env")
    env = {str(k): str(v) for k, v in env_raw.items()}
    known = {
        "argv",
        "cwd",
        "env",
        "mutates",
        "requires_materialized_deps",
        "report",
        "scope",
    }
    extra = {k: v for k, v in m.items() if k not in known}
    return CommandSpec(
        id=cmd_id,
        argv=argv,
        cwd=(str(m["cwd"]) if m.get("cwd") is not None else None),
        env=env,
        mutates=_as_str_tuple(m.get("mutates")),
        requires_materialized_deps=bool(m.get("requires_materialized_deps", False)),
        report=_parse_report(m.get("report")),
        scope=_parse_scope(m.get("scope")),
        extra=extra,
    )


def _parse_commands(doc: Mapping[str, Any]) -> dict[str, CommandSpec]:
    m = _as_mapping(doc.get("commands"), where="commands")
    return {str(cmd_id): _parse_command(str(cmd_id), raw) for cmd_id, raw in m.items()}


def _parse_imports(doc: Mapping[str, Any]) -> ImportsSpec | None:
    raw = doc.get("imports")
    if raw is None:
        return None
    m = _as_mapping(raw, where="imports")
    resolver = m.get("resolver_adapter")
    data = {k: v for k, v in m.items() if k != "resolver_adapter"}
    return ImportsSpec(
        resolver_adapter=(str(resolver) if resolver is not None else None),
        data=data,
    )


def _parse_tests(doc: Mapping[str, Any]) -> TestsSpec | None:
    raw = doc.get("tests")
    if raw is None:
        return None
    m = _as_mapping(raw, where="tests")
    return TestsSpec(
        semantics_adapter=(
            str(m["semantics_adapter"]) if m.get("semantics_adapter") is not None else None
        ),
        runner_report_adapter=(
            str(m["runner_report_adapter"])
            if m.get("runner_report_adapter") is not None
            else None
        ),
        test_file_globs=_as_str_tuple(m.get("test_file_globs")),
        assertion_hints=_as_mapping(m.get("assertion_hints"), where="tests.assertion_hints"),
        authenticity_policy=_as_mapping(
            m.get("authenticity_policy"), where="tests.authenticity_policy"
        ),
        test_block_kinds=_as_str_tuple(m.get("test_block_kinds")),
    )


def _parse_verify(doc: Mapping[str, Any]) -> VerifySpec | None:
    raw = doc.get("verify")
    if raw is None:
        return None
    m = _as_mapping(raw, where="verify")
    return VerifySpec(
        command=(str(m["command"]) if m.get("command") is not None else None),
        report=_parse_report(m.get("report")),
        execution_policy=_as_mapping(
            m.get("execution_policy"), where="verify.execution_policy"
        ),
    )


def _parse_artifacts(doc: Mapping[str, Any]) -> ArtifactsSpec | None:
    raw = doc.get("artifacts")
    if raw is None:
        return None
    m = _as_mapping(raw, where="artifacts")
    known = {"harness_root", "harness_owned", "ignored"}
    extra = {k: v for k, v in m.items() if k not in known}
    return ArtifactsSpec(
        harness_root=(str(m["harness_root"]) if m.get("harness_root") is not None else None),
        harness_owned=_as_str_tuple(m.get("harness_owned")),
        ignored=_as_str_tuple(m.get("ignored")),
        extra=extra,
    )


def _parse_scaffold(doc: Mapping[str, Any]) -> ScaffoldSpec | None:
    raw = doc.get("scaffold")
    if raw is None:
        return None
    m = _as_mapping(raw, where="scaffold")
    templates = tuple(
        _as_mapping(t, where=f"scaffold.templates[{i}]")
        for i, t in enumerate(_as_tuple(m.get("templates")))
    )
    return ScaffoldSpec(
        adapter=(str(m["adapter"]) if m.get("adapter") is not None else None),
        owned_files=_as_str_tuple(m.get("owned_files")),
        templates=templates,
    )


def _parse_ci(doc: Mapping[str, Any]) -> CiSpec | None:
    raw = doc.get("ci")
    if raw is None:
        return None
    m = _as_mapping(raw, where="ci")
    steps = tuple(
        _as_mapping(s, where=f"ci.setup_steps[{i}]")
        for i, s in enumerate(_as_tuple(m.get("setup_steps")))
    )
    return CiSpec(
        setup_steps=steps,
        runs_on=str(m.get("runs_on", "ubuntu-latest")),
    )


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

#: Top-level keys we model explicitly. Anything else is preserved in ``.extra``.
_KNOWN_TOP_LEVEL = frozenset(
    {
        "id",
        "aliases",
        "display_name",
        "file_extensions",
        "strictness",
        "layout",
        "toolchain",
        "commands",
        "imports",
        "tests",
        "verify",
        "artifacts",
        "scaffold",
        "ci",
    }
)


def load_language_profile(path: str | Path) -> LanguageProfile:
    """Parse a language-profile YAML file into a :class:`LanguageProfile`.

    Placeholder templates are preserved literally (no substitution).
    Unknown top-level keys are kept under ``LanguageProfile.extra``; the
    full document is kept under ``.raw``.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem error passthrough
        raise LanguageProfileError(f"cannot read language profile {p}: {exc}") from exc

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LanguageProfileError(f"invalid YAML in language profile {p}: {exc}") from exc

    if doc is None:
        raise LanguageProfileError(f"language profile {p} is empty")
    if not isinstance(doc, Mapping):
        raise LanguageProfileError(
            f"language profile {p} must be a mapping at the top level, "
            f"got {type(doc).__name__}"
        )

    extra = {k: v for k, v in doc.items() if k not in _KNOWN_TOP_LEVEL}

    return LanguageProfile(
        identity=_parse_identity(doc),
        layout=_parse_layout(doc),
        toolchain=_parse_toolchain(doc),
        commands=_parse_commands(doc),
        imports=_parse_imports(doc),
        tests=_parse_tests(doc),
        verify=_parse_verify(doc),
        artifacts=_parse_artifacts(doc),
        scaffold=_parse_scaffold(doc),
        ci=_parse_ci(doc),
        extra=extra,
        raw=dict(doc),
    )
