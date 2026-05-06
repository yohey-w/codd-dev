"""Requirement coverage auditing with conservative 3-class gap classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
import re
from typing import Any, Literal

from codd.config import load_project_config

try:
    from codd.knowledge_fetcher import (
        KnowledgeEntry,
        KnowledgeFetcher,
        load_ux_required_routes,
    )

    _HAS_KNOWLEDGE_FETCHER = True
except ImportError:  # pragma: no cover - exercised by monkeypatched fallback test
    KnowledgeEntry = Any  # type: ignore[misc, assignment]
    KnowledgeFetcher = None  # type: ignore[assignment]
    load_ux_required_routes = None  # type: ignore[assignment]
    _HAS_KNOWLEDGE_FETCHER = False


AUTO_ACCEPT = "AUTO_ACCEPT"
ASK = "ASK"
AUTO_REJECT = "AUTO_REJECT"

CONFIDENCE_THRESHOLD = 0.85


@dataclass
class GapItem:
    id: str
    label: str
    classification: str
    confidence: float = 0.9
    provenance: str = "web_search"
    fetched_at: str = ""
    rationale: str = ""
    question: str = ""
    reject_reason: str = ""
    category: str = ""
    description: str = ""
    expected_routes: Any | None = None
    audit_class: str = ""

    def __post_init__(self) -> None:
        if not self.fetched_at:
            self.fetched_at = _utc_now_iso()
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        if self.confidence < CONFIDENCE_THRESHOLD and self.classification == AUTO_ACCEPT:
            self.classification = ASK
            if not self.question:
                self.question = (
                    f"{self.label} has confidence={self.confidence:.2f}, "
                    "below the auto-accept threshold. Please confirm."
                )


@dataclass
class ArtifactGap:
    artifact_id: str
    title: str
    severity: Literal["ASK", "AUTO_REJECT"]
    rationale: str
    source: str = "ai_derived"


@dataclass
class AuditResult:
    project_type: str
    auto_accept: list[GapItem] = field(default_factory=list)
    ask: list[GapItem] = field(default_factory=list)
    auto_reject: list[GapItem] = field(default_factory=list)
    pending_review: list[GapItem] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"AUTO_ACCEPT={len(self.auto_accept)}, "
            f"ASK={len(self.ask)}, "
            f"AUTO_REJECT={len(self.auto_reject)}, "
            f"PENDING={len(self.pending_review)}"
        )


class CoverageAuditor:
    def __init__(self, project_root: str | Path = ".", fetcher: Any | None = None):
        self.project_root = Path(project_root)
        if fetcher is not None:
            self._fetcher = fetcher
        elif _HAS_KNOWLEDGE_FETCHER and KnowledgeFetcher is not None:
            self._fetcher = KnowledgeFetcher(self.project_root)
        else:
            self._fetcher = None
        self._ux_required_routes = (
            load_ux_required_routes(self.project_root)
            if load_ux_required_routes is not None
            else {}
        )

    def detect_project_type(self) -> str:
        """Detect a broad project type from manifests and requirement/design docs."""
        for text in _iter_project_text(self.project_root):
            lowered = text.lower()
            text_terms = _terms(lowered)
            if (
                {"lms", "e-learning", "elearning"} & text_terms
                or "learning management" in lowered
                or len(
                    {
                        keyword
                        for keyword in ("course", "learner", "instructor")
                        if keyword in text_terms
                    }
                )
                >= 2
            ):
                return "LMS/EdTech"
            if {"fintech", "pci-dss", "kyc", "aml"} & text_terms or (
                "payment" in text_terms
                and any(
                    keyword in text_terms
                    for keyword in ("cardholder", "checkout", "bank", "financial", "transaction")
                )
            ):
                return "FinTech"
            if any(
                keyword in text_terms
                for keyword in ("healthcare", "hipaa", "medical", "clinical", "samd")
            ):
                return "HealthTech"

        stacks = self._detect_tech_stack()
        if "Node.js/JavaScript/TypeScript" in stacks:
            return "Web/SaaS"
        if "Python" in stacks:
            return "Tool/SaaS"
        return "General/Web"

    def load_existing_requirements(self) -> list[str]:
        """Load normalized requirement terms from docs/requirements/*.md."""
        req_dir = self.project_root / "docs" / "requirements"
        keywords: set[str] = set()
        if not req_dir.exists():
            return []

        for md_path in req_dir.rglob("*.md"):
            try:
                text = md_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            keywords.update(_terms(text))
            keywords.update(_normalized_lines(text))
        return sorted(keywords)

    def get_standard_checklist(self, project_type: str) -> list[dict[str, Any]]:
        """Return a conservative standard requirement checklist for a project type."""
        base_web = [
            {
                "id": "https_tls",
                "label": "HTTPS/TLS",
                "classification": AUTO_ACCEPT,
                "confidence": 0.99,
                "provenance": "web_search:OWASP",
                "rationale": "Baseline transport security for web systems.",
                "aliases": ["https", "tls", "transport security"],
            },
            {
                "id": "csrf_protection",
                "label": "CSRF protection",
                "classification": AUTO_ACCEPT,
                "confidence": 0.99,
                "provenance": "web_search:OWASP_CSRF_Cheat_Sheet",
                "rationale": "Baseline browser-session protection for state-changing requests.",
                "aliases": ["csrf", "cross site request forgery"],
            },
            {
                "id": "xss_protection",
                "label": "XSS protection",
                "classification": AUTO_ACCEPT,
                "confidence": 0.99,
                "provenance": "web_search:OWASP_XSS_Cheat_Sheet",
                "rationale": "Baseline web application security requirement.",
                "aliases": ["xss", "cross site scripting"],
            },
            {
                "id": "sql_injection",
                "label": "SQL injection protection",
                "classification": AUTO_ACCEPT,
                "confidence": 0.99,
                "provenance": "web_search:OWASP_SQL_Injection",
                "rationale": "Baseline data access protection for SQL-backed systems.",
                "aliases": ["sql injection"],
            },
            {
                "id": "password_hashing",
                "label": "Password hashing",
                "classification": AUTO_ACCEPT,
                "confidence": 0.99,
                "provenance": "web_search:NIST_SP_800-63B",
                "rationale": "Plaintext password storage is never acceptable.",
                "aliases": ["password hash", "password hashing", "hashed password"],
            },
            {
                "id": "session_cookie_secure",
                "label": "Session cookie HttpOnly/Secure",
                "classification": AUTO_ACCEPT,
                "confidence": 0.97,
                "provenance": "web_search:OWASP_Session_Management",
                "rationale": "Baseline browser session hardening.",
                "aliases": ["httponly", "secure cookie", "session cookie"],
            },
            {
                "id": "input_validation",
                "label": "Input validation",
                "classification": AUTO_ACCEPT,
                "confidence": 0.98,
                "provenance": "web_search:OWASP_Input_Validation",
                "rationale": "Baseline integrity protection at trust boundaries.",
                "aliases": ["input validation", "validate input"],
            },
            {
                "id": "audit_log",
                "label": "Audit logging",
                "classification": ASK,
                "confidence": 0.8,
                "provenance": "web_search:security_best_practices",
                "question": "What audit log events and retention period are required?",
                "aliases": ["audit log", "audit logging"],
            },
            {
                "id": "disaster_recovery",
                "label": "Disaster recovery RPO/RTO",
                "classification": ASK,
                "confidence": 0.7,
                "provenance": "inferred",
                "question": "Are there RPO/RTO requirements for backup and disaster recovery?",
                "aliases": ["rpo", "rto", "disaster recovery"],
            },
        ]

        lms = [
            *base_web,
            *self._build_auth_ui_surface_checklist(),
            {
                "id": "role_access_control",
                "label": "Role-based access control",
                "classification": AUTO_ACCEPT,
                "confidence": 0.95,
                "provenance": "web_search:LMS_standard",
                "rationale": "LMS products require separate learner, instructor, and admin permissions.",
                "aliases": ["rbac", "role access", "role-based access control"],
            },
            {
                "id": "scorm_lti",
                "label": "SCORM/LTI interoperability",
                "classification": ASK,
                "confidence": 0.8,
                "provenance": "web_search:IMS_Global_LTI",
                "question": "Is SCORM or LTI interoperability required, optional, or out of scope?",
                "aliases": ["scorm", "lti"],
            },
            {
                "id": "wcag_aa",
                "label": "WCAG AA accessibility",
                "classification": ASK,
                "confidence": 0.75,
                "provenance": "web_search:W3C_WCAG",
                "question": "Is WCAG AA accessibility required for this delivery context?",
                "aliases": ["wcag", "accessibility"],
            },
            {
                "id": "gdpr",
                "label": "GDPR compliance",
                "classification": ASK,
                "confidence": 0.7,
                "provenance": "web_search:EU_GDPR",
                "question": "Will the product serve EU users or otherwise fall under GDPR?",
                "aliases": ["gdpr"],
            },
            {
                "id": "soc2_audit",
                "label": "SOC 2 audit",
                "classification": AUTO_REJECT,
                "confidence": 0.95,
                "provenance": "inferred",
                "reject_reason": "Usually excessive for an early small-project LMS unless enterprise sales require it.",
                "aliases": ["soc2", "soc 2"],
            },
            {
                "id": "hipaa",
                "label": "HIPAA compliance",
                "classification": AUTO_REJECT,
                "confidence": 0.98,
                "provenance": "inferred",
                "reject_reason": "Out of scope unless protected health information is handled.",
                "aliases": ["hipaa"],
            },
            {
                "id": "pci_dss",
                "label": "PCI-DSS",
                "classification": AUTO_REJECT,
                "confidence": 0.9,
                "provenance": "inferred",
                "reject_reason": "Out of scope when direct card processing is delegated to a payment provider.",
                "aliases": ["pci", "pci dss", "pci-dss"],
            },
        ]

        tool_saas = [
            {
                "id": "requirements_traceability",
                "label": "Requirements traceability",
                "classification": AUTO_ACCEPT,
                "confidence": 0.95,
                "provenance": "inferred",
                "rationale": "Development tools must preserve links between requirements, design, and implementation.",
                "aliases": ["traceability", "requirements traceability"],
            },
            {
                "id": "config_schema_validation",
                "label": "Configuration schema validation",
                "classification": AUTO_ACCEPT,
                "confidence": 0.9,
                "provenance": "inferred",
                "rationale": "Config-driven tools need early validation of malformed input.",
                "aliases": ["config schema", "schema validation", "configuration validation"],
            },
            *base_web[-2:],
            {
                "id": "soc2_audit",
                "label": "SOC 2 audit",
                "classification": AUTO_REJECT,
                "confidence": 0.95,
                "provenance": "inferred",
                "reject_reason": "A local developer tool does not need certification by default.",
                "aliases": ["soc2", "soc 2"],
            },
            {
                "id": "hipaa",
                "label": "HIPAA compliance",
                "classification": AUTO_REJECT,
                "confidence": 0.98,
                "provenance": "inferred",
                "reject_reason": "Out of scope unless protected health information is handled.",
                "aliases": ["hipaa"],
            },
            {
                "id": "pci_dss",
                "label": "PCI-DSS",
                "classification": AUTO_REJECT,
                "confidence": 0.9,
                "provenance": "inferred",
                "reject_reason": "Out of scope unless direct cardholder-data handling is introduced.",
                "aliases": ["pci", "pci dss", "pci-dss"],
            },
        ]

        checklists = {
            "LMS/EdTech": lms,
            "FinTech": [
                *base_web,
                {
                    "id": "kyc_aml",
                    "label": "KYC/AML controls",
                    "classification": ASK,
                    "confidence": 0.8,
                    "provenance": "web_search:financial_regulatory_guidance",
                    "question": "Does this product trigger KYC/AML obligations?",
                    "aliases": ["kyc", "aml"],
                },
                {
                    "id": "pci_dss",
                    "label": "PCI-DSS",
                    "classification": ASK,
                    "confidence": 0.82,
                    "provenance": "web_search:PCI_SSC",
                    "question": "Will the system directly store, process, or transmit cardholder data?",
                    "aliases": ["pci", "pci dss", "pci-dss"],
                },
            ],
            "HealthTech": [
                *base_web,
                {
                    "id": "medical_privacy",
                    "label": "Medical privacy controls",
                    "classification": ASK,
                    "confidence": 0.82,
                    "provenance": "web_search:healthcare_privacy_guidance",
                    "question": "Will the product handle protected health or clinical information?",
                    "aliases": ["medical privacy", "protected health information", "phi"],
                },
            ],
            "Web/SaaS": base_web,
            "General/Web": base_web,
            "Tool/SaaS": tool_saas,
        }
        return list(checklists.get(project_type, base_web))

    def _build_auth_ui_surface_checklist(self) -> list[dict[str, Any]]:
        route_keys = {
            "ux:auth:signin": "signin",
            "ux:landing:root": "root",
            "ux:auth:signup": "signup",
        }
        checklist = [
            {
                "id": "ux:auth:signin",
                "label": "User sign-in/login form",
                "category": "auth_ui_surface",
                "description": "User sign-in/login form",
                "expected_routes": None,
                "audit_class": ASK,
                "classification": ASK,
                "confidence": 0.8,
                "provenance": "project_config:codd.yaml[ux].required_routes",
                "question": (
                    "Which route implements the user sign-in/login form? "
                    "Configure ux.required_routes.signin in codd.yaml if needed."
                ),
                "aliases": ["sign in", "signin", "login", "login form", "auth ui"],
            },
            {
                "id": "ux:landing:root",
                "label": "Root landing page or redirect",
                "category": "auth_ui_surface",
                "description": "Root landing page or redirect to dashboard/login",
                "expected_routes": None,
                "audit_class": ASK,
                "classification": ASK,
                "confidence": 0.8,
                "provenance": "project_config:codd.yaml[ux].required_routes",
                "question": (
                    "What should the root route show or redirect to? "
                    "Configure ux.required_routes.root in codd.yaml if needed."
                ),
                "aliases": ["root landing", "landing page", "home page", "redirect"],
            },
            {
                "id": "ux:auth:signup",
                "label": "Sign-up/registration flow",
                "category": "auth_ui_surface",
                "description": "Sign-up/registration flow if user registration is required",
                "expected_routes": None,
                "audit_class": ASK,
                "classification": ASK,
                "confidence": 0.75,
                "provenance": "project_config:codd.yaml[ux].required_routes",
                "question": (
                    "Is user self-registration required, and which route implements it? "
                    "Configure ux.required_routes.signup in codd.yaml if needed."
                ),
                "aliases": ["sign up", "signup", "registration", "register"],
            },
        ]
        for item in checklist:
            route_key = route_keys[item["id"]]
            if route_key in self._ux_required_routes:
                item["expected_routes"] = self._ux_required_routes[route_key]
        return checklist

    def classify_gaps(self, project_type: str, existing_keywords: list[str]) -> AuditResult:
        """Detect missing checklist items and classify each gap."""
        result = AuditResult(project_type=project_type)
        existing = {keyword.lower() for keyword in existing_keywords}

        for item in self.get_standard_checklist(project_type):
            if _is_covered(item, existing):
                continue

            gap = GapItem(
                id=item["id"],
                label=item["label"],
                classification=item.get("classification", item.get("audit_class", ASK)),
                confidence=item.get("confidence", 0.9),
                provenance=item.get("provenance", "web_search"),
                rationale=item.get("rationale", ""),
                question=item.get("question", ""),
                reject_reason=item.get("reject_reason", ""),
                category=item.get("category", ""),
                description=item.get("description", ""),
                expected_routes=item.get("expected_routes"),
                audit_class=item.get("audit_class", item.get("classification", "")),
            )
            if gap.classification == AUTO_ACCEPT:
                result.auto_accept.append(gap)
            elif gap.classification == ASK:
                result.ask.append(gap)
            elif gap.classification == AUTO_REJECT:
                result.auto_reject.append(gap)
            else:
                result.pending_review.append(gap)

        return result

    def audit(self) -> AuditResult:
        """Run the full coverage audit."""
        project_type = self.detect_project_type()
        existing = self.load_existing_requirements()
        return self.classify_gaps(project_type, existing)

    def audit_required_artifacts(
        self,
        required_artifacts: list[dict[str, Any]],
        project_root: Path,
    ) -> list[ArtifactGap]:
        """Compare lexicon.required_artifacts with existing design documents."""
        root = Path(project_root)
        discovery_config = _load_artifact_discovery_config(root)
        existing = self._discover_existing_artifacts(root)
        gaps: list[ArtifactGap] = []

        for artifact in required_artifacts:
            artifact_id = str(artifact.get("id", "")).strip()
            if not artifact_id:
                continue
            if _artifact_exists(artifact_id, root, existing, discovery_config):
                continue

            source = str(artifact.get("source", "ai_derived") or "ai_derived")
            scope = str(artifact.get("scope", "") or "").strip()
            severity: Literal["ASK", "AUTO_REJECT"]
            if scope or source in {"ai_derived", "user_override"}:
                severity = ASK
            else:
                severity = AUTO_REJECT

            gaps.append(
                ArtifactGap(
                    artifact_id=artifact_id,
                    title=str(artifact.get("title", artifact_id) or artifact_id),
                    severity=severity,
                    rationale=str(artifact.get("rationale", "") or ""),
                    source=source,
                )
            )
        return gaps

    def _discover_existing_artifacts(self, project_root: Path) -> set[str]:
        """Discover existing design artifact ids from configured design-doc paths."""
        root = Path(project_root)
        discovery_config = _load_artifact_discovery_config(root)
        mappings = _artifact_file_mappings(discovery_config)
        artifacts: set[str] = set()

        for configured_path in _artifact_discovery_paths(discovery_config):
            path = _resolve_project_path(root, configured_path)
            if path.is_file():
                candidates = [path]
            elif path.exists():
                candidates = list(path.rglob("*.md"))
            else:
                candidates = []
            for md_path in candidates:
                artifact_id = _artifact_id_from_path(root, md_path, mappings)
                if artifact_id:
                    artifacts.add(artifact_id)

        for artifact_id, paths in _artifact_path_overrides(discovery_config).items():
            if any(_resolve_project_path(root, path).exists() for path in paths):
                artifacts.add(artifact_id)
        return artifacts

    def generate_required_artifacts_report(
        self,
        required_artifacts: list[dict[str, Any]],
        gaps: list[ArtifactGap],
        project_root: Path,
    ) -> str:
        """Render a Markdown section for required artifact coverage."""
        root = Path(project_root)
        existing = self._discover_existing_artifacts(root)
        discovery_config = _load_artifact_discovery_config(root)
        ask_count = sum(1 for gap in gaps if gap.severity == ASK)
        auto_reject_count = sum(1 for gap in gaps if gap.severity == AUTO_REJECT)

        lines = [
            "## Required Artifacts Audit",
            "",
            f"Required artifacts (from AI derivation): {len(required_artifacts)}",
            f"Existing design docs found: {len(existing)}",
            "",
        ]
        if not gaps:
            lines.append("✅ All required artifacts present")
            return "\n".join(lines) + "\n"

        lines.append("### Missing required artifacts")
        lines.append("")
        for gap in gaps:
            action = _suggest_artifact_path(gap.artifact_id, root, discovery_config)
            lines.extend(
                [
                    f"- [{gap.severity}] {gap.artifact_id}",
                    f"  - Title: {gap.title}",
                    f"  - Rationale: {gap.rationale or 'Required artifact is missing.'}",
                    f"  - Source: {gap.source}",
                    f"  - Action: Create {action}",
                ]
            )

        scope_exclusions = [gap for gap in gaps if gap.severity == AUTO_REJECT]
        if scope_exclusions:
            lines.extend(["", "### Scope exclusions (AUTO_REJECT)", ""])
            for gap in scope_exclusions:
                lines.append(f"- {gap.artifact_id}: omitted unless project scope requires it")

        lines.extend(["", f"Summary: {ask_count} ASK, {auto_reject_count} AUTO_REJECT"])
        return "\n".join(lines) + "\n"

    def generate_report(self, result: AuditResult, output_path: Path | None = None) -> str:
        """Generate a Markdown coverage audit report."""
        lines = [
            "# Requirement Coverage Audit Report",
            "",
            f"**Project Type**: {result.project_type}",
            f"**Summary**: {result.summary()}",
            f"**Generated**: {_utc_now_iso()}",
            "",
            "## AUTO_ACCEPT",
            "",
            "Items that are safe to adopt automatically because they are baseline requirements.",
            "",
        ]
        for item in result.auto_accept:
            lines.extend(
                [
                    f"- **{item.id}** ({item.label})",
                    f"  - Rationale: {item.rationale or 'Baseline requirement.'}",
                    f"  - Provenance: {item.provenance}",
                    f"  - Confidence: {item.confidence:.2f}",
                ]
            )

        lines.extend(
            [
                "",
                "## ASK",
                "",
                "Items that require human scope, priority, or applicability decisions.",
                "",
            ]
        )
        for item in result.ask:
            lines.extend(
                [
                    f"- **{item.id}** ({item.label})",
                    f"  - Question: {item.question or 'Please confirm applicability.'}",
                    f"  - Provenance: {item.provenance}",
                    f"  - Confidence: {item.confidence:.2f}",
                    *_gap_metadata_lines(item),
                ]
            )

        lines.extend(
            [
                "",
                "## AUTO_REJECT",
                "",
                "Items that are recorded as out of scope to prevent accidental implementation.",
                "",
            ]
        )
        for item in result.auto_reject:
            lines.extend(
                [
                    f"- **{item.id}** ({item.label})",
                    f"  - Reason: {item.reject_reason or 'Out of scope for this project type.'}",
                    f"  - Provenance: {item.provenance}",
                    f"  - Confidence: {item.confidence:.2f}",
                ]
            )

        if result.pending_review:
            lines.extend(["", "## PENDING", ""])
            for item in result.pending_review:
                lines.append(f"- **{item.id}** ({item.label})")

        report = "\n".join(lines) + "\n"
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report, encoding="utf-8")
        return report

    def _detect_tech_stack(self) -> list[str]:
        if self._fetcher is not None:
            detect = getattr(self._fetcher, "detect_tech_stack", None)
            if callable(detect):
                return list(detect())

        markers = {
            "package.json": "Node.js/JavaScript/TypeScript",
            "Cargo.toml": "Rust",
            "pyproject.toml": "Python",
            "go.mod": "Go",
            "Gemfile": "Ruby",
            "composer.json": "PHP",
            "pom.xml": "Java/Maven",
            "build.gradle": "Java/Kotlin/Gradle",
        }
        return [
            stack
            for filename, stack in markers.items()
            if (self.project_root / filename).exists()
        ]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _gap_metadata_lines(item: GapItem) -> list[str]:
    lines: list[str] = []
    if item.category:
        lines.append(f"  - Category: {item.category}")
    if item.description and item.description != item.label:
        lines.append(f"  - Description: {item.description}")
    if item.expected_routes is not None:
        lines.append(f"  - Expected routes: {item.expected_routes}")
    return lines


def _iter_project_text(project_root: Path) -> list[str]:
    candidates: list[Path] = []
    docs_dir = project_root / "docs"
    if docs_dir.exists():
        candidates.extend(docs_dir.rglob("*.md"))
    for name in ("README.md", "README_ja.md"):
        path = project_root / name
        if path.exists():
            candidates.append(path)

    texts: list[str] = []
    for path in candidates:
        if path.name == "coverage_audit_report.md":
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return texts


def _terms(text: str) -> set[str]:
    return {term.lower() for term in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text)}


def _normalized_lines(text: str) -> set[str]:
    return {
        _normalize_phrase(line)
        for line in text.splitlines()
        if line.strip() and len(line.strip()) <= 160
    }


def _normalize_phrase(text: str) -> str:
    return " ".join(_terms(text))


def _is_covered(item: dict[str, Any], existing: set[str]) -> bool:
    item_id = str(item["id"]).lower()
    label = str(item["label"])
    aliases = [str(alias) for alias in item.get("aliases", [])]
    direct_candidates = {
        item_id,
        item_id.replace("_", "-"),
        item_id.replace("_", " "),
        _normalize_phrase(label),
        *(_normalize_phrase(alias) for alias in aliases),
    }
    if any(candidate and candidate in existing for candidate in direct_candidates):
        return True

    id_terms = _terms(item_id.replace("_", " "))
    if id_terms and id_terms.issubset(existing):
        return True

    for alias in aliases:
        alias_terms = _terms(alias)
        if alias_terms and alias_terms.issubset(existing):
            return True
    return False


def _load_artifact_discovery_config(project_root: Path) -> dict[str, Any]:
    try:
        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}
    artifact_config = config.get("artifact_discovery", {})
    return artifact_config if isinstance(artifact_config, dict) else {}


def _artifact_discovery_paths(config: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("path", "paths", "design_path", "design_paths", "directories"):
        configured = config.get(key)
        if isinstance(configured, str) and configured.strip():
            values.append(configured)
        elif isinstance(configured, list):
            values.extend(str(path) for path in configured if str(path).strip())
    return values or ["docs/design"]


def _artifact_file_mappings(config: dict[str, Any]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for key in ("mappings", "file_mappings", "filename_mappings"):
        configured = config.get(key, {})
        if not isinstance(configured, dict):
            continue
        for filename, artifact_id in configured.items():
            if isinstance(artifact_id, str) and artifact_id.strip():
                mappings[_normalize_mapping_key(str(filename))] = artifact_id.strip()
    return mappings


def _artifact_path_overrides(config: dict[str, Any]) -> dict[str, list[str]]:
    configured = config.get("artifact_paths", {})
    if not isinstance(configured, dict):
        return {}
    overrides: dict[str, list[str]] = {}
    for artifact_id, paths in configured.items():
        if isinstance(paths, str):
            overrides[str(artifact_id)] = [paths]
        elif isinstance(paths, list):
            overrides[str(artifact_id)] = [str(path) for path in paths if str(path).strip()]
    return overrides


def _resolve_project_path(project_root: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    return path if path.is_absolute() else project_root / path


def _artifact_id_from_path(project_root: Path, path: Path, mappings: dict[str, str]) -> str:
    relative = _safe_relative_path(project_root, path)
    relative_key = _normalize_mapping_key(relative)
    for mapped_path, artifact_id in mappings.items():
        if relative_key == mapped_path or relative_key.endswith(f"/{mapped_path}"):
            return artifact_id
    for key in (
        relative_key,
        _normalize_mapping_key(path.name),
        _normalize_mapping_key(path.stem),
    ):
        if key in mappings:
            return mappings[key]
    return _default_artifact_id_from_filename(path)


def _default_artifact_id_from_filename(path: Path) -> str:
    stem = _normalize_artifact_stem(path.stem)
    if stem in {"screen_flow", "screen_flow_design"}:
        return "design:screen_flow_design"
    if stem in {"auth_design", "authorization_design", "auth_authorization_design"}:
        return "design:auth_authorization_design"
    return f"design:{stem}"


def _artifact_exists(
    artifact_id: str,
    project_root: Path,
    existing: set[str],
    config: dict[str, Any],
) -> bool:
    if artifact_id in existing:
        return True
    return any(
        _resolve_project_path(project_root, candidate).exists()
        for candidate in _candidate_artifact_paths(artifact_id, config)
    )


def _candidate_artifact_paths(artifact_id: str, config: dict[str, Any]) -> list[str]:
    override_paths = _artifact_path_overrides(config).get(artifact_id, [])
    if override_paths:
        return override_paths

    artifact_key = artifact_id.split(":", 1)[-1]
    stems = _candidate_artifact_stems(artifact_key)
    paths: list[str] = []
    for design_path in _artifact_discovery_paths(config):
        for stem in stems:
            paths.append(str(Path(design_path) / f"{stem}.md"))
    return paths


def _candidate_artifact_stems(artifact_key: str) -> list[str]:
    normalized = _normalize_artifact_stem(artifact_key)
    special = {
        "requirements": ["requirements"],
        "screen_flow_design": ["screen-flow", "screen_flow_design", "screen-flow-design"],
        "auth_authorization_design": [
            "auth-design",
            "auth_authorization_design",
            "authorization-design",
        ],
    }
    if normalized in special:
        return special[normalized]

    stems: list[str] = []
    if normalized.endswith("_design"):
        base = normalized.removesuffix("_design")
        stems.extend([f"{base}-design", normalized.replace("_", "-"), normalized])
        if base:
            stems.extend([base.replace("_", "-"), base])
    else:
        stems.extend([normalized.replace("_", "-"), normalized])

    deduped: list[str] = []
    for stem in stems:
        if stem and stem not in deduped:
            deduped.append(stem)
    return deduped


def _suggest_artifact_path(artifact_id: str, project_root: Path, config: dict[str, Any]) -> str:
    del project_root
    candidates = _candidate_artifact_paths(artifact_id, config)
    return candidates[0] if candidates else f"docs/design/{artifact_id.split(':', 1)[-1]}.md"


def _safe_relative_path(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def _normalize_mapping_key(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def _normalize_artifact_stem(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")
