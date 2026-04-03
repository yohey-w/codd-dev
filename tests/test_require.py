"""Tests for codd require."""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.require import (
    CROSS_CUTTING_CLUSTER,
    _build_frontmatter,
    build_require_prompt,
    cluster_extracted_docs,
    run_require,
)


BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "brownfield-project", "language": "python"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/design/", "docs/requirements/"],
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


def _setup_project(tmp_path: Path, *, service_boundaries: list[dict] | None = None) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()

    config = deepcopy(BASE_CONFIG)
    if service_boundaries is not None:
        config["service_boundaries"] = service_boundaries

    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    extracted_dir = codd_dir / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = extracted_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    _write_extracted_doc(
        extracted_dir / "system-context.md",
        node_id="design:extract:system-context",
        title="System Context",
        body="Project has auth and billing modules.\n",
    )
    _write_extracted_doc(
        extracted_dir / "architecture-overview.md",
        node_id="design:extract:architecture-overview",
        title="Architecture Overview",
        body="Architecture spans API and background jobs.\n",
    )
    _write_extracted_doc(
        modules_dir / "auth.md",
        node_id="design:extract:auth",
        title="auth",
        body="## Symbol Inventory\n\n- login(email, password)\n- verify_token(token)\n",
    )
    _write_extracted_doc(
        modules_dir / "billing.md",
        node_id="design:extract:billing",
        title="billing",
        body="## Symbol Inventory\n\n- create_invoice(customer_id)\n- sync_ledger()\n",
    )
    return project


def _write_extracted_doc(path: Path, *, node_id: str, title: str, body: str) -> None:
    payload = {
        "codd": {
            "node_id": node_id,
            "type": "design",
            "source": "extracted",
            "confidence": 0.75,
            "last_extracted": "2026-04-03",
        }
    }
    frontmatter = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}", encoding="utf-8")


def _load_extracted(project: Path):
    from codd.config import load_project_config
    from codd.planner import _load_extracted_documents

    config = load_project_config(project)
    return _load_extracted_documents(project, config), config


def _make_require_body(prompt: str) -> str:
    cluster_name = "Requirements"
    for line in prompt.splitlines():
        if line.startswith("  Cluster:"):
            cluster_name = line.split(":", 1)[1].strip()
            break
    return (
        f"# {cluster_name.title()} Requirements\n\n"
        "## 1. Overview\n\n"
        "This document infers requirements from extracted facts.\n\n"
        "## 2. Functional Requirements\n\n"
        "- Session-based authentication [observed]\n"
        "- Invoice generation [inferred]\n\n"
        "## 3. Non-Functional Requirements\n\n"
        "- Async/background processing support [inferred]\n\n"
        "## 4. Constraints\n\n"
        "- Python implementation with extracted-module traceability [observed]\n\n"
        "## 5. Open Questions\n\n"
        "- Human validation needed for inferred business intent.\n\n"
        "## 6. Human Review Issues\n\n"
        "- **HRI-1**: Authentication strategy — code uses sessions but config hints at JWT. [contradictory]\n"
    )


@pytest.fixture
def mock_require_ai(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_run(command, *, input, capture_output, text, check):
        calls.append({"command": command, "input": input})
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=_make_require_body(input),
            stderr="",
        )

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


def test_cluster_with_service_boundaries(tmp_path):
    project = _setup_project(
        tmp_path,
        service_boundaries=[
            {"name": "identity", "modules": ["src/services/auth/"]},
            {"name": "billing", "modules": ["src/services/billing/"]},
        ],
    )
    docs, config = _load_extracted(project)

    clusters = cluster_extracted_docs(docs, config)

    assert [doc.node_id for doc in clusters["identity"]] == ["design:extract:auth"]
    assert [doc.node_id for doc in clusters["billing"]] == ["design:extract:billing"]


def test_cluster_without_service_boundaries(tmp_path):
    project = _setup_project(tmp_path)
    docs, config = _load_extracted(project)

    clusters = cluster_extracted_docs(docs, config)

    assert [doc.node_id for doc in clusters["auth"]] == ["design:extract:auth"]
    assert [doc.node_id for doc in clusters["billing"]] == ["design:extract:billing"]


def test_cluster_always_has_cross_cutting(tmp_path):
    project = _setup_project(tmp_path)
    docs, config = _load_extracted(project)

    clusters = cluster_extracted_docs(docs, config)

    assert CROSS_CUTTING_CLUSTER in clusters
    assert [doc.node_id for doc in clusters[CROSS_CUTTING_CLUSTER]] == [
        "design:extract:architecture-overview",
        "design:extract:system-context",
    ]


def test_build_require_prompt_contains_sections(tmp_path):
    project = _setup_project(tmp_path)
    docs, config = _load_extracted(project)
    clusters = cluster_extracted_docs(docs, config)

    prompt = build_require_prompt("auth", clusters["auth"], clusters[CROSS_CUTTING_CLUSTER])

    assert "Functional Requirements" in prompt
    assert "Non-Functional Requirements" in prompt
    assert "Constraints" in prompt
    assert "[observed]" in prompt
    assert "[inferred]" in prompt
    assert "[speculative]" in prompt
    assert "Do not invent features" in prompt
    assert "login(email, password)" in prompt


def test_build_require_prompt_with_feedback(tmp_path):
    project = _setup_project(tmp_path)
    docs, config = _load_extracted(project)
    clusters = cluster_extracted_docs(docs, config)

    prompt = build_require_prompt(
        "auth",
        clusters["auth"],
        clusters[CROSS_CUTTING_CLUSTER],
        feedback="Clarify whether MFA is observed or speculative.",
    )

    assert "REVIEW FEEDBACK" in prompt
    assert "Clarify whether MFA is observed or speculative." in prompt


def test_build_frontmatter():
    frontmatter = _build_frontmatter("auth")
    payload = yaml.safe_load(frontmatter.removeprefix("---\n").removesuffix("\n---\n\n"))

    assert payload["codd"]["node_id"] == "req:auth"
    assert payload["codd"]["type"] == "requirement"
    assert payload["codd"]["confidence"] <= 0.75
    assert payload["codd"]["source"] == "codd-require"


def test_run_require_skip_existing(tmp_path, mock_require_ai):
    project = _setup_project(tmp_path)
    output_dir = project / "docs" / "requirements"
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = output_dir / "auth-requirements.md"
    existing.write_text("existing requirements\n", encoding="utf-8")

    results = run_require(project, output_dir="docs/requirements", scope="auth")

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert existing.read_text(encoding="utf-8") == "existing requirements\n"
    assert mock_require_ai == []


def test_run_require_force_overwrite(tmp_path, mock_require_ai):
    project = _setup_project(tmp_path)
    output_dir = project / "docs" / "requirements"
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = output_dir / "auth-requirements.md"
    existing.write_text("existing requirements\n", encoding="utf-8")

    results = run_require(project, output_dir="docs/requirements", scope="auth", force=True)

    assert len(results) == 1
    assert results[0].status == "generated"
    content = existing.read_text(encoding="utf-8")
    assert 'source: codd-require' in content
    assert "## 2. Functional Requirements" in content
    assert mock_require_ai[0]["command"] == ["mock-ai", "--print"]


def test_run_require_scope_filter(tmp_path, mock_require_ai):
    project = _setup_project(tmp_path)

    results = run_require(project, output_dir="docs/requirements", scope="billing")

    assert len(results) == 1
    assert results[0].node_id == "req:billing"
    assert results[0].path.name == "billing-requirements.md"
    assert "create_invoice(customer_id)" in mock_require_ai[0]["input"]
    assert "login(email, password)" not in mock_require_ai[0]["input"]


def test_run_require_no_extracted_docs_error(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(deepcopy(BASE_CONFIG), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Run 'codd extract' first"):
        run_require(project)
