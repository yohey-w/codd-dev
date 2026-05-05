from pathlib import Path

from click.testing import CliRunner

from codd.cli import main


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_codd_config(project: Path) -> None:
    _write(
        project / ".codd" / "codd.yaml",
        "project:\n"
        "  name: related-test-fixture\n"
        "scan:\n"
        "  source_dirs:\n"
        "    - codd/\n"
        "  test_dirs:\n"
        "    - tests/\n",
    )


def _write_related_fixture(project: Path) -> None:
    _write_codd_config(project)
    _write(project / "codd" / "foo.py", "def foo():\n    return 1\n")
    _write(project / "tests" / "test_foo.py", "from codd.foo import foo\n\ndef test_foo():\n    assert foo() == 1\n")


def test_test_runner_import():
    from codd.watch import test_runner

    assert callable(test_runner.find_related_tests)
    assert callable(test_runner.run_related_tests)


def test_find_related_tests_empty_dag(tmp_path):
    from codd.watch.test_runner import find_related_tests

    _write_codd_config(tmp_path)
    _write(tmp_path / "codd" / "foo.py", "def foo():\n    return 1\n")

    assert find_related_tests(tmp_path, ["codd/foo.py"]) == []


def test_find_related_tests_with_edge(tmp_path):
    from codd.watch.test_runner import find_related_tests

    _write_related_fixture(tmp_path)

    assert find_related_tests(tmp_path, ["codd/foo.py"]) == ["tests/test_foo.py"]


def test_detect_framework_pytest(tmp_path):
    from codd.watch.test_runner import detect_test_framework

    _write(tmp_path / "pyproject.toml", "[project]\nname = 'fixture'\n")

    assert detect_test_framework(tmp_path) == "pytest"


def test_detect_framework_jest(tmp_path):
    from codd.watch.test_runner import detect_test_framework

    _write(tmp_path / "package.json", '{"devDependencies": {"jest": "^29.0.0"}}')

    assert detect_test_framework(tmp_path) == "jest"


def test_detect_framework_vitest(tmp_path):
    from codd.watch.test_runner import detect_test_framework

    _write(tmp_path / "package.json", '{"devDependencies": {"vitest": "^1.0.0"}}')

    assert detect_test_framework(tmp_path) == "vitest"


def test_detect_framework_default(tmp_path):
    from codd.watch.test_runner import detect_test_framework

    assert detect_test_framework(tmp_path) == "pytest"


def test_run_related_tests_no_tests(tmp_path):
    from codd.watch.test_runner import run_related_tests

    _write_codd_config(tmp_path)
    _write(tmp_path / "codd" / "foo.py", "def foo():\n    return 1\n")

    result = run_related_tests(tmp_path, ["codd/foo.py"])

    assert result == {"status": "no_tests_found", "related": [], "exit_code": None}


def test_run_related_tests_dry_run(tmp_path):
    from codd.watch.test_runner import run_related_tests

    _write_related_fixture(tmp_path)

    result = run_related_tests(tmp_path, ["codd/foo.py"], dry_run=True)

    assert result["status"] == "dry_run"
    assert result["related"] == ["tests/test_foo.py"]
    assert result["cmd"] == "python -m pytest tests/test_foo.py -q"
    assert result["exit_code"] is None


def test_run_related_tests_framework_override(tmp_path):
    from codd.watch.test_runner import run_related_tests

    _write_related_fixture(tmp_path)

    result = run_related_tests(tmp_path, ["codd/foo.py"], settings={"test_framework": "vitest"}, dry_run=True)

    assert result["cmd"] == "npx vitest run tests/test_foo.py"


def test_cli_test_registered():
    result = CliRunner().invoke(main, ["test"])

    assert result.exit_code == 0
    assert "Use --related <file>" in result.output


def test_cli_test_help():
    result = CliRunner().invoke(main, ["test", "--help"])

    assert result.exit_code == 0
    assert "Run tests" in result.output


def test_cli_test_related_option():
    result = CliRunner().invoke(main, ["test", "--help"])

    assert "--related" in result.output


def test_cli_test_dry_run():
    result = CliRunner().invoke(main, ["test", "--help"])

    assert "--dry-run" in result.output


def test_framework_runners_dict():
    from codd.watch.test_runner import FRAMEWORK_RUNNERS

    assert {"pytest", "jest", "vitest", "bats", "go_test"} <= set(FRAMEWORK_RUNNERS)


def test_generality_gate_no_framework_hardcode(tmp_path):
    from codd.watch.test_runner import run_related_tests

    _write_related_fixture(tmp_path)

    result = run_related_tests(
        tmp_path,
        ["codd/foo.py"],
        settings={"test_framework": "custom", "test_runners": {"custom": "custom-runner {files}"}},
        dry_run=True,
    )

    assert result["cmd"] == "custom-runner tests/test_foo.py"
