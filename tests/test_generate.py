"""Tests for wave-based template generation."""

from pathlib import Path
import subprocess

from click.testing import CliRunner
import pytest
import yaml

import codd.generator as generator_module
from codd.cli import main
from codd.generator import generate_wave
from codd.scanner import _extract_frontmatter


CoDD_YAML = """
ai_command: "mock-ai --print"
project:
  name: test-project
  language: python
scan:
  source_dirs: []
  doc_dirs:
    - "docs/requirements/"
    - "docs/design/"
    - "docs/detailed_design/"
    - "docs/plan/"
    - "docs/governance/"
    - "docs/test/"
  exclude: []
graph:
  store: jsonl
  path: codd/scan
conventions:
  - targets:
      - "db:rls_policies"
    reason: "Global invariant"
wave_config:
  "1":
    - node_id: "design:acceptance-criteria"
      output: "docs/test/acceptance_criteria.md"
      title: "Acceptance Criteria"
      depends_on:
        - id: "req:lms-requirements-v2.0"
          relation: derives_from
      conventions:
        - targets:
            - "db_table:audit_logs"
          reason: "Artifact invariant"
    - node_id: "governance:decisions"
      output: "docs/governance/decisions.md"
      title: "Decisions"
      depends_on:
        - id: "req:lms-requirements-v2.0"
          relation: derives_from
  "2":
    - node_id: "design:system-design"
      output: "docs/design/system_design.md"
      title: "System Design"
      depends_on:
        - id: "design:acceptance-criteria"
          relation: constrained_by
        - id: "governance:decisions"
          relation: informed_by
  "3":
    - node_id: "design:implementation-plan"
      output: "docs/plan/implementation_plan.md"
      title: "Implementation Plan"
      depends_on:
        - id: "design:acceptance-criteria"
          relation: validates
"""


def _setup_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(CoDD_YAML, encoding="utf-8")
    requirement_path = project / "docs" / "requirements" / "lms_requirements_v2.0.md"
    requirement_path.parent.mkdir(parents=True, exist_ok=True)
    requirement_path.write_text(
        """---
codd:
  node_id: "req:lms-requirements-v2.0"
  type: "requirement"
---

# LMS Requirements

## Scope

Tenant isolation and audit logging are mandatory.
""",
        encoding="utf-8",
    )
    return project


@pytest.fixture
def mock_ai_cli(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        stdout = "# Generated Content\n\n## Overview\n\nThis document contains concrete content.\n"
        if "docs/detailed_design/" in input:
            stdout = (
                "# Generated Content\n\n"
                "## Overview\n\n"
                "This document contains concrete content.\n\n"
                "```mermaid\n"
                "flowchart TD\n"
                "    A[Source] --> B[Target]\n"
                "```\n"
            )
        calls.append(
            {
                "command": command,
                "input": input,
                "capture_output": capture_output,
                "text": text,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


def test_generate_command_creates_wave_documents_from_config(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    runner = CliRunner()

    result = runner.invoke(main, ["generate", "--wave", "1", "--path", str(project)])

    assert result.exit_code == 0
    assert (project / "docs" / "test" / "acceptance_criteria.md").exists()
    assert (project / "docs" / "governance" / "decisions.md").exists()
    assert "Generated: docs/test/acceptance_criteria.md" in result.output
    assert "Generated: docs/governance/decisions.md" in result.output
    assert mock_ai_cli[0]["command"] == ["mock-ai", "--print"]


def test_generate_frontmatter_infers_depended_by_and_inherits_conventions(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    codd = _extract_frontmatter(project / "docs" / "test" / "acceptance_criteria.md")
    assert codd is not None
    assert codd["node_id"] == "design:acceptance-criteria"
    assert codd["type"] == "test"
    assert codd["depends_on"] == [
        {
            "id": "req:lms-requirements-v2.0",
            "relation": "derives_from",
            "semantic": "governance",
        }
    ]

    depended_by = {entry["id"]: entry for entry in codd["depended_by"]}
    assert depended_by["design:system-design"]["relation"] == "constrained_by"
    assert depended_by["design:system-design"]["semantic"] == "governance"
    assert depended_by["design:implementation-plan"]["relation"] == "validates"

    reasons = {entry["reason"] for entry in codd["conventions"]}
    assert "Global invariant" in reasons
    assert "Artifact invariant" in reasons


def test_generate_skips_existing_files_without_force(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    target = project / "docs" / "test" / "acceptance_criteria.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom content\n", encoding="utf-8")

    results = generate_wave(project, 1)

    status_by_path = {result.path.relative_to(project).as_posix(): result.status for result in results}
    assert status_by_path["docs/test/acceptance_criteria.md"] == "skipped"
    assert target.read_text(encoding="utf-8") == "custom content\n"


def test_generate_force_overwrites_existing_files(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    target = project / "docs" / "test" / "acceptance_criteria.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom content\n", encoding="utf-8")

    results = generate_wave(project, 1, force=True)

    status_by_path = {result.path.relative_to(project).as_posix(): result.status for result in results}
    assert status_by_path["docs/test/acceptance_criteria.md"] == "generated"

    content = target.read_text(encoding="utf-8")
    assert content.startswith("---\ncodd:\n")
    assert "custom content" not in content
    assert "Generated Content" in content
    assert "TODO" not in content
    assert _extract_frontmatter(target)["node_id"] == "design:acceptance-criteria"


def test_generate_uses_dependency_documents_as_ai_context(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    prompt = mock_ai_cli[0]["input"]
    assert "ABSOLUTE PROHIBITION" in prompt
    assert "**Do not emit** YAML frontmatter" in prompt
    assert "CRITICAL ERROR" in prompt
    assert "release-blocking constraint" in prompt
    assert "Start directly with the document content." in prompt
    assert "Treat requirement documents as the source of truth" in prompt
    assert "Before finalizing, self-check that every capability and constraint mentioned in the depends_on documents is represented" in prompt
    assert "Use concrete tool names, framework names, services, table names, endpoints, thresholds, counts, and timelines" in prompt
    assert "Never use vague placeholders such as '推奨なし', '要検討', or 'TBD'." in prompt
    assert "Non-negotiable conventions:" in prompt
    assert "These are release-blocking constraints." in prompt
    assert "Explicitly state how the document complies with each convention and invariant listed below." in prompt
    assert "Targets: db:rls_policies" in prompt
    assert "Targets: db_table:audit_logs" in prompt
    assert "Global invariant" in prompt
    assert "Artifact invariant" in prompt
    assert "tenant isolation in security/data model sections" in prompt
    assert "--- BEGIN DEPENDENCY docs/requirements/lms_requirements_v2.0.md (req:lms-requirements-v2.0) ---" in prompt
    assert "Tenant isolation and audit logging are mandatory." in prompt
    assert "Prefer a structure that covers: Overview, Acceptance Criteria, Failure Criteria, E2E Test Generation Meta-Prompt." in prompt


def test_generate_supports_detailed_design_documents_with_mermaid_guidance(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    config_path = project / "codd" / "codd.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["wave_config"]["3"].append(
        {
            "node_id": "design:shared-domain-model",
            "output": "docs/detailed_design/shared_domain_model.md",
            "title": "Shared Domain Model",
            "depends_on": [
                {
                    "id": "design:system-design",
                    "relation": "depends_on",
                    "semantic": "technical",
                }
            ],
            "conventions": [
                {
                    "targets": ["module:auth", "db:rls_policies"],
                    "reason": "Canonical ownership of shared types must be explicit before implementation.",
                }
            ],
        }
    )
    config_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")

    generate_wave(project, 1)
    generate_wave(project, 2)
    generate_wave(project, 3)

    detailed_doc = project / "docs" / "detailed_design" / "shared_domain_model.md"
    assert detailed_doc.exists()
    assert _extract_frontmatter(detailed_doc)["type"] == "design"

    prompt = mock_ai_cli[-1]["input"]
    assert "docs/detailed_design/" in prompt
    assert "downstream-ready detailed design document" in prompt
    assert "Use Mermaid diagrams" in prompt
    assert "prevent reimplementation drift" in prompt


def test_generate_command_allows_ai_cmd_override(tmp_path, mock_ai_cli):
    project = _setup_project(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["generate", "--wave", "1", "--path", str(project), "--ai-cmd", "custom-ai --print"],
    )

    assert result.exit_code == 0
    assert mock_ai_cli[0]["command"] == ["custom-ai", "--print"]


def test_sanitize_generated_body_removes_meta_preamble_code_fence_and_duplicate_title():
    raw_body = """```markdown
The docs/design directory doesn't exist yet. Since the user asked me to write the document body (not save to a file), I'll output the complete Markdown document body directly.

---

# External Integration Design (LINE/Stripe/Bunny Stream)

## Overview

Concrete design content.
```"""

    sanitized = generator_module._sanitize_generated_body(
        "External Integration Design (LINE/Stripe/Bunny Stream)",
        raw_body,
    )

    assert sanitized == (
        "# External Integration Design (LINE/Stripe/Bunny Stream)\n\n"
        "## Overview\n\n"
        "Concrete design content.\n"
    )


def test_sanitize_generated_body_removes_leading_meta_line_without_heading():
    sanitized = generator_module._sanitize_generated_body(
        "System Architecture Design",
        "I'll write the complete Markdown body directly.\n\n## Overview\n\nDetails.\n",
    )

    assert sanitized == "# System Architecture Design\n\n## Overview\n\nDetails.\n"


def test_sanitize_generated_body_removes_inline_context_meta_preamble():
    raw_body = """
The dependency documents provided inline contain all the context needed. Let me now write the external integrations design document.

# External Integration Design

## Overview

Concrete design content.
"""

    sanitized = generator_module._sanitize_generated_body("External Integration Design", raw_body)

    assert sanitized == "# External Integration Design\n\n## Overview\n\nConcrete design content.\n"


def test_sanitize_generated_body_removes_docs_directory_meta_preamble():
    raw_body = """
The docs directory exists but is empty (the dependency documents were provided as inline context). Now let me write the implementation plan. I'll output the document body directly as requested.

## Overview

Milestones.
"""

    sanitized = generator_module._sanitize_generated_body("Implementation Plan", raw_body)

    assert sanitized == "# Implementation Plan\n\n## Overview\n\nMilestones.\n"


def test_sanitize_generated_body_removes_heres_meta_preamble():
    raw_body = """
Here's the architecture decisions document:

# Architecture Decision Record

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("Architecture Decision Record", raw_body)

    assert sanitized == "# Architecture Decision Record\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_meta_line_and_duplicate_title_after_heading():
    raw_body = """
# UX/UI Design

No existing file found. I'll now write the UX/UI design document based on the dependency documents.

# UX/UI Design

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("UX/UI Design", raw_body)

    assert sanitized == "# UX/UI Design\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_meta_line_inside_body_section():
    raw_body = """
# UX/UI Design

## Overview

No existing file found. I'll now write the UX/UI design document based on the dependency documents.

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("UX/UI Design", raw_body)

    assert sanitized == "# UX/UI Design\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_codex_existing_file_meta_block():
    raw_body = """
# ADR: Multi-tenant Strategy

The existing file already has complete content with YAML frontmatter. I need to write just the document body.

The existing document already covers all the key areas from the requirements.

- Author権限問題 → covered
- テナント間データ分離 → covered

# ADR: Multi-tenant Strategy

## Overview

Concrete content.
"""

    sanitized = generator_module._sanitize_generated_body("ADR: Multi-tenant Strategy", raw_body)

    assert sanitized == "# ADR: Multi-tenant Strategy\n\n## Overview\n\nConcrete content.\n"


def test_sanitize_generated_body_removes_japanese_created_file_meta_line():
    raw_body = """
# 共有ドメインモデル詳細設計書

`docs/detailed_design/shared_domain_model.md` を作成しました。

## 1. Overview

Concrete content.

```mermaid
flowchart TD
    A[Auth] --> B[Tenant]
```
"""

    sanitized = generator_module._sanitize_generated_body(
        "共有ドメインモデル詳細設計書",
        raw_body,
        output_path="docs/detailed_design/shared_domain_model.md",
    )

    assert sanitized == (
        "# 共有ドメインモデル詳細設計書\n\n"
        "## 1. Overview\n\n"
        "Concrete content.\n\n"
        "```mermaid\n"
        "flowchart TD\n"
        "    A[Auth] --> B[Tenant]\n"
        "```\n"
    )


def test_sanitize_generated_body_rejects_unstructured_detailed_design_summary():
    raw_body = """
# 共有ドメインモデル詳細設計書

`docs/detailed_design/shared_domain_model.md` を作成しました。

主要な構成:

1. **共有 Enum/型定義**
2. **共有契約**
"""

    with pytest.raises(ValueError, match="missing section headings"):
        generator_module._sanitize_generated_body(
            "共有ドメインモデル詳細設計書",
            raw_body,
            output_path="docs/detailed_design/shared_domain_model.md",
        )


def test_sanitize_generated_body_rejects_detailed_design_without_mermaid():
    raw_body = """
# リクエストライフサイクル詳細設計書

## 1. Overview

Concrete content without diagrams.
"""

    with pytest.raises(ValueError, match="without Mermaid diagrams"):
        generator_module._sanitize_generated_body(
            "リクエストライフサイクル詳細設計書",
            raw_body,
            output_path="docs/detailed_design/request_lifecycle_sequences.md",
        )


# --- _resolve_ai_command per-command tests ---


def test_resolve_ai_command_uses_default_when_no_config():
    result = generator_module._resolve_ai_command({}, None)
    assert result == generator_module.DEFAULT_AI_COMMAND


def test_resolve_ai_command_cli_override_takes_precedence():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "per-cmd-ai"},
    }
    result = generator_module._resolve_ai_command(config, "cli-override", command_name="generate")
    assert result == "cli-override"


def test_resolve_ai_command_per_command_overrides_global():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "opus-for-design"},
    }
    result = generator_module._resolve_ai_command(config, None, command_name="generate")
    assert result == "opus-for-design"


def test_resolve_ai_command_falls_back_to_global_when_command_not_in_ai_commands():
    config = {
        "ai_command": "global-ai",
        "ai_commands": {"generate": "opus-for-design"},
    }
    result = generator_module._resolve_ai_command(config, None, command_name="implement")
    assert result == "global-ai"


def test_resolve_ai_command_falls_back_to_default_when_no_ai_commands_dict():
    config = {"ai_command": "global-ai"}
    result = generator_module._resolve_ai_command(config, None, command_name="generate")
    assert result == "global-ai"


def test_resolve_ai_command_rejects_empty_string():
    with pytest.raises(ValueError, match="ai_command must be a non-empty string"):
        generator_module._resolve_ai_command({"ai_command": ""}, None)


def test_resolve_ai_command_per_command_rejects_empty_string():
    config = {"ai_commands": {"generate": "  "}}
    with pytest.raises(ValueError, match="ai_command must be a non-empty string"):
        generator_module._resolve_ai_command(config, None, command_name="generate")


# --- _normalize_section_headings tests ---


def test_normalize_section_headings_noop_when_h2_exists():
    body = "# Title\n\n## Overview\n\nContent.\n"
    assert generator_module._normalize_section_headings(body) == body


def test_normalize_section_headings_promotes_h3_to_h2():
    body = "# Title\n\n### 1. Overview\n\nContent.\n\n### 2. Scope\n\nMore content.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## 1. Overview" in result
    assert "## 2. Scope" in result
    assert "### " not in result


def test_normalize_section_headings_demotes_non_title_h1_to_h2():
    body = "# Title\n\n# Overview\n\nContent.\n\n# Architecture\n\nMore.\n"
    result = generator_module._normalize_section_headings(body)
    assert result.startswith("# Title\n")  # title preserved
    assert "## Overview" in result
    assert "## Architecture" in result


def test_normalize_section_headings_promotes_bold_pseudo_headings():
    body = "# Title\n\n**1. Overview**\n\nContent.\n\n**2. Acceptance Criteria**\n\nCriteria.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## 1. Overview" in result
    assert "## 2. Acceptance Criteria" in result
    assert "**1." not in result


def test_normalize_section_headings_preserves_fenced_code_blocks():
    body = "# Title\n\n### Overview\n\n```bash\n### this is a comment\n```\n\n### Scope\n\nEnd.\n"
    result = generator_module._normalize_section_headings(body)
    assert "## Overview" in result
    assert "## Scope" in result
    assert "### this is a comment" in result  # inside fence, unchanged


def test_normalize_section_headings_returns_unchanged_when_no_patterns():
    body = "# Title\n\nJust plain text without any section structure.\n"
    assert generator_module._normalize_section_headings(body) == body


def test_sanitize_normalizes_h3_before_validation():
    """Integration: _sanitize_generated_body no longer rejects h3-only output."""
    raw_body = "# Acceptance Criteria\n\n### 1. Overview\n\nContent.\n\n### 2. Acceptance Criteria\n\nCriteria.\n\n### 3. Failure Criteria\n\nFailures.\n"
    result = generator_module._sanitize_generated_body("Acceptance Criteria", raw_body)
    assert "## 1. Overview" in result
    assert "## 2. Acceptance Criteria" in result
    assert "## 3. Failure Criteria" in result


# -- Test code output detection --

def test_is_test_code_output_detects_spec_ts():
    assert generator_module._is_test_code_output("tests/e2e/auth.spec.ts") is True
    assert generator_module._is_test_code_output("tests/e2e/browser-nav.spec.js") is True
    assert generator_module._is_test_code_output("tests/test_auth.test.py") is True


def test_is_test_code_output_rejects_non_test_files():
    assert generator_module._is_test_code_output("docs/test/strategy.md") is False
    assert generator_module._is_test_code_output("tests/e2e/helpers/auth.ts") is False
    assert generator_module._is_test_code_output("src/app.ts") is False


def test_sanitize_generated_body_skips_markdown_checks_for_test_code():
    """Test code output should not be forced into Markdown structure."""
    code = 'import { test, expect } from "@playwright/test";\n\ntest("it works", async ({ page }) => {\n  await page.goto("/");\n});\n'
    result = generator_module._sanitize_generated_body("E2E Tests", code, output_path="tests/e2e/auth.spec.ts")
    assert "import" in result
    assert "# E2E Tests" not in result  # No Markdown heading injected


def test_render_document_uses_comment_headers_for_test_code():
    """Test code output should use // comment headers, not YAML frontmatter."""
    artifact = generator_module.WaveArtifact(
        wave=9,
        node_id="test:e2e-browser",
        output="tests/e2e/browser.spec.ts",
        title="Browser Tests",
        depends_on=[{"id": "design:auth", "relation": "depends_on"}],
        conventions=[],
    )
    body = 'import { test } from "@playwright/test";\n\ntest("smoke", async ({ page }) => {});\n'
    result = generator_module._render_document(artifact, [], [], body)
    assert result.startswith("// @generated-from: design:auth")
    assert "// @codd-node-id: test:e2e-browser" in result
    assert "---" not in result  # No YAML frontmatter
    assert "import" in result


def test_generate_test_document_includes_design_to_test_traceability(tmp_path, mock_ai_cli):
    """Test documents must include design-to-test traceability instructions."""
    project = _setup_project(tmp_path)

    generate_wave(project, 1)

    # Wave 1 includes acceptance_criteria.md which is a test doc
    prompt = mock_ai_cli[0]["input"]
    assert "Design-to-test traceability" in prompt
    assert "verifiable behaviors" in prompt
    assert "traceability section" in prompt


def test_render_document_strips_markdown_fences_from_test_code():
    """If AI wraps code in ```typescript fences, they should be stripped."""
    artifact = generator_module.WaveArtifact(
        wave=9,
        node_id="test:e2e",
        output="tests/e2e/auth.spec.ts",
        title="Auth Tests",
        depends_on=[],
        conventions=[],
    )
    body = '```typescript\nimport { test } from "@playwright/test";\ntest("ok", async () => {});\n```'
    result = generator_module._render_document(artifact, [], [], body)
    assert "```" not in result
    assert "import" in result
