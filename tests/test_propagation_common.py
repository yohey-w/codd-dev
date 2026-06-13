"""Tests for codd.propagation_common — shared propagation primitives (RF7a).

Covers three contracts:

1. Primitive behavior (git change detection, design-doc discovery,
   modules-field reading, file→module mapping, doc-body rendering).
2. Equivalence: ``require_propagate._parse_frontmatter_changes`` (now a
   wrapper over ``parse_frontmatter_diff``) returns the same results as the
   legacy hand-rolled line parser on representative diffs. The legacy parser
   is ported verbatim below as the oracle.
3. Band drift-guard: ``propagator._classify_docs_by_band`` agrees with
   ``codd.confidence.classify_band`` across a confidence × evidence grid
   (it delegates; this test pins the delegation).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from codd import confidence
from codd.propagation_common import (
    doc_modules,
    get_changed_files,
    iter_design_docs,
    map_files_to_modules,
    parse_frontmatter_diff,
    read_codd_frontmatter,
    render_updated_doc_content,
)
from codd.propagator import (
    AffectedDoc,
    _classify_docs_by_band,
    _write_updated_doc,
)
from codd.require_propagate import (
    _is_requirement_path,
    _parse_frontmatter_changes,
    _render_updated_doc_content,
)


# ---------------------------------------------------------------------------
# get_changed_files
# ---------------------------------------------------------------------------


def test_get_changed_files_parses_name_only_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.propagation_common.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=0,
            stdout="src/auth/service.py\n\ndocs/design/auth.md\n", stderr="",
        ),
    )

    assert get_changed_files(tmp_path, "HEAD") == [
        "src/auth/service.py",
        "docs/design/auth.md",
    ]


def test_get_changed_files_failure_silent_by_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "codd.propagation_common.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=128, stdout="", stderr="fatal: bad revision",
        ),
    )

    assert get_changed_files(tmp_path, "HEAD") == []
    assert capsys.readouterr().out == ""


def test_get_changed_files_failure_warns_when_asked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        "codd.propagation_common.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0], returncode=128, stdout="", stderr="fatal: bad revision",
        ),
    )

    assert get_changed_files(tmp_path, "HEAD", warn=True) == []
    assert "Warning: git diff failed: fatal: bad revision" in capsys.readouterr().out


def test_get_changed_files_git_missing(tmp_path, monkeypatch, capsys):
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("codd.propagation_common.subprocess.run", raise_missing)

    assert get_changed_files(tmp_path, "HEAD") == []
    assert get_changed_files(tmp_path, "HEAD", warn=True) == []
    assert "Warning: git not found." in capsys.readouterr().out


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def test_get_changed_files_returns_project_relative_paths_in_monorepo_subdir(tmp_path):
    """FIX 5 (false-RED, monorepo): when the CoDD project is a git-repo SUBDIR,
    change detection must emit PROJECT-relative paths (``docs/x.md``), not
    repo-root-relative ones (``packages/app/docs/x.md``). Otherwise propagate
    resolves them against project_root with a doubled prefix, finds zero changed
    docs, and freshness later false-REDs ``never_reconciled``."""
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    project = repo / "packages" / "app"
    (project / "docs").mkdir(parents=True)
    doc = project / "docs" / "design.md"
    doc.write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    doc.write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")

    # project_root is the SUBDIR, not the repo root.
    changed = get_changed_files(project, "HEAD~1")
    assert changed == ["docs/design.md"]


def test_get_changed_files_returns_relative_paths_at_repo_root(tmp_path):
    """FIX 5 non-regression: when repo root == project root, paths are already
    project-relative and ``--relative`` is a harmless no-op."""
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "docs").mkdir()
    doc = repo / "docs" / "design.md"
    doc.write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    doc.write_text("v2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit")

    changed = get_changed_files(repo, "HEAD~1")
    assert changed == ["docs/design.md"]


# ---------------------------------------------------------------------------
# read_codd_frontmatter / iter_design_docs / doc_modules
# ---------------------------------------------------------------------------


def _write_doc(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_read_codd_frontmatter_matches_scanner_extractor(tmp_path):
    """Byte-for-byte the same semantics as codd.scanner._extract_frontmatter."""
    from codd.scanner import _extract_frontmatter as scanner_extract

    with_codd = _write_doc(
        tmp_path / "a.md",
        "---\ncodd:\n  node_id: design:a\n  modules: [auth]\n---\n\n# A\n",
    )
    without_codd = _write_doc(tmp_path / "b.md", "---\ntitle: B\n---\n\n# B\n")
    no_frontmatter = _write_doc(tmp_path / "c.md", "# C\n")
    missing = tmp_path / "missing.md"

    for doc in (with_codd, without_codd, no_frontmatter, missing):
        assert read_codd_frontmatter(doc) == scanner_extract(doc)

    assert read_codd_frontmatter(with_codd) == {
        "node_id": "design:a",
        "modules": ["auth"],
    }
    assert read_codd_frontmatter(without_codd) is None
    assert read_codd_frontmatter(no_frontmatter) is None


def test_iter_design_docs_yields_only_node_id_docs(tmp_path):
    config = {"scan": {"doc_dirs": ["docs/design/", "docs/requirements/", "missing/"]}}
    _write_doc(
        tmp_path / "docs" / "design" / "auth.md",
        "---\ncodd:\n  node_id: design:auth\n---\n\n# Auth\n",
    )
    _write_doc(tmp_path / "docs" / "design" / "notes.md", "# Plain notes\n")
    _write_doc(
        tmp_path / "docs" / "design" / "no_id.md",
        "---\ncodd:\n  type: design\n---\n\n# No id\n",
    )
    _write_doc(
        tmp_path / "docs" / "requirements" / "req.md",
        "---\ncodd:\n  node_id: req:auth\n---\n\n# Req\n",
    )
    # Outside doc_dirs → never visited
    _write_doc(tmp_path / "src" / "x.md", "---\ncodd:\n  node_id: x\n---\n\n# X\n")

    results = list(iter_design_docs(tmp_path, config))

    node_ids = [data["node_id"] for _, data in results]
    assert node_ids == ["design:auth", "req:auth"]  # doc_dirs config order
    paths = [p.relative_to(tmp_path).as_posix() for p, _ in results]
    assert paths == ["docs/design/auth.md", "docs/requirements/req.md"]


def test_doc_modules_default_and_no_coercion():
    assert doc_modules({"node_id": "design:a", "modules": ["auth", "tasks"]}) == [
        "auth",
        "tasks",
    ]
    assert doc_modules({"node_id": "design:a"}) == []
    # Deliberately NO scalar→list coercion (legacy raw-read semantics):
    # consumers have always received the raw value.
    assert doc_modules({"modules": "auth"}) == "auth"


# ---------------------------------------------------------------------------
# map_files_to_modules (moved verbatim from propagator)
# ---------------------------------------------------------------------------


def test_map_files_to_modules_basic():
    assert map_files_to_modules(
        ["src/auth/service.py", "src/tasks/models.py", "README.md"], ["src"]
    ) == {"src/auth/service.py": "auth", "src/tasks/models.py": "tasks"}


def test_map_files_to_modules_root_level_excluded():
    assert map_files_to_modules(["src/main.py"], ["src"]) == {}


def test_propagator_alias_is_shared_implementation():
    from codd.propagator import _map_files_to_modules

    assert _map_files_to_modules is map_files_to_modules


# ---------------------------------------------------------------------------
# render_updated_doc_content (+ equivalence with both consumers)
# ---------------------------------------------------------------------------

ORIGINAL_DOC = (
    "---\ncodd:\n  node_id: design:auth\n  title: Auth Design\n---\n"
    "# Auth Design\n\n## Overview\n\nOld content.\n"
)


def test_render_preserves_frontmatter_and_title():
    rendered = render_updated_doc_content(
        ORIGINAL_DOC, "# Renamed By AI\n\n## Overview\n\nNew content.\n"
    )

    assert rendered.startswith("---\ncodd:\n  node_id: design:auth\n")
    assert "# Auth Design\n" in rendered  # original title kept
    assert "# Renamed By AI" not in rendered
    assert "New content." in rendered
    assert rendered.endswith("\n")


def test_render_without_frontmatter_or_title():
    rendered = render_updated_doc_content("Just text.\n", "Body only.\n")
    assert rendered == "Body only.\n"


def test_write_updated_doc_writes_exactly_rendered_content(tmp_path):
    doc = tmp_path / "auth.md"
    doc.write_text(ORIGINAL_DOC, encoding="utf-8")
    new_body = "# Auth Design\n\n## Overview\n\nUpdated.\n"

    _write_updated_doc(doc, ORIGINAL_DOC, new_body)

    assert doc.read_text(encoding="utf-8") == render_updated_doc_content(
        ORIGINAL_DOC, new_body
    )


def test_require_preview_renders_exactly_what_apply_writes(tmp_path):
    """The require --propagate dry-run preview and --apply share one renderer."""
    new_body = "# Auth Design\n\nProposal body.\n"

    preview = _render_updated_doc_content(ORIGINAL_DOC, new_body)

    doc = tmp_path / "auth.md"
    doc.write_text(ORIGINAL_DOC, encoding="utf-8")
    _write_updated_doc(doc, ORIGINAL_DOC, new_body)
    assert preview == doc.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_frontmatter_diff — legacy oracle equivalence
# ---------------------------------------------------------------------------

# The pre-RF7a hand parser from codd/require_propagate.py, ported VERBATIM as
# the equivalence oracle (including its requirement-path filter).

_LEGACY_FIELD = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*(.*?)\s*$")


def _legacy_path_from_diff_header(line):
    parts = line.split()
    if len(parts) >= 4 and parts[3].startswith("b/"):
        return parts[3][2:]
    return None


def _legacy_parse_frontmatter_changes(diff_text):
    changes = []
    current_file = None
    removed = {}
    added = {}
    field_order = []
    in_frontmatter = False
    saw_frontmatter = False

    def remember(field, value, bucket):
        if field not in field_order:
            field_order.append(field)
        bucket[field] = value

    def flush():
        nonlocal removed, added, field_order
        if current_file is None:
            return
        for field in field_order:
            old = removed.get(field)
            new = added.get(field)
            if old == new:
                continue
            changes.append(
                {"file": current_file, "field": field, "old": old, "new": new}
            )
        removed = {}
        added = {}
        field_order = []

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_file = _legacy_path_from_diff_header(line)
            in_frontmatter = False
            saw_frontmatter = False
            continue

        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            continue
        if line.startswith("--- a/") or line.startswith("index "):
            continue
        if current_file is None or not _is_requirement_path(current_file):
            continue
        if not line or line[0] not in " +-":
            continue

        prefix = line[0]
        content = line[1:]
        if content.strip() == "---":
            if not saw_frontmatter:
                saw_frontmatter = True
                in_frontmatter = True
            elif in_frontmatter:
                in_frontmatter = False
            continue
        if not in_frontmatter:
            continue
        if prefix not in "+-":
            continue

        match = _LEGACY_FIELD.match(content)
        if not match:
            continue
        field, value = match.groups()
        if prefix == "-":
            remember(field, value, removed)
        else:
            remember(field, value, added)

    flush()
    return changes


DIFF_BASIC = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,7 +1,7 @@
 ---
 codd:
   node_id: req:auth
-status: draft
+status: approved
 priority: high
 ---
"""

DIFF_MULTI_FIELD = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,9 +1,9 @@
 ---
 codd:
   node_id: req:auth
-status: draft
-priority: high
+status: approved
+priority: low
 owner: alice
 ---
"""

DIFF_FIELD_ADDED = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,6 +1,7 @@
 ---
 codd:
   node_id: req:auth
 status: draft
+reviewer: bob
 ---
"""

DIFF_FIELD_REMOVED = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,7 +1,6 @@
 ---
 codd:
   node_id: req:auth
 status: draft
-deadline: 2026-07-01
 ---
"""

DIFF_MULTI_FILE = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,5 +1,5 @@
 ---
-status: draft
+status: approved
 priority: high
 ---
diff --git a/docs/design/auth.md b/docs/design/auth.md
index 3333333..4444444 100644
--- a/docs/design/auth.md
+++ b/docs/design/auth.md
@@ -1,4 +1,4 @@
 ---
-status: old
+status: new
 ---
diff --git a/requirements/billing.md b/requirements/billing.md
index 5555555..6666666 100644
--- a/requirements/billing.md
+++ b/requirements/billing.md
@@ -1,4 +1,4 @@
 ---
-priority: low
+priority: high
 ---
"""

DIFF_BODY_ONLY = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,8 +1,8 @@
 ---
 codd:
   node_id: req:auth
 status: draft
 ---
-Old body line: text
+New body line: text
"""


@pytest.mark.parametrize(
    "diff_text",
    [
        pytest.param("", id="empty"),
        pytest.param(DIFF_BASIC, id="basic-field-change"),
        pytest.param(DIFF_MULTI_FIELD, id="multi-field-change"),
        pytest.param(DIFF_FIELD_ADDED, id="field-added"),
        pytest.param(DIFF_FIELD_REMOVED, id="field-removed"),
        pytest.param(DIFF_MULTI_FILE, id="multi-file-with-path-filter"),
        pytest.param(DIFF_BODY_ONLY, id="body-only-change"),
    ],
)
def test_parse_frontmatter_diff_matches_legacy_parser(diff_text):
    """Equivalence proof: new parser == legacy hand parser, fixture by fixture."""
    expected = _legacy_parse_frontmatter_changes(diff_text)

    assert parse_frontmatter_diff(diff_text, path_filter=_is_requirement_path) == expected
    # The retained require_propagate wrapper must agree too.
    assert _parse_frontmatter_changes(diff_text) == expected


def test_parse_frontmatter_diff_basic_expected_values():
    """Pin the absolute expected output (not just oracle agreement)."""
    assert _parse_frontmatter_changes(DIFF_BASIC) == [
        {
            "file": "docs/requirements/auth.md",
            "field": "status",
            "old": "draft",
            "new": "approved",
        }
    ]


def test_parse_frontmatter_diff_multi_file_filter():
    changes = _parse_frontmatter_changes(DIFF_MULTI_FILE)
    assert [c["file"] for c in changes] == [
        "docs/requirements/auth.md",
        "requirements/billing.md",  # docs/design/ filtered out
    ]


def test_parse_frontmatter_diff_without_filter_sees_all_files():
    changes = parse_frontmatter_diff(DIFF_MULTI_FILE)
    assert [c["file"] for c in changes] == [
        "docs/requirements/auth.md",
        "docs/design/auth.md",
        "requirements/billing.md",
    ]


def test_parse_frontmatter_diff_quote_change_is_not_a_change():
    """Deliberate upgrade over the legacy regex parser: YAML semantics.

    ``status: "draft"`` → ``status: draft`` is a formatting-only edit; the
    YAML value is identical, so no change is reported (the legacy parser
    compared raw line text and would have flagged it).
    """
    diff_text = """diff --git a/docs/requirements/auth.md b/docs/requirements/auth.md
index 1111111..2222222 100644
--- a/docs/requirements/auth.md
+++ b/docs/requirements/auth.md
@@ -1,5 +1,5 @@
 ---
-status: "draft"
+status: draft
 priority: high
 ---
"""
    assert _parse_frontmatter_changes(diff_text) == []


# ---------------------------------------------------------------------------
# Band classification drift-guard (propagator ↔ codd.confidence)
# ---------------------------------------------------------------------------

_GRID_CONFIDENCES = (0.0, 0.3, 0.49, 0.5, 0.6, 0.79, 0.8, 0.89, 0.9, 0.95, 1.0)
_GRID_EVIDENCE = (0, 1, 2, 3)


@pytest.mark.parametrize(
    "bands_config",
    [
        pytest.param({}, id="default-thresholds"),
        pytest.param(
            {
                "green": {"min_confidence": 0.80, "min_evidence_count": 1},
                "amber": {"min_confidence": 0.40},
            },
            id="custom-thresholds",
        ),
    ],
)
def test_classify_docs_by_band_agrees_with_confidence_model(
    tmp_path, monkeypatch, bands_config
):
    """Drift-guard: propagator band classification == codd.confidence on a grid."""
    grid = [(c, e) for c in _GRID_CONFIDENCES for e in _GRID_EVIDENCE]
    docs = [
        AffectedDoc(
            node_id=f"design:d{i}", path=f"docs/d{i}.md", title=f"D{i}",
            modules=[], matched_modules=[], changed_files=[],
        )
        for i in range(len(grid))
    ]
    values = {f"design:d{i}": grid[i] for i in range(len(grid))}

    monkeypatch.setattr("codd.propagator._load_graph", lambda root, cfg: object())
    monkeypatch.setattr(
        "codd.propagator._get_doc_confidence",
        lambda graph, doc: values[doc.node_id],
    )

    verified = _classify_docs_by_band(tmp_path, {}, docs, bands_config)

    green_t, green_e, amber_t = confidence.thresholds_from_config(bands_config)
    assert len(verified) == len(grid)
    for v in verified:
        expected = confidence.classify_band(
            v.confidence, v.evidence_count, green_t, green_e, amber_t
        )
        assert v.band == expected, (
            f"band drift at confidence={v.confidence}, "
            f"evidence={v.evidence_count}: {v.band} != {expected}"
        )


def test_classify_docs_by_band_no_graph_amber_agrees_with_model(tmp_path, monkeypatch):
    """The explicit no-graph→amber fallback matches classify_band(0.5, 0)."""
    monkeypatch.setattr("codd.propagator._load_graph", lambda root, cfg: None)

    docs = [
        AffectedDoc(
            node_id="design:d", path="docs/d.md", title="D",
            modules=[], matched_modules=[], changed_files=[],
        )
    ]
    verified = _classify_docs_by_band(tmp_path, {}, docs, {})

    assert [v.band for v in verified] == ["amber"]
    assert verified[0].confidence == 0.5
    assert verified[0].evidence_count == 0
    assert confidence.classify_band(0.5, 0) == "amber"


def test_thresholds_from_config_matches_legacy_manual_reads():
    """codd.confidence.thresholds_from_config == the manual `bands:` reads that
    propagate.run_impact and propagator._classify_docs_by_band used to inline."""
    for config in (
        {},
        {"bands": {}},
        {"bands": {"green": {"min_confidence": 0.85}}},
        {
            "bands": {
                "green": {"min_confidence": 0.95, "min_evidence_count": 3},
                "amber": {"min_confidence": 0.60},
            }
        },
    ):
        bands = config.get("bands", {})
        legacy = (
            bands.get("green", {}).get("min_confidence", 0.90),
            bands.get("green", {}).get("min_evidence_count", 2),
            bands.get("amber", {}).get("min_confidence", 0.50),
        )
        assert confidence.thresholds_from_config(config) == legacy
