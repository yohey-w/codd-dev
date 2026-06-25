"""GENERIC FIX 2 — robust, COMPLETE source discovery in ``_detect_source_dirs``.

``_detect_source_dirs`` used to stop at the first conventional top-level dir it
found, so source living elsewhere was silently dropped from extraction:

* BUG A — root-level source files ALONGSIDE subpackages (5 languages): a repo with
  ``main.py`` + ``config.py`` at the root plus a ``subpkg/`` package returned only
  ``['subpkg']``; the root files vanished.
* Java BUG 2 — a top-level dir whose source lives ONLY in a nested subdir
  (``util/`` → ``util/concurrent/Service.java``): once another dir (``src``) was
  found, the nested-only dir was never scanned.

The fix is ONE generic mechanism: recursively find ALL detected-language source
under the root, then return a source-root set that COVERS every source file (root
``.`` when root-level source exists, plus each top-level dir that contains source
at any depth) — tolerant of arbitrary scoping, no per-layout case. Discovery is
de-duplicated so an overlapping cover never double-counts a file.
"""

from __future__ import annotations

from pathlib import Path

from codd.extractor import extract_facts


def _write(path: Path, content: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _discovered_files(facts) -> set[str]:
    return {f for mod in facts.modules.values() for f in mod.files}


def test_root_level_files_alongside_subpackage_are_discovered(tmp_path):
    """BUG A: root-level ``main.py``/``config.py`` are discovered, not dropped."""
    _write(tmp_path / "main.py", "from subpkg.helper import help_fn\n")
    _write(tmp_path / "config.py", "VALUE = 1\n")
    _write(tmp_path / "subpkg" / "__init__.py", "")
    _write(tmp_path / "subpkg" / "helper.py", "def help_fn():\n    return 42\n")

    facts = extract_facts(tmp_path, "python")
    files = _discovered_files(facts)

    assert "main.py" in files, f"root-level main.py dropped; discovered={files}"
    assert "config.py" in files, f"root-level config.py dropped; discovered={files}"
    assert "subpkg/helper.py" in files, f"subpackage file dropped; discovered={files}"


def test_nested_only_source_dir_is_discovered(tmp_path):
    """Java BUG 2: a top-level dir whose source is only in a nested subdir."""
    _write(tmp_path / "src" / "Main.java", "package com.x;\npublic class Main {}\n")
    _write(
        tmp_path / "util" / "concurrent" / "Service.java",
        "package com.x.util.concurrent;\npublic class Service {}\n",
    )

    facts = extract_facts(tmp_path, "java")
    files = _discovered_files(facts)

    assert "src/Main.java" in files, f"src file dropped; discovered={files}"
    assert "util/concurrent/Service.java" in files, (
        f"nested-only util/concurrent source dropped; discovered={files}"
    )


def test_discovery_does_not_double_count_overlapping_cover(tmp_path):
    """An overlapping source-root cover must not list a file twice."""
    _write(tmp_path / "app.py", "import lib.mod\n")
    _write(tmp_path / "lib" / "__init__.py", "")
    _write(tmp_path / "lib" / "mod.py", "VALUE = 1\n")

    facts = extract_facts(tmp_path, "python")
    all_files = [f for mod in facts.modules.values() for f in mod.files]
    assert len(all_files) == len(set(all_files)), (
        f"a file was discovered more than once (overlapping cover not de-duped); "
        f"files={sorted(all_files)}"
    )


def test_subpackage_only_layout_still_works(tmp_path):
    """No regression: a pure subpackage layout (no root files) is unaffected."""
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "a.py", "import pkg.b\n")
    _write(tmp_path / "pkg" / "b.py", "VALUE = 1\n")

    facts = extract_facts(tmp_path, "python")
    files = _discovered_files(facts)
    assert {"pkg/a.py", "pkg/b.py"} <= files, f"subpackage layout regressed; {files}"
