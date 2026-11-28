"""Bytecode cache behavior for the project's feedback test command."""

import shlex
import sys
from textwrap import dedent

import pytest

from .test_implement_test_feedback import FIXED, HELPER, TEST_FILE, feedback, project_factory


PYTEST_COMMAND = shlex.join((sys.executable, "-m", "pytest", "checks", "-q", "-p", "no:cacheprovider"))


@pytest.mark.parametrize("command_prefix", [
    "PYTHONPYCACHEPREFIX=$(printf %s {cache})",
    "PYTHONPATH=./checks PYTHONPYCACHEPREFIX={cache}",
    "env PYTHONPYCACHEPREFIX={cache}",
], ids=["substitution", "assignment", "env"])
def test_feedback_preserves_cache_prefix_in_test_command(project_factory, tmp_path, command_prefix):
    cache = tmp_path / "project bytecode"
    prefix = command_prefix.format(cache=shlex.quote(str(cache)))
    # Change the file size so the shared cache cannot hide the candidate edit.
    pipeline, root, _, _ = project_factory(
        command=f"{prefix} {PYTEST_COMMAND}",
        fixes={"case": {"files": {TEST_FILE: FIXED + "\n# Updated test.\n"}}},
    )
    (root / "checks/conftest.py").write_text(
        f"import sys\n\nassert sys.pycache_prefix == {str(cache)!r}\n", encoding="utf-8",
    )

    result = pipeline.run(root)

    [record] = feedback(root)["rounds"]
    first_run = record["runs"][0]
    assert first_run["executed_cases"] == 2, first_run
    assert result.status == "success", result.error
    [attempt] = record["attempts"]
    assert attempt["decision"] == "accepted"


def test_feedback_uses_fresh_bytecode_after_restoring_snapshot(project_factory, tmp_path, monkeypatch):
    original = "def value():\n    return [7][1]\n"
    corrected = original.replace("[7][1]", "[7][0]")
    subject = dedent("""\
        from checks.support import helpers

        def test_subject():
            assert helpers.value() == 7
        """)
    # A shared cache outside the snapshot would survive a restore.
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "inherited bytecode"))
    pipeline, root, _, _ = project_factory(
        command=PYTEST_COMMAND,
        initial={"case": {TEST_FILE: subject}, "helper": {HELPER: original}},
        owned={"case": (TEST_FILE,), "helper": (HELPER,)},
        fixes={"helper": {"files": {HELPER: corrected}}},
    )
    (root / "checks/conftest.py").write_text(dedent(f"""\
        import os
        import py_compile
        from importlib.util import cache_from_source
        from pathlib import Path

        helper = {HELPER!r}
        # Both versions have the same size and mtime for bytecode validation.
        os.utime(helper, (1700000000, 1700000000))
        if not Path(cache_from_source(helper)).is_file():
            py_compile.compile(helper, doraise=True)
        """), encoding="utf-8")

    result = pipeline.run(root)

    [record] = feedback(root)["rounds"]
    [attempt] = record["attempts"]
    assert (attempt["owner"], attempt["decision"]) == ("helper", "accepted"), attempt
    assert (attempt["isolated_before"], attempt["isolated_after"]) == (1, 0)
    assert result.status == "success", result.error
