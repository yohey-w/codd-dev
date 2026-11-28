"""Observe individual test outcomes without changing the verify command."""

from __future__ import annotations

import ast
import json
import os
import shutil
from collections import Counter, defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from shlex import quote


_PROBE = Path(__file__).with_name("_pytest_feedback_probe.py")


@dataclass(frozen=True)
class PytestFeedbackAdapter:
    def instrument_command(self, command, scratch: Path):
        if not isinstance(command, str):
            return command
        from codd.repair.verify_runner import _SHELL_ASSIGNMENT, _shell_word_end
        # Leading shell assignments take precedence over the spawn environment.
        # Prepend without evaluating or re-quoting the user's original value.
        pieces = []
        end = 0
        while assignment := _SHELL_ASSIGNMENT.match(command, end):
            value = assignment.end()
            name = assignment.group().strip()[:-1]
            next_end = _shell_word_end(command, value)
            prefix = quote(str(scratch) + os.pathsep) if name == "PYTHONPATH" else ""
            pieces.extend((command[end:value], prefix, command[value:next_end]))
            end = next_end
        return "".join((*pieces, command[end:]))

    def environment(
        self, env: dict[str, str], scratch: Path, project_root: Path,
        source_dirs: list[str], case: str | None,
    ) -> dict[str, str]:
        # The project's provisioned interpreter need not have CoDD installed.
        shutil.copyfile(_PROBE, scratch / _PROBE.name)
        return {
            **env,
            "PYTHONPATH": os.pathsep.join(filter(None, (str(scratch), env.get("PYTHONPATH")))),
            # A snapshot can replace source bytes without changing size or mtime at
            # Python's cache resolution. Default to a private cache for this run.
            "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
            "PYTEST_PLUGINS": ",".join(filter(None, (env.get("PYTEST_PLUGINS"), _PROBE.stem))),
            "CODD_TEST_FEEDBACK_CACHE_DIR": str(scratch / "pytest-cache"),
            "CODD_TEST_FEEDBACK_ROOT": str(project_root),
            "CODD_TEST_FEEDBACK_SOURCES": json.dumps(source_dirs),
            "CODD_TEST_FEEDBACK_REPORT": str(scratch),
            "CODD_TEST_FEEDBACK_CASE": case or "",
        }

    def read(
        self, scratch: Path, returncode: int, output: str,
    ) -> tuple[dict[str, str], list[dict], str | None, dict[str, str]]:
        try:
            reports = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(scratch.glob("report-*.json"))
            ]
            if not reports:
                raise ValueError("missing reports")
        except (OSError, ValueError):
            reason = {
                126: "permission denied starting the test command",
                127: "test executable unavailable",
            }.get(returncode, "test command did not produce a case-level report")
            return {}, [], f"{reason}\n{output}".rstrip(), {}

        cases = {}
        failures = []
        unavailable = None
        collection_errors = {}
        collection_interrupted = False
        precedence = {"passed": 0, "skipped": 1, "failed": 2}
        for report in reports:
            for case, status in report["cases"].items():
                if case not in cases or precedence[status] > precedence[cases[case]]:
                    cases[case] = status
            failures.extend(report["failures"])
            collection_errors.update(report["collection_errors"])
            if report["environment"]:
                unavailable = unavailable or report["environment"][0]
            if report["exitstatus"] == 2 and report["collection_errors"] and not report["cases"]:
                collection_interrupted = True
            elif report["exitstatus"] not in (0, 1, 5):
                unavailable = unavailable or (
                    f"test command could not complete (exit {report['exitstatus']})\n{output}"
                )
        failures = [failure for failure in failures if failure["file"] not in collection_errors]
        completed = returncode in (0, 1, 5) or returncode == 2 and collection_interrupted
        if unavailable:
            return cases, failures, unavailable, collection_errors
        if f'Error importing plugin "{_PROBE.stem}"' in output:
            unavailable = f"test command could not load the feedback probe\n{output}"
        elif not completed or (returncode == 1 and not (failures or collection_errors)):
            unavailable = f"test command could not complete (exit {returncode})\n{output}"
        return cases, failures, unavailable, collection_errors

    def test_metrics(
        self, path: str, content: bytes, *, callable_assertions: Collection[str],
    ) -> tuple[int, int, int] | None:
        from codd.vb_marker_authenticity import PythonTestBlockProfile, _python_test_function_nodes

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
        assertions = _assertion_nodes(nodes, nodes, callable_assertions)
        return len(functions), sum(1 for _ in assertions), disabled

    def preserves_case_assertions(
        self, path: str, case: str, before: bytes, after: bytes, *, callable_assertions: Collection[str],
    ) -> bool:
        """Compare direct assertions for the collected test, ignoring source positions."""
        from codd.vb_marker_authenticity import PythonTestBlockProfile

        if not PythonTestBlockProfile().handles_file(path):
            return True
        _, delimiter, selection = case.partition("::")
        names = selection.partition("[")[0].split("::")
        if not delimiter or not names[-1].startswith("test_") or not all(n.isidentifier() for n in names):
            return True
        try:
            old_tree = ast.parse(before, filename=path)
            new_tree = ast.parse(after, filename=path)
        except (SyntaxError, ValueError):
            return True

        def assertions(tree):
            body = tree.body
            kinds_by_name = [ast.ClassDef] * (len(names) - 1) + [(ast.FunctionDef, ast.AsyncFunctionDef)]
            for name, kinds in zip(names, kinds_by_name):
                definition = next((node for node in reversed(body) if isinstance(node, kinds) and node.name == name), None)
                if definition is None:
                    return Counter()
                body = definition.body
            return Counter(ast.dump(node) for node in _assertion_nodes(
                list(_test_body_nodes(body)), ast.walk(tree), callable_assertions,
            ))

        return not (assertions(old_tree) - assertions(new_tree))


def _test_body_nodes(body):
    pending = list(body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _assertion_nodes(nodes, alias_nodes, callable_assertions):
    from codd.vb_marker_authenticity import (
        _WITH_ASSERT_CONTEXT_CALLEES,
        _is_py_primitive_assert_node,
        _py_callee_name,
    )

    # Keep every meaning of an alias in this file. An unrelated import
    # in another scope must not hide a possible assertion.
    aliases = defaultdict(set)
    for node in alias_nodes:
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.asname:
                    aliases[name.asname].add(name.name)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for name in node.names:
                aliases[name.asname or name.name].add(f"{node.module}.{name.name}")

    def matches(call, known_assertions):
        callee = _py_callee_name(call.func)
        name, dot, member = callee.partition(".")
        return callee in known_assertions or any(
            alias + dot + member in known_assertions for alias in aliases.get(name, ())
        )

    context_calls = {
        item.context_expr
        for node in nodes if isinstance(node, (ast.With, ast.AsyncWith))
        for item in node.items if isinstance(item.context_expr, ast.Call)
    }
    for node in nodes:
        if not isinstance(node, (ast.With, ast.AsyncWith)) and _is_py_primitive_assert_node(node):
            yield node
        if isinstance(node, ast.Call) and node not in context_calls and len(node.args) >= 2:
            if matches(node, callable_assertions):
                yield node
    yield from (call for call in context_calls if matches(call, _WITH_ASSERT_CONTEXT_CALLEES))
