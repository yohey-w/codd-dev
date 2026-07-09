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
    ProjectCapabilities,
    harness_owned_output_paths,
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


# ═══════════════════════════════════════════════════════════
# facade_output_paths — the ownership carve-out accessor (v3.18.0)
# ═══════════════════════════════════════════════════════════
#
# The package-FACADE file's TOPOLOGY is harness-owned (the scaffold creates it)
# but its CONTENT — the package's public-API re-exports — is SUT-authored. This
# accessor names ONLY the facade file(s) the harness creates but must NOT claim
# as an AI obligation. It is a STRICT NO-OP for every stack whose scaffolder does
# not emit a content-bearing package facade (the routing mirrors
# ``harness_owned_scaffold_paths`` — same scaffolder-realizer id — so it never
# widens beyond the one Python src-package stack), and it is a strict SUBSET of
# ``harness_owned_scaffold_paths`` (the scaffold still creates the file and the
# orphan-gate/write-fence still exempt it; only the obligation authority subtracts
# it).


class TestFacadeOutputPaths:
    def _resolve(self, language, name="demo-app"):
        return resolve_layout_profile(
            language=language, project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_python_src_package_declares_package_init_facade(self):
        profile = self._resolve("python")
        assert profile.facade_output_paths() == (f"{profile.package_root}/__init__.py",)
        # Concrete shape (package derived from the project name): src/<pkg>/__init__.py.
        assert profile.facade_output_paths() == ("src/demo_app/__init__.py",)

    @pytest.mark.parametrize("language", ["javascript", "typescript", "java", "cpp", "csharp"])
    def test_non_python_stacks_declare_no_facade(self, language):
        # STRICT NO-OP: no non-Python stack scaffolds a content-bearing package
        # facade, so the carve-out never fires (no path prefix, no ``language ==``).
        assert self._resolve(language).facade_output_paths() == ()

    def test_python_facade_is_subset_of_harness_owned_scaffold_paths(self):
        # The scaffold still CREATES the facade (fence/orphan authority keeps it);
        # only the obligation authority subtracts it. So the carve-out is a strict
        # subset of the harness-owned scaffold declaration.
        profile = self._resolve("python")
        facade = set(profile.facade_output_paths())
        assert facade  # non-empty for Python
        assert facade <= set(profile.harness_owned_scaffold_paths())


# ═══════════════════════════════════════════════════════════
# optional_surfaces accessor + excluded_surface_paths no-op  (R5, v3.19.0)
# ═══════════════════════════════════════════════════════════
#
# A stack MAY declare OPTIONAL deliverable surfaces (a surface the harness
# scaffolds by default but a project may exclude — Python's runnable console
# ``__main__.py`` entry point). ``optional_surfaces`` is profile DATA that
# defaults to ``()`` so every stack declaring none is byte-identical BY
# CONSTRUCTION; only Python declares the runnable-entrypoint surface, and
# ``excluded_surface_paths()`` is a STRICT no-op (``()``) until a project actually
# excludes a declared surface.


class TestOptionalSurfaces:
    def _resolve(self, language, name="ExprCalc"):
        return resolve_layout_profile(
            language=language, project_name=name, source_dirs=["src/"], test_dirs=["tests/"]
        )

    def test_python_declares_the_runnable_entrypoint_surface(self):
        # Duck-typed on purpose: SurfaceSpec does not exist pre-impl, so we assert
        # on the (id + paths) shape at RUNTIME rather than importing the symbol.
        profile = resolve_layout_profile(
            language="python", project_name="todo-cli", source_dirs=["src/"], test_dirs=["tests/"]
        )
        surfaces = profile.optional_surfaces
        assert len(surfaces) == 1
        surface = surfaces[0]
        assert surface.id == "runnable-entrypoint"
        # The surface's path is the scaffold's console entry point: src/<pkg>/__main__.py.
        assert surface.paths == ("src/todo_cli/__main__.py",)

    @pytest.mark.parametrize("language", ["javascript", "typescript", "java", "cpp", "csharp"])
    def test_non_python_stacks_declare_cli_backing_runnable_entrypoint(self, language):
        # GENERAL parity (was: "declare no optional surface" — the K3 leak). Every
        # buildable non-Python stack now declares the SAME CLI-backing surface (via
        # the shared constructor, no `language ==` branch), so a pure-library project
        # in ANY stack can downgrade its CLI e2e modality. ``paths`` is empty for
        # these path/glob-resolved stacks (nothing to TRUE-SUBTRACT), so the surface
        # is byte-neutral to scaffolding — it is purely the e2e-modality marker.
        profile = self._resolve(language)
        assert profile is not None
        surfaces = {s.id: s for s in profile.optional_surfaces}
        assert "runnable-entrypoint" in surfaces
        surface = surfaces["runnable-entrypoint"]
        assert surface.backs_e2e_modality == "cli"
        assert surface.paths == ()
        # No scaffold path to subtract even under exclusion (empty paths).
        assert profile.excluded_surface_paths() == ()

    def test_excluded_surface_paths_empty_without_exclusion(self):
        # Nothing excluded ⇒ the accessor is a strict no-op even though the python
        # profile DOES declare an optional surface.
        profile = self._resolve("python")
        assert profile.excluded_surface_paths() == ()


# ═══════════════════════════════════════════════════════════
# excluded deliverable surface — TRUE subtraction  (R1, v3.19.0)
# ═══════════════════════════════════════════════════════════
#
# Distinct from the v3.18 facade carve-out (CONTENT carve-out: the file is STILL
# created + STILL owned): an EXCLUDED surface is a TRUE SUBTRACTION — the scaffold
# does NOT create it, it is REMOVED from BOTH ownership authorities (so a stray one
# is an honest orphan), and the derive fence rejects any task declaring it. The
# excluded set is threaded onto the profile ONCE, in ``resolve_layout_profile``,
# from ``config["deliverable"]["excluded_surfaces"]`` — every consumer just reads
# ``profile.excluded_surface_paths()`` with no signature change.


class TestExcludedSurfaces:
    _PKG = "todo_cli"
    _MAIN = "src/todo_cli/__main__.py"
    _INIT = "src/todo_cli/__init__.py"

    def _config(self, *, excluded: bool) -> dict:
        config: dict = {
            "project": {"name": "todo-cli", "language": "python"},
            "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        }
        if excluded:
            # The plan-persisted exclusion set (id-based): resolve_layout_profile
            # reads it and threads the excluded ids onto the profile.
            config["deliverable"] = {"excluded_surfaces": ["runnable-entrypoint"]}
        return config

    def _profile(self, *, excluded: bool):
        config = self._config(excluded=excluded)
        profile = resolve_layout_profile(
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=config,
        )
        return profile, config

    def test_excluded_surface_paths_names_main(self):
        profile, _ = self._profile(excluded=True)
        assert profile.excluded_surface_paths() == (self._MAIN,)

    def test_excluded_main_dropped_from_both_authorities(self):
        profile, config = self._profile(excluded=True)
        # (1) profile-level owned-scaffold authority (orphan-gate / write-fence):
        # true subtraction — the excluded surface is owned by NO ONE.
        assert self._MAIN not in profile.harness_owned_scaffold_paths()
        # (2) module-level obligation authority (deriver + kind/completeness gate).
        assert self._MAIN not in harness_owned_output_paths(config)
        # The package facade __init__ is UNAFFECTED — still scaffolded topology.
        assert self._INIT in profile.harness_owned_scaffold_paths()

    def test_excluded_main_not_scaffolded_but_init_is(self, tmp_path):
        profile, _ = self._profile(excluded=True)
        result = scaffold_layout(tmp_path, profile)
        # The excluded surface is NOT written to disk...
        assert not (tmp_path / self._MAIN).exists()
        assert self._MAIN not in result.created
        # ...but the (non-excluded) package __init__ still is.
        assert (tmp_path / self._INIT).is_file()
        # AUTHORITY PARITY: the files the scaffold created are EXACTLY the profile's
        # declared owned-scaffold set (both drop __main__ under exclusion).
        assert set(result.created) == set(profile.harness_owned_scaffold_paths())

    def test_without_exclusion_main_present_and_created(self, tmp_path):
        # CONTROL (unchanged legacy path): no exclusion ⇒ __main__ owned by BOTH
        # authorities AND scaffolded — exclusion is strictly opt-in.
        profile, config = self._profile(excluded=False)
        assert self._MAIN in profile.harness_owned_scaffold_paths()
        assert self._MAIN in harness_owned_output_paths(config)
        result = scaffold_layout(tmp_path, profile)
        assert (tmp_path / self._MAIN).is_file()
        assert self._MAIN in result.created
        # Authority parity holds in the control too.
        assert set(result.created) == set(profile.harness_owned_scaffold_paths())


# ═══════════════════════════════════════════════════════════
# runnable-entrypoint / cli-backing flag — the GENERAL regression net (K3 leak)
# ═══════════════════════════════════════════════════════════
#
# The e2e cli→none downgrade (:func:`effective_e2e_modality`) is a DATA join on
# ``SurfaceSpec.backs_e2e_modality == "cli"``. That flag lived on ONLY Python's
# runnable-entrypoint surface, so a pure-library TS/Java/C++/C#/JS project never
# downgraded and generated a CLI e2e suite (invokeCli.ts / tempWorkspace.ts) for a
# CLI that does not exist → verify failed. This guard makes "a language profile
# forgot the CLI-backing surface / flag" a TEST FAILURE, not a silent leak. It is
# enumerated from the language REGISTRY, so a NEW language is auto-covered.


class TestRunnableEntrypointFlagGuard:
    def _buildable_layout_profiles(self):
        """Every (language_id, LayoutProfile) the resolver actually BUILDS.

        Registry-driven (not a hardcoded language list) so a new profile is
        auto-guarded. A language with no layout profile (Go → resolver returns
        None) is skipped — it declares no topology, so it has no surface to guard.
        """
        from codd.languages.registry import default_registry

        out = []
        for lang in default_registry.all_profiles():
            profile = resolve_layout_profile(
                language=lang.id,
                project_name="guard-probe",
                source_dirs=["src/"],
                test_dirs=["tests/"],
            )
            if profile is not None:
                out.append((lang.id, profile))
        return out

    def test_enumeration_is_non_trivial(self):
        # Sanity: the guards below are not vacuously green — python + typescript +
        # the greenfield-synthesis stacks all build a profile.
        ids = {lang_id for lang_id, _ in self._buildable_layout_profiles()}
        assert {"python", "typescript"} <= ids
        assert len(ids) >= 3

    def test_every_buildable_profile_declares_cli_backing_runnable_entrypoint(self):
        # COMPLETENESS (RED at HEAD): every buildable layout profile MUST declare a
        # runnable-entrypoint surface so a pure-library exclusion CAN downgrade its
        # CLI e2e. At HEAD only Python declares it; TS + synthesized stacks do not.
        for lang_id, profile in self._buildable_layout_profiles():
            surfaces = {s.id: s for s in profile.optional_surfaces}
            assert "runnable-entrypoint" in surfaces, (
                f"{lang_id}: layout profile declares no runnable-entrypoint surface — "
                "a pure-library project cannot downgrade its CLI e2e modality and will "
                "leak a CLI e2e suite it cannot satisfy"
            )

    def test_any_runnable_entrypoint_surface_backs_cli(self):
        # INVARIANT: wherever a 'runnable-entrypoint' surface appears, its DATA join
        # key is 'cli' — a stack cannot ship the surface with a wrong/missing flag.
        for lang_id, profile in self._buildable_layout_profiles():
            for surface in profile.optional_surfaces:
                if surface.id == "runnable-entrypoint":
                    assert surface.backs_e2e_modality == "cli", (
                        f"{lang_id}: runnable-entrypoint backs "
                        f"{surface.backs_e2e_modality!r}, not 'cli' — the cli→none "
                        "downgrade will never fire for this stack"
                    )


# ═══════════════════════════════════════════════════════════
# effective e2e modality — data-driven downgrade  (K3 envelope alignment)
# ═══════════════════════════════════════════════════════════
#
# ``e2e_modality`` is a STATIC per-type capability (default "cli"). When a project
# EXCLUDES the optional surface that BACKS that modality (Python's
# runnable-entrypoint surface backs "cli"), there is no CLI to subprocess, so
# prompting for CLI-subprocess e2e tests is a variance. ``effective_e2e_modality``
# downgrades the modality to "none" via a DATA join —
# ``SurfaceSpec.backs_e2e_modality == capabilities.e2e_modality`` AND the surface is
# excluded — with NO language/framework literal. Fail-safe: a None profile / any
# error / a surface that declares no backing / a non-matching modality all return
# the loaded modality unchanged (legacy).


class TestEffectiveE2EModality:
    def _profile(self, *, excluded: bool):
        config: dict = {
            "project": {"name": "todo-cli", "language": "python"},
            "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        }
        if excluded:
            config["deliverable"] = {"excluded_surfaces": ["runnable-entrypoint"]}
        return resolve_layout_profile(
            language="python",
            project_name="todo-cli",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=config,
        )

    def test_cli_downgraded_to_none_when_entrypoint_excluded(self):
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        profile = self._profile(excluded=True)
        assert effective_e2e_modality(caps, profile) == "none"

    def test_cli_kept_when_entrypoint_not_excluded(self):
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        profile = self._profile(excluded=False)
        assert effective_e2e_modality(caps, profile) == "cli"

    # ── NON-Python parity (the bug fix): the downgrade is per-profile DATA, so a
    # pure-library TypeScript project must downgrade EXACTLY like Python. RED at
    # HEAD (TS declared no runnable-entrypoint surface, so the exclusion could not
    # fire → a CLI e2e suite leaked for a library) → GREEN after the general fix.

    def _ts_profile(self, *, excluded: bool):
        config: dict = {
            "project": {"name": "exprcalc", "language": "typescript"},
            "scan": {"source_dirs": ["src/"], "test_dirs": ["tests/"]},
        }
        if excluded:
            config["deliverable"] = {"excluded_surfaces": ["runnable-entrypoint"]}
        return resolve_layout_profile(
            language="typescript",
            project_name="exprcalc",
            source_dirs=["src/"],
            test_dirs=["tests/"],
            config=config,
        )

    def test_typescript_cli_downgraded_to_none_when_entrypoint_excluded(self):
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        assert effective_e2e_modality(caps, self._ts_profile(excluded=True)) == "none"

    def test_typescript_cli_kept_when_entrypoint_not_excluded(self):
        # CONTROL (anti-false-green): a TS project that keeps the CLI stays "cli".
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        assert effective_e2e_modality(caps, self._ts_profile(excluded=False)) == "cli"

    def test_non_cli_modality_unchanged_even_if_entrypoint_excluded(self):
        # The join is ``backs_e2e_modality == e2e_modality``; a browser project
        # never matches the cli-backing surface, so exclusion never downgrades it.
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="browser")
        profile = self._profile(excluded=True)
        assert effective_e2e_modality(caps, profile) == "browser"

    def test_none_profile_returns_modality_unchanged(self):
        from codd.project_types import effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        assert effective_e2e_modality(caps, None) == "cli"

    def test_surface_without_backing_declaration_never_downgrades(self):
        # A surface that declares NO backing (backs_e2e_modality is None) must not
        # match any modality (None != "cli"), even when excluded — anti-false-green.
        from codd.project_types import LayoutProfile, SurfaceSpec, effective_e2e_modality

        caps = ProjectCapabilities(e2e_modality="cli")
        surface = SurfaceSpec(id="doc-site", description="docs", paths=("docs/",))
        profile = LayoutProfile(
            language="python",
            package_name="p",
            source_root="src",
            package_root="src/p",
            test_root="tests",
            optional_surfaces=(surface,),
            excluded_surface_ids=frozenset({"doc-site"}),
        )
        assert effective_e2e_modality(caps, profile) == "cli"
