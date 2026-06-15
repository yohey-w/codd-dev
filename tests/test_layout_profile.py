"""Tests for the harness-owned layout profile + deterministic scaffold (A-core).

The greenfield autopilot must OWN the repository topology and module resolution
so cross-vendor builds produce coherent source + tests. These tests cover the
:class:`~codd.project_types.LayoutProfile` registry and
:func:`~codd.project_types.scaffold_layout`:

* the Python profile derives package_name from the project name and roots from
  ``scan.*_dirs`` (no hardcoded literals);
* the scaffold creates the topology IDEMPOTENTLY and does NOT clobber existing
  valid files (so a coherent Claude layout / a --resume is a no-op);
* the emitted pyproject runs tests against the real package (NO pythonpath ".").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd.project_types import (
    LayoutProfile,
    normalize_package_name,
    resolve_layout_profile,
    scaffold_layout,
    supported_layout_profile_languages,
)


# ═══════════════════════════════════════════════════════════
# package-name normalization (deterministic, valid identifier)
# ═══════════════════════════════════════════════════════════


class TestNormalizePackageName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("todo-cli", "todo_cli"),
            ("Todo CLI", "todo_cli"),
            ("my.cool.app", "my_cool_app"),
            ("2048-game", "_2048_game"),  # cannot start with a digit
            ("  spaced  name ", "spaced_name"),
            ("already_ok", "already_ok"),
            ("--weird--", "weird"),
        ],
    )
    def test_derives_valid_identifier(self, raw, expected):
        result = normalize_package_name(raw)
        assert result == expected
        assert result.isidentifier()

    def test_empty_falls_back(self):
        assert normalize_package_name("") == "app"
        assert normalize_package_name(None) == "app"
        assert normalize_package_name("---") == "app"


# ═══════════════════════════════════════════════════════════
# profile resolution (registry, derived paths, no literals)
# ═══════════════════════════════════════════════════════════


class TestResolveLayoutProfile:
    def test_python_profile_resolves_with_derived_paths(self):
        profile = resolve_layout_profile(
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
        )
        assert isinstance(profile, LayoutProfile)
        assert profile.language == "python"
        assert profile.package_name == "todo_cli"
        assert profile.source_root == "src"
        assert profile.package_root == "src/todo_cli"
        assert profile.test_root == "tests"
        assert profile.runner == "pytest"
        assert profile.install_mode == "editable"
        assert profile.test_import_policy == "package_absolute"

    def test_paths_derive_from_scan_dirs_not_literals(self):
        profile = resolve_layout_profile(
            language="python",
            project_name="my-app",
            source_dirs=["app/"],
            test_dirs=["spec/"],
        )
        assert profile.source_root == "app"
        assert profile.package_root == "app/my_app"
        assert profile.test_root == "spec"

    def test_defaults_when_scan_dirs_absent(self):
        profile = resolve_layout_profile(
            language="python", project_name="demo", source_dirs=None, test_dirs=None
        )
        assert profile.source_root == "src"
        assert profile.test_root == "tests"
        assert profile.package_root == "src/demo"

    def test_unknown_language_has_no_profile(self):
        assert resolve_layout_profile(language="rust", project_name="x") is None
        assert resolve_layout_profile(language=None, project_name="x") is None

    def test_python_is_registered(self):
        assert "python" in supported_layout_profile_languages()


# ═══════════════════════════════════════════════════════════
# deterministic scaffold (idempotent, non-clobbering)
# ═══════════════════════════════════════════════════════════


class TestScaffoldLayout:
    def _profile(self, name="todo-cli"):
        return resolve_layout_profile(
            language="python", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_creates_full_topology(self, tmp_path):
        profile = self._profile()
        result = scaffold_layout(tmp_path, profile)

        assert (tmp_path / "src" / "todo_cli" / "__init__.py").is_file()
        assert (tmp_path / "src" / "todo_cli" / "__main__.py").is_file()
        assert (tmp_path / "tests" / "__init__.py").is_file()
        assert (tmp_path / "pyproject.toml").is_file()
        # the pyproject runs tests against the real package — NO "." hole.
        text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert 'pythonpath = ["src"]' in text
        assert '"."' not in text
        assert "--import-mode=importlib" in text
        assert "src/todo_cli/__init__.py" in result.created

    def test_idempotent_second_call_creates_nothing(self, tmp_path):
        profile = self._profile()
        scaffold_layout(tmp_path, profile)
        snapshot = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        result = scaffold_layout(tmp_path, profile)
        assert result.created == ()  # nothing new
        after = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        assert after == snapshot  # byte-for-byte unchanged

    def test_does_not_clobber_existing_package_files(self, tmp_path):
        # A coherent Claude layout already on disk must be left untouched.
        profile = self._profile()
        pkg = tmp_path / "src" / "todo_cli"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text('"""authored."""\n', encoding="utf-8")
        (pkg / "__main__.py").write_text("# authored main\n", encoding="utf-8")

        result = scaffold_layout(tmp_path, profile)

        assert (pkg / "__init__.py").read_text(encoding="utf-8") == '"""authored."""\n'
        assert (pkg / "__main__.py").read_text(encoding="utf-8") == "# authored main\n"
        assert "src/todo_cli/__init__.py" in result.skipped

    def test_does_not_clobber_existing_strong_pyproject(self, tmp_path):
        original = '[tool.pytest.ini_options]\naddopts = "-x"\ntestpaths = ["tests"]\n'
        (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8")
        profile = self._profile()

        scaffold_layout(tmp_path, profile)

        # an author/AI pytest config is authoritative — left byte-for-byte.
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original

    def test_unknown_stack_is_noop(self, tmp_path):
        profile = LayoutProfile(
            language="go",
            package_name="x",
            source_root="src",
            package_root="src/x",
            test_root="tests",
        )
        result = scaffold_layout(tmp_path, profile)
        assert result.created == ()
        assert list(tmp_path.iterdir()) == []


# ═══════════════════════════════════════════════════════════
# TypeScript (node) profile + scaffold (first-class TS support)
# ═══════════════════════════════════════════════════════════


import json as _json

from codd.project_types import (  # noqa: E402
    detect_node_package_manager,
    node_install_command,
)


class TestTypeScriptLayoutProfile:
    def test_typescript_profile_resolves_path_relative(self):
        profile = resolve_layout_profile(
            language="typescript",
            project_name="ts-converter",
            source_dirs=["src/"],
            test_dirs=["tests/"],
        )
        assert profile is not None
        assert profile.language == "typescript"
        # path-relative: package_root == source_root (no named-package subdir).
        assert profile.source_root == "src"
        assert profile.package_root == "src"
        assert profile.test_root == "tests"
        assert profile.runner == "vitest"
        assert profile.install_mode == "node"
        assert profile.test_import_policy == "relative"

    def test_node_alias_resolves_same_profile(self):
        profile = resolve_layout_profile(language="node", project_name="x")
        assert profile is not None
        assert profile.language == "typescript"

    def test_typescript_and_node_registered(self):
        langs = supported_layout_profile_languages()
        assert "typescript" in langs
        assert "node" in langs


class TestTypeScriptScaffold:
    def _profile(self, name="ts-converter"):
        return resolve_layout_profile(
            language="typescript", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_creates_tsconfig_and_package_json(self, tmp_path):
        result = scaffold_layout(tmp_path, self._profile())
        assert (tmp_path / "tsconfig.json").is_file()
        assert (tmp_path / "package.json").is_file()
        assert "tsconfig.json" in result.created
        assert "package.json" in result.created

        tsconfig = _json.loads((tmp_path / "tsconfig.json").read_text(encoding="utf-8"))
        opts = tsconfig["compilerOptions"]
        # strict + NodeNext = a coherent module contract so imports resolve.
        assert opts["strict"] is True
        assert opts["module"] == "NodeNext"
        assert opts["moduleResolution"] == "NodeNext"
        assert opts["noEmit"] is True

        pkg = _json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert pkg["scripts"]["test"] == "vitest run"
        assert "build" in pkg["scripts"]
        assert pkg["type"] == "module"  # coherent with NodeNext

        # vitest.config.ts owns COLLECTION: its ``test.include`` must cover the
        # routed ``.e2e.*`` convention (not just vitest's default .test/.spec),
        # else a routed ``.e2e.ts`` collects 0 tests. vitest's CLI has no
        # ``--include`` flag, so collection MUST be config-driven here.
        assert (tmp_path / "vitest.config.ts").is_file()
        assert "vitest.config.ts" in result.created
        vitest_cfg = (tmp_path / "vitest.config.ts").read_text(encoding="utf-8")
        assert "defineConfig" in vitest_cfg
        assert "include:" in vitest_cfg
        assert "**/*.e2e.{ts,tsx,cts,mts,js,jsx,cjs,mjs}" in vitest_cfg
        assert "**/*.{test,spec}.{ts,tsx,cts,mts,js,jsx,cjs,mjs}" in vitest_cfg

    def test_idempotent_second_call_creates_nothing(self, tmp_path):
        profile = self._profile()
        scaffold_layout(tmp_path, profile)
        snapshot = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        result = scaffold_layout(tmp_path, profile)
        assert result.created == ()
        after = {
            p.relative_to(tmp_path).as_posix(): p.read_text(encoding="utf-8")
            for p in tmp_path.rglob("*")
            if p.is_file()
        }
        assert after == snapshot  # byte-for-byte unchanged

    def test_does_not_clobber_model_authored_tsconfig(self, tmp_path):
        authored = '{\n  "compilerOptions": { "strict": false }\n}\n'
        (tmp_path / "tsconfig.json").write_text(authored, encoding="utf-8")
        result = scaffold_layout(tmp_path, self._profile())
        # model-generated tsconfig is authoritative — left byte-for-byte.
        assert (tmp_path / "tsconfig.json").read_text(encoding="utf-8") == authored
        assert "tsconfig.json" in result.skipped

    def test_merges_scripts_into_existing_package_json_without_clobber(self, tmp_path):
        authored = {
            "name": "authored",
            "version": "1.2.3",
            "dependencies": {"left-pad": "^1.0.0"},
            "scripts": {"build": "tsc"},
        }
        (tmp_path / "package.json").write_text(_json.dumps(authored, indent=2), encoding="utf-8")
        scaffold_layout(tmp_path, self._profile())
        pkg = _json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        # added the missing test script, preserved everything else.
        assert pkg["scripts"]["test"] == "vitest run"
        assert pkg["scripts"]["build"] == "tsc"  # author's build untouched
        assert pkg["version"] == "1.2.3"
        assert pkg["dependencies"] == {"left-pad": "^1.0.0"}

    def test_existing_real_test_script_left_untouched(self, tmp_path):
        authored = {"name": "x", "scripts": {"test": "jest --ci"}}
        (tmp_path / "package.json").write_text(_json.dumps(authored, indent=2), encoding="utf-8")
        scaffold_layout(tmp_path, self._profile())
        pkg = _json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))
        assert pkg["scripts"]["test"] == "jest --ci"  # author runner respected
        assert "build" not in pkg["scripts"]  # untouched file


class TestNodeInstallCommand:
    def test_npm_default_no_lockfile(self, tmp_path):
        assert detect_node_package_manager(tmp_path) == "npm"
        assert node_install_command(tmp_path) == "npm install"

    def test_npm_ci_with_lockfile(self, tmp_path):
        (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
        assert node_install_command(tmp_path) == "npm ci"

    def test_pnpm_detected(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        assert detect_node_package_manager(tmp_path) == "pnpm"
        assert node_install_command(tmp_path) == "pnpm install --frozen-lockfile"

    def test_yarn_detected(self, tmp_path):
        (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
        assert node_install_command(tmp_path) == "yarn install --frozen-lockfile"

    def test_bun_detected(self, tmp_path):
        (tmp_path / "bun.lockb").write_text("", encoding="utf-8")
        assert detect_node_package_manager(tmp_path) == "bun"
        assert node_install_command(tmp_path) == "bun install --frozen-lockfile"


# ═══════════════════════════════════════════════════════════
# harness-owned scaffold paths (orphan-gate / write-fence contract)
# ═══════════════════════════════════════════════════════════
#
# The orphan-artifact invariant is "every generated artifact is owned by exactly
# one task OR an explicit harness/profile contract". The scaffold files (TS
# vitest.config.ts / tsconfig.json / package.json) are the profile-contract
# branch: declared here so the gate never mis-flags them as unowned orphans and
# the scoped-rerun fence never reverts them. These tests pin the declaration AND
# bind it to what the scaffolder ACTUALLY writes (drift guard: a new scaffold
# file that is not also declared would re-open the false-positive).


class TestHarnessOwnedScaffoldPaths:
    def _ts_profile(self, name="ts-converter"):
        return resolve_layout_profile(
            language="typescript", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def _py_profile(self, name="todo-cli"):
        return resolve_layout_profile(
            language="python", project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_typescript_declares_scaffold_config(self):
        paths = self._ts_profile().harness_owned_scaffold_paths()
        # The exact files _scaffold_typescript creates as harness contract.
        for expected in ("tsconfig.json", "vitest.config.ts", "package.json"):
            assert expected in paths, f"{expected} must be a declared harness scaffold path"
        # Toolchain lock is part of the contract too (manifest↔lock coherence).
        assert "package-lock.json" in paths

    def test_python_declares_scaffold_topology(self):
        paths = self._py_profile().harness_owned_scaffold_paths()
        for expected in (
            "pyproject.toml",
            "src/todo_cli/__init__.py",
            "src/todo_cli/__main__.py",
            "tests/__init__.py",
        ):
            assert expected in paths, f"{expected} must be a declared harness scaffold path"

    def test_python_paths_derive_from_profile_not_literals(self):
        # Roots come from scan.*_dirs / the derived package — not hardcoded.
        profile = resolve_layout_profile(
            language="python", project_name="my-app", source_dirs=["lib/"], test_dirs=["spec/"]
        )
        paths = profile.harness_owned_scaffold_paths()
        assert "lib/my_app/__init__.py" in paths
        assert "spec/__init__.py" in paths

    def test_unknown_stack_declares_nothing_stackspecific(self):
        # A stack with no scaffolder declares no stack files (its toolchain, if any,
        # still contributes the manifest/lock — here there is none → empty).
        profile = LayoutProfile(
            language="go",
            package_name="x",
            source_root="src",
            package_root="src/x",
            test_root="tests",
        )
        assert profile.harness_owned_scaffold_paths() == ()

    def test_ts_declaration_covers_everything_the_scaffolder_creates(self, tmp_path):
        """DRIFT GUARD: every config file the TS scaffold writes must be declared.

        A scaffold file the scaffolder creates but the profile does NOT declare
        would reappear as an orphan false-positive (the codex12 vitest.config.ts
        bug). Bind the two so adding a scaffold file forces declaring it.
        """
        profile = self._ts_profile()
        result = scaffold_layout(tmp_path, profile)
        declared = set(profile.harness_owned_scaffold_paths())
        # Restrict to root-level config files the scaffolder owns (it also creates
        # package.json via the runner ensurer); generated package source/tests are
        # task-owned, not harness-owned, so the scaffolder does not emit them here.
        for created in result.created:
            assert created in declared, (
                f"scaffolder created {created!r} but the profile does not declare it as a "
                f"harness-owned scaffold path — it would be mis-flagged as an orphan"
            )

    def test_py_declaration_covers_everything_the_scaffolder_creates(self, tmp_path):
        """DRIFT GUARD (Python): scaffold-created files are all declared."""
        profile = self._py_profile()
        result = scaffold_layout(tmp_path, profile)
        declared = set(profile.harness_owned_scaffold_paths())
        for created in result.created:
            assert created in declared, (
                f"scaffolder created {created!r} but the profile does not declare it"
            )
