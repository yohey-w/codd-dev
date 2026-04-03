"""Tests for codd policy — enterprise policy checker."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codd.policy import (
    PolicyResult,
    PolicyRule,
    PolicyViolation,
    format_policy_text,
    load_policies,
    run_policy,
    _file_matches_glob,
)


class TestLoadPolicies:
    def test_load_forbidden_rule(self):
        config = {
            "policies": [
                {
                    "id": "SEC-001",
                    "description": "No hardcoded passwords",
                    "severity": "CRITICAL",
                    "kind": "forbidden",
                    "pattern": r"password\s*=\s*['\"]",
                    "glob": "*.py",
                }
            ]
        }
        rules = load_policies(config)
        assert len(rules) == 1
        assert rules[0].id == "SEC-001"
        assert rules[0].kind == "forbidden"
        assert rules[0].compiled is not None

    def test_load_required_rule(self):
        config = {
            "policies": [
                {
                    "id": "LOG-001",
                    "description": "Must import logging",
                    "severity": "WARNING",
                    "kind": "required",
                    "pattern": r"import logging",
                    "glob": "*.py",
                }
            ]
        }
        rules = load_policies(config)
        assert len(rules) == 1
        assert rules[0].kind == "required"

    def test_empty_policies(self):
        assert load_policies({}) == []
        assert load_policies({"policies": []}) == []

    def test_skip_invalid_entry(self):
        config = {"policies": [{"no_id": True}, {"id": "OK", "pattern": "x"}]}
        rules = load_policies(config)
        assert len(rules) == 1
        assert rules[0].id == "OK"

    def test_invalid_regex_still_loads(self):
        config = {"policies": [{"id": "BAD", "pattern": "[invalid"}]}
        rules = load_policies(config)
        assert len(rules) == 1
        assert rules[0].compiled is None


class TestFileMatchesGlob:
    def test_simple_extension(self):
        assert _file_matches_glob("src/auth.py", "*.py")
        assert not _file_matches_glob("src/auth.ts", "*.py")

    def test_path_glob(self):
        assert _file_matches_glob("src/auth/login.py", "src/**/*.py")

    def test_no_glob_chars(self):
        assert _file_matches_glob("Makefile", "Makefile")


class TestRunPolicy:
    def test_forbidden_pattern_detected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "bad.py").write_text('DB_PASS = "hunter2"\n', encoding="utf-8")
        (src / "good.py").write_text("import os\n", encoding="utf-8")

        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {
            "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            "policies": [
                {"id": "SEC-001", "kind": "forbidden", "pattern": r'_PASS\s*=\s*"', "severity": "CRITICAL", "glob": "*.py"}
            ],
        }
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        result = run_policy(project)
        assert result.files_checked == 2
        assert result.critical_count == 1
        assert result.violations[0].file == "src/bad.py"
        assert result.violations[0].line == 1

    def test_required_pattern_missing(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "mod.py").write_text("print('hello')\n", encoding="utf-8")

        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {
            "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            "policies": [
                {"id": "LOG-001", "kind": "required", "pattern": "import logging", "severity": "WARNING", "glob": "*.py"}
            ],
        }
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        result = run_policy(project)
        assert result.warning_count == 1
        assert result.violations[0].line is None  # required checks whole file

    def test_no_violations(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "clean.py").write_text("import logging\nx = 1\n", encoding="utf-8")

        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {
            "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            "policies": [
                {"id": "LOG-001", "kind": "required", "pattern": "import logging", "severity": "WARNING", "glob": "*.py"}
            ],
        }
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        result = run_policy(project)
        assert result.pass_
        assert len(result.violations) == 0

    def test_changed_files_filter(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "a.py").write_text('password = "bad"\n', encoding="utf-8")
        (src / "b.py").write_text('password = "bad"\n', encoding="utf-8")

        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {
            "scan": {"source_dirs": ["src/"], "test_dirs": [], "doc_dirs": [], "config_files": [], "exclude": []},
            "policies": [
                {"id": "SEC-001", "kind": "forbidden", "pattern": "password", "severity": "CRITICAL", "glob": "*.py"}
            ],
        }
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        # Only check a.py
        result = run_policy(project, changed_files=["src/a.py"])
        assert result.files_checked == 1
        assert result.critical_count == 1


class TestFormatPolicyText:
    def test_pass_format(self):
        result = PolicyResult(files_checked=10, rules_applied=3)
        text = format_policy_text(result)
        assert "PASS" in text
        assert "Files: 10" in text

    def test_fail_format(self):
        result = PolicyResult(
            files_checked=5,
            rules_applied=2,
            violations=[
                PolicyViolation(rule_id="SEC-001", severity="CRITICAL", file="a.py", line=5, message="bad"),
            ],
        )
        text = format_policy_text(result)
        assert "FAIL" in text
        assert "SEC-001" in text
        assert "a.py:5" in text
