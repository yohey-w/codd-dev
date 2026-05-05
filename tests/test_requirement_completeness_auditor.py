import json
from pathlib import Path

from click.testing import CliRunner

from codd.cli import main
from codd.knowledge_fetcher import KnowledgeEntry, KnowledgeFetcher
from codd.lexicon import load_lexicon
from codd.requirement_completeness_auditor import (
    DEFAULTS_DIR,
    RequirementCompletenessAuditor,
)


def _write_config(project: Path, extra: str = "") -> None:
    codd_dir = project / ".codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(extra or "{}\n", encoding="utf-8")


def _write_requirements(project: Path, text: str = "") -> None:
    req_dir = project / "docs" / "requirements"
    req_dir.mkdir(parents=True)
    (req_dir / "requirements.md").write_text(text, encoding="utf-8")


def test_initializes_with_detected_web_project_type(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    auditor = RequirementCompletenessAuditor(tmp_path)

    assert auditor.project_type == "web"


def test_initializes_with_detected_cli_project_type(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='tool'\n", encoding="utf-8")

    auditor = RequirementCompletenessAuditor(tmp_path)

    assert auditor.project_type == "cli"


def test_codd_yaml_project_type_override_wins(tmp_path):
    _write_config(tmp_path, "requirement_completeness:\n  project_type: iot\n")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    auditor = RequirementCompletenessAuditor(tmp_path)

    assert auditor.project_type == "iot"


def test_generate_missing_items_uses_ai_response_when_enabled(monkeypatch, tmp_path):
    _write_config(tmp_path, "requirement_completeness:\n  ai_missing_items: true\n")
    payload = [{"id": "q_custom", "question": "Custom?", "blocking": False}]

    def fake_run(*args, **kwargs):
        class Result:
            returncode = 0
            stdout = json.dumps(payload)

        return Result()

    monkeypatch.setattr("codd.requirement_completeness_auditor.subprocess.run", fake_run)

    items = RequirementCompletenessAuditor(tmp_path, ai_command="mock-ai")._generate_missing_items("", "web")

    assert any(item["id"] == "q_custom" for item in items)


def test_check_item_in_requirements_matches_alias(tmp_path):
    _write_config(tmp_path)
    auditor = RequirementCompletenessAuditor(tmp_path)
    item = {"id": "q_auth_method", "question": "Auth?", "aliases": ["authentication"]}

    assert auditor._check_item_in_requirements(item, "Authentication uses OAuth.") is True


def test_check_item_in_requirements_returns_false_for_missing_text(tmp_path):
    _write_config(tmp_path)
    auditor = RequirementCompletenessAuditor(tmp_path)
    item = {"id": "q_wcag_level", "question": "Accessibility?", "aliases": ["wcag"]}

    assert auditor._check_item_in_requirements(item, "The app has billing pages.") is False


def test_generate_ask_options_uses_default_options(tmp_path):
    _write_config(tmp_path)
    auditor = RequirementCompletenessAuditor(tmp_path)
    item = auditor._load_default_questions("web")[0]

    options = auditor._generate_ask_options(item, "web")

    assert len(options) >= 3
    assert sum(option.recommended for option in options) == 1


def test_generate_ask_options_falls_back_when_no_options(tmp_path):
    _write_config(tmp_path)
    auditor = RequirementCompletenessAuditor(tmp_path)
    auditor.fetcher = type(
        "Fetcher",
        (),
        {"fetch": lambda self, query: KnowledgeEntry(query=query, result="summary")},
    )()

    options = auditor._generate_ask_options({"id": "q_x", "question": "X?", "search_query": "x"}, "web")

    assert options[0].id == "recommended_baseline"
    assert options[0].recommended is True


def test_audit_cooperative_mode_proceeds_non_blocking_and_keeps_blocking_ask(tmp_path):
    _write_config(tmp_path)
    _write_requirements(tmp_path, "")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    session = RequirementCompletenessAuditor(tmp_path).audit([])

    statuses = {item.id: item.status for item in session.ask_items}
    assert statuses["q_auth_method"] == "ASK"
    assert statuses["q_concurrent_users"] == "RECOMMENDED_PROCEEDING"


def test_audit_blocking_mode_leaves_all_items_as_ask(tmp_path):
    _write_config(tmp_path, "hitl:\n  mode: blocking\n")
    _write_requirements(tmp_path, "")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    session = RequirementCompletenessAuditor(tmp_path).audit([])

    assert {item.status for item in session.ask_items} == {"ASK"}


def test_project_type_defaults_exist_for_all_required_types():
    assert sorted(path.stem for path in DEFAULTS_DIR.glob("*.yaml")) == [
        "cli",
        "iot",
        "mobile",
        "web",
    ]


def test_codd_yaml_questions_override_defaults(tmp_path):
    _write_config(
        tmp_path,
        "requirement_completeness:\n"
        "  project_type: web\n"
        "  questions:\n"
        "    - id: q_auth_method\n"
        "      question: Override auth question?\n",
    )

    item = RequirementCompletenessAuditor(tmp_path)._load_default_questions("web")[0]

    assert item["question"] == "Override auth question?"


def test_cli_completeness_audit_writes_lexicon_and_exits_for_blocking_ask(tmp_path):
    _write_config(tmp_path)
    _write_requirements(tmp_path, "")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    result = CliRunner().invoke(main, ["require", "--path", str(tmp_path), "--completeness-audit"])

    assert result.exit_code == 1
    assert "Requirement completeness audit complete:" in result.output
    assert load_lexicon(tmp_path).coverage_decisions


def test_requirement_specific_questions_are_not_hardcoded_in_core():
    source = Path("codd/requirement_completeness_auditor.py").read_text(encoding="utf-8")

    assert "Which authentication method should the product assume?" not in source
    assert "Which WCAG accessibility level is required?" not in source


def test_audit_skips_items_already_present_in_requirements(tmp_path):
    _write_config(tmp_path)
    _write_requirements(tmp_path, "Authentication and concurrent users are already specified.")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    session = RequirementCompletenessAuditor(tmp_path).audit([])
    ids = {item.id for item in session.ask_items}

    assert "q_auth_method" not in ids
    assert "q_concurrent_users" not in ids


def test_knowledge_fetcher_detects_mobile_project_type(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"dependencies":{"react-native":"1.0.0"}}',
        encoding="utf-8",
    )

    assert KnowledgeFetcher(tmp_path).detect_project_type() == "mobile"


def test_audit_persists_ask_items_to_lexicon(tmp_path):
    _write_config(tmp_path)
    _write_requirements(tmp_path, "")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    RequirementCompletenessAuditor(tmp_path).audit([])

    decisions = load_lexicon(tmp_path).coverage_decisions
    assert decisions[0].asked_at
    assert any(decision.proceeded_with for decision in decisions)
