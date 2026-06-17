"""Fixtures for the greenfield autopilot tests.

The keystone fixture is ``stub_ai``: an executable, vendor-neutral, scripted
AI command. It reads the prompt on stdin, dispatches on stage-specific prompt
markers, and answers with canned-but-valid payloads (wave_config YAML, design
document bodies, implementation step JSON, ``=== FILE: ===`` blocks, elicit
findings JSON). The full pipeline therefore runs requirements → generated
docs → implemented files → verify → check WITHOUT any real LLM, proving the
orchestration is AI-CLI agnostic end to end.
"""

from __future__ import annotations

from pathlib import Path
import sys
import textwrap

import pytest
import yaml


STUB_WAVE_CONFIG = {
    "1": [
        {
            "node_id": "design:core-design",
            "output": "docs/design/core_design.md",
            "title": "Core Design",
            "depends_on": [{"id": "__REQ_NODE__", "relation": "implements"}],
            "conventions": [],
            "modules": ["core"],
        }
    ],
    "2": [
        {
            "node_id": "design:cli-design",
            "output": "docs/design/cli_design.md",
            "title": "CLI Design",
            "depends_on": [{"id": "design:core-design", "relation": "implements"}],
            "conventions": [],
            "modules": ["cli"],
        }
    ],
}


STUB_SCRIPT = '''
"""Scripted stand-in for ANY text-in/text-out AI CLI (vendor-neutral)."""
import json
import pathlib
import re
import sys

PROMPT = sys.stdin.read()
LOG = pathlib.Path(__file__).with_name("stub_calls.log")

WAVE_CONFIG_YAML = """__WAVE_CONFIG__"""


def log(kind):
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(kind + "\\n")


def main():
    if "You are initializing CoDD wave_config" in PROMPT:
        log("plan_init")
        node = re.search(r'node_id:\\s*"?(req:[^"\\s]+)"?', PROMPT)
        req_node = node.group(1) if node else "req:requirements"
        sys.stdout.write(WAVE_CONFIG_YAML.replace("__REQ_NODE__", req_node))
        return
    if "You are writing a CoDD" in PROMPT:
        log("generate")
        sys.stdout.write(design_document())
        return
    if "You are generating implementation code" in PROMPT:
        log("implement")
        sys.stdout.write(implementation_files())
        return
    if "V-model plan derivation assistant" in PROMPT:
        log("plan_derive")
        sys.stdout.write(json.dumps({"tasks": [
            {
                "id": "implement_core_module",
                "title": "Implement the core module",
                "description": "Create the core add operation described by the core design document.",
                "source_design_doc": "docs/design/core_design.md",
                "v_model_layer": "detailed",
                "expected_outputs": ["src"],
                "test_kinds": ["unit"],
                "dependencies": [],
            },
            {
                "id": "implement_cli_module",
                "title": "Implement the CLI front end",
                "description": "Create the thin command-line front end described by the CLI design document.",
                "source_design_doc": "docs/design/cli_design.md",
                "v_model_layer": "detailed",
                "expected_outputs": ["src"],
                "test_kinds": ["unit"],
                "dependencies": ["implement_core_module"],
            },
        ]}))
        return
    if "implementation depth deriver" in PROMPT:
        log("impl_steps")
        sys.stdout.write(json.dumps({"steps": [{
            "id": "implement_core_module",
            "kind": "code",
            "rationale": "Implement the core module described by the design document.",
            "source_design_section": "1. Overview",
            "target_path_hint": "src/core",
            "expected_outputs": ["src/core/core.py"],
            "required_axes": [],
        }]}))
        return
    if "Elicitation Prompt" in PROMPT or "lexicon_coverage_report" in PROMPT:
        log("elicit")
        sys.stdout.write(json.dumps({"findings": [], "lexicon_coverage_report": {}}))
        return
    log("other")
    sys.stdout.write(json.dumps({"findings": [], "lexicon_coverage_report": {}}))


def design_document():
    title_match = re.search(r"^Title: (.+)$", PROMPT, re.M)
    title = title_match.group(1).strip() if title_match else "Document"
    headings = []
    seen_marker = False
    for line in PROMPT.splitlines():
        if "Use these section headings exactly once and in this order:" in line:
            seen_marker = True
            continue
        if seen_marker:
            if line.startswith("## "):
                headings.append(line)
            elif headings:
                break
    if not headings:
        headings = ["## 1. Overview"]
    parts = ["# " + title, ""]
    for index, heading in enumerate(headings):
        parts.append(heading)
        parts.append("")
        parts.append(
            "Concrete content for %s: the stub system stores records in SQLite, "
            "exposes an add(a, b) operation through src/core, and ships a thin "
            "command-line front end." % title
        )
        parts.append("")
        if index == 0:
            parts.extend(["```mermaid", "graph TD; CORE[Core] --> CLI[CLI];", "```", ""])
    return "\\n".join(parts)


def implementation_files():
    match = re.search(r"^Output paths: (.+)$", PROMPT, re.M)
    output = match.group(1).split(",")[0].strip() if match else "src/core"
    log("output:" + output)
    # A-core: the harness OWNS the topology — the implement output root is the
    # src-layout PACKAGE (``src/<package_name>``), so source lands INSIDE the
    # package and tests import it PACKAGE-ABSOLUTELY. Derive the package token
    # from the output path (segment after ``src/``); fall back to the leaf.
    parts = [p for p in output.replace("\\\\", "/").strip("/").split("/") if p]
    package = parts[1] if len(parts) >= 2 and parts[0] == "src" else (parts[-1] if parts else "app")
    if "Design node: docs/design/cli_design.md" in PROMPT:
        # Second derived task: a DIFFERENT module written into the SAME
        # canonical package root — the two tasks must coexist, not fragment
        # into per-task src/<task_id>/ app copies.
        return (
            "=== FILE: " + output + "/cli.py ===\\n"
            "```python\\n"
            "def main():\\n"
            "    return 0\\n"
            "```\\n"
        )
    # FX3 + A-core: the build must contain something verify can EXECUTE, AND
    # source + tests must be COHERENT (share one package context). The stub emits
    # the module INSIDE the package and a real pytest file under tests/ that
    # imports it PACKAGE-ABSOLUTELY (``from <package>.core import add``). The
    # greenfield verify stage scaffolds the runnable pyproject (editable package +
    # importlib mode, NO pythonpath ".") and then proves the autopilot actually
    # RAN the generated tests instead of certifying an unexecuted build.
    return (
        "=== FILE: " + output + "/core.py ===\\n"
        "```python\\n"
        "def add(a, b):\\n"
        "    return a + b\\n"
        "```\\n"
        "=== FILE: tests/test_core.py ===\\n"
        "```python\\n"
        "from " + package + ".core import add\\n"
        "\\n"
        "\\n"
        "def test_add():\\n"
        "    assert add(2, 3) == 5\\n"
        "```\\n"
        "=== FILE: .github/workflows/ci.yml ===\\n"
        "```yaml\\n"
        "name: ci\\n"
        "on:\\n"
        "  push:\\n"
        "  pull_request:\\n"
        "jobs:\\n"
        "  test:\\n"
        "    runs-on: ubuntu-latest\\n"
        "    steps:\\n"
        "      - uses: actions/checkout@v4\\n"
        "```\\n"
    )


main()
'''


@pytest.fixture()
def stub_ai(tmp_path: Path) -> dict[str, object]:
    """Write the scripted AI command and return its command string + call log."""
    script = tmp_path / "stub_ai_cli.py"
    wave_config_yaml = yaml.safe_dump(STUB_WAVE_CONFIG, sort_keys=False)
    script.write_text(STUB_SCRIPT.replace("__WAVE_CONFIG__", wave_config_yaml), encoding="utf-8")
    log = tmp_path / "stub_calls.log"
    return {
        "command": f"{sys.executable} {script}",
        "log": log,
        "calls": lambda: log.read_text(encoding="utf-8").split() if log.exists() else [],
    }


def _package_name(project_name: str) -> str:
    """Derive the package identifier the layout profile uses (``stub-app`` -> ``stub_app``)."""
    from codd.project_types import normalize_package_name

    return normalize_package_name(project_name)


def make_stub_project(
    tmp_path: Path,
    ai_command: str,
    *,
    name: str = "stub-app",
    greenfield_config: dict | None = None,
) -> Path:
    """Create a pre-initialized synthetic CoDD project wired to the stub AI."""
    project = tmp_path / name
    codd_dir = project / "codd"
    (codd_dir / "scan").mkdir(parents=True)
    (codd_dir / "reports").mkdir()
    config: dict = {
        "project": {"name": name, "language": "python"},
        "ai_command": ai_command,
        "scan": {
            "source_dirs": ["src/"],
            "test_dirs": ["tests/"],
            "doc_dirs": ["docs/"],
            "config_files": [],
            "exclude": [],
        },
        "graph": {"store": "jsonl", "path": "codd/scan"},
        # A-core: the configured output root is the src-layout PACKAGE
        # (``src/<package_name>``) so source lands inside the package and the
        # import-coherence gate passes. Package name derives from the project
        # name (``stub-app`` -> ``stub_app``).
        "implement": {
            "default_output_paths": {
                "docs/design/core_design.md": [f"src/{_package_name(name)}"]
            },
            # These greenfield-ORCHESTRATION tests use stub runners that do NOT
            # produce coherent source/tests (the implement + verify runners are
            # fakes), so the real implement-time composite oracle is not the system
            # under test here — opt it out (the documented
            # ``implement.implement_oracle: false`` escape hatch) so a stub project
            # is not gated by a real compile/import/collect over placeholder files.
            # The oracle itself is certified in tests/test_python_implement_oracle.py
            # + tests/test_implement_oracle.py; its pipeline wiring (order: oracle →
            # VB) is covered by test_vb_gate_reruns_native_oracle_after_test_repair,
            # which patches the gate.
            "implement_oracle": False,
        },
    }
    if greenfield_config is not None:
        config["greenfield"] = greenfield_config
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    write_requirements(project)
    write_ci_workflow(project)
    return project


def write_ci_workflow(project: Path) -> Path:
    """A realistic CI asset so the final `codd check` ci_health gate is green."""
    workflow = project / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(
        textwrap.dedent(
            """\
            name: ci
            on:
              push:
              pull_request:
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/checkout@v4
            """
        ),
        encoding="utf-8",
    )
    return workflow


def write_requirements(project: Path, *, slug: str = "stub-app") -> Path:
    requirements = project / "docs" / "requirements" / "requirements.md"
    requirements.parent.mkdir(parents=True, exist_ok=True)
    requirements.write_text(
        textwrap.dedent(
            f"""\
            ---
            codd:
              node_id: "req:{slug}-requirements"
              type: requirement
              status: approved
              confidence: 0.95
            ---

            # Stub App Requirements

            ## Functional requirements

            - The system stores numeric records and can add two numbers.
            - A command-line front end exposes the add operation.
            """
        ),
        encoding="utf-8",
    )
    return requirements
