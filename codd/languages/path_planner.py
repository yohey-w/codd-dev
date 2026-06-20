"""``PathPlanner`` — the single authority for declared-output paths.

This implements **Phase 2** of GPT-5.5 Pro's language-generality redesign
(``dogfood/gpt_language_generality_design.md`` §1.6 "Declared-output path
convention" and §4 "Phase 2: PathPlanner を単一 authority にする").

The defect it closes
--------------------
The C-Go dogfood run wrote ``go.mod`` / ``cmd/`` / ``internal/`` UNDER ``src/``
because two different code paths computed the path: the *declared output* was
repo-root-relative, while the *write target* was ``package_root + declared
output`` (with a Python/TS ``source_root=src`` assumption baked in). When the two
disagree you get ``src/cmd/server/main.go``.

The fix (§1.6): a **single** authority — ``PathPlanner`` — whose
``OutputPlan.repo_relpath`` is BOTH the declared-output identity AND the write
target. There is exactly one canonical, repo-relative POSIX path per planned
output, derived from the language profile's layout. Because the Go profile has
**no ``src/`` source root**, Go plans land at the repo root (``go.mod``,
``cmd/server/main.go``, ``internal/<pkg>/<file>.go``) — structurally, not by a
reactive patch.

What this module is / is not (Phase 2 scope)
--------------------------------------------
* It IS the pure path-resolution authority: ``(role, name, …) -> repo_relpath``.
* It does NOT write files, run commands, or read the project tree.
* It is **purely additive**: nothing in the live pipeline calls it yet (see the
  task report — live wiring is gated on proving Python/TS path invariance).

``project_context``
-------------------
The design references an abstract ``ProjectContext``; there is no such class in
the codebase yet, so :class:`PathPlanner` accepts a plain mapping of
substitution values (``package_name``, ``module_path``, …). It also accepts an
object exposing those as attributes, for forward-compatibility with a future
``ProjectContext`` dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, Mapping

from .profile import LanguageProfile

#: Output ownership (design §1.6 ``OutputPlan.owner``).
Owner = Literal["sut", "harness"]


class PathPlanError(ValueError):
    """Raised when a role cannot be resolved, or a planned path is illegal."""


@dataclass(frozen=True)
class OutputPlan:
    """A single planned output (design §1.6).

    ``repo_relpath`` is THE canonical, repo-root-relative POSIX path — it is both
    the declared-output identity and the write target. ``owner`` says whether the
    SUT or the harness owns the file. ``source_set`` / ``test_set`` record which
    declared set (if any) the path belongs to, for downstream scope checks.
    """

    role: str
    repo_relpath: PurePosixPath
    owner: Owner
    language: str
    source_set: str | None = None
    test_set: str | None = None

    @property
    def posix(self) -> str:
        """The path as a plain POSIX string (convenience for assertions/IO)."""
        return self.repo_relpath.as_posix()


def _ctx_get(context: Any, key: str) -> Any:
    """Read *key* from a mapping-like OR attribute-bearing project context."""
    if context is None:
        return None
    if isinstance(context, Mapping):
        return context.get(key)
    return getattr(context, key, None)


def _format_template(template: str, *, context: Any, extra: Mapping[str, Any]) -> str:
    """Substitute ``{placeholder}`` tokens from *extra* first, then *context*.

    Unknown placeholders are a hard error (``PathPlanError``) — a silent
    leftover ``{name}`` in a write path is exactly the class of bug this module
    exists to prevent.
    """
    import string

    result: list[str] = []
    formatter = string.Formatter()
    for literal_text, field_name, format_spec, conversion in formatter.parse(template):
        result.append(literal_text)
        if field_name is None:
            continue
        if field_name in extra and extra[field_name] is not None:
            value = extra[field_name]
        else:
            value = _ctx_get(context, field_name)
        if value is None:
            raise PathPlanError(
                f"cannot resolve placeholder {{{field_name}}} in template "
                f"{template!r} (provide it via name=/extra or project_context)"
            )
        result.append(str(value))
    return "".join(result)


def _canonical(rel: str) -> PurePosixPath:
    """Normalize a relative path string to a canonical repo-relative POSIX path."""
    cleaned = str(rel).strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise PathPlanError("planned path is empty after normalization")
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathPlanError(f"planned path escapes the repo root: {rel!r}")
    return PurePosixPath(*parts)


class PathPlanner:
    """Resolve declared-output roles to canonical repo-relative paths.

    Resolution strategy, in priority order:

    1. **Declared ``path_rules.output_roles``** (the profile's explicit role map,
       e.g. Go's ``command_entrypoint`` / ``internal_package_file`` / a fixed
       ``module_manifest`` path). This is the most precise source and is how the
       Go profile pins ``cmd/{command_name}/main.go`` etc.
    2. **Structural fallbacks** derived from ``layout``/``toolchain`` for the
       common roles (``manifest``, ``package_module``, a test path) so a profile
       that has not (yet) declared ``output_roles`` still plans coherently — this
       keeps Python/TS working from their declarative ``source_sets`` without
       needing a ``path_rules`` block.

    Every result is forced through :func:`_canonical` and then asserted against
    the profile's ``forbidden_generated_prefixes`` (Go: ``src/``), so an illegal
    ``src/...`` plan can never escape this authority.
    """

    def __init__(self, profile: LanguageProfile, project_context: Any = None) -> None:
        self.profile = profile
        self.context = project_context
        self._output_roles = self._load_output_roles(profile)
        self._forbidden_prefixes = self._load_forbidden_prefixes(profile)

    # -- profile-data loading ---------------------------------------------

    @staticmethod
    def _load_output_roles(profile: LanguageProfile) -> Mapping[str, Mapping[str, Any]]:
        path_rules = profile.extra.get("path_rules")
        if not isinstance(path_rules, Mapping):
            return {}
        roles = path_rules.get("output_roles")
        if not isinstance(roles, Mapping):
            return {}
        return roles

    @staticmethod
    def _load_forbidden_prefixes(profile: LanguageProfile) -> tuple[str, ...]:
        path_rules = profile.extra.get("path_rules")
        if not isinstance(path_rules, Mapping):
            return ()
        raw = path_rules.get("forbidden_generated_prefixes")
        if not raw:
            return ()
        if isinstance(raw, (list, tuple)):
            return tuple(str(p).strip().strip("/") + "/" for p in raw if str(p).strip())
        return ()

    # -- role aliasing -----------------------------------------------------

    #: Generic role name -> the declared ``output_roles`` key a profile may use.
    #: Lets callers ask for a stable generic role (``"entrypoint"``,
    #: ``"manifest"``) regardless of the profile's local naming.
    _ROLE_ALIASES: Mapping[str, tuple[str, ...]] = {
        "entrypoint": ("command_entrypoint", "entrypoint"),
        "manifest": ("module_manifest", "manifest"),
        "internal_package_file": ("internal_package_file",),
        "package_module": ("package_module", "internal_package_file"),
        "colocated_test_file": ("colocated_test_file",),
        "e2e_test_file": ("e2e_test_file",),
    }

    def _declared_role(self, role: str) -> tuple[str, Mapping[str, Any]] | None:
        """Find the declared ``output_roles`` entry for *role* (direct or alias)."""
        if role in self._output_roles:
            return role, self._output_roles[role]
        for alias in self._ROLE_ALIASES.get(role, ()):
            if alias in self._output_roles:
                return alias, self._output_roles[alias]
        return None

    # -- public API --------------------------------------------------------

    def plan_output(
        self,
        role: str,
        *,
        name: str | None = None,
        package: str | None = None,
        file: str | None = None,
        owner: Owner | None = None,
        **extra: Any,
    ) -> OutputPlan:
        """Plan one output: ``(role, …) -> OutputPlan`` with a canonical path.

        ``name`` is the primary parameter (e.g. an entrypoint command name like
        ``"server"``). ``package`` / ``file`` fill the corresponding template
        placeholders for package-file roles. Extra ``**extra`` keys are exposed
        to template substitution too (and override ``project_context``).

        The returned :attr:`OutputPlan.repo_relpath` is the SINGLE canonical
        write-target/declared-output path. Raises :class:`PathPlanError` if the
        role is unknown or the resolved path is illegal (e.g. would sit under a
        forbidden ``src/`` prefix).
        """
        # ``name`` is the primary token; a declared template may spell the file
        # stem as ``{name}`` (Go's internal_package_file: ``internal/{package}/
        # {name}.go``) OR a caller may pass ``file=``. Treat them as aliases so
        # either spelling resolves the same stem, without ambiguity (explicit
        # ``name=`` wins).
        stem = name if name is not None else file
        subst: dict[str, Any] = {
            "name": stem,
            "command_name": name if name is not None else file,
            "package": package,
            "file": file if file is not None else name,
        }
        subst.update(extra)

        declared = self._declared_role(role)
        if declared is not None:
            role_key, spec = declared
            rel, declared_owner = self._resolve_declared(role_key, spec, subst)
            return self._finalize(
                role=role,
                rel=rel,
                owner=owner or declared_owner,
                source_set=self._source_set_for(rel),
                test_set=self._test_set_for(rel),
            )

        rel, fallback_owner, source_set, test_set = self._resolve_structural(role, subst)
        return self._finalize(
            role=role,
            rel=rel,
            owner=owner or fallback_owner,
            source_set=source_set,
            test_set=test_set,
        )

    # -- declared resolution ----------------------------------------------

    def _resolve_declared(
        self, role_key: str, spec: Mapping[str, Any], subst: Mapping[str, Any]
    ) -> tuple[str, Owner]:
        owner = self._owner_from_spec(spec)
        # A role may declare a fixed ``path`` (e.g. go.mod) or a ``template``.
        if spec.get("path") is not None:
            return str(spec["path"]), owner
        if spec.get("template") is not None:
            rel = _format_template(str(spec["template"]), context=self.context, extra=subst)
            return rel, owner
        raise PathPlanError(
            f"output_role {role_key!r} declares neither 'path' nor 'template'"
        )

    @staticmethod
    def _owner_from_spec(spec: Mapping[str, Any]) -> Owner:
        raw = str(spec.get("owner", "sut")).strip().lower()
        return "harness" if raw == "harness" else "sut"

    # -- structural fallbacks ---------------------------------------------

    def _resolve_structural(
        self, role: str, subst: Mapping[str, Any]
    ) -> tuple[str, Owner, str | None, str | None]:
        """Derive common roles from ``layout``/``toolchain`` when not declared.

        Supports the roles Python/TS need from their declarative profiles
        (manifest, a package/source module, a test file) without requiring a
        ``path_rules`` block.
        """
        layout = self.profile.layout

        if role in ("manifest", "module_manifest"):
            if self.profile.toolchain is None:
                raise PathPlanError(
                    f"language {self.profile.id!r}: no toolchain manifest to plan"
                )
            return self.profile.toolchain.manifest.path, "harness", None, None

        if role in ("package_module", "source_module", "module"):
            base = self._primary_source_base(subst)
            file = subst.get("file") or subst.get("name")
            if not file:
                raise PathPlanError(
                    f"role {role!r} needs name=/file= (the module's relative path)"
                )
            rel = f"{base}/{file}" if base not in ("", ".") else str(file)
            return rel, "sut", self._primary_source_set_id(), None

        if role in ("test", "test_file", "unit_test"):
            test_set = layout.test_sets[0] if layout.test_sets else None
            base = self._subst_root(test_set.root) if test_set else "tests"
            file = subst.get("file") or subst.get("name")
            if not file:
                raise PathPlanError(
                    f"role {role!r} needs name=/file= (the test's relative path)"
                )
            rel = f"{base}/{file}" if base not in ("", ".") else str(file)
            return rel, "sut", None, (test_set.id if test_set else None)

        raise PathPlanError(
            f"language {self.profile.id!r}: no output_role or structural rule for "
            f"role {role!r} (declared roles: {sorted(self._output_roles)})"
        )

    def _primary_source_base(self, subst: Mapping[str, Any]) -> str:
        """The base dir for a generic source/package module.

        Prefers the declared ``package_root.path`` (Python ``src/<pkg>``, TS
        ``src``); else the first source-set root.
        """
        pkg = self.profile.layout.package_root
        if pkg.kind != "none" and pkg.path:
            return self._subst_root(pkg.path, subst)
        sets = self.profile.layout.source_sets
        if sets:
            return self._subst_root(sets[0].root, subst)
        raise PathPlanError(
            f"language {self.profile.id!r}: no package_root/source_set to plan a module"
        )

    def _primary_source_set_id(self) -> str | None:
        sets = self.profile.layout.source_sets
        return sets[0].id if sets else None

    def _subst_root(self, root: str, subst: Mapping[str, Any] | None = None) -> str:
        return _format_template(
            root, context=self.context, extra=dict(subst or {})
        ) if "{" in root else root

    # -- set attribution ---------------------------------------------------

    def _source_set_for(self, rel: str) -> str | None:
        norm = str(rel).strip().replace("\\", "/").strip("/")
        for ss in self.profile.layout.source_sets:
            root = self._safe_subst_root(ss.root)
            if root and root != "." and (norm == root or norm.startswith(root + "/")):
                return ss.id
        return None

    def _test_set_for(self, rel: str) -> str | None:
        norm = str(rel).strip().replace("\\", "/").strip("/")
        for ts in self.profile.layout.test_sets:
            root = self._safe_subst_root(ts.root)
            if root and root != "." and (norm == root or norm.startswith(root + "/")):
                return ts.id
        return None

    def _safe_subst_root(self, root: str) -> str | None:
        try:
            return self._subst_root(root)
        except PathPlanError:
            return None

    # -- finalization + invariants ----------------------------------------

    def _finalize(
        self,
        *,
        role: str,
        rel: str,
        owner: Owner,
        source_set: str | None,
        test_set: str | None,
    ) -> OutputPlan:
        path = _canonical(rel)
        posix = path.as_posix()
        for prefix in self._forbidden_prefixes:
            if posix == prefix.rstrip("/") or posix.startswith(prefix):
                raise PathPlanError(
                    f"language {self.profile.id!r}: planned path {posix!r} for role "
                    f"{role!r} hits forbidden prefix {prefix!r} (design §1.6)"
                )
        return OutputPlan(
            role=role,
            repo_relpath=path,
            owner=owner,
            language=self.profile.id,
            source_set=source_set,
            test_set=test_set,
        )
