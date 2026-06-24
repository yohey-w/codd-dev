"""Tests for the negative_space DAG check (declared forbidden-evidence scan).

This check does **not** make a general absence guarantee ("no PII exists").
It only inspects the forbidden evidence a project *explicitly declares*: for
each declaration it resolves the scope globs, scans the matched files for the
declared regex patterns, and reports hits. The honest claim is never "the data
is clean", only "within the declared scan scope, N forbidden-pattern hits".

Severity model:

* hit(s) found AND ``on_violation: fail`` explicitly declared -> red
  (logically derivable: the project itself declared this a deploy blocker).
* hit(s) found with default / ``warn`` -> amber (visibility, not a blocker).
* scope declares no paths -> amber ``malformed_negative_space``.
* scope resolves to 0 files -> amber ``vacuous`` (never a clean pass).
* no declaration at all -> skip (checked_count=0, skipped=True).

The fixtures write real files under ``tmp_path`` and include them in the scope
so the regex scan exercises the real filesystem path (resolution + traversal
guard), mirroring how the check runs in a project tree.
"""

from __future__ import annotations

from pathlib import Path

from codd.dag import DAG, Node
from codd.dag.checks import get_registry
from codd.dag.checks.negative_space import NegativeSpaceCheck


def _dag(*nodes: Node) -> DAG:
    dag = DAG()
    for node in nodes:
        dag.add_node(node)
    return dag


def _config(forbidden_evidence) -> dict:
    """Mirror the project-side ``negative_space.forbidden_evidence`` location."""
    if forbidden_evidence is None:
        return {}
    return {"negative_space": {"forbidden_evidence": forbidden_evidence}}


def _run(forbidden_evidence, project_root):
    config = _config(forbidden_evidence)
    return NegativeSpaceCheck(
        dag=_dag(), project_root=project_root, settings=config
    ).run(codd_config=config)


def _warnings_of_type(result, diagnostic_type: str) -> list[dict]:
    return [w for w in result.warnings if w.get("type") == diagnostic_type]


def test_negative_space_registered():
    assert get_registry()["negative_space"] is NegativeSpaceCheck


# Fixture 1 — scope resolves to real files, zero hits -> pass (checked_count>0).
def test_files_scanned_no_hits_passes(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "clean.py").write_text("x = 1\nprint('ok')\n")
    (tmp_path / "src" / "also_clean.py").write_text("y = 2\n")

    result = _run(
        [
            {
                "id": "no_secret_token",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "pass"
    assert result.passed is True
    assert result.skipped is False
    assert result.block_deploy is False
    assert result.severity == "amber"
    assert result.checked_count == 2  # two .py files scanned
    assert result.warnings == []


# Fixture 2 — a hit with on_violation: warn -> amber, does not block deploy.
def test_hit_with_warn_is_amber(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "app.txt").write_text("user logged in\nSECRET_KEY=abc123\n")

    result = _run(
        [
            {
                "id": "no_secret_in_logs",
                "scope": {"paths": ["logs/**/*.txt"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "warn",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 1
    hits = _warnings_of_type(result, "forbidden_evidence_hit")
    assert len(hits) == 1
    entry = hits[0]
    assert entry["declaration_id"] == "no_secret_in_logs"
    assert entry["pattern"] == "secret"
    assert entry["severity"] == "amber"
    assert entry["hit_count"] >= 1


# Fixture 3 — a hit with on_violation: fail -> red, blocks deploy.
def test_hit_with_fail_is_red(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text("API = 'x'\nSECRET_KEY = 'leak'\n")

    result = _run(
        [
            {
                "id": "no_secret_in_source",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "fail"
    assert result.severity == "red"
    assert result.passed is False
    assert result.block_deploy is True
    assert result.checked_count == 1
    hits = _warnings_of_type(result, "forbidden_evidence_hit")
    assert len(hits) == 1
    assert hits[0]["declaration_id"] == "no_secret_in_source"
    assert hits[0]["severity"] == "red"


# Fixture 4 — scope resolves to 0 files -> vacuous amber (not a clean pass).
def test_scope_resolves_zero_files_is_vacuous_amber(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("ok = True\n")

    result = _run(
        [
            {
                "id": "no_secret_in_missing_dir",
                "scope": {"paths": ["does_not_exist/**/*.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    vacuous = _warnings_of_type(result, "vacuous")
    assert len(vacuous) == 1
    assert vacuous[0]["declaration_id"] == "no_secret_in_missing_dir"


# --- no_usable_patterns: scope is valid but nothing is scannable -----------
# A declaration that resolves real files but declares no compilable pattern has
# verified nothing, yet pre-fix it fell through to a clean PASS (checked_count=0
# with no diagnostic) — a vacuous false-green. It must surface amber, never pass.


def test_scope_valid_but_empty_patterns_is_no_usable_patterns_amber(tmp_path: Path):
    # scope matches a real file, but patterns: [] (no usable pattern at all).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("ok = True\n")

    result = _run(
        [
            {
                "id": "declared_but_no_patterns",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    # Must NOT be a clean pass: a scope with no usable pattern verified nothing.
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    nup = _warnings_of_type(result, "no_usable_patterns")
    assert len(nup) == 1
    assert nup[0]["declaration_id"] == "declared_but_no_patterns"
    # The vacuous diagnostic is NOT also emitted (the cause is missing patterns,
    # not an empty scope), and nothing was scanned.
    assert _warnings_of_type(result, "vacuous") == []


def test_scope_valid_but_pattern_regex_missing_is_no_usable_patterns_amber(
    tmp_path: Path,
):
    # scope matches a real file; a pattern entry exists but its regex is absent.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("ok = True\n")

    result = _run(
        [
            {
                "id": "pattern_without_regex",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "secret"}],  # regex key missing entirely
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    nup = _warnings_of_type(result, "no_usable_patterns")
    assert len(nup) == 1
    assert nup[0]["declaration_id"] == "pattern_without_regex"
    assert _warnings_of_type(result, "vacuous") == []


def test_normal_pattern_still_passes_unchanged(tmp_path: Path):
    # Regression guard: a well-formed declaration with a usable pattern and no
    # hit is still a clean PASS (no no_usable_patterns diagnostic introduced).
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "clean.py").write_text("value = 42\n")

    result = _run(
        [
            {
                "id": "normal_decl",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "pass"
    assert result.passed is True
    assert result.checked_count == 1
    assert result.warnings == []
    assert _warnings_of_type(result, "no_usable_patterns") == []


def test_invalid_regex_only_does_not_also_emit_no_usable_patterns(tmp_path: Path):
    # When the sole pattern is present but fails to compile, the existing
    # invalid_regex diagnostic already covers it. no_usable_patterns must NOT
    # double-report the same declaration.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.py").write_text("hello\n")

    result = _run(
        [
            {
                "id": "only_bad_regex",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "broken", "regex": "["}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert len(_warnings_of_type(result, "invalid_regex")) == 1
    assert _warnings_of_type(result, "no_usable_patterns") == []


# --- Guard / edge coverage (beyond the 4 mandated fixtures) ----------------


def test_no_declaration_skips(tmp_path: Path):
    # no negative_space section at all -> skip (the forbidden_evidence key is
    # absent, so the project never opted in).
    result = _run(None, project_root=tmp_path)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.checked_count == 0
    assert result.warnings == []

    # Note: an *empty* forbidden_evidence list ([]) is NOT a skip — the key is
    # present (the project opted in) but declares nothing, which is a malformed
    # declaration (amber). That distinct case is covered by
    # test_forbidden_evidence_empty_list_is_malformed_amber.

    # None config entirely -> skip (no key present).
    none_result = NegativeSpaceCheck(
        dag=_dag(), project_root=tmp_path, settings=None
    ).run(codd_config=None)
    assert none_result.skipped is True
    assert none_result.status == "skip"


def test_forbidden_evidence_mapping_is_malformed_amber(tmp_path: Path):
    # forbidden_evidence declared as a mapping (not a list of declarations).
    # Pre-fix this fell through to SKIP (treated as "no declaration") — a
    # vacuous false-green: the project explicitly declared the key yet nothing
    # was checked and no diagnostic was raised. It must surface amber.
    config = {
        "negative_space": {
            "forbidden_evidence": {
                "id": "single",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
            }
        }
    }
    result = NegativeSpaceCheck(
        dag=_dag(), project_root=tmp_path, settings=config
    ).run(codd_config=config)

    # NOT a skip, NOT a clean pass: a malformed-but-declared container.
    assert result.skipped is False
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    malformed = _warnings_of_type(result, "malformed_negative_space")
    assert len(malformed) == 1


def test_forbidden_evidence_empty_list_is_malformed_amber(tmp_path: Path):
    # forbidden_evidence declared as an empty list. The key IS present (the
    # project opted in) but contains zero usable declarations. Pre-fix this fell
    # through to SKIP — a vacuous false-green. It must surface amber, distinct
    # from "no declaration (key absent)".
    config = {"negative_space": {"forbidden_evidence": []}}
    result = NegativeSpaceCheck(
        dag=_dag(), project_root=tmp_path, settings=config
    ).run(codd_config=config)

    assert result.skipped is False
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    malformed = _warnings_of_type(result, "malformed_negative_space")
    assert len(malformed) == 1


def test_forbidden_evidence_list_without_mapping_entries_is_malformed_amber(
    tmp_path: Path,
):
    # forbidden_evidence is a list, but contains no mapping entry (e.g. scalars).
    # The key is present yet no usable declaration exists. Pre-fix this fell
    # through to SKIP — vacuous false-green. Must surface amber.
    config = {
        "negative_space": {"forbidden_evidence": ["not_a_mapping", 42, None]}
    }
    result = NegativeSpaceCheck(
        dag=_dag(), project_root=tmp_path, settings=config
    ).run(codd_config=config)

    assert result.skipped is False
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    assert result.checked_count == 0
    malformed = _warnings_of_type(result, "malformed_negative_space")
    assert len(malformed) == 1


def test_forbidden_evidence_key_absent_still_skips(tmp_path: Path):
    # Regression: the key itself is absent (negative_space present but no
    # forbidden_evidence key, and negative_space absent entirely). Both remain
    # skip — "no declaration" must stay distinct from "declared but malformed".
    no_key = {"negative_space": {"something_else": 1}}
    result = NegativeSpaceCheck(
        dag=_dag(), project_root=tmp_path, settings=no_key
    ).run(codd_config=no_key)
    assert result.skipped is True
    assert result.status == "skip"
    assert result.passed is True
    assert result.checked_count == 0
    assert result.warnings == []

    # negative_space section absent entirely -> still skip.
    empty_cfg = _run(None, project_root=tmp_path)
    assert empty_cfg.skipped is True
    assert empty_cfg.status == "skip"
    assert empty_cfg.warnings == []


def test_scope_without_paths_is_malformed_amber(tmp_path: Path):
    result = _run(
        [{"id": "broken", "scope": {}, "patterns": [{"name": "s", "regex": "x"}]}],
        project_root=tmp_path,
    )
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    malformed = _warnings_of_type(result, "malformed_negative_space")
    assert len(malformed) == 1
    assert malformed[0]["declaration_id"] == "broken"


def test_invalid_regex_is_amber_not_swallowed(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "f.py").write_text("hello\n")
    result = _run(
        [
            {
                "id": "bad_regex_decl",
                "scope": {"paths": ["src/**/*.py"]},
                "patterns": [{"name": "broken", "regex": "["}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )
    # Compile error must surface as amber, never crash and never red.
    assert result.status == "warn"
    assert result.severity == "amber"
    assert result.passed is True
    assert result.block_deploy is False
    bad = _warnings_of_type(result, "invalid_regex")
    assert len(bad) == 1
    assert bad[0]["declaration_id"] == "bad_regex_decl"
    assert bad[0]["pattern"] == "broken"
    assert bad[0]["error"]


def test_path_traversal_outside_root_is_rejected(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "inside.py").write_text("clean\n")
    # A secret file lives OUTSIDE the project root.
    outside = tmp_path / "outside_secret.py"
    outside.write_text("SECRET_KEY = 'leak'\n")

    result = _run(
        [
            {
                "id": "traversal_attempt",
                "scope": {"paths": ["../outside_secret.py"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=root,
    )

    # The out-of-root file must not be scanned: no hit, and a traversal
    # diagnostic must be raised (not silently swallowed). It resolves to 0
    # in-scope files -> vacuous amber, never red.
    assert result.passed is True
    assert result.block_deploy is False
    assert _warnings_of_type(result, "forbidden_evidence_hit") == []
    traversal = _warnings_of_type(result, "path_outside_root")
    assert len(traversal) == 1


def test_binary_unreadable_file_is_skipped_diagnostic(tmp_path: Path):
    (tmp_path / "data").mkdir()
    # A clean text file plus a binary blob with embedded NULs.
    (tmp_path / "data" / "ok.bin").write_text("plain text no hit\n")
    (tmp_path / "data" / "blob.bin").write_bytes(b"\x00\x01\x02\xff\xfe SECRET_KEY \x00")

    result = _run(
        [
            {
                "id": "scan_blobs",
                "scope": {"paths": ["data/**/*.bin"]},
                "patterns": [{"name": "secret", "regex": r"SECRET_[A-Z]+"}],
                "on_violation": "fail",
            }
        ],
        project_root=tmp_path,
    )

    # Binary file is skipped (not decoded as text), so no red from it.
    assert result.passed is True
    assert result.block_deploy is False
    skipped = _warnings_of_type(result, "skipped")
    assert len(skipped) == 1
    assert "blob.bin" in skipped[0]["path"]
