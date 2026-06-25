"""C++ extract/scan facts (Increment 1, Piece 3).

* ``_symbols_cpp`` captures ``class`` / ``struct`` / ``enum`` / ``namespace`` and
  free functions.
* ``_imports_cpp`` classifies quote-form ``#include "…"`` as ``internal`` vs
  angle-form ``#include <…>`` (system/STL) as ``external``.
* A C++ scanner CEG resolver materializes PATH-resolved ``file:`` import edges
  (modeled on the TS/JS resolver, NOT the Java module-node resolver — C++
  resolution is path-based).
"""

from __future__ import annotations

from pathlib import Path

from codd.extractor import _extract_imports, _extract_symbols
from codd.parsing.regex_strategies import (
    _CEG_IMPORT_RESOLVERS,
    _STRATEGIES,
    ceg_import_targets,
    strategy_for,
)


CPP_TYPES_SRC = """\
#include "demo/core.h"
#include <vector>

namespace demo {

class Service {
 public:
  int do_work(int n) { return n; }
};

struct Point {
  int x;
  int y;
};

enum Color { RED, GREEN };

int free_function(int a) { return a + 1; }

}  // namespace demo
"""


def _symbol_kinds(symbols) -> dict[str, str]:
    return {s.name: s.kind for s in symbols}


def test_cpp_strategy_registered():
    assert "cpp" in _STRATEGIES
    assert ".cc" in strategy_for("cpp").extensions
    assert ".h" in strategy_for("cpp").extensions
    assert ".hpp" in strategy_for("cpp").extensions


def test_regex_cpp_symbols_capture_class_struct_enum_namespace():
    kinds = _symbol_kinds(_extract_symbols(CPP_TYPES_SRC, "service.cc", "cpp"))
    assert kinds.get("Service") == "class"
    assert kinds.get("Point") == "struct"
    assert kinds.get("Color") == "enum"
    assert kinds.get("demo") == "namespace"
    assert kinds.get("free_function") == "function"


CPP_IMPORTS_SRC = """\
#include "demo/core.h"
#include "demo/util/helper.h"
#include <string>
#include <memory>
"""


def test_cpp_imports_classify_internal_vs_external(tmp_path):
    internal, external = _extract_imports(
        CPP_IMPORTS_SRC, "cpp", tmp_path, tmp_path / "src", tmp_path / "src" / "service.cc"
    )
    # Quote-form includes → internal (carry the literal include line).
    internal_lines = [line for lines in internal.values() for line in lines]
    assert any("demo/core.h" in line for line in internal_lines)
    assert any("demo/util/helper.h" in line for line in internal_lines)
    # Angle-form (system/STL) → external.
    assert "string" in external
    assert "memory" in external
    # No quote-form path leaked into external.
    assert not any("demo/" in ext for ext in external)


def test_cpp_ceg_resolver_registered_and_resolves_file_nodes(tmp_path):
    """C++ CEG resolver produces PATH-resolved ``file:`` nodes (like TS/JS)."""
    assert "cpp" in _CEG_IMPORT_RESOLVERS

    # Lay out a real header so the path-based resolver can find it.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "demo").mkdir()
    header = tmp_path / "src" / "demo" / "core.h"
    header.write_text("#pragma once\n", encoding="utf-8")

    # ``internal`` mirrors what ``_imports_cpp`` emits: marker-tagged include
    # lines keyed by some bucket.
    internal = {"demo": ['#include "demo/core.h"']}
    targets = ceg_import_targets(
        "cpp", internal, tmp_path, tmp_path / "src" / "service.cc"
    )
    ids = {t.target_id for t in targets}
    node_types = {t.node_type for t in targets}
    assert "file:src/demo/core.h" in ids, f"path-resolved file node missing; ids={ids}"
    assert node_types == {"file"}, f"C++ CEG nodes must be file:, got {node_types}"


def test_cpp_ceg_resolver_skips_angle_includes(tmp_path):
    """Angle-form (system/STL) includes never become file nodes."""
    internal = {"std": ["#include <vector>"]}
    targets = ceg_import_targets(
        "cpp", internal, tmp_path, tmp_path / "src" / "service.cc"
    )
    assert targets == [], f"system includes must not resolve to file nodes; {targets}"
