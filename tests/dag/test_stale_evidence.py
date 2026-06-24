"""Tests for the stale_evidence DAG check (fingerprint-only freshness).

``stale_evidence`` is a **diagnostic-only, amber-only** check. It never gates a
deploy and it never uses wall-clock / mtime — those are environment-dependent and
would produce false reds. It only acts when an evidence payload carries a
recorded ``source_sha256`` fingerprint: if the referenced ``source_path`` still
exists and its current sha256 differs from the recorded one, the evidence is
``stale_evidence`` (amber). Evidence without a recorded hash is left alone (no
``freshness_not_provable`` noise), and a missing source file is reported as
``source_missing`` amber, not red.

The 4 fixtures (per the design spec, section "9. stale_evidence"):
  1. recorded hash == current hash      -> pass, no warning
  2. recorded hash != current hash      -> stale_evidence amber
  3. evidence with no recorded hash      -> no warning
  4. mtime only older/newer (hash same)  -> no warning
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.stale_evidence import StaleEvidenceCheck


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _evidence_node(node_id: str, evidence: list[dict]) -> Node:
    # Mirror the builder's runtime-evidence carrier: an impl-file node whose
    # attributes hold an evidence payload list.
    return Node(
        id=node_id,
        kind="impl_file",
        path=node_id,
        attributes={"runtime_evidence": evidence},
    )


def _run(dag: DAG, project_root: Path):
    return StaleEvidenceCheck(dag=dag, project_root=project_root).run(
        dag=dag, project_root=project_root
    )


def _warnings_of_type(result, warning_type: str) -> list[dict]:
    return [w for w in result.warnings if w.get("type") == warning_type]


def test_stale_evidence_registered():
    assert get_registry()["stale_evidence"] is StaleEvidenceCheck


# Fixture 1 — recorded hash matches the current file content: pass, no warning.
def test_matching_fingerprint_passes(tmp_path: Path):
    src = tmp_path / "src" / "module.py"
    src.parent.mkdir(parents=True)
    content = "def handler():\n    return 1\n"
    src.write_text(content, encoding="utf-8")

    node = _evidence_node(
        "src/module.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/module.py",
                "source_sha256": _sha256(content),
                "generated_at": "2026-01-01T00:00:00Z",
                "tool": "extractor",
            }
        ],
    )
    result = _run(_dag(node), tmp_path)

    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.block_deploy is False
    assert result.severity == "amber"
    assert result.checked_count == 1
    assert result.warnings == []


# Fixture 2 — recorded hash differs from current content: stale_evidence amber.
def test_mismatched_fingerprint_warns_amber(tmp_path: Path):
    src = tmp_path / "src" / "module.py"
    src.parent.mkdir(parents=True)
    src.write_text("def handler():\n    return 2  # edited\n", encoding="utf-8")

    node = _evidence_node(
        "src/module.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/module.py",
                # hash recorded against the *old* content
                "source_sha256": _sha256("def handler():\n    return 1\n"),
                "tool": "extractor",
            }
        ],
    )
    result = _run(_dag(node), tmp_path)

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 1

    stale = _warnings_of_type(result, "stale_evidence")
    assert len(stale) == 1
    entry = stale[0]
    assert entry["severity"] == "amber"
    assert entry["source_path"] == "src/module.py"
    assert entry["recorded_sha256"] == _sha256("def handler():\n    return 1\n")
    assert entry["current_sha256"] != entry["recorded_sha256"]
    assert entry.get("remediation")
    # never red
    assert _warnings_of_type(result, "source_missing") == []


# Fixture 3 — evidence carries no recorded hash: no warning at all
# (do not emit a freshness_not_provable; that would be noise).
def test_no_recorded_hash_no_warning(tmp_path: Path):
    src = tmp_path / "src" / "module.py"
    src.parent.mkdir(parents=True)
    src.write_text("anything at all\n", encoding="utf-8")

    node = _evidence_node(
        "src/module.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/module.py",
                # no source_sha256 key
                "generated_at": "2026-01-01T00:00:00Z",
            }
        ],
    )
    result = _run(_dag(node), tmp_path)

    # Nothing was fingerprint-checkable -> the check has no checkable evidence
    # and must not warn. Either skip or pass with checked_count == 0; never warn.
    assert result.warnings == []
    assert result.status in {"pass", "skip"}
    assert result.passed is True
    assert result.checked_count == 0


# Fixture 4 — only the mtime differs (hash still matches): no warning.
# Proves the check is fingerprint-driven, not wall-clock / mtime driven.
def test_mtime_only_change_no_warning(tmp_path: Path):
    src = tmp_path / "src" / "module.py"
    src.parent.mkdir(parents=True)
    content = "def handler():\n    return 1\n"
    src.write_text(content, encoding="utf-8")

    recorded_hash = _sha256(content)

    node = _evidence_node(
        "src/module.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/module.py",
                "source_sha256": recorded_hash,
                # an old generated_at timestamp must NOT, on its own, warn
                "generated_at": "1999-01-01T00:00:00Z",
                "tool": "extractor",
            }
        ],
    )

    # Make the file *much* newer than the recorded generated_at by bumping mtime
    # forward, and also test an old mtime — neither must produce a warning while
    # the content (hash) is unchanged.
    future = time.time() + 10_000
    os.utime(src, (future, future))
    result_future = _run(_dag(node), tmp_path)
    assert result_future.warnings == []
    assert result_future.status == "pass"
    assert result_future.checked_count == 1

    past = time.time() - 10_000_000
    os.utime(src, (past, past))
    node_again = _evidence_node(
        "src/module.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/module.py",
                "source_sha256": recorded_hash,
                "generated_at": "2999-01-01T00:00:00Z",
                "tool": "extractor",
            }
        ],
    )
    result_past = _run(_dag(node_again), tmp_path)
    assert result_past.warnings == []
    assert result_past.status == "pass"
    assert result_past.checked_count == 1


# Extra guard — a recorded hash but the source file is gone: source_missing amber,
# never stale_evidence and never red.
def test_missing_source_file_is_source_missing_amber(tmp_path: Path):
    node = _evidence_node(
        "src/gone.py",
        [
            {
                "capability_kind": "handler",
                "source_path": "src/gone.py",
                "source_sha256": _sha256("whatever"),
            }
        ],
    )
    result = _run(_dag(node), tmp_path)

    assert result.severity == "amber"
    assert result.block_deploy is False
    assert result.passed is True
    missing = _warnings_of_type(result, "source_missing")
    assert len(missing) == 1
    assert missing[0]["severity"] == "amber"
    assert missing[0]["source_path"] == "src/gone.py"
    # a missing file is NOT counted as stale
    assert _warnings_of_type(result, "stale_evidence") == []


# Forward-guard — a DAG with evidence that carries no fingerprints anywhere
# (the current real-world state: runtime_evidence has no source_sha256) skips.
def test_no_fingerprinted_evidence_skips(tmp_path: Path):
    node = _evidence_node(
        "src/module.py",
        [
            # shaped like today's real runtime_evidence: no source_sha256
            {
                "capability_kind": "handler",
                "value": "true",
                "line_ref": "src/module.py:1",
                "source": "capability_patterns",
            }
        ],
    )
    empty = Node(id="src/other.py", kind="impl_file", path="src/other.py")
    result = _run(_dag(node, empty), tmp_path)

    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.checked_count == 0
    assert result.warnings == []
