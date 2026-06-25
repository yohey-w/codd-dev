"""Java extract/scan facts (Increment 1, Piece 2).

* ``_symbols_java`` now captures ``interface`` / ``enum`` / ``record`` in addition
  to ``class`` + methods (regex strategy, the tree-sitter fallback).
* ``_imports_java`` classifies first-party packages as ``internal`` vs
  ``java.*`` / third-party as ``external``.
* Java auto-promotes from RegexExtractor → tree-sitter via the registry when the
  binding is installed.
* The Java scanner CEG resolver materializes import edges.
"""

from __future__ import annotations

from pathlib import Path

from codd.extractor import _extract_imports, _extract_symbols
from codd.parsing import RegexExtractor, TreeSitterExtractor, get_extractor
from codd.parsing.regex_strategies import _CEG_IMPORT_RESOLVERS, ceg_import_targets


JAVA_TYPES_SRC = """\
package com.acme.app;

public class Service {
    public int doWork(String name) { return 0; }
}

interface Handler {}

enum Color { RED, GREEN }

record Point(int x, int y) {}
"""


def _symbol_kinds(symbols) -> dict[str, str]:
    return {s.name: s.kind for s in symbols}


def test_regex_java_symbols_capture_interface_enum_record():
    kinds = _symbol_kinds(_extract_symbols(JAVA_TYPES_SRC, "Service.java", "java"))
    assert kinds.get("Service") == "class"
    assert kinds.get("doWork") == "function"
    assert kinds.get("Handler") == "interface"
    assert kinds.get("Color") == "enum"
    # ``record`` collapses to the nearest existing kind (class) for downstream
    # consumers that only know class/interface/enum/function.
    assert kinds.get("Point") == "class"


JAVA_IMPORTS_SRC = """\
package com.acme.app;

import com.acme.app.util.Helper;
import com.acme.app.model.User;
import java.util.List;
import javax.annotation.Nullable;
import org.thirdparty.Lib;

public class Service {}
"""


def test_regex_java_imports_classify_internal_vs_external(tmp_path):
    src_dir = tmp_path / "src" / "main" / "java"
    internal, external = _extract_imports(
        JAVA_IMPORTS_SRC, "java", tmp_path, src_dir, src_dir / "com" / "acme" / "app" / "Service.java"
    )
    # First-party (shares the com.acme org+domain prefix) → internal.
    assert "util" in internal
    assert "model" in internal
    # JDK / platform / unrelated third-party → external (full FQN preserved).
    assert "java.util.List" in external
    assert "javax.annotation.Nullable" in external
    assert "org.thirdparty.Lib" in external
    # No first-party FQN leaked into external.
    assert not any(fqn.startswith("com.acme") for fqn in external)


def test_java_registry_promotes_to_tree_sitter_when_available():
    ext = get_extractor("java", "source")
    if TreeSitterExtractor.is_available("java"):
        assert isinstance(ext, TreeSitterExtractor)
    else:
        assert isinstance(ext, RegexExtractor)


def test_java_ceg_resolver_registered_and_builds_module_targets(tmp_path):
    assert "java" in _CEG_IMPORT_RESOLVERS
    internal = {"util": ["com.acme.app.util.Helper"], "model": ["static com.acme.app.model.User.find"]}
    targets = ceg_import_targets("java", internal, tmp_path, tmp_path / "Service.java")
    ids = {t.target_id for t in targets}
    assert "module:com.acme.app.util.Helper" in ids
    # PRECISION: a ``static`` import collapses to its OWNING CLASS (member dropped)
    # so per-member static imports of the same class dedup to one node; it is also
    # labeled ``static_import`` (the plain import above is labeled ``import``).
    assert "module:com.acme.app.model.User" in ids
    assert "module:com.acme.app.model.User.find" not in ids
    by_id = {t.target_id: t.evidence_method for t in targets}
    assert by_id["module:com.acme.app.util.Helper"] == "import"
    assert by_id["module:com.acme.app.model.User"] == "static_import"
