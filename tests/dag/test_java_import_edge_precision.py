"""PRECISION — Java import-edge kind labeling + static-import dedup + package-line edges.

Three generic precision refinements to the Java import graph:

1. The scanner-CEG resolver labeled EVERY Java import ``evidence_method=
   "static_import"`` — even plain (``import com.x.Y``) and wildcard
   (``import com.x.*``) imports. Edges are now labeled by their ACTUAL kind.
2. Per-member ``static`` imports of the same owning class (``static com.x.Y.a`` +
   ``static com.x.Y.b``) collapsed to DISTINCT member module nodes. They now dedup
   to the owning class so two members of one class do not double the node/edge set.
3. The DAG builder synthesized an implicit ``imports`` edge from a ``package`` line
   to EVERY same-package sibling (Java needs no import for siblings). That is both
   imprecise (not every sibling is referenced) AND explosive (O(package_size²) —
   >400k spurious edges on Guava). We now emit ONLY real import edges: a
   ``package`` line contributes none; an explicit ``import`` (incl. a same-package
   one, or a wildcard) still does.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.parsing import get_extractor
from codd.parsing.regex_strategies import ceg_import_targets


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _ceg_targets_for(content: str, file_path: Path, project_root: Path):
    internal, _ = get_extractor("java", "source").extract_imports(
        content, file_path, project_root, project_root
    )
    return ceg_import_targets("java", internal, project_root, file_path)


def test_ceg_labels_plain_import_not_as_static(tmp_path):
    content = (
        "package com.acme.app;\n"
        "import com.acme.app.util.Helper;\n"
        "public class Service {}\n"
    )
    file_path = tmp_path / "Service.java"
    targets = _ceg_targets_for(content, file_path, tmp_path)
    helper = [t for t in targets if t.target_id.endswith("Helper")]
    assert helper, f"plain import target missing; targets={[t.target_id for t in targets]}"
    assert helper[0].evidence_method == "import", (
        f"plain import mislabeled as {helper[0].evidence_method!r} (should be 'import')"
    )


def test_ceg_labels_wildcard_and_static_distinctly(tmp_path):
    content = (
        "package com.acme.app;\n"
        "import com.acme.app.pkg.*;\n"
        "import static com.acme.app.util.Helpers.build;\n"
        "public class Service {}\n"
    )
    file_path = tmp_path / "Service.java"
    targets = _ceg_targets_for(content, file_path, tmp_path)
    by_method = {t.evidence_method for t in targets}
    assert "wildcard_import" in by_method, f"wildcard not labeled distinctly; {by_method}"
    assert "static_import" in by_method, f"static not labeled; {by_method}"


def test_ceg_dedups_per_member_static_imports_to_owning_class(tmp_path):
    content = (
        "package com.acme.app;\n"
        "import static com.acme.app.util.Helpers.alpha;\n"
        "import static com.acme.app.util.Helpers.beta;\n"
        "public class Service {}\n"
    )
    file_path = tmp_path / "Service.java"
    targets = _ceg_targets_for(content, file_path, tmp_path)
    static_targets = [t for t in targets if t.evidence_method == "static_import"]
    # Both members belong to ``com.acme.app.util.Helpers`` → ONE node, not two.
    assert len(static_targets) == 1, (
        f"per-member static imports not deduped to owning class; "
        f"targets={[t.target_id for t in static_targets]}"
    )
    assert static_targets[0].target_id.endswith("util.Helpers"), (
        f"static import not collapsed to owning class; got {static_targets[0].target_id}"
    )


def _import_edges_with_attrs(dag):
    return [
        (edge.from_id, edge.to_id, edge.attributes or {})
        for edge in dag.edges
        if edge.kind == "imports"
    ]


def test_builder_emits_only_real_import_edges_no_package_fanout(tmp_path):
    """A ``package`` line creates NO sibling edge; explicit imports still do.

    ``A`` explicitly imports ``B`` (cross-package) and ``D`` (same package via an
    explicit ``import com.a.D``). ``C`` is a same-package sibling of ``A`` with NO
    explicit import — it must NOT get a fabricated package-sibling edge. This pins
    the precision/anti-explosion fix (no O(n²) package fan-out).
    """
    _write(tmp_path / "pom.xml", "<project />\n")
    _write(
        tmp_path / "codd" / "codd.yaml",
        yaml.safe_dump(
            {"scan": {"source_dirs": ["src/main/java"], "doc_dirs": []}},
            sort_keys=False,
        ),
    )
    base = tmp_path / "src" / "main" / "java"
    _write(
        base / "com" / "a" / "A.java",
        "package com.a;\nimport com.b.B;\nimport com.a.D;\npublic class A { B b; D d; }\n",
    )
    _write(base / "com" / "b" / "B.java", "package com.b;\npublic class B {}\n")
    _write(base / "com" / "a" / "D.java", "package com.a;\npublic class D {}\n")
    _write(base / "com" / "a" / "C.java", "package com.a;\npublic class C {}\n")

    dag = build_dag(tmp_path)
    edges = {(f, t) for (f, t, _at) in _import_edges_with_attrs(dag)}
    a = "src/main/java/com/a/A.java"
    b = "src/main/java/com/b/B.java"
    c = "src/main/java/com/a/C.java"
    d = "src/main/java/com/a/D.java"

    assert (a, b) in edges, f"explicit cross-package import edge missing; edges={edges}"
    assert (a, d) in edges, f"explicit same-package import edge missing; edges={edges}"
    # C is a same-package sibling with NO explicit import → NO fabricated edge.
    assert (a, c) not in edges, (
        f"package-line sibling fan-out edge wrongly synthesized; edges={edges}"
    )
    # And no implicit-marked edges exist at all (we emit only real imports).
    assert all(
        (at or {}).get("implicit") is None for (_f, _t, at) in _import_edges_with_attrs(dag)
    ), "no edge should carry an 'implicit' marker"
