"""R2: all-evidence brownfield restoration with provenance + confidence + open-questions.

These tests assert that:
  * deterministic test_details reach the restore prompt (tests as requirements
    evidence),
  * IaC-derived NFR candidates + structured infra_config reach the
    infra/NFR/operations restoration prompts,
  * restored docs carry structured provenance + confidence-band + open_questions
    frontmatter (lifted from a machine-readable block),
  * absence of evidence steers toward open_questions rather than assertion,
  * capability gating suppresses infra/ops evidence for pure library/CLI types,
  * greenfield `codd generate` output is byte-for-byte unchanged.

The AI is mocked via the existing subprocess injection point used by
``tests/test_restore.py`` (``generator_module.subprocess.run``).
"""

from __future__ import annotations

import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

import codd.generator as generator_module
from codd.generator import WaveArtifact, extract_restoration_meta
from codd.planner import ExtractedDocument
from codd.restore import (
    EvidenceBundle,
    _assemble_evidence_bundle,
    _build_restoration_prompt,
    _collect_rationale_docs,
    _collect_test_evidence,
    _infra_ops_evidence_relevant,
    restore_wave,
)
from codd.iac_nfr import NfrCandidate
from codd.project_types import ProjectCapabilities


# ---------------------------------------------------------------------------
# Fixture: a REAL brownfield project on disk (source + tests + IaC + README)
# so restore's deterministic evidence assembly has something to consume.
# ---------------------------------------------------------------------------

WAVE_CONFIG = {
    "0": [
        {
            "node_id": "req:inferred-requirements",
            "output": "docs/requirements/inferred_requirements.md",
            "title": "Inferred Requirements",
            "modules": ["billing"],
            "depends_on": [
                {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"}
            ],
            "conventions": [],
        }
    ],
    "1": [
        {
            "node_id": "nfr:non-functional-requirements",
            "output": "docs/requirements/non_functional_requirements.md",
            "title": "Non-Functional Requirements",
            "modules": ["billing"],
            "depends_on": [
                {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"}
            ],
            "conventions": [],
        }
    ],
    "2": [
        {
            "node_id": "ops:operations-runbook",
            "output": "docs/operations/operations_runbook.md",
            "title": "Operations Runbook",
            "modules": ["billing"],
            "depends_on": [
                {"id": "design:extract:system-context", "relation": "derives_from", "semantic": "technical"}
            ],
            "conventions": [],
        }
    ],
}

BASE_CONFIG = {
    "version": "0.1.0",
    "project": {"name": "shopcart", "language": "python", "type": "web"},
    "ai_command": "mock-ai --print",
    "scan": {
        "source_dirs": ["src"],
        "test_dirs": ["tests"],
        "doc_dirs": ["docs/requirements/", "docs/operations/", "docs/design/"],
        "config_files": [],
        "exclude": [],
    },
    "graph": {"store": "jsonl", "path": "codd/scan"},
    "bands": {
        "green": {"min_confidence": 0.90, "min_evidence_count": 2},
        "amber": {"min_confidence": 0.50},
    },
    "propagation": {"max_depth": 10},
    "wave_config": WAVE_CONFIG,
}


def _setup_brownfield_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()

    # codd config
    codd_dir = project / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(deepcopy(BASE_CONFIG), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Real source module
    src = project / "src" / "billing"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "service.py").write_text(
        "class BillingService:\n"
        "    def charge(self, amount: int) -> bool:\n"
        "        return amount > 0\n",
        encoding="utf-8",
    )

    # Real tests (the richest functional-requirements evidence)
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_billing.py").write_text(
        "from billing.service import BillingService\n\n\n"
        "def test_charge_accepts_positive_amount():\n"
        "    assert BillingService().charge(10) is True\n\n\n"
        "def test_charge_rejects_zero_amount():\n"
        "    assert BillingService().charge(0) is False\n",
        encoding="utf-8",
    )

    # Real IaC: a k8s Deployment with replicas → availability/scalability NFRs
    k8s = project / "k8s"
    k8s.mkdir()
    (k8s / "deployment.yaml").write_text(
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: billing-api\n"
        "spec:\n"
        "  replicas: 3\n"
        "  template:\n"
        "    spec:\n"
        "      containers:\n"
        "        - name: api\n"
        "          image: billing:latest\n",
        encoding="utf-8",
    )

    # Rationale evidence: README + ADR
    (project / "README.md").write_text(
        "# ShopCart\n\nA billing service. We chose 3 replicas for HA.\n",
        encoding="utf-8",
    )
    adr = project / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001-use-postgres.md").write_text(
        "# ADR 0001: Use Postgres\n\nWe picked Postgres for transactional billing.\n",
        encoding="utf-8",
    )

    _write_extracted_docs(project)
    return project


def _write_extracted_docs(project: Path):
    extracted_dir = project / "codd" / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = extracted_dir / "modules"
    modules_dir.mkdir(exist_ok=True)

    _write_extracted_doc(
        extracted_dir / "system-context.md",
        node_id="design:extract:system-context",
        title="ShopCart System Context",
        body="1 module: billing. 200 lines total.",
    )
    _write_extracted_doc(
        modules_dir / "billing.md",
        node_id="design:extract:billing",
        title="billing",
        body=(
            "## Symbol Inventory\n\n"
            "| Kind | Name | Signature |\n"
            "|------|------|-----------|\n"
            "| class | BillingService | — |\n"
            "| function | charge | charge(amount: int) -> bool |"
        ),
        source_files=["src/billing/service.py"],
    )


def _write_extracted_doc(path: Path, *, node_id, title, body, source_files=None):
    codd_meta: dict = {
        "node_id": node_id,
        "type": "design",
        "source": "extracted",
        "confidence": 0.75,
        "last_extracted": "2026-06-11",
    }
    if source_files:
        codd_meta["source_files"] = source_files
    frontmatter = yaml.safe_dump({"codd": codd_meta}, sort_keys=False)
    path.write_text(f"---\n{frontmatter}---\n\n# {title}\n\n{body}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# AI mock that emits a body WITH a codd_restoration provenance block
# ---------------------------------------------------------------------------

def _restoration_block() -> str:
    return (
        "```yaml\n"
        "codd_restoration:\n"
        "  provenance:\n"
        "    - statement: Service runs 3 replicas for availability\n"
        "      evidence:\n"
        "        - k8s/deployment.yaml::Deployment::billing-api\n"
        "      band: green\n"
        "    - statement: charge rejects zero amount\n"
        "      evidence:\n"
        "        - tests/test_billing.py::test_charge_rejects_zero_amount\n"
        "      band: green\n"
        "  confidence_bands:\n"
        "    green: 2\n"
        "    amber: 1\n"
        "  open_questions:\n"
        "    - question: Why was 3 replicas chosen over 2 or 5?\n"
        "      why_unrecoverable: Sizing rationale is not encoded in code or IaC.\n"
        "      needs_human_confirmation: true\n"
        "  assumptions:\n"
        "    - assumption: Billing is the core revenue path\n"
        "      basis: none\n"
        "      needs_human_confirmation: true\n"
        "```\n"
    )


def _make_restored_body(input_text: str) -> str:
    title = "Document"
    for line in (input_text or "").splitlines():
        if "Title:" in line:
            title = line.split("Title:")[-1].strip()
            break

    text = input_text or ""
    block = _restoration_block()

    if "docs/operations/" in text:
        return (
            f"# {title}\n\n"
            "## 1. Overview\n\nOps overview.\n\n"
            "## 2. Runbook\n\nScale via replicas.\n\n"
            "## 3. Monitoring\n\nHealth probes.\n\n"
            "## 4. CI/CD Pipeline Generation Meta-Prompt\n\nPipeline.\n\n"
            f"{block}"
        )

    # requirements / nfr (both live under docs/requirements/ → requirement type)
    return (
        f"# {title}\n\n"
        "## 1. Overview\n\nInferred overview.\n\n"
        "## 2. Functional Requirements\n\n- Charge billing [inferred]\n\n"
        "## 3. Non-Functional Requirements\n\n- 3 replicas for availability\n\n"
        "## 4. Constraints\n\n- Python\n\n"
        "## 5. Open Questions\n\n- See structured block.\n\n"
        "## 6. Human Review Issues\n\n- None.\n\n"
        f"{block}"
    )


@pytest.fixture
def mock_restore_ai(monkeypatch):
    calls: list[dict[str, object]] = []
    real_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        # Intercept only the mocked AI command. Other subprocesses (notably the
        # read-only `git` calls made by H2's git-evidence collection) must run
        # for real, otherwise blame-driven testimony cannot be exercised.
        if isinstance(command, list) and command and command[0] == "mock-ai":
            prompt = kwargs.get("input")
            calls.append({"command": command, "input": prompt})
            return subprocess.CompletedProcess(
                args=command, returncode=0, stdout=_make_restored_body(prompt), stderr=""
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 1. Evidence ingestion — tests reach the restore prompt
# ---------------------------------------------------------------------------

def test_test_details_reach_restore_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=0)  # requirements doc — wants test evidence

    prompt = mock_restore_ai[0]["input"]
    assert "Test evidence" in prompt
    # Deterministic test names + provenance reach the prompt
    assert "test_charge_accepts_positive_amount" in prompt
    assert "test_charge_rejects_zero_amount" in prompt
    assert "tests/test_billing.py::test_charge_rejects_zero_amount" in prompt


def test_collect_test_evidence_carries_provenance(tmp_path):
    project = _setup_brownfield_project(tmp_path)
    from codd.extractor import extract_facts

    facts = extract_facts(project, "python", ["src"])
    evidence = _collect_test_evidence(facts)

    names = {e["test"] for e in evidence}
    assert "test_charge_accepts_positive_amount" in names
    sources = {e["source"] for e in evidence}
    assert any(s.endswith("::test_charge_rejects_zero_amount") for s in sources)


# ---------------------------------------------------------------------------
# 2. IaC NFR candidates + infra_config reach the infra/NFR restoration
# ---------------------------------------------------------------------------

def test_iac_nfr_candidates_reach_nfr_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=1)  # non_functional_requirements

    prompt = mock_restore_ai[0]["input"]
    assert "Infrastructure / NFR evidence" in prompt
    # The replica-count availability candidate (HIGH confidence) is present
    assert "replicas" in prompt
    assert "availability" in prompt
    assert "k8s/deployment.yaml::Deployment::billing-api" in prompt
    # Structured infra facts are also surfaced
    assert "Structured infrastructure facts" in prompt


def test_iac_evidence_reaches_operations_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=2)  # operations_runbook

    prompt = mock_restore_ai[0]["input"]
    assert "Infrastructure / NFR evidence" in prompt
    assert "k8s/deployment.yaml" in prompt


def test_assemble_evidence_bundle_is_populated(tmp_path):
    project = _setup_brownfield_project(tmp_path)
    config = deepcopy(BASE_CONFIG)

    bundle = _assemble_evidence_bundle(project, config)

    assert bundle.has_any()
    assert bundle.test_evidence
    assert bundle.nfr_candidates  # k8s replicas → NFR candidates
    assert bundle.infra_facts
    assert bundle.rationale_docs  # README + ADR


# ---------------------------------------------------------------------------
# 3. Provenance + confidence bands rendered into frontmatter
# ---------------------------------------------------------------------------

def test_restored_doc_carries_provenance_and_confidence_frontmatter(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=1)

    doc = project / "docs" / "requirements" / "non_functional_requirements.md"
    content = doc.read_text(encoding="utf-8")
    fm = _frontmatter(content)
    codd = fm["codd"]

    assert "provenance" in codd
    assert "confidence_bands" in codd
    # Provenance points at REAL evidence locators
    flat = yaml.safe_dump(codd["provenance"])
    assert "k8s/deployment.yaml::Deployment::billing-api" in flat
    assert "tests/test_billing.py::test_charge_rejects_zero_amount" in flat
    # Confidence band per statement
    assert any(item.get("band") in {"green", "amber"} for item in codd["provenance"])


def test_restored_doc_carries_open_questions_frontmatter(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=1)

    doc = project / "docs" / "requirements" / "non_functional_requirements.md"
    codd = _frontmatter(doc.read_text(encoding="utf-8"))["codd"]

    assert "open_questions" in codd
    oq = codd["open_questions"]
    assert oq and all(item.get("needs_human_confirmation") is True for item in oq)
    assert "assumptions" in codd


def test_provenance_contract_in_prompt_uses_bands_config(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=1)
    prompt = mock_restore_ai[0]["input"]

    assert "PROVENANCE + CONFIDENCE + OPEN-QUESTIONS CONTRACT" in prompt
    assert "codd_restoration:" in prompt
    assert "needs_human_confirmation: true" in prompt
    # bands config values flow into the contract text
    assert ">= 2 evidence sources" in prompt
    assert "min_confidence 0.9" in prompt


def test_extract_restoration_meta_lifts_block():
    body = "# Doc\n\n## 1. Overview\n\nText.\n\n" + _restoration_block()
    meta = extract_restoration_meta(body)
    assert meta is not None
    assert "provenance" in meta
    assert "open_questions" in meta
    assert meta["open_questions"][0]["needs_human_confirmation"] is True


def test_extract_restoration_meta_none_when_absent():
    body = "# Doc\n\n## 1. Overview\n\nNo block here.\n"
    assert extract_restoration_meta(body) is None


# ---------------------------------------------------------------------------
# 4. No-evidence → open_question rather than assertion (never fabricate)
# ---------------------------------------------------------------------------

def test_no_evidence_prompt_instructs_open_questions(tmp_path):
    """With an EMPTY evidence bundle, the prompt tells the model to emit
    open_questions for rationale/NFR rather than asserting them."""
    artifact = WaveArtifact(
        wave=1,
        node_id="nfr:test",
        output="docs/requirements/non_functional_requirements.md",
        title="NFR",
        depends_on=[],
        conventions=[],
        modules=[],
    )
    extracted = [
        ExtractedDocument(
            node_id="design:extract:system-context",
            path="codd/extracted/system-context.md",
            content="# ctx\n",
        )
    ]

    prompt = _build_restoration_prompt(
        artifact,
        extracted,
        evidence=EvidenceBundle(),  # nothing found
        capabilities=ProjectCapabilities(network_surface="http"),
        bands=BASE_CONFIG["bands"],
    )

    # No-evidence guidance present for each evidence class
    assert "No automated test evidence was found" in prompt
    assert "No Infrastructure-as-Code evidence was found" in prompt
    assert "No README/ADR/decision/CHANGELOG documents were found" in prompt
    # The hard never-fabricate rule is present
    assert "NEVER-FABRICATE RULE" in prompt
    assert "emit an open_question" in prompt.lower()


def test_never_fabricate_rule_always_present(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)
    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]
    assert "NEVER-FABRICATE RULE" in prompt
    assert "FAILED restoration" in prompt


# ---------------------------------------------------------------------------
# 5. Capability-aware infra/ops/NFR gating
# ---------------------------------------------------------------------------

def test_infra_evidence_gated_off_for_pure_library():
    """A pure library/CLI (no service, no network) must NOT be pushed to invent
    infra/ops content for a generic requirement doc."""
    artifact = WaveArtifact(
        wave=0,
        node_id="req:lib",
        output="docs/requirements/inferred_requirements.md",
        title="Reqs",
        depends_on=[],
        conventions=[],
        modules=[],
    )
    lib_caps = ProjectCapabilities(
        user_interface=False, network_surface="none",
        e2e_modality="cli", long_running_service=False,
    )
    assert _infra_ops_evidence_relevant(artifact, "requirement", True, lib_caps) is False

    web_caps = ProjectCapabilities(network_surface="http", long_running_service=True)
    assert _infra_ops_evidence_relevant(artifact, "requirement", True, web_caps) is True


def test_explicit_infra_artifact_always_gets_evidence():
    """Explicit infrastructure/ops/NFR artifacts always receive the evidence,
    regardless of capabilities."""
    infra_artifact = WaveArtifact(
        wave=0, node_id="infra:x", output="docs/infra/infrastructure_design.md",
        title="Infra", depends_on=[], conventions=[], modules=[],
    )
    lib_caps = ProjectCapabilities(network_surface="none", long_running_service=False)
    assert _infra_ops_evidence_relevant(infra_artifact, "design", False, lib_caps) is True

    ops_artifact = WaveArtifact(
        wave=0, node_id="ops:x", output="docs/operations/operations_runbook.md",
        title="Ops", depends_on=[], conventions=[], modules=[],
    )
    assert _infra_ops_evidence_relevant(ops_artifact, "operations", False, lib_caps) is True


def test_pure_library_restore_omits_infra_block(tmp_path, mock_restore_ai):
    """End-to-end: a CLI-typed project's requirements restoration omits the
    infra/NFR evidence block (capability gating in the real prompt)."""
    project = _setup_brownfield_project(tmp_path)
    # Re-type the project as a library (no service / no network)
    config = deepcopy(BASE_CONFIG)
    config["project"]["type"] = "library"
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    # Test evidence still present (always relevant to requirements)...
    assert "Test evidence" in prompt
    # ...but infra/NFR evidence is gated OFF for a generic requirements doc.
    assert "Infrastructure / NFR evidence" not in prompt


# ---------------------------------------------------------------------------
# 6. Rationale ingestion
# ---------------------------------------------------------------------------

def test_collect_rationale_docs_finds_readme_and_adr(tmp_path):
    project = _setup_brownfield_project(tmp_path)

    docs = _collect_rationale_docs(project)
    paths = {d["path"] for d in docs}

    assert "README.md" in paths
    assert any(p.endswith("0001-use-postgres.md") for p in paths)
    readme = next(d for d in docs if d["path"] == "README.md")
    assert "3 replicas for HA" in readme["content"]


def test_rationale_evidence_reaches_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    assert "Rationale evidence" in prompt
    assert "BEGIN RATIONALE README.md" in prompt
    assert "3 replicas for HA" in prompt


# ---------------------------------------------------------------------------
# 7. Backward compatibility: graceful degradation + no extracted docs
# ---------------------------------------------------------------------------

def test_restore_degrades_without_iac_tests_docs(tmp_path, mock_restore_ai):
    """A project with extracted docs but NO source/tests/IaC/docs still
    restores (empty evidence bundle), leaning on open_questions."""
    project = tmp_path / "bare"
    project.mkdir()
    codd_dir = project / "codd"
    codd_dir.mkdir()
    config = deepcopy(BASE_CONFIG)
    (codd_dir / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    _write_extracted_docs(project)

    results = restore_wave(project, wave=1)
    assert results[0].status == "restored"

    prompt = mock_restore_ai[0]["input"]
    assert "No automated test evidence was found" in prompt
    assert "No Infrastructure-as-Code evidence was found" in prompt


# ---------------------------------------------------------------------------
# 8. Greenfield generation output is UNCHANGED (additive-only render)
# ---------------------------------------------------------------------------

def test_render_document_greenfield_unchanged_without_restoration_meta():
    """_render_document with no restoration_meta yields the SAME frontmatter as
    before R2 (no provenance/confidence/open_questions keys leak in)."""
    from codd.generator import _render_document

    artifact = WaveArtifact(
        wave=1, node_id="design:x", output="docs/design/system_design.md",
        title="System Design", depends_on=[], conventions=[], modules=["billing"],
    )
    body = "# System Design\n\n## 1. Overview\n\nText.\n\n## 2. Architecture\n\nMore.\n\n## 3. Open Questions\n\n- None.\n"

    rendered = _render_document(
        artifact=artifact, global_conventions=[], depended_by=[], body=body,
    )
    codd = _frontmatter(rendered)["codd"]

    # Exactly the historical keys — no restoration-only keys present.
    assert set(codd) == {"node_id", "type", "depends_on", "depended_by", "conventions", "modules"}
    assert "provenance" not in codd
    assert "open_questions" not in codd
    assert "confidence_bands" not in codd

    # Passing None explicitly is identical to omitting it.
    rendered_none = _render_document(
        artifact=artifact, global_conventions=[], depended_by=[], body=body,
        restoration_meta=None,
    )
    assert rendered_none == rendered


def test_render_document_ignores_empty_restoration_meta():
    """Empty/falsey restoration values are not serialized (no empty keys)."""
    from codd.generator import _render_document

    artifact = WaveArtifact(
        wave=1, node_id="design:x", output="docs/design/system_design.md",
        title="System Design", depends_on=[], conventions=[], modules=[],
    )
    body = "# System Design\n\n## 1. Overview\n\nText.\n\n## 2. Architecture\n\nMore.\n\n## 3. Open Questions\n\n- None.\n"

    rendered = _render_document(
        artifact=artifact, global_conventions=[], depended_by=[], body=body,
        restoration_meta={"provenance": [], "open_questions": None},
    )
    codd = _frontmatter(rendered)["codd"]
    assert "provenance" not in codd
    assert "open_questions" not in codd


def _frontmatter(content: str) -> dict:
    assert content.startswith("---\n")
    end = content.index("\n---", 4)
    return yaml.safe_load(content[4:end])


# ---------------------------------------------------------------------------
# 9. H2: git-history testimony (blame-driven) wiring + prompt + contract
# ---------------------------------------------------------------------------

import os as _os
import subprocess as _subprocess


def _h2_git_env(date: str) -> dict:
    return {
        **_os.environ,
        "GIT_AUTHOR_NAME": "Restorer",
        "GIT_AUTHOR_EMAIL": "restorer@example.com",
        "GIT_COMMITTER_NAME": "Restorer",
        "GIT_COMMITTER_EMAIL": "restorer@example.com",
        "GIT_AUTHOR_DATE": f"{date}T00:00:00 +0000",
        "GIT_COMMITTER_DATE": f"{date}T00:00:00 +0000",
    }


def _h2_git(project: Path, *args: str, date: str = "2024-01-05") -> None:
    _subprocess.run(
        ["git", *args], cwd=str(project), env=_h2_git_env(date),
        capture_output=True, text=True, check=True,
    )


def _init_git_history(project: Path) -> None:
    """Turn the brownfield fixture into a real git repo with informative
    history on the billing module: an initial commit, then a substantial
    rewrite of service.py (→ surviving testimony + a supersession chain)."""
    service = project / "src" / "billing" / "service.py"
    v1_lines = [
        "class BillingService:",
        "    def charge(self, amount: int) -> bool:",
        "        return amount > 0",
    ] + [f"    # placeholder note {i} kept for padding" for i in range(10)]
    service.write_text("\n".join(v1_lines) + "\n", encoding="utf-8")

    _h2_git(project, "init", "-q", "-b", "main")
    _h2_git(project, "add", "-A")
    _h2_git(
        project, "commit", "-q", "-m",
        "feat(billing): validate charge amounts before capture", date="2024-01-05",
    )

    v2_lines = [
        "class BillingService:",
        "    MIN_AMOUNT = 1",
        "",
        "    def charge(self, amount: int) -> bool:",
        "        if amount < self.MIN_AMOUNT:",
        "            return False",
        "        return True",
    ] + [f"    # revised audit note {i} after the policy change" for i in range(8)]
    service.write_text("\n".join(v2_lines) + "\n", encoding="utf-8")
    _h2_git(project, "add", "-A")
    _h2_git(
        project, "commit", "-q", "-m",
        "fix(billing): reject zero-amount charges per finance policy", date="2024-03-15",
    )


def test_git_testimony_reaches_restore_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)
    _init_git_history(project)

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    # The testimony section, clearly marked as testimony-not-fact.
    assert "Git history testimony (UNVERIFIED — testimony, not fact)" in prompt
    assert "capped at the amber band" in prompt
    assert "NOT assert testimony as fact" in prompt
    assert "candidate_answer" in prompt
    # The surviving commit (the rewrite) attaches; entries carry the contract shape.
    assert "fix(billing): reject zero-amount charges per finance policy" in prompt
    assert "commit:" in prompt
    assert "[corroborated]" in prompt
    assert "still present at HEAD" in prompt


def test_supersession_chain_reaches_restore_prompt(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)
    _init_git_history(project)

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    assert "Supersession chains" in prompt
    assert "decision trail:" in prompt
    # Both ends of the trail are present, oldest first.
    assert "feat(billing): validate charge amounts before capture" in prompt
    assert "rejected" in prompt


def test_no_git_section_for_non_repo_project(tmp_path, mock_restore_ai):
    """Backward compat: a project that is not a git repository restores
    exactly as before — no testimony section, no fake absence text."""
    project = _setup_brownfield_project(tmp_path)  # no git init

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    assert "Git history testimony" not in prompt
    assert "Supersession chains" not in prompt


def test_git_evidence_disabled_via_config(tmp_path, mock_restore_ai):
    project = _setup_brownfield_project(tmp_path)
    _init_git_history(project)
    config = deepcopy(BASE_CONFIG)
    config["restore"] = {"git_evidence": {"enabled": False}}
    (project / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    restore_wave(project, wave=0)
    prompt = mock_restore_ai[0]["input"]

    assert "Git history testimony" not in prompt
    assert "Supersession chains" not in prompt


def test_contract_block_defines_candidate_answer_rules():
    from codd.restore import _build_provenance_contract_block

    text = "\n".join(_build_provenance_contract_block(BASE_CONFIG["bands"]))

    # candidate_answer schema: provenance commit:<sha>, corroborated flag,
    # human confirmation retained, testimony never green.
    assert "candidate_answer" in text
    assert 'commit:<short-sha> (<date>)' in text
    assert "corroborated: true|false" in text
    assert "needs_human_confirmation MUST remain true" in text
    assert "NEVER be promoted into a green statement" in text
    assert "whose only evidence is commit testimony can NEVER be green" in text
    # The candidate answer is a lead, not an answer; absence stays absent.
    assert "LEAD" in text
    assert "absence of evidence stays absent" in text


def test_evidence_bundle_git_fields_default_empty():
    bundle = EvidenceBundle()
    assert bundle.git_testimony == []
    assert bundle.supersession_chains == []
    assert bundle.has_any() is False

    from codd.git_evidence import GitTestimony

    bundle.git_testimony = [
        GitTestimony(locator="a.py", commit="abc1234", date="2024-01-01", subject="feat: x")
    ]
    assert bundle.has_any() is True


# ---------------------------------------------------------------------------
# 10. H2: candidate_answer passes through the meta lift + serializer
# ---------------------------------------------------------------------------

def _restoration_block_with_candidate_answer() -> str:
    return (
        "```yaml\n"
        "codd_restoration:\n"
        "  provenance:\n"
        "    - statement: charge rejects zero amount\n"
        "      evidence:\n"
        "        - tests/test_billing.py::test_charge_rejects_zero_amount\n"
        "      band: green\n"
        "  open_questions:\n"
        "    - question: Why are zero-amount charges rejected?\n"
        "      why_unrecoverable: The business rationale is not encoded in code.\n"
        "      needs_human_confirmation: true\n"
        "      candidate_answer:\n"
        "        text: Finance policy required rejecting zero-amount charges.\n"
        "        provenance: 'commit:abc1234 (2024-03-15)'\n"
        "        corroborated: true\n"
        "```\n"
    )


def test_extract_restoration_meta_passes_candidate_answer_through():
    body = "# Doc\n\n## 1. Overview\n\nText.\n\n" + _restoration_block_with_candidate_answer()
    meta = extract_restoration_meta(body)

    assert meta is not None
    question = meta["open_questions"][0]
    # The lead is preserved AND the question stays open.
    assert question["needs_human_confirmation"] is True
    assert question["candidate_answer"]["provenance"] == "commit:abc1234 (2024-03-15)"
    assert question["candidate_answer"]["corroborated"] is True
    assert "Finance policy" in question["candidate_answer"]["text"]


def test_render_document_serializes_candidate_answer():
    from codd.generator import _render_document

    artifact = WaveArtifact(
        wave=1, node_id="req:x", output="docs/requirements/inferred_requirements.md",
        title="Reqs", depends_on=[], conventions=[], modules=[],
    )
    body = "# Reqs\n\n## 1. Overview\n\nText.\n"
    meta = extract_restoration_meta(
        "# Doc\n\n" + _restoration_block_with_candidate_answer()
    )

    rendered = _render_document(
        artifact=artifact, global_conventions=[], depended_by=[], body=body,
        restoration_meta=meta,
    )
    codd = _frontmatter(rendered)["codd"]
    question = codd["open_questions"][0]
    assert question["candidate_answer"]["provenance"] == "commit:abc1234 (2024-03-15)"
    assert question["candidate_answer"]["corroborated"] is True
    assert question["needs_human_confirmation"] is True


def test_restored_doc_roundtrips_candidate_answer(tmp_path, monkeypatch):
    """End-to-end: an AI body carrying a candidate_answer lands in the restored
    document's frontmatter unchanged (no whitelist drops nested keys)."""

    def fake_run(command, *, input, capture_output, text, check, **kwargs):
        body = (
            "# Inferred Requirements\n\n"
            "## 1. Overview\n\nO.\n\n"
            "## 2. Functional Requirements\n\nF.\n\n"
            "## 3. Non-Functional Requirements\n\nN.\n\n"
            "## 4. Constraints\n\nC.\n\n"
            "## 5. Open Questions\n\nSee block.\n\n"
            "## 6. Human Review Issues\n\nNone.\n\n"
            + _restoration_block_with_candidate_answer()
        )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout=body, stderr="")

    monkeypatch.setattr(generator_module.subprocess, "run", fake_run)

    project = _setup_brownfield_project(tmp_path)
    restore_wave(project, wave=0)

    doc = project / "docs" / "requirements" / "inferred_requirements.md"
    codd = _frontmatter(doc.read_text(encoding="utf-8"))["codd"]
    question = codd["open_questions"][0]
    assert question["candidate_answer"]["provenance"] == "commit:abc1234 (2024-03-15)"
    assert question["needs_human_confirmation"] is True
