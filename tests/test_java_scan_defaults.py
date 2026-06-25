"""Java-aware bootstrap scan defaults (Increment 1, Piece 3).

A Maven/Gradle Java project keeps its sources under ``src/main/java`` and tests
under ``src/test/java`` — NOT a top-level ``tests/``. The bootstrap codd.yaml
must point ``scan.source_dirs`` / ``scan.test_dirs`` at those so first-touch
``codd extract`` separates impl from test correctly (otherwise ``src/test/java``
Test files are swept into the ``src/**`` impl glob). Other languages unchanged.
"""

from __future__ import annotations

from pathlib import Path

from codd.extractor import _detect_source_dirs, _detect_test_dirs


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _seed_maven_layout(root: Path) -> None:
    _write(root / "pom.xml", "<project />\n")
    _write(root / "src" / "main" / "java" / "com" / "a" / "A.java", "package com.a;\npublic class A {}\n")
    _write(root / "src" / "test" / "java" / "com" / "a" / "ATest.java", "package com.a;\npublic class ATest {}\n")


def test_java_source_dirs_point_at_src_main_java(tmp_path):
    _seed_maven_layout(tmp_path)
    found = _detect_source_dirs(tmp_path, "java")
    assert "src/main/java" in found
    # The bare ``src`` (which would also sweep in src/test/java) is NOT used.
    assert "src" not in found


def test_java_test_dirs_detect_src_test_java(tmp_path):
    _seed_maven_layout(tmp_path)
    found = _detect_test_dirs(tmp_path)
    assert "src/test/java" in found


def test_non_java_source_detection_unchanged(tmp_path):
    # A Python project with a top-level ``src`` keeps the legacy behavior.
    _write(tmp_path / "src" / "app.py", "def run(): pass\n")
    found = _detect_source_dirs(tmp_path, "python")
    assert found == ["src"]


def test_non_java_test_detection_unchanged(tmp_path):
    _write(tmp_path / "tests" / "test_app.py", "def test_x(): pass\n")
    found = _detect_test_dirs(tmp_path)
    assert found == ["tests"]


def test_java_without_maven_layout_falls_back(tmp_path):
    # A Java project with a flat ``src`` (no main/java) keeps ``src``.
    _write(tmp_path / "pom.xml", "<project />\n")
    _write(tmp_path / "src" / "App.java", "public class App {}\n")
    found = _detect_source_dirs(tmp_path, "java")
    assert "src" in found
