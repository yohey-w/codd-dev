from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.dag import DAG, Node
from codd.dag.checks.stale_evidence import StaleEvidenceCheck
from codd.dag.evidence_snapshot import (
    collect_evidence_fingerprints,
    load_evidence_snapshot,
    write_evidence_snapshot,
)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _project_with_evidence(root: Path, content: str = "enableRuntime(true)\n") -> Path:
    _write(root / "package.json", "{}\n")
    source = _write(root / "src" / "service.ts", content)
    _write(
        root / "codd" / "codd.yaml",
        yaml.safe_dump(
            {
                "coherence": {
                    "capability_patterns": {
                        "runtime_flag_enabled": {
                            "matches": [
                                {
                                    "regex": r"enableRuntime\(true\)",
                                    "languages": ["typescript"],
                                }
                            ]
                        }
                    }
                }
            },
            sort_keys=False,
        ),
    )
    return source


def _invoke(root: Path, *args: str):
    return CliRunner().invoke(
        main,
        ["dag", "build", "--project-path", str(root), *args],
    )


def _valid_snapshot_text(source_path: str = "src/old.ts") -> str:
    return yaml.safe_dump(
        {
            "version": 1,
            "records": [
                {
                    "node_id": source_path,
                    "source_path": source_path,
                    "source_sha256": hashlib.sha256(b"old\n").hexdigest(),
                }
            ],
        },
        sort_keys=False,
    )


def test_refresh_help_explains_explicit_non_cache_boundary():
    result = CliRunner().invoke(main, ["dag", "build", "--help"])
    help_text = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "--refresh-evidence" in help_text
    assert "normal build/verify never refresh" in help_text
    assert "new comparison baseline" in help_text


def test_refresh_records_t0_and_normal_build_verify_leave_bytes_unchanged(tmp_path: Path):
    source = _project_with_evidence(tmp_path)
    refreshed = _invoke(tmp_path, "--refresh-evidence")

    assert refreshed.exit_code == 0, refreshed.output
    assert "Refreshed evidence fingerprints: 1 record(s)" in refreshed.output
    snapshot_path = tmp_path / ".codd" / "evidence_fingerprints.yaml"
    before = snapshot_path.read_bytes()
    snapshot = load_evidence_snapshot(tmp_path)
    assert snapshot.records == (
        {
            "node_id": "src/service.ts",
            "source_path": "src/service.ts",
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        },
    )

    normal_build = _invoke(tmp_path)
    normal_verify = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "stale_evidence"],
    )

    assert normal_build.exit_code == 0, normal_build.output
    assert normal_verify.exit_code == 0, normal_verify.output
    assert "PASS  stale_evidence" in normal_verify.output
    assert snapshot_path.read_bytes() == before


def test_changed_source_warns_without_implicit_refresh_then_explicit_refresh_passes(tmp_path: Path):
    source = _project_with_evidence(tmp_path)
    assert _invoke(tmp_path, "--refresh-evidence").exit_code == 0
    snapshot_path = tmp_path / ".codd" / "evidence_fingerprints.yaml"
    t0 = snapshot_path.read_bytes()
    source.write_text("enableRuntime(true) // changed but still matches\n", encoding="utf-8")

    stale = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "stale_evidence"],
    )

    assert stale.exit_code == 0, stale.output
    assert "WARN  stale_evidence" in stale.output
    assert snapshot_path.read_bytes() == t0

    refreshed = _invoke(tmp_path, "--refresh-evidence")
    clean = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "stale_evidence"],
    )
    assert refreshed.exit_code == 0, refreshed.output
    assert snapshot_path.read_bytes() != t0
    assert clean.exit_code == 0, clean.output
    assert "PASS  stale_evidence" in clean.output


def test_deleted_source_is_reported_from_snapshot_after_dag_node_disappears(tmp_path: Path):
    source = _project_with_evidence(tmp_path)
    assert _invoke(tmp_path, "--refresh-evidence").exit_code == 0
    source.unlink()

    result = CliRunner().invoke(
        main,
        ["dag", "verify", "--project-path", str(tmp_path), "--check", "stale_evidence"],
    )

    assert result.exit_code == 0, result.output
    assert "WARN  stale_evidence" in result.output
    assert "1 missing-source" in result.output


def test_source_symlink_retarget_is_stale_under_same_logical_path(tmp_path: Path):
    first = _write(tmp_path / "targets" / "first.ts", "enableRuntime(true)\n")
    second = _write(
        tmp_path / "targets" / "second.ts",
        "enableRuntime(true) // different target\n",
    )
    logical = tmp_path / "src" / "service.ts"
    logical.parent.mkdir()
    logical.symlink_to(first)
    dag = DAG()
    dag.add_node(
        Node(
            id="src/service.ts",
            kind="impl_file",
            path="src/service.ts",
            attributes={"runtime_evidence": [{"capability_kind": "runtime_flag_enabled"}]},
        )
    )
    records = collect_evidence_fingerprints(dag, tmp_path)
    assert records[0]["source_path"] == "src/service.ts"
    write_evidence_snapshot(tmp_path, records, expected_bytes=None)
    logical.unlink()
    logical.symlink_to(second)

    result = StaleEvidenceCheck(dag=DAG(), project_root=tmp_path).run()

    assert result.status == "warn"
    assert result.checked_count == 1
    assert result.warnings[0]["type"] == "stale_evidence"
    assert result.warnings[0]["source_path"] == "src/service.ts"


def test_cache_and_refresh_are_rejected_before_any_write(tmp_path: Path):
    snapshot = _write(
        tmp_path / ".codd" / "evidence_fingerprints.yaml",
        _valid_snapshot_text(),
    )
    dag_output = _write(tmp_path / ".codd" / "dag.json", "sentinel-dag\n")
    snapshot_before = snapshot.read_bytes()
    dag_before = dag_output.read_bytes()

    result = _invoke(tmp_path, "--cache", "--refresh-evidence")

    assert result.exit_code != 0
    assert "--cache cannot be combined with --refresh-evidence" in result.output
    assert snapshot.read_bytes() == snapshot_before
    assert dag_output.read_bytes() == dag_before


def test_zero_record_refresh_reports_zero_and_preserves_existing_snapshot(tmp_path: Path):
    _project_with_evidence(tmp_path, content="disableRuntime()\n")
    snapshot = _write(
        tmp_path / ".codd" / "evidence_fingerprints.yaml",
        _valid_snapshot_text(),
    )
    before = snapshot.read_bytes()

    result = _invoke(tmp_path, "--refresh-evidence")

    assert result.exit_code != 0
    assert "0 record(s); snapshot unchanged" in result.output
    assert snapshot.read_bytes() == before


def test_malformed_snapshot_blocks_refresh_before_build_and_preserves_bytes(tmp_path: Path):
    _project_with_evidence(tmp_path)
    malformed = b"version: [unterminated\n"
    snapshot = tmp_path / ".codd" / "evidence_fingerprints.yaml"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(malformed)

    result = _invoke(tmp_path, "--refresh-evidence")

    assert result.exit_code != 0
    assert "cannot parse evidence fingerprint snapshot" in result.output
    assert snapshot.read_bytes() == malformed
    assert not (tmp_path / ".codd" / "dag.json").exists()


def test_refresh_rejects_out_of_root_codd_symlink_before_build(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    _project_with_evidence(project)
    sentinel = _write(outside / "dag.json", "outside-sentinel\n")
    before = sentinel.read_bytes()
    (project / ".codd").symlink_to(outside, target_is_directory=True)

    result = _invoke(project, "--refresh-evidence")

    assert result.exit_code != 0
    assert "outside the project root" in result.output
    assert sentinel.read_bytes() == before
    assert not (outside / "evidence_fingerprints.yaml").exists()


def test_refresh_rejects_out_of_root_snapshot_symlink_before_build(tmp_path: Path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    _project_with_evidence(project)
    codd_dir = project / ".codd"
    codd_dir.mkdir()
    sentinel = _write(outside / "sentinel.yaml", _valid_snapshot_text())
    before = sentinel.read_bytes()
    (codd_dir / "evidence_fingerprints.yaml").symlink_to(sentinel)

    result = _invoke(project, "--refresh-evidence")

    assert result.exit_code != 0
    assert "outside the project root" in result.output
    assert sentinel.read_bytes() == before
    assert not (codd_dir / "dag.json").exists()


def test_refresh_rejects_dag_output_snapshot_collision_before_build(tmp_path: Path):
    _project_with_evidence(tmp_path)

    result = _invoke(
        tmp_path,
        "--refresh-evidence",
        "--output",
        ".codd/evidence_fingerprints.yaml",
    )

    assert result.exit_code != 0
    assert "must not overwrite the evidence fingerprint snapshot" in result.output
    assert not (tmp_path / ".codd" / "dag.json").exists()
    assert not (tmp_path / ".codd" / "evidence_fingerprints.yaml").exists()


def test_normal_build_rejects_reserved_snapshot_output_before_build(tmp_path: Path):
    _project_with_evidence(tmp_path)

    result = _invoke(
        tmp_path,
        "--output",
        ".codd/evidence_fingerprints.yaml",
    )

    assert result.exit_code != 0
    assert "must not overwrite the evidence fingerprint snapshot" in result.output
    assert not (tmp_path / ".codd" / "dag.json").exists()
    assert not (tmp_path / ".codd" / "evidence_fingerprints.yaml").exists()
