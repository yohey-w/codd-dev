import importlib

from codd.dag import DAG, Edge, Node
from codd.dag import checks as dag_checks
from codd.dag.checks.task_completion import TaskCompletionCheck


def _write(path, content=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _dag_with_plan_task(task_id="implementation_plan.md#1-1"):
    dag = DAG()
    dag.add_node(Node(id=task_id, kind="plan_task", path="docs/design/implementation_plan.md"))
    return dag, task_id


def _add_output(dag, task_id, output_path, *, exists=True, project_root=None, attributes=None):
    if exists and project_root is not None:
        _write(project_root / output_path, "ok\n")
    dag.add_node(
        Node(
            id=output_path,
            kind="impl_file",
            path=output_path,
            attributes=attributes or {},
        )
    )
    dag.add_edge(Edge(from_id=task_id, to_id=output_path, kind="produces"))


def _run(dag, tmp_path, settings=None):
    return TaskCompletionCheck().run(dag, tmp_path, settings or {})


def test_task_completion_registered(monkeypatch):
    monkeypatch.setattr(dag_checks, "_REGISTRY", {})
    module = importlib.reload(importlib.import_module("codd.dag.checks.task_completion"))

    assert dag_checks.get_registry()["task_completion"] is module.TaskCompletionCheck


def test_no_plan_tasks_pass(tmp_path):
    result = _run(DAG(), tmp_path)

    assert result.passed is True
    assert result.total_tasks == 0
    assert result.completion_rate == 1.0


def test_all_outputs_exist_pass(tmp_path):
    dag, task_id = _dag_with_plan_task()
    _add_output(dag, task_id, "src/feature.py", project_root=tmp_path)

    result = _run(dag, tmp_path)

    assert result.passed is True
    assert result.incomplete_tasks == []
    assert result.completed_tasks == 1


def test_missing_output_file_fail(tmp_path):
    dag, task_id = _dag_with_plan_task()
    _add_output(dag, task_id, "src/missing.py", exists=False, project_root=tmp_path)

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.incomplete_tasks[0].task_id == task_id
    assert result.incomplete_tasks[0].missing_outputs == ["src/missing.py"]
    assert result.incomplete_tasks[0].reason == "file_missing"


def test_no_produces_edge_fail(tmp_path):
    dag, task_id = _dag_with_plan_task()

    result = _run(dag, tmp_path)

    assert result.passed is False
    assert result.incomplete_tasks[0].task_id == task_id
    assert result.incomplete_tasks[0].missing_outputs == []
    assert result.incomplete_tasks[0].reason == "no_produces_edge"


def test_completion_rate_calculated(tmp_path):
    dag, first_task = _dag_with_plan_task("implementation_plan.md#1-1")
    dag.add_node(Node(id="implementation_plan.md#1-2", kind="plan_task"))
    _add_output(dag, first_task, "src/feature.py", project_root=tmp_path)

    result = _run(dag, tmp_path)

    assert result.total_tasks == 2
    assert result.completed_tasks == 1
    assert result.completion_rate == 0.5


def test_completion_rate_below_threshold_fail(tmp_path):
    dag, task_id = _dag_with_plan_task()
    _add_output(dag, task_id, "src/feature.py", project_root=tmp_path)
    dag.add_node(Node(id="implementation_plan.md#1-2", kind="plan_task"))

    result = _run(dag, tmp_path, {"task_completion_threshold": 0.75})

    assert result.passed is False


def test_multiple_incomplete_collected(tmp_path):
    dag, first_task = _dag_with_plan_task("implementation_plan.md#1-1")
    second_task = "implementation_plan.md#1-2"
    dag.add_node(Node(id=second_task, kind="plan_task"))
    _add_output(dag, first_task, "src/missing.py", exists=False, project_root=tmp_path)

    result = _run(dag, tmp_path)

    assert [item.task_id for item in result.incomplete_tasks] == [first_task, second_task]


def test_severity_is_red(tmp_path):
    result = _run(DAG(), tmp_path)

    assert result.severity == "red"
    assert result.check_name == "task_completion"


def test_passed_flag_true_on_complete(tmp_path):
    dag, task_id = _dag_with_plan_task()
    _add_output(dag, task_id, "src/feature.py", project_root=tmp_path)

    assert _run(dag, tmp_path).passed is True


def test_passed_flag_false_on_incomplete(tmp_path):
    dag, _task_id = _dag_with_plan_task()

    assert _run(dag, tmp_path).passed is False


def test_threshold_override_via_settings(tmp_path):
    dag, first_task = _dag_with_plan_task("implementation_plan.md#1-1")
    second_task = "implementation_plan.md#1-2"
    dag.add_node(Node(id=second_task, kind="plan_task"))
    _add_output(dag, first_task, "src/feature.py", project_root=tmp_path)

    result = _run(dag, tmp_path, {"task_completion_threshold": 0.5})

    assert result.passed is True
