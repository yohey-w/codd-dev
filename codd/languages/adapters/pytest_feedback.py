"""Observe individual test outcomes without changing the verify command."""

from __future__ import annotations

import ast
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PytestFeedbackAdapter:
    def environment(
        self, env: dict[str, str], scratch: Path, project_root: Path,
        source_dirs: list[str], case: str | None,
    ) -> dict[str, str]:
        # The project's provisioned interpreter need not have CoDD installed.
        plugin = Path(__file__).with_name("_pytest_feedback_probe.py")
        shutil.copyfile(plugin, scratch / plugin.name)
        return {
            **env,
            "PYTHONPATH": os.pathsep.join(filter(None, (str(scratch), env.get("PYTHONPATH")))),
            "PYTEST_PLUGINS": ",".join(filter(None, (env.get("PYTEST_PLUGINS"), plugin.stem))),
            "CODD_TEST_FEEDBACK_ROOT": str(project_root),
            "CODD_TEST_FEEDBACK_SOURCES": json.dumps(source_dirs),
            "CODD_TEST_FEEDBACK_REPORT": str(scratch / "report.json"),
            "CODD_TEST_FEEDBACK_CASE": case or "",
        }

    def read(
        self, scratch: Path, returncode: int, output: str,
    ) -> tuple[dict[str, str], list[dict[str, str]], str | None]:
        try:
            report = json.loads((scratch / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reason = {
                126: "permission denied starting the test command",
                127: "test executable unavailable",
            }.get(returncode, "test command did not produce a case-level report")
            return {}, [], f"{reason}\n{output}".rstrip()

        cases = report["cases"]
        failures = report["failures"]
        if report["environment"]:
            return cases, failures, report["environment"][0]
        if returncode not in (0, 1, 5) or (returncode == 1 and not failures):
            return cases, failures, f"test command could not complete (exit {returncode})\n{output}"
        return cases, failures, None

    def test_metrics(self, path: str, content: bytes) -> tuple[int, int, int] | None:
        from codd.vb_marker_authenticity import (
            PythonTestBlockProfile,
            _is_py_primitive_assert_node,
            _python_test_function_nodes,
        )

        if not PythonTestBlockProfile().handles_file(path):
            return None
        tree = ast.parse(content, filename=path)
        nodes = list(ast.walk(tree))
        # Count skip/xfail in helpers as well as in test bodies and decorators.
        disabled = sum(
            isinstance(node, (ast.Attribute, ast.Name))
            and (node.attr if isinstance(node, ast.Attribute) else node.id)
            in {"skip", "skipif", "skipUnless", "skipIf", "skipTest", "xfail", "expectedFailure", "importorskip"}
            for node in nodes
        )
        functions = _python_test_function_nodes(ast.unparse(tree))
        return len(functions), sum(map(_is_py_primitive_assert_node, nodes)), disabled
