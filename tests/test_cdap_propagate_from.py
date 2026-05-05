from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from codd.cli import main
from codd.dag import DAG, Edge, Node
from codd.watch.propagation_log import read_propagation_log
from codd.watch.propagation_pipeline import PropagationResult, run_propagation_pipeline


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node(id="src/index.ts", kind="impl_file", path="src/index.ts"))
    dag.add_node(Node(id="docs/design/api.md", kind="design_doc", path="docs/design/api.md"))
    dag.add_edge(Edge(from_id="docs/design/api.md", to_id="src/index.ts", kind="expects"))
    return dag


def _patch_build_dag(monkeypatch, dag: DAG | None = None):
    monkeypatch.setattr("codd.dag.builder.build_dag", lambda project_root, settings=None: dag or _dag())


def _patch_runtime_steps(monkeypatch):
    monkeypatch.setattr(
        "codd.propagator.run_propagate",
        lambda *args, **kwargs: SimpleNamespace(updated=["docs/design/api.md"], affected_docs=[]),
    )
    monkeypatch.setattr("codd.dag.runner.run_all_checks", lambda *args, **kwargs: [object(), object()])
    monkeypatch.setattr(
        "codd.fixer.run_fix",
        lambda *args, **kwargs: SimpleNamespace(fixed=True, attempts=[object()]),
    )
    monkeypatch.setattr("codd.drift.run_drift", lambda *args, **kwargs: SimpleNamespace(drift=[]))


def test_propagation_pipeline_import():
    from codd.watch import propagation_pipeline

    assert propagation_pipeline.run_propagation_pipeline is run_propagation_pipeline


def test_propagation_result_default_fields():
    result = PropagationResult()

    assert result.impacted_nodes == []
    assert result.propagated_count == 0
    assert result.fixed_count == 0
    assert result.drift_events == []
    assert result.errors == []
    assert result.success is True


def test_run_pipeline_empty_files(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)

    result = run_propagation_pipeline(tmp_path, [], dry_run=True)

    assert result.impacted_nodes == []


def test_run_pipeline_nonexistent_project(tmp_path):
    result = run_propagation_pipeline(tmp_path / "missing", ["src/index.ts"], dry_run=True)

    assert result.success is False
    assert result.errors


def test_run_pipeline_dry_run_no_write(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)
    monkeypatch.setattr(
        "codd.propagator.run_propagate",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("propagate should not run")),
    )
    monkeypatch.setattr(
        "codd.fixer.run_fix",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("fix should not run")),
    )

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"], dry_run=True)

    assert result.impacted_nodes == ["docs/design/api.md", "src/index.ts"]
    assert not (tmp_path / ".codd" / "propagation_log.jsonl").exists()


def test_run_pipeline_returns_result(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"], dry_run=True)

    assert isinstance(result, PropagationResult)


def test_pipeline_logs_to_propagation_log(tmp_path, monkeypatch):
    _write(tmp_path / "codd" / "codd.yaml", "project:\n  name: demo\n")
    _patch_build_dag(monkeypatch)
    _patch_runtime_steps(monkeypatch)

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"])

    entries = read_propagation_log(tmp_path)
    assert result.log_written is True
    assert len(entries) == 1
    assert entries[0]["files"] == ["src/index.ts"]
    assert entries[0]["propagation_result"]["impacted_nodes"] == ["docs/design/api.md", "src/index.ts"]


def test_pipeline_impact_analysis(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"], dry_run=True)

    assert result.impacted_nodes == ["docs/design/api.md", "src/index.ts"]


def test_pipeline_error_handling(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)
    monkeypatch.setattr(
        "codd.propagator.run_propagate",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("codd.dag.runner.run_all_checks", lambda *args, **kwargs: [])
    monkeypatch.setattr("codd.fixer.run_fix", lambda *args, **kwargs: SimpleNamespace(fixed=True, attempts=[]))

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"])

    assert any("propagate: boom" in error for error in result.errors)
    assert result.success is False


def test_cli_propagate_from_registered():
    assert "propagate-from" in main.commands


def test_cli_propagate_from_help():
    result = CliRunner().invoke(main, ["propagate-from", "--help"])

    assert result.exit_code == 0
    assert "--files" in result.output


def test_cli_propagate_from_requires_files(tmp_path):
    result = CliRunner().invoke(main, ["propagate-from", "--project-path", str(tmp_path)])

    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_cli_propagate_from_source_options(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.watch.propagation_pipeline.run_propagation_pipeline",
        lambda *args, **kwargs: PropagationResult(impacted_nodes=["src/index.ts"]),
    )

    for source in ["watch", "git_hook", "editor_hook", "manual"]:
        result = CliRunner().invoke(
            main,
            ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts", "--source", source],
        )
        assert result.exit_code == 0


def test_cli_propagate_from_editor_options(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.watch.propagation_pipeline.run_propagation_pipeline",
        lambda *args, **kwargs: PropagationResult(impacted_nodes=["src/index.ts"]),
    )

    for editor in ["claude", "codex", "manual"]:
        result = CliRunner().invoke(
            main,
            ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts", "--editor", editor],
        )
        assert result.exit_code == 0


def test_cli_propagate_from_dry_run(tmp_path, monkeypatch):
    calls = []

    def fake_pipeline(project_root, files, settings=None, dry_run=False, event=None):
        calls.append({"dry_run": dry_run})
        return PropagationResult()

    monkeypatch.setattr("codd.watch.propagation_pipeline.run_propagation_pipeline", fake_pipeline)

    result = CliRunner().invoke(
        main,
        ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts", "--dry-run"],
    )

    assert result.exit_code == 0
    assert calls == [{"dry_run": True}]


def test_cli_propagate_from_outputs_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "codd.watch.propagation_pipeline.run_propagation_pipeline",
        lambda *args, **kwargs: PropagationResult(
            impacted_nodes=["a", "b"],
            propagated_count=3,
            fixed_count=1,
        ),
    )

    result = CliRunner().invoke(main, ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts"])

    assert result.exit_code == 0
    assert "Impacted nodes: 2" in result.output
    assert "Propagated: 3" in result.output
    assert "Fixed: 1" in result.output


def test_file_change_event_created_in_cli(tmp_path, monkeypatch):
    calls = []

    def fake_pipeline(project_root, files, settings=None, dry_run=False, event=None):
        calls.append(event)
        return PropagationResult()

    monkeypatch.setattr("codd.watch.propagation_pipeline.run_propagation_pipeline", fake_pipeline)

    result = CliRunner().invoke(
        main,
        [
            "propagate-from",
            "--project-path",
            str(tmp_path),
            "--files",
            "src/index.ts",
            "--source",
            "editor_hook",
            "--editor",
            "codex",
        ],
    )

    assert result.exit_code == 0
    assert calls[0].files == ["src/index.ts"]
    assert calls[0].source == "editor_hook"
    assert calls[0].editor == "codex"


def test_propagation_log_appended_after_cli(tmp_path, monkeypatch):
    _write(tmp_path / "codd" / "codd.yaml", "project:\n  name: demo\n")
    _patch_build_dag(monkeypatch)
    _patch_runtime_steps(monkeypatch)

    result = CliRunner().invoke(main, ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts"])

    assert result.exit_code == 0
    assert len(read_propagation_log(tmp_path)) == 1


def test_propagate_from_multiple_files(tmp_path, monkeypatch):
    calls = []

    def fake_pipeline(project_root, files, settings=None, dry_run=False, event=None):
        calls.append(files)
        return PropagationResult()

    monkeypatch.setattr("codd.watch.propagation_pipeline.run_propagation_pipeline", fake_pipeline)

    result = CliRunner().invoke(
        main,
        [
            "propagate-from",
            "--project-path",
            str(tmp_path),
            "--files",
            "src/index.ts",
            "--files",
            "docs/design/api.md",
        ],
    )

    assert result.exit_code == 0
    assert calls == [["src/index.ts", "docs/design/api.md"]]


def test_pipeline_success_flag_true_on_success(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)
    _patch_runtime_steps(monkeypatch)

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"])

    assert result.success is True


def test_pipeline_success_flag_false_on_error(tmp_path, monkeypatch):
    _patch_build_dag(monkeypatch)
    monkeypatch.setattr(
        "codd.dag.runner.run_all_checks",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verify failed")),
    )
    monkeypatch.setattr("codd.propagator.run_propagate", lambda *args, **kwargs: SimpleNamespace(updated=[]))
    monkeypatch.setattr("codd.fixer.run_fix", lambda *args, **kwargs: SimpleNamespace(fixed=True, attempts=[]))

    result = run_propagation_pipeline(tmp_path, ["src/index.ts"])

    assert result.success is False
    assert any("verify: verify failed" in error for error in result.errors)


def test_propagate_from_project_path_option(tmp_path, monkeypatch):
    calls = []

    def fake_pipeline(project_root, files, settings=None, dry_run=False, event=None):
        calls.append(project_root)
        return PropagationResult()

    monkeypatch.setattr("codd.watch.propagation_pipeline.run_propagation_pipeline", fake_pipeline)

    result = CliRunner().invoke(main, ["propagate-from", "--project-path", str(tmp_path), "--files", "src/index.ts"])

    assert result.exit_code == 0
    assert calls == [tmp_path.resolve()]
