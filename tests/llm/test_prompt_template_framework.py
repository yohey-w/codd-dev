from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.deployment.providers.llm_consideration import Consideration, VerificationStrategy
from codd.llm.means_catalog_loader import MeansCatalogLoader
from codd.llm.parser import DerivedConsideration, LlmOutputParser
from codd.llm.prompt_builder import PromptBuilder
from codd.llm.strategy_validator import StrategyValidator


SPECIFIC_TERMS = ("Cookie", "OAuth", "Safari", "iPhone", "NextAuth", "React", "LMS", "osato")
DEFAULT_DOMAINS = ("web_app", "mobile_app", "desktop_app", "cli_tool", "backend_api", "embedded")
CATALOG_VALUES = (
    "cdp_browser",
    "playwright",
    "appium",
    "winappdriver",
    "bats",
    "pact_contract",
    "hil_test",
    "sil_test",
)


def _template_text() -> str:
    return PromptBuilder.DEFAULT_TEMPLATE_PATH.read_text(encoding="utf-8")


def _item(item_id: str = "runtime_contract", **extra) -> dict:
    payload = {
        "id": item_id,
        "description": "Verify the described runtime contract.",
        "domain_hints": ["runtime"],
        "verification_strategy": {
            "engine": "registered_engine",
            "layer": "contract",
            "parallelizable": True,
            "reason_for_choice": "The document names a runtime behavior.",
            "required_capabilities": ["process"],
        },
    }
    payload.update(extra)
    return payload


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_meta_instruction_template_has_required_placeholders():
    template = _template_text()

    assert "{domain_guidance_block}" in template
    assert "{means_catalog_hint}" in template
    assert "{design_doc_content}" in template
    assert "verification_strategy" in template
    assert "three-layer" in template
    assert "DerivedConsideration" in template


def test_meta_instruction_template_has_no_specific_terms():
    template = _template_text()

    assert not [term for term in SPECIFIC_TERMS if term in template]


def test_prompt_build_domain_guidance_none_returns_valid_prompt():
    prompt = PromptBuilder().build("Design body", domain_guidance=None, means_catalog_hint="runner: [one]")

    assert "Design body" in prompt
    assert "{domain_guidance_block}" not in prompt
    assert "DOMAIN GUIDANCE" not in prompt


def test_prompt_build_means_catalog_hint_none_returns_valid_prompt():
    prompt = PromptBuilder().build("Design body", domain_guidance="Use neutral checks.", means_catalog_hint=None)

    assert "Design body" in prompt
    assert "{means_catalog_hint}" not in prompt
    assert "VERIFICATION MEANS CATALOG" not in prompt


def test_prompt_build_with_both_optional_inputs_none_preserves_structure():
    prompt = PromptBuilder().build("Design body", domain_guidance=None, means_catalog_hint=None)

    assert "Return JSON only" in prompt
    assert "DESIGN_DOC:" in prompt
    assert "Design body" in prompt
    assert "{}" not in prompt


def test_prompt_build_includes_optional_blocks_when_present():
    prompt = PromptBuilder().build(
        "Design body",
        domain_guidance="Prefer project-specific wording.",
        means_catalog_hint="custom_domain:\n- custom_runner",
    )

    assert "DOMAIN GUIDANCE:\nPrefer project-specific wording." in prompt
    assert "VERIFICATION MEANS CATALOG:\ncustom_domain:\n- custom_runner" in prompt


def test_extract_parameter_placeholders_returns_full_tokens():
    prompt = "Use ${ADMIN_USER} and ${TENANT_ID} in the steps."

    assert PromptBuilder.extract_parameter_placeholders(prompt) == ["${ADMIN_USER}", "${TENANT_ID}"]


def test_extract_parameter_placeholders_deduplicates_in_first_seen_order():
    prompt = "${ONE} ${TWO} ${ONE}"

    assert PromptBuilder.extract_parameter_placeholders(prompt) == ["${ONE}", "${TWO}"]


def test_extract_parameter_placeholders_ignores_invalid_patterns():
    prompt = "${1BAD} ${GOOD_NAME} ${bad-name}"

    assert PromptBuilder.extract_parameter_placeholders(prompt) == ["${GOOD_NAME}"]


def test_default_catalog_path_points_to_phase_f_default():
    assert MeansCatalogLoader.DEFAULT_CATALOG_PATH == "codd/deployment/defaults/verification_means_catalog.yaml"


def test_means_catalog_resolve_uses_project_lexicon_override(tmp_path: Path):
    lexicon = tmp_path / "project_lexicon.yaml"
    _write_yaml(lexicon, {"verification_means_catalog": {"custom_domain": ["runner_a"]}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), None)

    assert catalog == {"custom_domain": ["runner_a"]}


def test_means_catalog_project_lexicon_override_completely_replaces_default(tmp_path: Path):
    lexicon = tmp_path / "project_lexicon.yaml"
    _write_yaml(lexicon, {"verification_means_catalog": {"custom_domain": ["runner_a"]}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), None)

    assert "custom_domain" in catalog
    assert not any(domain in catalog for domain in DEFAULT_DOMAINS)


def test_means_catalog_resolve_uses_codd_yaml_llm_path(tmp_path: Path):
    means = tmp_path / "config" / "means.yaml"
    _write_yaml(means, {"custom_domain": ["runner_b"]})
    codd_yaml = tmp_path / "codd.yaml"
    _write_yaml(codd_yaml, {"llm": {"means_catalog_path": "config/means.yaml"}})

    catalog = MeansCatalogLoader().resolve(None, str(codd_yaml))

    assert catalog == {"custom_domain": ["runner_b"]}


def test_means_catalog_resolve_codd_yaml_path_relative_to_project_root(tmp_path: Path):
    means = tmp_path / "config" / "means.yaml"
    _write_yaml(means, {"custom_domain": ["runner_c"]})
    codd_yaml = tmp_path / "codd" / "codd.yaml"
    _write_yaml(codd_yaml, {"llm": {"means_catalog_path": "config/means.yaml"}})

    catalog = MeansCatalogLoader().resolve(None, str(codd_yaml))

    assert catalog == {"custom_domain": ["runner_c"]}


def test_means_catalog_project_lexicon_wins_over_codd_yaml(tmp_path: Path):
    lexicon = tmp_path / "project_lexicon.yaml"
    means = tmp_path / "means.yaml"
    codd_yaml = tmp_path / "codd.yaml"
    _write_yaml(lexicon, {"verification_means_catalog": {"lexicon_domain": ["runner_a"]}})
    _write_yaml(means, {"config_domain": ["runner_b"]})
    _write_yaml(codd_yaml, {"llm": {"means_catalog_path": "means.yaml"}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), str(codd_yaml))

    assert catalog == {"lexicon_domain": ["runner_a"]}


def test_means_catalog_resolve_falls_back_to_core_default():
    catalog = MeansCatalogLoader().resolve(None, None)

    assert sorted(catalog) == sorted(DEFAULT_DOMAINS)


def test_default_catalog_contains_all_six_domains():
    catalog = MeansCatalogLoader().resolve(None, None)

    for domain in DEFAULT_DOMAINS:
        assert domain in catalog


def test_means_catalog_to_hint_text_returns_yaml_string():
    hint = MeansCatalogLoader.to_hint_text({"custom_domain": ["runner_a", "runner_b"]})

    assert yaml.safe_load(hint) == {"custom_domain": ["runner_a", "runner_b"]}


def test_means_catalog_to_hint_text_preserves_empty_lists():
    hint = MeansCatalogLoader.to_hint_text({"custom_domain": []})

    assert yaml.safe_load(hint) == {"custom_domain": []}


def test_llm_output_parser_converts_valid_json_to_derived_considerations():
    parsed = LlmOutputParser().parse(json.dumps([_item()]))

    assert len(parsed) == 1
    assert isinstance(parsed[0], DerivedConsideration)
    assert parsed[0].id == "runtime_contract"
    assert parsed[0].verification_strategy is not None
    assert parsed[0].verification_strategy.engine == "registered_engine"


def test_llm_output_parser_accepts_top_level_considerations_object():
    raw = json.dumps({"considerations": [_item("one"), _item("two")]})

    parsed = LlmOutputParser().parse(raw)

    assert [item.id for item in parsed] == ["one", "two"]


def test_llm_output_parser_accepts_markdown_json_fence():
    raw = "```json\n" + json.dumps({"considerations": [_item("fenced")]}) + "\n```"

    parsed = LlmOutputParser().parse(raw)

    assert [item.id for item in parsed] == ["fenced"]


def test_llm_output_parser_skips_invalid_entry_and_keeps_valid(caplog):
    raw = json.dumps({"considerations": [_item("valid"), {"id": "missing_description"}]})

    parsed = LlmOutputParser().parse(raw)

    assert [item.id for item in parsed] == ["valid"]
    assert "id and description are required" in caplog.text


def test_llm_output_parser_skips_missing_id(caplog):
    raw = json.dumps([_item(id="")])

    parsed = LlmOutputParser().parse(raw)

    assert parsed == []
    assert "id and description are required" in caplog.text


def test_llm_output_parser_skips_missing_description(caplog):
    raw = json.dumps([{"id": "missing_description", "domain_hints": []}])

    parsed = LlmOutputParser().parse(raw)

    assert parsed == []
    assert "id and description are required" in caplog.text


def test_llm_output_parser_accepts_empty_domain_hints_list():
    raw = json.dumps([_item(domain_hints=[])])

    parsed = LlmOutputParser().parse(raw)

    assert parsed[0].domain_hints == []


def test_llm_output_parser_skips_non_list_domain_hints(caplog):
    raw = json.dumps([_item(domain_hints="runtime")])

    parsed = LlmOutputParser().parse(raw)

    assert parsed == []
    assert "domain_hints must be a list" in caplog.text


def test_llm_output_parser_warns_and_returns_empty_on_invalid_json(caplog):
    parsed = LlmOutputParser().parse("{not-json")

    assert parsed == []
    assert "invalid JSON" in caplog.text


def test_strategy_validator_skips_unregistered_engine_with_warning(caplog):
    considerations = [
        Consideration("known", "Known.", verification_strategy=VerificationStrategy(engine="registered")),
        Consideration("unknown", "Unknown.", verification_strategy=VerificationStrategy(engine="missing")),
    ]

    validated = StrategyValidator().validate(considerations, {"registered": object()})

    assert [item.id for item in validated] == ["known"]
    assert "not registered" in caplog.text


def test_strategy_validator_keeps_registered_engine():
    considerations = [Consideration("known", "Known.", verification_strategy=VerificationStrategy(engine="registered"))]

    validated = StrategyValidator().validate(considerations, {"registered": object()})

    assert [item.id for item in validated] == ["known"]


def test_strategy_validator_keeps_consideration_without_strategy():
    considerations = [Consideration("no_strategy", "No strategy.")]

    validated = StrategyValidator().validate(considerations, {})

    assert [item.id for item in validated] == ["no_strategy"]


def test_strategy_validator_keeps_strategy_with_empty_engine():
    considerations = [Consideration("empty_engine", "Empty.", verification_strategy=VerificationStrategy(engine=""))]

    validated = StrategyValidator().validate(considerations, {})

    assert [item.id for item in validated] == ["empty_engine"]


def test_llm_core_code_does_not_branch_on_catalog_contents():
    source = "\n".join(path.read_text(encoding="utf-8") for path in (Path("codd") / "llm").glob("*.py"))

    for term in (*DEFAULT_DOMAINS, *CATALOG_VALUES):
        assert term not in source


def test_llm_core_code_and_template_have_no_specific_terms():
    files = list((Path("codd") / "llm").glob("*.py")) + list((Path("codd") / "llm" / "templates").glob("*.md"))
    hits = {
        str(path): [term for term in SPECIFIC_TERMS if term in path.read_text(encoding="utf-8")]
        for path in files
    }

    assert all(not terms for terms in hits.values())


def test_project_lexicon_catalog_accepts_catalog_wrapper(tmp_path: Path):
    lexicon = tmp_path / "project_lexicon.yaml"
    _write_yaml(lexicon, {"verification_means_catalog": {"catalog": {"custom_domain": "runner"}}})

    catalog = MeansCatalogLoader().resolve(str(lexicon), None)

    assert catalog == {"custom_domain": ["runner"]}
