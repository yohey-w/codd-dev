"""Tests for codd.discovery — unified file discovery / exclusion.

Includes the drift-prevention gate: if any consumer re-declares its own
ignore set or extension set and drifts from codd.discovery again, these
tests fail.
"""

from pathlib import Path

import pytest

from codd import discovery
from codd.discovery import (
    DEFAULT_IGNORED_DIRS,
    SOURCE_EXTENSIONS,
    default_exclude_patterns,
    iter_source_files,
    matches_exclude_pattern,
    scan_exclude_patterns,
    should_skip_path,
)


# ═══════════════════════════════════════════════════════════
# Drift prevention — all consumers resolve to the SAME sets
# ═══════════════════════════════════════════════════════════


class TestDriftPrevention:
    def test_parsing_ignored_dirs_is_the_shared_set(self):
        from codd import parsing

        assert parsing._IGNORED_DIR_NAMES is DEFAULT_IGNORED_DIRS

    def test_extract_ai_skip_dirs_is_the_shared_set(self):
        from codd import extract_ai

        assert extract_ai.SKIP_DIRS is DEFAULT_IGNORED_DIRS

    def test_extract_ai_source_extensions_is_the_shared_set(self):
        from codd import extract_ai

        assert extract_ai.SOURCE_EXTENSIONS is SOURCE_EXTENSIONS

    def test_extractor_language_extensions_subset_of_shared_set(self):
        """Every extension the deterministic extractor can scan must be in
        the shared language coverage — otherwise deterministic and AI
        extraction silently diverge on module inventories."""
        from codd.extractor import _language_extensions

        for language in ("python", "typescript", "javascript", "java", "go"):
            assert _language_extensions(language) <= set(SOURCE_EXTENSIONS), language

    def test_union_repaired_the_historical_asymmetry(self):
        # parsing had these, extract_ai did not:
        assert {".terraform", ".tox", "site-packages"} <= set(DEFAULT_IGNORED_DIRS)
        # extract_ai had these, parsing did not:
        assert {".next", "coverage", ".cache", "env", "tmp", ".turbo"} <= set(
            DEFAULT_IGNORED_DIRS
        )

    def test_language_coverage_includes_audit_named_languages(self):
        # extract_ai previously lacked these despite the rest of the
        # codebase declaring them (implementer / DAG suffix maps).
        assert {".rs", ".swift", ".kt", ".dart", ".cs", ".scala"} <= set(
            SOURCE_EXTENSIONS
        )

    def test_default_exclude_patterns_cover_top_level_and_nested(self):
        patterns = default_exclude_patterns()
        for name in DEFAULT_IGNORED_DIRS:
            assert f"{name}/**" in patterns
            assert f"**/{name}/**" in patterns
        assert "*.egg-info/**" in patterns
        assert "**/*.egg-info/**" in patterns

    def test_extractor_defaults_skip_unified_dirs(self, tmp_path):
        """Functional gate: extract_facts must not inventory files inside
        the unified ignore set (e.g. .next/ — previously scanned because
        the extractor's own list lacked it)."""
        from codd.extractor import extract_facts

        src = tmp_path / "src"
        src.mkdir()
        (src / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")
        for ignored in (".next", "node_modules", "site-packages", "env"):
            bad_dir = tmp_path / ignored / "pkg"
            bad_dir.mkdir(parents=True)
            (bad_dir / "junk.py").write_text("x = 1\n", encoding="utf-8")

        facts = extract_facts(tmp_path, language="python", source_dirs=["."])

        all_files = [f for mod in facts.modules.values() for f in mod.files]
        assert any("app.py" in f for f in all_files)
        for ignored in (".next", "node_modules", "site-packages", "env"):
            assert not any(f.startswith(f"{ignored}/") for f in all_files), ignored


# ═══════════════════════════════════════════════════════════
# scan_exclude_patterns — the one safe config accessor
# ═══════════════════════════════════════════════════════════


class TestScanExcludePatterns:
    def test_missing_scan_section_returns_empty(self):
        # The old config["scan"].get("exclude", []) pattern crashed here.
        assert scan_exclude_patterns({"project": {"name": "x"}}) == []

    def test_none_config_returns_empty(self):
        assert scan_exclude_patterns(None) == []

    def test_scan_section_none_returns_empty(self):
        assert scan_exclude_patterns({"scan": None}) == []

    def test_exclude_none_returns_empty(self):
        assert scan_exclude_patterns({"scan": {"exclude": None}}) == []

    def test_returns_configured_patterns(self):
        config = {"scan": {"exclude": ["**/generated/**", "*.gen.py"]}}
        assert scan_exclude_patterns(config) == ["**/generated/**", "*.gen.py"]

    def test_single_string_is_wrapped(self):
        assert scan_exclude_patterns({"scan": {"exclude": "**/gen/**"}}) == ["**/gen/**"]

    def test_blank_and_non_string_entries_dropped(self):
        config = {"scan": {"exclude": ["ok/**", "", "   ", 42, None]}}
        assert scan_exclude_patterns(config) == ["ok/**"]


# ═══════════════════════════════════════════════════════════
# should_skip_path
# ═══════════════════════════════════════════════════════════


class TestShouldSkipPath:
    def test_skips_paths_inside_ignored_dirs(self, tmp_path):
        path = tmp_path / "node_modules" / "pkg" / "index.js"
        assert should_skip_path(path, tmp_path) is True

    def test_keeps_regular_source_paths(self, tmp_path):
        path = tmp_path / "src" / "app.py"
        assert should_skip_path(path, tmp_path) is False

    def test_filename_matching_ignored_dir_name_is_kept(self, tmp_path):
        # Only DIRECTORY components are matched against the ignore set.
        path = tmp_path / "src" / "build"
        assert should_skip_path(path, tmp_path) is False

    def test_exclude_pattern_full_path(self, tmp_path):
        path = tmp_path / "src" / "legacy" / "old.py"
        assert should_skip_path(path, tmp_path, exclude_patterns=["src/legacy/*"]) is True

    def test_exclude_pattern_basename(self, tmp_path):
        path = tmp_path / "src" / "models.gen.py"
        assert should_skip_path(path, tmp_path, exclude_patterns=["*.gen.py"]) is True

    def test_custom_ignored_dirs_override(self, tmp_path):
        path = tmp_path / "node_modules" / "x.js"
        assert should_skip_path(path, tmp_path, ignored_dirs={"other"}) is False


class TestMatchesExcludePattern:
    def test_plain_pattern_matches_basename(self):
        assert matches_exclude_pattern("deep/nested/file.gen.py", "*.gen.py") is True

    def test_path_pattern_matches_full_relative_path(self):
        assert matches_exclude_pattern("src/legacy/x.py", "src/legacy/*") is True
        assert matches_exclude_pattern("other/legacy/x.py", "src/legacy/*") is False


# ═══════════════════════════════════════════════════════════
# iter_source_files — the shared walker
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "view.tsx").write_text("export {}\n", encoding="utf-8")
    (tmp_path / "src" / "notes.txt").write_text("not source\n", encoding="utf-8")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "core.rs").write_text("fn main() {}\n", encoding="utf-8")
    for ignored in ("node_modules", ".next", "env", "vendor"):
        bad = tmp_path / ignored
        bad.mkdir()
        (bad / "skipped.py").write_text("x = 1\n", encoding="utf-8")
    hidden = tmp_path / ".github"
    hidden.mkdir()
    (hidden / "script.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


class TestIterSourceFiles:
    def test_skips_ignored_and_hidden_dirs(self, project):
        found = {p.relative_to(project).as_posix() for p in iter_source_files(project)}
        assert found == {"src/app.py", "src/view.tsx", "lib/core.rs"}

    def test_source_dirs_limits_the_walk(self, project):
        found = {
            p.relative_to(project).as_posix()
            for p in iter_source_files(project, source_dirs=["src"])
        }
        assert found == {"src/app.py", "src/view.tsx"}

    def test_extra_excludes_apply_fnmatch(self, project):
        found = {
            p.relative_to(project).as_posix()
            for p in iter_source_files(project, extra_excludes=["src/*.tsx"])
        }
        assert found == {"src/app.py", "lib/core.rs"}

    def test_explicit_extensions_filter(self, project):
        found = {
            p.relative_to(project).as_posix()
            for p in iter_source_files(project, extensions={".rs"})
        }
        assert found == {"lib/core.rs"}

    def test_empty_extensions_means_all_files(self, project):
        found = {
            p.relative_to(project).as_posix()
            for p in iter_source_files(project, source_dirs=["src"], extensions=())
        }
        assert "src/notes.txt" in found

    def test_results_are_deterministic_and_unique(self, project):
        first = list(iter_source_files(project))
        second = list(iter_source_files(project))
        assert first == second
        assert len(first) == len(set(first))

    def test_overlapping_source_dirs_deduplicate(self, project):
        found = [
            p.relative_to(project).as_posix()
            for p in iter_source_files(project, source_dirs=[".", "src"])
        ]
        assert found.count("src/app.py") == 1


# ═══════════════════════════════════════════════════════════
# Path-escape jail — source_dirs (scan.source_dirs, user-controllable)
# must never walk/read OUTSIDE the project root (RC-2: the shared walker
# feeds env_refs/schema_refs/wiring/contracts/traceability transitively).
# ═══════════════════════════════════════════════════════════


@pytest.fixture
def escape_project(tmp_path):
    """A project dir with a SIBLING ``outside`` dir holding a stray source file."""
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    return proj, outside


class TestIterSourceFilesPathEscapeJail:
    def test_parent_traversal_source_dir_is_not_walked(self, escape_project):
        # ``../outside`` survives scan.*_dirs normalization (only slashes are
        # stripped); the walker must NOT escape the project to read it.
        proj, outside = escape_project
        found = [p.resolve() for p in iter_source_files(proj, source_dirs=["../outside"])]
        assert (outside / "secret.py").resolve() not in found
        assert all(str(p).startswith(str(proj.resolve())) for p in found), found

    def test_absolute_out_of_root_source_dir_is_not_walked(self, escape_project):
        # An absolute dir pointing OUTSIDE the root must be dropped, not read.
        proj, outside = escape_project
        found = [p.resolve() for p in iter_source_files(proj, source_dirs=[str(outside)])]
        assert (outside / "secret.py").resolve() not in found
        assert all(str(p).startswith(str(proj.resolve())) for p in found), found

    def test_symlinked_source_dir_escaping_root_is_not_walked(self, escape_project):
        # An IN-ROOT source dir that is a symlink whose target escapes the tree
        # must not smuggle an off-root file into the walk.
        proj, outside = escape_project
        link = proj / "linked_src"
        link.symlink_to(outside, target_is_directory=True)
        found = [p.resolve() for p in iter_source_files(proj, source_dirs=["linked_src"])]
        assert (outside / "secret.py").resolve() not in found

    def test_symlinked_file_inside_walk_escaping_root_is_dropped(self, escape_project):
        # An in-root walked tree may contain a symlink FILE whose target escapes;
        # the re-confinement of walk results must drop it.
        proj, outside = escape_project
        (proj / "src" / "leak.py").symlink_to(outside / "secret.py")
        found = [p.resolve() for p in iter_source_files(proj, source_dirs=["src"])]
        assert (outside / "secret.py").resolve() not in found
        # the genuine in-root source file is still discovered (anti-false-RED).
        assert (proj / "src" / "app.py").resolve() in found

    def test_in_root_source_dirs_unchanged(self, escape_project):
        # ANTI-FALSE-RED: a normal in-root source dir is walked exactly as before.
        proj, _outside = escape_project
        found = {p.relative_to(proj).as_posix() for p in iter_source_files(proj, source_dirs=["src"])}
        assert found == {"src/app.py"}


# ═══════════════════════════════════════════════════════════
# Crash regression — scanner with a config missing the scan section
# ═══════════════════════════════════════════════════════════


class TestScannerMissingScanSection:
    def test_run_scan_does_not_crash_without_scan_section(self, tmp_path, capsys):
        import yaml as _yaml

        from codd.scanner import run_scan

        codd_dir = tmp_path / "codd"
        codd_dir.mkdir()
        # No "scan" section at all — the old config["scan"] access raised
        # KeyError before any scanning happened.
        (codd_dir / "codd.yaml").write_text(
            _yaml.safe_dump({"project": {"name": "demo", "language": "python"}}),
            encoding="utf-8",
        )

        run_scan(tmp_path, codd_dir)

        out = capsys.readouterr().out
        assert "Scan complete" in out
