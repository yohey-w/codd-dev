from codd import coverage_auditor as auditor_module
from codd.coverage_auditor import (
    ASK,
    AUTO_ACCEPT,
    AUTO_REJECT,
    AuditResult,
    CoverageAuditor,
    GapItem,
)


def test_detect_project_type_returns_lms_for_lms_docs(tmp_path):
    docs_dir = tmp_path / "docs" / "requirements"
    docs_dir.mkdir(parents=True)
    (docs_dir / "requirements.md").write_text(
        "Learning management system for course learners and instructors.",
        encoding="utf-8",
    )

    assert CoverageAuditor(tmp_path).detect_project_type() == "LMS/EdTech"


def test_detect_project_type_ignores_incidental_payment_examples(tmp_path):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "explainer.md").write_text(
        "Example node id: src/payment/processor.py::route_payment",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    assert CoverageAuditor(tmp_path).detect_project_type() == "Tool/SaaS"


def test_gap_item_forces_low_confidence_auto_accept_to_ask():
    gap = GapItem(
        id="low_confidence_control",
        label="Low confidence control",
        classification=AUTO_ACCEPT,
        confidence=0.84,
    )

    assert gap.classification == ASK
    assert "confidence=0.84" in gap.question


def test_classify_gaps_excludes_existing_requirement_keywords(tmp_path):
    result = CoverageAuditor(tmp_path).classify_gaps("LMS/EdTech", ["https_tls"])

    gap_ids = {gap.id for gap in result.auto_accept + result.ask + result.auto_reject}
    assert "https_tls" not in gap_ids


def test_audit_result_summary_counts_each_bucket():
    result = AuditResult(
        project_type="General/Web",
        auto_accept=[
            GapItem(id="https_tls", label="HTTPS/TLS", classification=AUTO_ACCEPT),
            GapItem(id="xss_protection", label="XSS protection", classification=AUTO_ACCEPT),
        ],
        ask=[GapItem(id="audit_log", label="Audit logging", classification=ASK)],
        auto_reject=[GapItem(id="hipaa", label="HIPAA", classification=AUTO_REJECT)],
    )

    assert result.summary() == "AUTO_ACCEPT=2, ASK=1, AUTO_REJECT=1, PENDING=0"


def test_generate_report_contains_three_class_sections(tmp_path):
    result = AuditResult(
        project_type="General/Web",
        auto_accept=[GapItem(id="https_tls", label="HTTPS/TLS", classification=AUTO_ACCEPT)],
        ask=[GapItem(id="audit_log", label="Audit logging", classification=ASK)],
        auto_reject=[GapItem(id="hipaa", label="HIPAA", classification=AUTO_REJECT)],
    )
    output_path = tmp_path / "coverage_audit_report.md"

    report = CoverageAuditor(tmp_path).generate_report(result, output_path)

    assert "## AUTO_ACCEPT" in report
    assert "## ASK" in report
    assert "## AUTO_REJECT" in report
    assert output_path.read_text(encoding="utf-8") == report


def test_coverage_auditor_instantiates_without_knowledge_fetcher(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_module, "_HAS_KNOWLEDGE_FETCHER", False)
    monkeypatch.setattr(auditor_module, "KnowledgeFetcher", None)

    auditor = CoverageAuditor(tmp_path)

    assert auditor.detect_project_type() == "General/Web"
