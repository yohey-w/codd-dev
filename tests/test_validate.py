"""Tests for codd validate."""

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.validator import validate_project


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "test-project", "language": "python"},
    "scan": {
        "source_dirs": [],
        "test_dirs": [],
        "doc_dirs": ["docs/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {
        "green": {"min_confidence": 0.90, "min_evidence_count": 2},
        "amber": {"min_confidence": 0.50},
    },
    "propagation": {"max_depth": 10},
}


def _setup_project(tmp_path, docs: dict[str, str], wave_config=None):
    project = tmp_path / "project"
    project.mkdir()
    docs_dir = project / "docs"
    docs_dir.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = dict(BASE_CONFIG)
    if wave_config is not None:
        config["wave_config"] = wave_config
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))

    for relative_path, content in docs.items():
        doc_path = project / relative_path
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(content)

    return project, codd_dir


def test_validate_error_when_frontmatter_missing(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {"docs/requirements.md": "# missing frontmatter\n"},
    )

    result = validate_project(project, codd_dir)

    assert result.error_count == 1
    assert any(issue.code == "missing_frontmatter" for issue in result.issues)


def test_validate_error_when_depends_on_dangles(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/system.md": """---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "design:missing-design"
      relation: derives_from
---

# system
""",
        },
    )

    result = validate_project(project, codd_dir)

    assert any(issue.code == "dangling_depends_on" for issue in result.issues)
    assert result.exit_code == 1


def test_validate_marks_wave_config_forward_reference_as_blocked(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
---

# Requirements
""",
            "docs/system.md": """---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "req:project-requirements"
      relation: implements
  depended_by:
    - id: "design:database-design"
      relation: informs
---

# System
""",
        },
        wave_config={
            "waves": [
                {
                    "wave": 2,
                    "nodes": [
                        {
                            "node_id": "design:system-design",
                            "depends_on": [{"id": "req:project-requirements"}],
                        },
                        {
                            "node_id": "design:database-design",
                            "depends_on": [{"id": "design:system-design"}],
                        },
                    ],
                }
            ]
        },
    )

    result = validate_project(project, codd_dir)

    assert result.error_count == 0
    assert result.blocked_count == 2
    assert result.exit_code == 0
    assert result.status() == "BLOCKED"
    assert any(
        issue.code == "dangling_depended_by" and issue.level == "BLOCKED"
        for issue in result.issues
    )


def test_validate_marks_missing_wave_config_output_as_blocked(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
---

# Requirements
""",
        },
        wave_config={
            "1": [
                {
                    "node_id": "design:system-design",
                    "output": "docs/system.md",
                    "title": "System",
                    "depends_on": [{"id": "req:project-requirements"}],
                }
            ]
        },
    )

    result = validate_project(project, codd_dir)

    assert result.error_count == 0
    assert result.blocked_count == 1
    assert any(issue.code == "wave_config_missing_node" and issue.level == "BLOCKED" for issue in result.issues)


def test_validate_warns_for_requirement_references_to_implementation_phase_nodes(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
  depends_on:
    - id: "module:auth"
      relation: specifies
    - id: "db_table:users"
      relation: requires
    - id: "design:auth-service"
      relation: specifies
---

# Requirements
""",
        },
    )
    codd_config = yaml.safe_load((codd_dir / "codd.yaml").read_text())
    codd_config["service_boundaries"] = [{"name": "auth", "modules": ["src/services/auth/"]}]
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(codd_config, sort_keys=False, allow_unicode=True))

    result = validate_project(project, codd_dir)

    assert result.error_count == 0
    assert result.warning_count == 3
    assert all(issue.level == "WARNING" for issue in result.issues)


def test_validate_allows_convention_targets_present_in_scanned_nodes(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/example.md": """---
codd:
  node_id: "design:example"
  type: design
  conventions:
    - targets:
        - "module:my_module"
      reason: "Must match the scanned module"
---

# Example
""",
        },
    )
    scan_dir = codd_dir / "scan"
    scan_dir.mkdir()
    (scan_dir / "nodes.jsonl").write_text(
        '{"id": "module:my_module", "type": "module", "name": "module:my_module"}\n'
    )

    result = validate_project(project, codd_dir)

    assert not any(issue.code == "dangling_convention" for issue in result.issues)
    assert result.status() == "OK"
    assert result.exit_code == 0


def test_validate_error_when_cycle_exists(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/a.md": """---
codd:
  node_id: "design:a"
  type: design
  depends_on:
    - id: "design:b"
      relation: derives_from
  depended_by:
    - id: "design:b"
      relation: derives_from
---

# A
""",
            "docs/b.md": """---
codd:
  node_id: "design:b"
  type: design
  depends_on:
    - id: "design:a"
      relation: derives_from
  depended_by:
    - id: "design:a"
      relation: derives_from
---

# B
""",
        },
    )

    result = validate_project(project, codd_dir)

    assert any(issue.code == "circular_dependency" for issue in result.issues)
    assert result.exit_code == 1


def test_validate_ok_when_documents_are_consistent(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
  depended_by:
    - id: "design:system-design"
      relation: specifies
---

# Requirements
""",
            "docs/system.md": """---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "req:project-requirements"
      relation: implements
---

# System
""",
        },
    )

    result = validate_project(project, codd_dir)

    assert result.status() == "OK"
    assert result.exit_code == 0


def test_validate_allows_plan_and_operations_node_prefixes(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/plan.md": """---
codd:
  node_id: "plan:implementation-plan"
  type: plan
  depended_by:
    - id: "operations:runbook"
      relation: informs
---

# Plan
""",
            "docs/runbook.md": """---
codd:
  node_id: "operations:runbook"
  type: operations
  depends_on:
    - id: "plan:implementation-plan"
      relation: derives_from
---

# Runbook
""",
        },
    )

    result = validate_project(project, codd_dir)

    assert result.status() == "OK"
    assert result.exit_code == 0


def test_validate_error_when_wave_config_mismatches_depends_on(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
  depended_by:
    - id: "design:system-design"
      relation: specifies
---

# Requirements
""",
            "docs/decisions.md": """---
codd:
  node_id: "governance:decisions"
  type: governance
---

# Decisions
""",
            "docs/system.md": """---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "req:project-requirements"
      relation: implements
---

# System
""",
        },
        wave_config={
            "waves": [
                {
                    "wave": 2,
                    "nodes": [
                        {
                            "node_id": "design:system-design",
                            "depends_on": [
                                {"id": "req:project-requirements"},
                                {"id": "governance:decisions"},
                            ],
                        }
                    ],
                }
            ]
        },
    )

    result = validate_project(project, codd_dir)

    assert any(issue.code == "wave_config_mismatch" for issue in result.issues)
    assert result.exit_code == 1


def test_validate_cli_reports_ok_status(tmp_path):
    project, _ = _setup_project(
        tmp_path,
        {
            "docs/requirements.md": """---
codd:
  node_id: "req:project-requirements"
  type: requirement
  depended_by:
    - id: "design:system-design"
      relation: specifies
---

# Requirements
""",
            "docs/system.md": """---
codd:
  node_id: "design:system-design"
  type: design
  depends_on:
    - id: "req:project-requirements"
      relation: implements
---

# System
""",
        },
    )

    runner = CliRunner()
    cli_result = runner.invoke(main, ["validate", "--path", str(project)])

    assert cli_result.exit_code == 0
    assert "OK:" in cli_result.output


def test_validate_rejects_unknown_prefix_without_config(tmp_path):
    project, codd_dir = _setup_project(
        tmp_path,
        {
            "docs/kb.md": """---
codd:
  node_id: "knowledge:domain-model"
  type: design
---

# Knowledge Base
""",
        },
    )

    result = validate_project(project, codd_dir)

    assert result.error_count == 1
    assert any("invalid_node_id" in i.code for i in result.issues)


def test_validate_accepts_custom_prefix_from_config(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = dict(BASE_CONFIG)
    config["prefixes"] = ["knowledge", "schema", "review"]
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    )

    (project / "docs" / "kb.md").write_text("""---
codd:
  node_id: "knowledge:domain-model"
  type: design
---

# Knowledge Base
""")
    (project / "docs" / "api.md").write_text("""---
codd:
  node_id: "schema:api-spec"
  type: design
---

# API Schema
""")

    result = validate_project(project, codd_dir)

    assert result.status() == "OK"
    assert result.error_count == 0


def test_validate_custom_prefix_merged_with_defaults(tmp_path):
    """Custom prefixes add to defaults, not replace them."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = dict(BASE_CONFIG)
    config["prefixes"] = ["knowledge"]
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    )

    (project / "docs" / "design.md").write_text("""---
codd:
  node_id: "design:system-design"
  type: design
---

# Design
""")
    (project / "docs" / "kb.md").write_text("""---
codd:
  node_id: "knowledge:insights"
  type: design
---

# Knowledge
""")

    result = validate_project(project, codd_dir)

    assert result.status() == "OK"
    assert result.documents_checked == 2


def test_validate_rejects_invalid_custom_prefix_format(tmp_path):
    """Prefixes with uppercase or special chars are silently ignored."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "docs").mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = dict(BASE_CONFIG)
    config["prefixes"] = ["Valid-Not", "UPPER", "ok_prefix"]
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True)
    )

    # ok_prefix should work, the others should be ignored
    (project / "docs" / "good.md").write_text("""---
codd:
  node_id: "ok_prefix:my-doc"
  type: design
---

# Good
""")

    result = validate_project(project, codd_dir)
    assert result.status() == "OK"
