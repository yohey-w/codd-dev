"""GENERIC FIX 1 — discovery completeness accounting (convergence safety-net).

Surfacing-only (autonomous, anti-false-green for the DISCOVERY layer; NOT a
fail-gate — that would be owner-gated). Two WARNs make unknown discovery gaps
VISIBLE so the system converges instead of silently dropping source:

1. After the DAG build, if MORE detected-language source files exist on disk than
   there are source nodes in the graph → WARN (count + a few example missing
   files). This catches an under-scoped ``source_dirs`` (C++ ``["include"]``-only)
   that would otherwise leave impl files inert.
2. When an INTERNAL-looking import specifier (a relative ``.``/``./`` import, or a
   first-party dotted/FQN) fails to resolve to any in-tree node → WARN with the
   count of such "unresolved residue".

Both are ``UserWarning`` (advisory), never an exception / red verdict.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import yaml

from codd.dag.builder import build_dag


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _warn_texts(record) -> list[str]:
    return [str(w.message) for w in record]


def test_under_scoped_source_dirs_warns_missing_files(tmp_path):
    """C++ ``source_dirs: ["include"]`` while impl lives in db/ → completeness WARN."""
    _write(tmp_path / "CMakeLists.txt", "project(demo)\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": ["include"], "test_dirs": [], "doc_dirs": []}},
            sort_keys=False,
        ),
    )
    # Only this header is in the scoped node-set:
    _write(tmp_path / "include" / "api.h", "#pragma once\nint api();\n")
    # These impl files are on disk but OUTSIDE the scoped source_dirs → orphaned.
    _write(tmp_path / "db" / "impl.cc", "namespace x { int impl() { return 0; } }\n")
    _write(tmp_path / "util" / "helper.cc", "namespace x { int helper() { return 0; } }\n")

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        build_dag(tmp_path)

    texts = _warn_texts(record)
    completeness = [t for t in texts if "source file" in t.lower() and "node" in t.lower()]
    assert completeness, (
        f"expected a completeness WARN naming missing source files; warnings={texts}"
    )
    # The warning should mention an example missing file (db/impl.cc or util/helper.cc).
    assert any("impl.cc" in t or "helper.cc" in t for t in completeness), (
        f"completeness WARN missing example file names; warnings={completeness}"
    )


def test_complete_scoping_emits_no_completeness_warning(tmp_path):
    """When every source file is a node, NO completeness WARN fires (no false alarm)."""
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["."], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    _write(tmp_path / "main.py", "import helper\n")
    _write(tmp_path / "helper.py", "def work():\n    return 1\n")

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        build_dag(tmp_path)

    texts = _warn_texts(record)
    completeness = [t for t in texts if "source file" in t.lower() and "node" in t.lower()]
    assert not completeness, f"unexpected completeness WARN on full scoping: {completeness}"


def test_unresolved_internal_import_warns_residue(tmp_path):
    """An internal-looking relative import that resolves to nothing → residue WARN."""
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "scan": {"source_dirs": ["."], "doc_dirs": []},
                "required_artifacts": {"project_type": "generic"},
            },
            sort_keys=False,
        ),
    )
    # ``.missing`` is a relative (internal-looking) import with NO target on disk.
    _write(
        tmp_path / "pkg" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "pkg" / "mod.py",
        "from .missing import gone\n\ndef f():\n    return gone()\n",
    )

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        build_dag(tmp_path)

    texts = _warn_texts(record)
    residue = [t for t in texts if "unresolved" in t.lower()]
    assert residue, f"expected an unresolved-residue WARN; warnings={texts}"
