from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from codd.deployment.providers.ai_command import (
    AiCommandError,
    AiCommandTimeout,
    SubprocessAiCommand,
    resolve_command,
    resolve_model,
)
from codd.deployment.providers.llm_consideration import (
    ConsiderationResult,
    LlmConsiderationProvider,
    VerificationStrategy,
    parse_considerations,
)
from codd.deployment.providers.verification.means_catalog import VerificationMeansCatalog


class FakeAiCommand:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[tuple[str, str | None]] = []

    def invoke(self, prompt: str, model: str | None = None) -> str:
        self.calls.append((prompt, model))
        return self.outputs.pop(0)


class TimeoutAiCommand:
    def invoke(self, prompt: str, model: str | None = None) -> str:
        raise AiCommandTimeout("timeout")


def _response(*items: dict) -> str:
    return json.dumps({"considerations": list(items)})


def _item(item_id: str = "runtime_check", **extra) -> dict:
    payload = {
        "id": item_id,
        "description": "Verify the runtime contract.",
        "domain_hints": ["service"],
    }
    payload.update(extra)
    return payload


def test_provider_returns_consideration_list_from_mock_ai(tmp_path: Path):
    fake = FakeAiCommand([_response(_item())])
    provider = LlmConsiderationProvider(fake, provider_id="fake", cache_dir=tmp_path)

    result = provider.provide("design body", {"model": "m1"})

    assert result.provider_id == "fake"
    assert result.considerations[0].id == "runtime_check"
    assert result.considerations[0].description == "Verify the runtime contract."
    assert result.considerations[0].domain_hints == ["service"]
    assert fake.calls[0][1] == "m1"


def test_provider_writes_cache_on_miss(tmp_path: Path):
    fake = FakeAiCommand([_response(_item())])
    provider = LlmConsiderationProvider(fake, provider_id="fake", cache_dir=tmp_path)

    result = provider.provide("design body", {})

    cache_files = list(tmp_path.glob(f"{result.design_doc_sha}_fake.json"))
    assert len(cache_files) == 1
    cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
    assert cached["considerations"][0]["id"] == "runtime_check"


def test_provider_reads_cache_without_invoking_ai(tmp_path: Path):
    first = LlmConsiderationProvider(FakeAiCommand([_response(_item("first"))]), provider_id="fake", cache_dir=tmp_path)
    first.provide("design body", {})
    second_fake = FakeAiCommand([_response(_item("second"))])
    second = LlmConsiderationProvider(second_fake, provider_id="fake", cache_dir=tmp_path)

    result = second.provide("design body", {})

    assert result.considerations[0].id == "first"
    assert second_fake.calls == []


def test_provider_cache_misses_when_design_sha_changes(tmp_path: Path):
    fake = FakeAiCommand([_response(_item("first")), _response(_item("second"))])
    provider = LlmConsiderationProvider(fake, provider_id="fake", cache_dir=tmp_path)

    first = provider.provide("design body", {})
    second = provider.provide("changed body", {})

    assert first.design_doc_sha != second.design_doc_sha
    assert second.considerations[0].id == "second"
    assert len(fake.calls) == 2


def test_provider_cache_misses_when_provider_id_changes(tmp_path: Path):
    LlmConsiderationProvider(
        FakeAiCommand([_response(_item("first"))]),
        provider_id="fake_a",
        cache_dir=tmp_path,
    ).provide("design body", {})

    result = LlmConsiderationProvider(
        FakeAiCommand([_response(_item("second"))]),
        provider_id="fake_b",
        cache_dir=tmp_path,
    ).provide("design body", {})

    assert result.provider_id == "fake_b"
    assert result.considerations[0].id == "second"


def test_consideration_result_round_trips_schema():
    result = ConsiderationResult.from_dict(
        {
            "provider_id": "fake",
            "design_doc_sha": "abc",
            "generated_at": "2026-05-06T00:00:00Z",
            "considerations": [
                {
                    "id": "runtime_check",
                    "description": "Verify behavior.",
                    "domain_hints": ["service"],
                    "verification_strategy": {
                        "engine": "dummy",
                        "layer": "contract",
                        "parallelizable": True,
                        "reason_for_choice": "Focused check.",
                        "required_capabilities": ["network"],
                    },
                    "approval_status": "approved",
                }
            ],
        }
    )

    assert result.to_dict()["considerations"][0]["verification_strategy"]["engine"] == "dummy"
    assert result.considerations[0].verification_strategy == VerificationStrategy(
        engine="dummy",
        layer="contract",
        parallelizable=True,
        reason_for_choice="Focused check.",
        required_capabilities=["network"],
    )
    assert result.considerations[0].approval_status == "approved"


def test_parse_considerations_accepts_top_level_list():
    parsed = parse_considerations(json.dumps([_item("one"), _item("two")]))

    assert [item.id for item in parsed] == ["one", "two"]


def test_parse_considerations_accepts_markdown_json_fence():
    raw = "```json\n" + _response(_item("fenced")) + "\n```"

    parsed = parse_considerations(raw)

    assert parsed[0].id == "fenced"


def test_parse_considerations_resolves_environment_placeholders():
    raw = _response(_item(description="Use ${TEST_ACCOUNT}."))

    parsed = parse_considerations(raw, environ={"TEST_ACCOUNT": "account-1"})

    assert parsed[0].description == "Use account-1."


def test_unregistered_strategy_is_warned_and_skipped(caplog):
    provider = LlmConsiderationProvider(FakeAiCommand([]), provider_id="fake")
    result = ConsiderationResult.from_dict(
        {
            "provider_id": "fake",
            "design_doc_sha": "abc",
            "generated_at": "2026-05-06T00:00:00Z",
            "considerations": [
                {
                    "id": "known",
                    "description": "Known engine.",
                    "verification_strategy": {"engine": "registered"},
                },
                {
                    "id": "unknown",
                    "description": "Unknown engine.",
                    "verification_strategy": {"engine": "missing"},
                },
            ],
        }
    )

    filtered = provider.filter_registered_verification_strategies(result, registry={"registered": object})

    assert [item.id for item in filtered.considerations] == ["known"]
    assert "not registered" in caplog.text


def test_ai_command_uses_runner_and_model_flag():
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    command = SubprocessAiCommand(
        command="mock-ai --print",
        config={"llm": {"model": "m1"}},
        runner=fake_run,
    )

    assert command.invoke("prompt") == "[]"
    assert calls[0][0] == ["mock-ai", "--print", "--model", "m1"]
    assert calls[0][1]["input"] == "prompt"


def test_ai_command_timeout_becomes_adapter_error():
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    command = SubprocessAiCommand(command="mock-ai", timeout=0.01, runner=fake_run)

    with pytest.raises(AiCommandTimeout):
        command.invoke("prompt")


def test_ai_command_nonzero_exit_becomes_adapter_error():
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="failed")

    command = SubprocessAiCommand(command="mock-ai", runner=fake_run)

    with pytest.raises(AiCommandError, match="failed"):
        command.invoke("prompt")


def test_provider_timeout_returns_empty_result(tmp_path: Path):
    provider = LlmConsiderationProvider(TimeoutAiCommand(), provider_id="fake", cache_dir=tmp_path)

    result = provider.provide("design body", {})

    assert result.considerations == []
    assert list(tmp_path.glob("*.json")) == []


def test_resolve_command_and_model_from_config_and_env(monkeypatch):
    monkeypatch.setenv("CODD_AI_COMMAND", "env-ai --json")
    monkeypatch.setenv("CODD_LLM_MODEL", "env-model")

    assert resolve_command({"llm": {"command": "config-ai"}}) == ["env-ai", "--json"]
    assert resolve_model({"llm": {"model": "config-model"}}) == "env-model"


def test_means_catalog_loads_all_default_domains():
    catalog = VerificationMeansCatalog.load()

    assert sorted(catalog) == [
        "backend_api",
        "cli_tool",
        "desktop_app",
        "embedded",
        "mobile_app",
        "web_app",
    ]
    assert "cdp_browser" in catalog["web_app"]
    assert "sil_test" in catalog["embedded"]


def test_means_catalog_explicit_override_path_replaces_default(tmp_path: Path):
    override = tmp_path / "means.yaml"
    override.write_text(yaml.safe_dump({"custom": ["runner"]}), encoding="utf-8")

    catalog = VerificationMeansCatalog.load(override)

    assert catalog == {"custom": ["runner"]}


def test_means_catalog_uses_project_codd_yaml_override(tmp_path: Path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    override = tmp_path / "config" / "means.yaml"
    override.parent.mkdir()
    override.write_text(yaml.safe_dump({"custom": ["runner"]}), encoding="utf-8")
    (codd_dir / "codd.yaml").write_text(
        "verification:\n"
        "  means_catalog_path: config/means.yaml\n",
        encoding="utf-8",
    )

    catalog = VerificationMeansCatalog.load(project_root=tmp_path)

    assert catalog == {"custom": ["runner"]}
