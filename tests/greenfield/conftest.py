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
    if "Design node: docs/design/cli_design.md" in PROMPT:
        # Second derived task: a DIFFERENT module written into the SAME
        # canonical output root — the two tasks must coexist, not fragment
        # into per-task src/<task_id>/ app copies.
        return (
            "=== FILE: " + output + "/cli.py ===\\n"
            "```python\\n"
            "def main():\\n"
            "    return 0\\n"
            "```\\n"
        )
    # FX3: the build must contain something verify can EXECUTE. The stub
    # emits a real (trivial but executable) pytest file next to the module it
    # tests, plus a repo-root pyproject.toml whose [tool.pytest.ini_options]
    # section makes detect_test_command resolve "pytest --tb=short -q". The
    # greenfield verify stage then proves the autopilot actually RAN the
    # generated tests instead of certifying an unexecuted build.
    return (
        "=== FILE: " + output + "/core.py ===\\n"
        "```python\\n"
        "def add(a, b):\\n"
        "    return a + b\\n"
        "```\\n"
        "=== FILE: " + output + "/test_core.py ===\\n"
        "```python\\n"
        "from core import add\\n"
        "\\n"
        "\\n"
        "def test_add():\\n"
        "    assert add(2, 3) == 5\\n"
        "```\\n"
        "=== FILE: pyproject.toml ===\\n"
        "```toml\\n"
        "[tool.pytest.ini_options]\\n"
        "addopts = \\"-p no:cacheprovider\\"\\n"
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
        "implement": {"default_output_paths": {"docs/design/core_design.md": ["src/core"]}},
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
