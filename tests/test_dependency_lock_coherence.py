"""Tests for the implement-end manifest↔lock coherence finalization.

Covers the design (/tmp/gpt_result_dep.txt, verdict (b)+(a)+(c)):
  * (b) harness-owned toolchain dep VERSIONS are reconciled to the profile in the
    SUT's package.json (app/domain deps untouched);
  * (a) the lock is refreshed (``npm install --package-lock-only``) at implement-
    end so a subsequent frozen ``npm ci`` matches — proven with a REAL ``npm ci``;
  * (c) verify keeps using ``npm ci`` (frozen), never ``npm install``;
  * Python is a strict NO-OP (no toolchain profile → today's path unaffected);
  * the greenfield pipeline wires the finalization at implement-end, before the
    implement-oracle's frozen install.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codd.dependency_lock_coherence import (
    DependencyLockResult,
    LockFreshnessResult,
    _LOCK_STATE_RELPATH,
    _read_frozen_digest,
    _write_frozen_digest,
    compute_manifest_digest,
    ensure_lock_freshness_barrier,
    finalize_dependency_lock_coherence,
    reconcile_manifest_toolchain_deps,
    resolve_toolchain_profile,
)
from codd.project_types import (
    ToolchainDependency,
    ToolchainDependencyProfile,
    node_install_command,
    resolve_layout_profile,
)


def _ts_toolchain() -> ToolchainDependencyProfile:
    profile = resolve_layout_profile(language="typescript", project_name="x")
    assert profile is not None
    assert profile.toolchain_dependencies is not None
    return profile.toolchain_dependencies


# ════════════════════════════════════════════════════════════
# The profile (b): harness owns the toolchain dep versions
# ════════════════════════════════════════════════════════════


class TestToolchainProfile:
    def test_typescript_profile_declares_toolchain_deps(self):
        tc = _ts_toolchain()
        names = {d.name for d in tc.deps}
        # The toolchain the TS scaffold's test/build scripts need.
        assert "vitest" in names
        assert "typescript" in names
        assert "@types/node" in names
        # All dev deps (the app does not ship its test runner / compiler).
        assert all(d.dev for d in tc.deps)

    def test_typescript_lock_refresh_is_package_lock_only(self):
        tc = _ts_toolchain()
        # Verdict (a): refresh ONLY the lock, no frozen check, no resolve-from-net.
        assert tc.lock_refresh_command == "npm install --package-lock-only"
        assert tc.manifest_filename == "package.json"
        assert "package-lock.json" in tc.lock_filenames

    def test_typescript_materialize_is_frozen(self):
        tc = _ts_toolchain()
        # The OPTIONAL same-process materialization stays FROZEN (npm ci), so even
        # node_modules honors the freshly-coherent lock.
        assert tc.materialize_command == "npm ci"

    def test_python_profile_has_no_toolchain_contract(self):
        profile = resolve_layout_profile(language="python", project_name="x")
        assert profile is not None
        # NO-OP: Python's path is unaffected (deferred extension point).
        assert profile.toolchain_dependencies is None

    def test_profile_to_dict_round_trips(self):
        profile = resolve_layout_profile(language="typescript", project_name="x")
        payload = profile.to_dict()
        assert "toolchain_dependencies" in payload
        assert payload["toolchain_dependencies"]["lock_refresh_command"] == (
            "npm install --package-lock-only"
        )


# ════════════════════════════════════════════════════════════
# Reconcile (b): SUT toolchain versions → profile; app deps untouched
# ════════════════════════════════════════════════════════════


class TestReconcileManifest:
    def test_old_toolchain_version_reconciled_to_profile(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "version": "0.0.0",
                    "type": "module",
                    "devDependencies": {"vitest": "^1.6.0", "typescript": "^4.0.0"},
                }
            ),
            encoding="utf-8",
        )
        changed = reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain())
        assert changed["vitest"] == ("^1.6.0", "^3.2.4")
        assert changed["typescript"] == ("^4.0.0", "^5.9.2")
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert pkg["devDependencies"]["vitest"] == "^3.2.4"
        assert pkg["devDependencies"]["typescript"] == "^5.9.2"
        # missing toolchain dep is ADDED at the profile version.
        assert pkg["devDependencies"]["@types/node"] == "^24.3.0"

    def test_app_and_domain_deps_left_untouched(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "version": "1.2.3",
                    "type": "module",
                    "dependencies": {"chalk": "^5.0.0", "commander": "^12.0.0"},
                    "devDependencies": {"vitest": "^1.6.0"},
                    "scripts": {"test": "vitest run", "build": "tsc -p tsconfig.json"},
                }
            ),
            encoding="utf-8",
        )
        reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain())
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        # app deps + scripts + name/version are the SUT's property — byte-for-byte.
        assert pkg["dependencies"] == {"chalk": "^5.0.0", "commander": "^12.0.0"}
        assert pkg["scripts"] == {"test": "vitest run", "build": "tsc -p tsconfig.json"}
        assert pkg["name"] == "todo"
        assert pkg["version"] == "1.2.3"

    def test_toolchain_dep_misplaced_in_dependencies_is_moved(self, tmp_path):
        # A SUT that put vitest in (prod) dependencies → reconcile moves it to
        # devDependencies so the lock never resolves two ranges for one name.
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "dependencies": {"vitest": "^1.6.0", "chalk": "^5.0.0"},
                }
            ),
            encoding="utf-8",
        )
        reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain())
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert "vitest" not in pkg["dependencies"]
        assert pkg["dependencies"] == {"chalk": "^5.0.0"}  # app dep stays
        assert pkg["devDependencies"]["vitest"] == "^3.2.4"

    def test_already_coherent_is_a_noop(self, tmp_path):
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "devDependencies": {
                        "vitest": "^3.2.4",
                        "typescript": "^5.9.2",
                        "@types/node": "^24.3.0",
                    },
                }
            ),
            encoding="utf-8",
        )
        before = (tmp_path / "package.json").read_text(encoding="utf-8")
        changed = reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain())
        assert changed == {}
        # already coherent → file is not rewritten.
        assert (tmp_path / "package.json").read_text(encoding="utf-8") == before

    def test_missing_manifest_is_noop(self, tmp_path):
        assert reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain()) == {}

    def test_unparseable_manifest_left_untouched(self, tmp_path):
        (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
        # A broken manifest is the verify parse/honesty gate's job — never guessed.
        assert reconcile_manifest_toolchain_deps(tmp_path, _ts_toolchain()) == {}
        assert (tmp_path / "package.json").read_text(encoding="utf-8") == "{not json"


# ════════════════════════════════════════════════════════════
# Resolution + Python NO-OP
# ════════════════════════════════════════════════════════════


class TestResolveAndNoop:
    def test_python_finalize_is_strict_noop(self, tmp_path):
        # Python: a pyproject + a (hypothetical) package.json must NOT be touched.
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        result = finalize_dependency_lock_coherence(
            tmp_path, language="python", project_name="x", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.applied is False
        assert result.ok is True
        assert resolve_toolchain_profile(tmp_path, language="python", project_name="x") is None

    def test_unknown_language_is_noop(self, tmp_path):
        result = finalize_dependency_lock_coherence(
            tmp_path, language="ruby", project_name="x", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ok is True

    def test_opt_out_disables_finalization(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        config = {"implement": {"dependency_lock_coherence": False}}
        assert (
            resolve_toolchain_profile(
                tmp_path, language="typescript", project_name="x", config=config
            )
            is None
        )

    def test_typescript_no_manifest_skips_without_failure(self, tmp_path):
        # TS stack but the SUT authored no package.json (unusual) → skip, never fail.
        result = finalize_dependency_lock_coherence(
            tmp_path, language="typescript", project_name="x", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ok is True


# ════════════════════════════════════════════════════════════
# (c) verify stays FROZEN — node_install_command never loosens to npm install
# ════════════════════════════════════════════════════════════


class TestVerifyStaysFrozen:
    def test_npm_ci_when_lock_present(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        # Verdict (c): with a lock, verify's install is the FROZEN npm ci — NOT
        # npm install. The whole fix is to make the lock match BEFORE this runs.
        assert node_install_command(tmp_path) == "npm ci"

    def test_npm_install_only_when_no_lock(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        # No lock yet → npm install (which creates one). After our finalization a
        # lock always exists, so verify takes the frozen npm ci path above.
        assert node_install_command(tmp_path) == "npm install"

    def test_verify_runner_install_preflight_uses_node_install_command(self):
        # Guard: verify's blocking preflight resolves its command through
        # node_install_command (the frozen-aware resolver) — it does not hardcode
        # ``npm install``. (Belt-and-suspenders against a future loosening.)
        import ast
        import inspect

        from codd.repair import verify_runner

        source = inspect.getsource(verify_runner.VerifyRunner._run_install_preflight)
        assert "node_install_command(self.project_root)" in source
        # Check the CODE (not the docstring, which legitimately mentions the
        # no-lock ``npm install`` fallback): no string literal in the function
        # body hardcodes the unfrozen ``npm install`` install command.
        func = ast.parse(inspect.getsource(verify_runner).encode()).body
        target = next(
            node
            for node in ast.walk(ast.Module(body=func, type_ignores=[]))
            if isinstance(node, ast.FunctionDef) and node.name == "_run_install_preflight"
        )
        body_strings = [
            node.value
            for node in ast.walk(target)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        body_strings = body_strings[1:]  # drop the docstring (first constant)
        assert not any("npm install" in s for s in body_strings)


# ════════════════════════════════════════════════════════════
# REAL npm integration (guarded) — the load-bearing anti-mock proof
# ════════════════════════════════════════════════════════════


def _npm() -> str | None:
    return shutil.which("npm")


@pytest.mark.skipif(_npm() is None, reason="npm not available")
class TestRealNpmCi:
    def _seed_bug(self, root: Path) -> None:
        """Reproduce the codex9/codex10 bug exactly.

        STEP 1 (scaffold/gate): package.json pins the LATEST vitest → the lock
        resolves vitest 3.x. STEP 2 (SUT rewrites package.json to an OLD vitest) →
        the lock now holds 3.x while the manifest demands ^1.6.0, the precise
        ``@vitest/expect@3.x does not satisfy ^1.6.0`` divergence ``npm ci`` rejects.
        """
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "version": "0.0.0",
                    "private": True,
                    "type": "module",
                    "devDependencies": {"vitest": "^3.2.4"},
                    "scripts": {"test": "vitest run"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["npm", "install", "--package-lock-only", "--no-audit", "--no-fund"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        # SUT downgrade.
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        pkg["devDependencies"]["vitest"] = "^1.6.0"
        (root / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")

    def _npm_ci(self, root: Path) -> int:
        return subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
        ).returncode

    def test_npm_ci_fails_before_and_passes_after_finalization(self, tmp_path):
        self._seed_bug(tmp_path)

        # PRECONDITION: the bug is real — frozen npm ci rejects the mismatch.
        assert self._npm_ci(tmp_path) != 0, (
            "expected npm ci to FAIL on the lock↔manifest mismatch (the bug)"
        )

        # FINALIZE: reconcile manifest → refresh lock → materialize (frozen).
        result = finalize_dependency_lock_coherence(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok, result.detail
        assert result.applied is True
        # (1) package.json's vitest is reconciled to the profile version.
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert pkg["devDependencies"]["vitest"] == "^3.2.4"

        # (2) the lock matches: a subsequent FROZEN npm ci now SUCCEEDS — proving
        # verify's frozen install would pass honestly (no loosening needed).
        assert self._npm_ci(tmp_path) == 0, "npm ci must pass after finalization"

    def test_lock_and_manifest_agree_after_finalization(self, tmp_path):
        self._seed_bug(tmp_path)
        finalize_dependency_lock_coherence(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        # The lock's resolved vitest must satisfy the reconciled manifest range.
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((tmp_path / "package-lock.json").read_text(encoding="utf-8"))
        manifest_range = pkg["devDependencies"]["vitest"]
        locked_root = lock["packages"][""]["devDependencies"]["vitest"]
        # The lockfile records the SAME range the manifest declares (agreement).
        assert locked_root == manifest_range
        # And node_modules/vitest resolved to a concrete 3.x satisfying ^3.2.4.
        resolved = lock["packages"]["node_modules/vitest"]["version"]
        assert resolved.startswith("3.")


# ════════════════════════════════════════════════════════════
# Pipeline wiring: finalization runs at implement-end, before the oracle install
# ════════════════════════════════════════════════════════════


class TestPipelineWiring:
    def test_stage_implement_calls_finalization_before_oracle(self, tmp_path, monkeypatch):
        """The finalization is invoked at implement-end, BEFORE the implement-oracle
        gate (so the oracle's own frozen ``npm ci`` benefits from the coherent
        lock) and before the VB coverage gate."""
        from codd.greenfield import pipeline as pipeline_module

        calls: list[str] = []

        pipe = pipeline_module.GreenfieldPipeline(
            project_name="todo", language="typescript"
        )

        # Stub the three implement-end gates to record their order.
        monkeypatch.setattr(
            pipe, "_finalize_dependency_lock_coherence", lambda _root: calls.append("finalize")
        )
        monkeypatch.setattr(
            pipe,
            "_enforce_implement_oracle_gate",
            lambda _root, _tasks, _options: calls.append("oracle"),
        )
        monkeypatch.setattr(
            pipeline_module,
            "_enforce_stage_coverage_gate",
            lambda *_a, **_k: calls.append("coverage"),
        )

        # One trivial task so the loop body runs; its runner is a no-op.
        task = pipeline_module.ImplementTaskRef(task_id="t1", design_node="d1")
        monkeypatch.setattr(pipe, "task_lister", lambda _root: [task])
        pipe.implement_task_runner = (
            lambda *_a, **_k: "ok"  # type: ignore[assignment]
        )

        record: dict[str, object] = {}
        pipe._stage_implement(tmp_path, record, {"coverage_gate": True})

        assert calls == ["finalize", "oracle", "coverage"], calls

    def test_finalize_helper_raises_stage_error_on_hard_failure(self, tmp_path, monkeypatch):
        """A hard finalization failure (e.g. lock refresh exit!=0) becomes a
        StageError so the autopilot fails honestly instead of proceeding to a
        guaranteed-broken verify."""
        from codd.greenfield import pipeline as pipeline_module

        pipe = pipeline_module.GreenfieldPipeline(
            project_name="todo", language="typescript"
        )
        (tmp_path / "package.json").write_text('{"name":"todo"}', encoding="utf-8")

        # Make resolution non-None, scaffolding a no-op, and the finalization fail.
        monkeypatch.setattr(pipe, "_ensure_test_runner", lambda _root: None)
        monkeypatch.setattr(
            pipeline_module.GreenfieldPipeline,
            "_layout_inputs",
            lambda _self, _root: ({}, "typescript", None, None),
        )
        import codd.dependency_lock_coherence as dlc

        monkeypatch.setattr(
            dlc,
            "resolve_toolchain_profile",
            lambda *_a, **_k: (object(), object()),
        )
        monkeypatch.setattr(
            dlc,
            "finalize_dependency_lock_coherence",
            lambda *_a, **_k: DependencyLockResult(ok=False, applied=True, detail="lock refresh failed (exit 1)"),
        )

        with pytest.raises(pipeline_module.StageError, match="manifest↔lock coherence"):
            pipe._finalize_dependency_lock_coherence(tmp_path)


# ════════════════════════════════════════════════════════════
# Lock-freshness barrier (codex15): the manifest DIGEST (dirty-mark by content)
# ════════════════════════════════════════════════════════════


def _no_pm_version_toolchain() -> ToolchainDependencyProfile:
    """A TS toolchain with the package-manager version probe disabled.

    The default profile folds ``npm --version`` into the digest, which (a) makes
    the fast digest tests depend on npm being installed and (b) makes them
    environment-sensitive. Disabling the probe isolates the FILE-content digest
    behavior under test; a separate test covers the version-probe component.
    """
    base = resolve_layout_profile(language="typescript", project_name="x").toolchain_dependencies
    return ToolchainDependencyProfile(
        deps=base.deps,
        manifest_filename=base.manifest_filename,
        lock_filenames=base.lock_filenames,
        lock_refresh_command=base.lock_refresh_command,
        materialize_command=base.materialize_command,
        frozen_install_command=base.frozen_install_command,
        completeness_refresh_command=base.completeness_refresh_command,
        workspace_manifest_globs=base.workspace_manifest_globs,
        config_filenames=base.config_filenames,
        package_manager_version_command=None,
    )


class TestManifestDigest:
    def _pkg(self, root: Path, vitest: str = "^3.2.4") -> None:
        (root / "package.json").write_text(
            json.dumps({"name": "todo", "devDependencies": {"vitest": vitest}}) + "\n",
            encoding="utf-8",
        )

    def test_digest_is_deterministic_for_same_content(self, tmp_path):
        self._pkg(tmp_path)
        tc = _no_pm_version_toolchain()
        a = compute_manifest_digest(tmp_path, tc)
        b = compute_manifest_digest(tmp_path, tc)
        assert a == b
        assert len(a) == 64  # hex sha-256

    def test_same_content_rewrite_does_not_change_digest(self, tmp_path):
        # A same-content rewrite (idempotent rerun) must NOT look "dirty" — this is
        # exactly why the design uses a CONTENT digest, not mtime.
        self._pkg(tmp_path)
        tc = _no_pm_version_toolchain()
        before = compute_manifest_digest(tmp_path, tc)
        self._pkg(tmp_path)  # rewrite identical bytes
        assert compute_manifest_digest(tmp_path, tc) == before

    def test_root_manifest_change_changes_digest(self, tmp_path):
        self._pkg(tmp_path, vitest="^3.2.4")
        tc = _no_pm_version_toolchain()
        before = compute_manifest_digest(tmp_path, tc)
        self._pkg(tmp_path, vitest="^1.6.0")  # the codex15-class re-write
        assert compute_manifest_digest(tmp_path, tc) != before

    def test_workspace_manifest_change_changes_digest(self, tmp_path):
        # A WORKSPACE manifest dep change must re-freeze even when the root manifest
        # is byte-for-byte unchanged (design §C: workspace manifests in the digest).
        self._pkg(tmp_path)
        ws = tmp_path / "packages" / "core"
        ws.mkdir(parents=True)
        (ws / "package.json").write_text(
            json.dumps({"name": "@todo/core", "dependencies": {"left-pad": "^1.0.0"}}) + "\n",
            encoding="utf-8",
        )
        tc = _no_pm_version_toolchain()
        before = compute_manifest_digest(tmp_path, tc)
        (ws / "package.json").write_text(
            json.dumps({"name": "@todo/core", "dependencies": {"left-pad": "^1.3.0"}}) + "\n",
            encoding="utf-8",
        )
        assert compute_manifest_digest(tmp_path, tc) != before

    def test_npmrc_change_changes_digest(self, tmp_path):
        # ``.npmrc`` flags change the resolved tree (legacy-peer-deps) → must change
        # the digest even though no manifest changed (design §C: config in digest).
        self._pkg(tmp_path)
        tc = _no_pm_version_toolchain()
        before = compute_manifest_digest(tmp_path, tc)
        (tmp_path / ".npmrc").write_text("legacy-peer-deps=true\n", encoding="utf-8")
        after = compute_manifest_digest(tmp_path, tc)
        assert after != before
        # And a change to its CONTENT changes it again.
        (tmp_path / ".npmrc").write_text("legacy-peer-deps=false\n", encoding="utf-8")
        assert compute_manifest_digest(tmp_path, tc) != after

    def test_harness_profile_change_changes_digest(self, tmp_path):
        # A harness-owned dependency PROFILE change (a version bump of a toolchain
        # dep) must re-freeze even with the SUT's files untouched (design §C:
        # harness-owned dependency profile in the digest).
        self._pkg(tmp_path)
        tc_old = _no_pm_version_toolchain()
        before = compute_manifest_digest(tmp_path, tc_old)
        tc_new = ToolchainDependencyProfile(
            deps=(ToolchainDependency(name="vitest", version="^4.0.0"),),  # bumped
            manifest_filename=tc_old.manifest_filename,
            lock_filenames=tc_old.lock_filenames,
            lock_refresh_command=tc_old.lock_refresh_command,
            materialize_command=tc_old.materialize_command,
            frozen_install_command=tc_old.frozen_install_command,
            completeness_refresh_command=tc_old.completeness_refresh_command,
            workspace_manifest_globs=tc_old.workspace_manifest_globs,
            config_filenames=tc_old.config_filenames,
            package_manager_version_command=None,
        )
        assert compute_manifest_digest(tmp_path, tc_new) != before

    def test_pm_version_component_folds_in(self, tmp_path, monkeypatch):
        # The package-manager version is part of the digest: a different reported
        # version → a different digest (a manager upgrade can change lock format).
        self._pkg(tmp_path)
        tc = resolve_layout_profile(
            language="typescript", project_name="x"
        ).toolchain_dependencies  # has package_manager_version_command set
        import codd.dependency_lock_coherence as dlc

        class _Completed:
            def __init__(self, out):
                self.returncode = 0
                self.stdout = out
                self.stderr = ""

        monkeypatch.setattr(dlc, "_run", lambda *_a, **_k: _Completed("10.8.2"))
        d1 = compute_manifest_digest(tmp_path, tc)
        monkeypatch.setattr(dlc, "_run", lambda *_a, **_k: _Completed("11.0.0"))
        d2 = compute_manifest_digest(tmp_path, tc)
        assert d1 != d2

    def test_state_roundtrip(self, tmp_path):
        assert _read_frozen_digest(tmp_path) is None  # never frozen
        _write_frozen_digest(tmp_path, "abc123")
        assert (tmp_path / _LOCK_STATE_RELPATH).is_file()
        assert _read_frozen_digest(tmp_path) == "abc123"

    def test_corrupt_state_reads_as_none(self, tmp_path):
        path = tmp_path / _LOCK_STATE_RELPATH
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        # A missing/garbled record ⇒ "not known fresh" ⇒ the barrier re-freezes (safe).
        assert _read_frozen_digest(tmp_path) is None


# ════════════════════════════════════════════════════════════
# Lock-freshness barrier — control flow (stubbed commands, no real npm)
# ════════════════════════════════════════════════════════════


class TestLockFreshnessBarrierFlow:
    def _pkg(self, root: Path, vitest: str = "^3.2.4") -> None:
        (root / "package.json").write_text(
            json.dumps({"name": "todo", "devDependencies": {"vitest": vitest}}) + "\n",
            encoding="utf-8",
        )

    def _stub_commands(self, monkeypatch, *, fail_frozen_until_fallback=False):
        """Record commands the barrier runs; no real npm. Returns the call list.

        When ``fail_frozen_until_fallback`` is set, the FROZEN install
        (``npm ci``) returns nonzero until the completeness fallback
        (``npm install``) has run, then succeeds — modeling the ufo/path-key
        transitive-omission recovery.
        """
        import codd.dependency_lock_coherence as dlc

        calls: list[str] = []
        state = {"fallback_done": False}

        class _Completed:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = "Missing from lock file: ufo, path-key" if rc else ""

        def _fake_run(command, _root, _timeout):
            calls.append(command)
            if command == "npm install":  # completeness fallback
                state["fallback_done"] = True
                return _Completed(0)
            if command == "npm ci":  # frozen install
                if fail_frozen_until_fallback and not state["fallback_done"]:
                    return _Completed(1)
                return _Completed(0)
            # lock refresh (--package-lock-only) and pm version probe
            return _Completed(0)

        monkeypatch.setattr(dlc, "_run", _fake_run)
        # Pin the digest so we control "changed" vs "unchanged" deterministically.
        return calls

    def test_unchanged_manifest_is_a_noop(self, tmp_path, monkeypatch):
        # Record the digest as already-frozen → the barrier must SKIP (no refresh,
        # no reconcile, no frozen install) — the "rerun が複数回続くと無駄" guard.
        self._pkg(tmp_path)
        calls = self._stub_commands(monkeypatch)
        import codd.dependency_lock_coherence as dlc

        digest = dlc.compute_manifest_digest(tmp_path, _no_pm_version_toolchain())
        # Freeze record uses the SAME profile the barrier resolves (TS default has a
        # pm-version probe; stub _run returns "" so the digest is stable here).
        frozen = dlc.compute_manifest_digest(
            tmp_path,
            resolve_layout_profile(language="typescript", project_name="todo").toolchain_dependencies,
        )
        _write_frozen_digest(tmp_path, frozen)

        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ran is False
        assert result.ok is True
        # No npm command ran at all (pure no-op besides the in-process digest probe).
        assert "npm install --package-lock-only" not in calls
        assert "npm ci" not in calls
        assert "npm install" not in calls

    def test_changed_manifest_refreshes_and_records(self, tmp_path, monkeypatch):
        # No prior record (or a different digest) → the barrier reconciles, refreshes
        # (--package-lock-only), validates with the frozen install, records digest.
        self._pkg(tmp_path, vitest="^1.6.0")  # SUT downgrade (the codex15 re-write)
        calls = self._stub_commands(monkeypatch)

        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok is True
        assert result.ran is True
        assert result.skipped is False
        assert result.used_fallback is False
        # Order (ignoring the digest's pm-version probe): refresh
        # (--package-lock-only) THEN the frozen install validates it. The
        # completeness fallback (full ``npm install``) was NOT needed.
        dep_calls = [c for c in calls if c != "npm --version"]
        assert dep_calls == ["npm install --package-lock-only", "npm ci"], dep_calls
        # vitest was reconciled to the profile version in the manifest.
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert pkg["devDependencies"]["vitest"] == "^3.2.4"
        # The new digest is recorded → an immediate re-run is now a no-op.
        assert _read_frozen_digest(tmp_path) is not None

    def test_completeness_fallback_runs_inside_barrier(self, tmp_path, monkeypatch):
        # --package-lock-only leaves the frozen install incoherent (transitive
        # omission) → the barrier runs the FULL ``npm install`` fallback, THEN the
        # frozen install passes. The fallback is INSIDE the barrier (not verify).
        self._pkg(tmp_path, vitest="^1.6.0")
        calls = self._stub_commands(monkeypatch, fail_frozen_until_fallback=True)

        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok is True
        assert result.ran is True
        assert result.used_fallback is True
        # Sequence of the DEPENDENCY commands (ignoring the digest's pm-version
        # probe): --package-lock-only → npm ci (fails) → npm install (fallback) →
        # npm ci (passes).
        dep_calls = [c for c in calls if c != "npm --version"]
        assert dep_calls == [
            "npm install --package-lock-only",
            "npm ci",
            "npm install",
            "npm ci",
        ], dep_calls

    def test_refresh_failure_is_hard_fail(self, tmp_path, monkeypatch):
        # A lock refresh that exits nonzero is an honest environment failure.
        self._pkg(tmp_path, vitest="^1.6.0")
        import codd.dependency_lock_coherence as dlc

        class _Completed:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = "boom" if rc else ""

        monkeypatch.setattr(
            dlc,
            "_run",
            lambda command, _r, _t: _Completed(1 if command == "npm install --package-lock-only" else 0),
        )
        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok is False
        assert result.ran is True
        assert "lock refresh failed" in result.detail

    def test_fallback_exhausted_is_hard_fail(self, tmp_path, monkeypatch):
        # Frozen install fails, fallback runs, frozen install STILL fails → hard fail
        # (not a silent green): the lock genuinely cannot reproduce the manifest.
        self._pkg(tmp_path, vitest="^1.6.0")
        import codd.dependency_lock_coherence as dlc

        class _Completed:
            def __init__(self, rc, err=""):
                self.returncode = rc
                self.stdout = ""
                self.stderr = err

        def _fake_run(command, _r, _t):
            if command == "npm ci":
                return _Completed(1, "Missing from lock file")
            return _Completed(0)  # refresh + fallback "succeed" but never fix it

        monkeypatch.setattr(dlc, "_run", _fake_run)
        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok is False
        assert result.used_fallback is True
        assert "STILL failed" in result.detail

    def test_python_is_strict_noop(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        result = ensure_lock_freshness_barrier(
            tmp_path, language="python", project_name="x", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ran is False
        assert result.ok is True

    def test_no_manifest_is_noop(self, tmp_path):
        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="x", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ok is True


# ════════════════════════════════════════════════════════════
# Lock-freshness barrier — REAL npm (the codex15 sequencing-gap proof)
# ════════════════════════════════════════════════════════════


@pytest.mark.skipif(_npm() is None, reason="npm not available")
class TestRealNpmLockFreshnessBarrier:
    def _npm_ci(self, root: Path) -> int:
        return subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
        ).returncode

    def test_codex15_rerun_modified_manifest_passes_after_barrier(self, tmp_path):
        """codex15 end-to-end: implement-end freeze, THEN a rerun re-writes the
        manifest, leaving the lock stale; the verify-direct barrier re-freezes and
        the frozen ``npm ci`` passes.

        1. IMPLEMENT-END: finalize (reconcile + refresh + materialize) → lock fresh,
           digest recorded.
        2. POST-IMPLEMENT RERUN: a VB/oracle rerun re-writes package.json (adds an
           app dep + downgrades the toolchain dep), making the recorded lock STALE.
        3. PRECONDITION: a frozen ``npm ci`` now FAILS (the codex15 symptom).
        4. VERIFY-DIRECT BARRIER: re-freezes the lock for the new manifest set.
        5. POSTCONDITION: ``npm ci`` PASSES — verify's frozen install is honest.
        """
        # (1) implement-end finalization.
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "name": "todo",
                    "version": "0.0.0",
                    "private": True,
                    "type": "module",
                    "devDependencies": {"vitest": "^3.2.4"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        fin = finalize_dependency_lock_coherence(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert fin.ok, fin.detail
        first_digest = _read_frozen_digest(tmp_path)
        assert first_digest is not None  # implement-end recorded the freeze
        assert self._npm_ci(tmp_path) == 0  # lock is fresh right after implement-end

        # (2) a later rerun re-writes package.json: add a REAL app dep with a
        # transitive subtree + downgrade the toolchain dep. The recorded lock no
        # longer reproduces this manifest.
        pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        pkg["dependencies"] = {"execa": "^9.0.0"}  # pulls a transitive subtree
        pkg["devDependencies"]["vitest"] = "^1.6.0"  # toolchain downgrade
        (tmp_path / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")

        # (3) PRECONDITION: frozen npm ci fails on the now-stale lock.
        assert self._npm_ci(tmp_path) != 0, "expected stale-lock npm ci to FAIL (the codex15 bug)"

        # (4) the verify-direct barrier sees the digest changed → re-freezes.
        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.ok, result.detail
        assert result.ran is True
        assert result.skipped is False
        assert _read_frozen_digest(tmp_path) != first_digest  # re-froze for new set

        # (5) POSTCONDITION: the frozen npm ci now PASSES honestly.
        assert self._npm_ci(tmp_path) == 0, "npm ci must pass after the lock-freshness barrier"
        # The toolchain dep was reconciled; the app dep was preserved.
        final_pkg = json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert final_pkg["devDependencies"]["vitest"] == "^3.2.4"
        assert final_pkg["dependencies"] == {"execa": "^9.0.0"}

    def test_unchanged_manifest_barrier_skips_no_refresh(self, tmp_path):
        """After implement-end freeze, with NO further manifest change, the barrier
        is a NO-OP — it does not refresh again (avoids the wasted-rerun cost) and
        the frozen install still passes."""
        (tmp_path / "package.json").write_text(
            json.dumps(
                {"name": "todo", "private": True, "type": "module", "devDependencies": {"vitest": "^3.2.4"}}
            )
            + "\n",
            encoding="utf-8",
        )
        finalize_dependency_lock_coherence(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        digest_after_finalize = _read_frozen_digest(tmp_path)
        lock_mtime = (tmp_path / "package-lock.json").stat().st_mtime_ns

        result = ensure_lock_freshness_barrier(
            tmp_path, language="typescript", project_name="todo", echo=lambda _m: None
        )
        assert result.skipped is True
        assert result.ran is False
        # The barrier did NOT rewrite the lock (no refresh happened).
        assert (tmp_path / "package-lock.json").stat().st_mtime_ns == lock_mtime
        assert _read_frozen_digest(tmp_path) == digest_after_finalize
        assert self._npm_ci(tmp_path) == 0


# ════════════════════════════════════════════════════════════
# Pipeline wiring: barrier runs verify-direct, BEFORE the verify runner
# ════════════════════════════════════════════════════════════


class TestBarrierPipelineWiring:
    def test_stage_verify_runs_barrier_before_verify_runner(self, tmp_path, monkeypatch):
        """The lock-freshness barrier is invoked in _stage_verify BEFORE the verify
        runner (whose blocking ``npm ci`` preflight is a frozen install) and before
        the coverage-execution campaign (also a frozen install)."""
        from codd.greenfield import pipeline as pipeline_module

        calls: list[str] = []
        pipe = pipeline_module.GreenfieldPipeline(project_name="todo", language="typescript")

        monkeypatch.setattr(pipe, "_ensure_test_runner", lambda _root: None)
        monkeypatch.setattr(pipe, "_enforce_import_coherence", lambda _root: None)
        monkeypatch.setattr(pipe, "_ensure_lock_freshness", lambda _root: calls.append("barrier"))
        monkeypatch.setattr(
            pipe, "_enforce_coverage_execution_coherence", lambda _root, _opts: (calls.append("campaign") or "")
        )
        pipe.verify_runner = lambda *_a, **_k: (calls.append("verify_runner") or "ok")  # type: ignore[assignment]

        record: dict[str, object] = {}
        pipe._stage_verify(tmp_path, record, {"max_repair_attempts": 1})

        assert calls == ["barrier", "verify_runner", "campaign"], calls

    def test_barrier_helper_raises_stage_error_on_hard_failure(self, tmp_path, monkeypatch):
        """A hard barrier failure becomes a StageError so verify fails honestly
        instead of proceeding to a guaranteed-broken frozen install."""
        from codd.greenfield import pipeline as pipeline_module

        pipe = pipeline_module.GreenfieldPipeline(project_name="todo", language="typescript")
        (tmp_path / "package.json").write_text('{"name":"todo"}', encoding="utf-8")

        monkeypatch.setattr(pipe, "_ensure_test_runner", lambda _root: None)
        monkeypatch.setattr(
            pipeline_module.GreenfieldPipeline,
            "_layout_inputs",
            lambda _self, _root: ({}, "typescript", None, None),
        )
        import codd.dependency_lock_coherence as dlc

        monkeypatch.setattr(dlc, "resolve_toolchain_profile", lambda *_a, **_k: (object(), object()))
        monkeypatch.setattr(
            dlc,
            "ensure_lock_freshness_barrier",
            lambda *_a, **_k: LockFreshnessResult(ok=False, ran=True, detail="lock cannot reproduce manifest"),
        )

        with pytest.raises(pipeline_module.StageError, match="lock-freshness barrier"):
            pipe._ensure_lock_freshness(tmp_path)
