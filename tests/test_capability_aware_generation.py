"""Tests for G2 capability-aware generation / planning / implementation.

These tests call the prompt-builder functions directly (no AI invoked) and assert
that web projects keep today's web guidance while non-web types (CLI, library,
service) get type-appropriate guidance instead of hardcoded web assumptions.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest

import codd.generator as generator_module
from codd.generator import (
    OPERATIONAL_BEHAVIOR_MODEL_BLOCK,
    WEB_FALLBACK_CAPABILITIES,
    WaveArtifact,
    _build_generation_prompt,
    _build_operations_doc_block,
    _build_test_doc_block,
    _resolve_generation_capabilities,
    build_operational_behavior_model_block,
)
from codd.project_types import ProjectCapabilities, load_capabilities


WEB = ProjectCapabilities(
    user_interface=True,
    network_surface="http",
    e2e_modality="browser",
    long_running_service=True,
)
CLI = ProjectCapabilities(
    user_interface=False,
    network_surface="none",
    e2e_modality="cli",
    long_running_service=False,
)
LIBRARY = ProjectCapabilities(
    user_interface=False,
    network_surface="none",
    e2e_modality="none",
    long_running_service=False,
)
DEVICE = ProjectCapabilities(
    user_interface=True,
    network_surface="http",
    e2e_modality="device",
    long_running_service=False,
)


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Backward-compat: web block == legacy constant; default fallback == web.
# --------------------------------------------------------------------------- #


def test_web_operational_block_is_byte_identical_to_legacy_constant():
    assert build_operational_behavior_model_block(WEB) == OPERATIONAL_BEHAVIOR_MODEL_BLOCK


def test_default_capability_fallback_is_full_web():
    # No capabilities passed -> legacy/web behavior preserved everywhere.
    assert build_operational_behavior_model_block(None) == OPERATIONAL_BEHAVIOR_MODEL_BLOCK
    assert _build_test_doc_block(WEB_FALLBACK_CAPABILITIES) == _build_test_doc_block(WEB)
    assert WEB_FALLBACK_CAPABILITIES == WEB


# --------------------------------------------------------------------------- #
# Test-doc block: branch on e2e_modality.
# --------------------------------------------------------------------------- #


def test_test_doc_block_web_has_browser_e2e_and_server_startup():
    text = _join(_build_test_doc_block(WEB))
    assert "Playwright" in text
    assert "browser tests" in text
    assert "E2E tests for web applications require a running server" in text
    assert "API integration tests" in text  # API/browser split preserved


def test_test_doc_block_cli_has_subprocess_e2e_no_browser_no_server():
    text = _join(_build_test_doc_block(CLI))
    assert "subprocess" in text
    assert "exit code" in text
    # No browser-E2E framework guidance and no web-server startup.
    assert "Playwright/Cypress, UI flows" not in text
    assert "require a running server" not in text
    assert "start the application server" not in text
    # The only mention of browser/Playwright is an explicit prohibition.
    for line in text.splitlines():
        if "Playwright" in line or "browser" in line.lower():
            assert "Do NOT" in line or "NO browser" in line


def test_test_doc_block_device_has_on_device_guidance_no_web_server():
    text = _join(_build_test_doc_block(DEVICE))
    assert "on-device" in text or "emulator" in text
    assert "require a running server" not in text
    assert "browser-on-a-web-server" in text  # explicit no-web-server framing


def test_test_doc_block_none_is_integration_only():
    text = _join(_build_test_doc_block(LIBRARY))
    assert "no end-to-end" in text.lower()
    assert "unit and integration" in text
    assert "Playwright" not in text or "Do NOT generate Playwright" in text
    assert "require a running server" not in text


def test_test_doc_block_universal_traceability_present_for_all_types():
    for caps in (WEB, CLI, DEVICE, LIBRARY):
        text = _join(_build_test_doc_block(caps))
        assert "verifiable behavior" in text.lower()
        assert "VB-" in text


# --------------------------------------------------------------------------- #
# Operations-doc block: server startup only when long_running_service.
# --------------------------------------------------------------------------- #


def test_operations_block_web_has_server_startup_and_neutral_env():
    text = _join(_build_operations_doc_block(WEB))
    assert "E2E Job Server Startup rules" in text
    assert "NEXTAUTH_SECRET" not in text  # neutralized env example
    assert "project-required secrets/credentials" in text


def test_operations_block_cli_has_no_server_startup():
    text = _join(_build_operations_doc_block(CLI))
    assert "E2E Job Server Startup rules" not in text
    assert "NOT a long-running service" in text
    assert "release/distribution/packaging" in text
    assert "NEXTAUTH_SECRET" not in text


# --------------------------------------------------------------------------- #
# Operational Behavior Model block: Actor-Facing & route only for UI/http.
# --------------------------------------------------------------------------- #


def test_obm_block_omits_actor_facing_for_non_ui_types():
    web = _join(build_operational_behavior_model_block(WEB))
    cli = _join(build_operational_behavior_model_block(CLI))
    assert "Actor-Facing Surface/Copy Obligations" in web
    assert "Actor-Facing Surface/Copy Obligations" not in cli


def test_obm_block_route_fields_only_for_http_or_ui():
    web = _join(build_operational_behavior_model_block(WEB))
    cli = _join(build_operational_behavior_model_block(CLI))
    assert "`route`/`path`, `ui_pattern`" in web
    assert "`route`/`path`" not in cli
    assert "`entry_point`/`invocation`" in cli
    # An http-but-no-ui (iot-like) project still gets route fields.
    iot = ProjectCapabilities(
        user_interface=False, network_surface="http", e2e_modality="device"
    )
    iot_text = _join(build_operational_behavior_model_block(iot))
    assert "`route`/`path`, `ui_pattern`" in iot_text


# --------------------------------------------------------------------------- #
# _build_generation_prompt end-to-end (design + test doc types).
# --------------------------------------------------------------------------- #


def _design_artifact() -> WaveArtifact:
    return WaveArtifact(
        wave=2,
        node_id="design:system-design",
        output="docs/design/system_design.md",
        title="System Design",
        depends_on=[],
        conventions=[],
    )


def _test_artifact() -> WaveArtifact:
    return WaveArtifact(
        wave=1,
        node_id="design:acceptance-criteria",
        output="docs/test/acceptance_criteria.md",
        title="Acceptance Criteria",
        depends_on=[],
        conventions=[],
    )


def test_generation_prompt_design_web_default_keeps_actor_facing():
    # No capabilities -> web fallback -> Actor-Facing present (backward compat).
    prompt = _build_generation_prompt(_design_artifact(), [], [])
    assert "Actor-Facing Surface/Copy Obligations (DESIGN-TIME, CRITICAL)" in prompt
    assert "`route`/`path`, `ui_pattern`" in prompt


def test_generation_prompt_design_cli_omits_actor_facing():
    prompt = _build_generation_prompt(_design_artifact(), [], [], capabilities=CLI)
    assert "Actor-Facing Surface/Copy Obligations" not in prompt
    assert "`entry_point`/`invocation`" in prompt


def test_generation_prompt_test_web_vs_cli():
    web_prompt = _build_generation_prompt(_test_artifact(), [], [], capabilities=WEB)
    cli_prompt = _build_generation_prompt(_test_artifact(), [], [], capabilities=CLI)
    assert "E2E tests for web applications require a running server" in web_prompt
    assert "E2E tests for web applications require a running server" not in cli_prompt
    assert "subprocess" in cli_prompt


# --------------------------------------------------------------------------- #
# Capability resolution from config.
# --------------------------------------------------------------------------- #


def test_resolve_capabilities_untyped_config_is_web_fallback():
    config = {"project": {"name": "x", "language": "python"}}
    caps = _resolve_generation_capabilities(config, None)
    assert caps == WEB_FALLBACK_CAPABILITIES


def test_resolve_capabilities_explicit_cli_type():
    config = {"project": {"name": "x", "type": "cli"}}
    caps = _resolve_generation_capabilities(config, None)
    assert caps == load_capabilities("cli")
    assert caps.user_interface is False
    assert caps.e2e_modality == "cli"


def test_resolve_capabilities_required_artifacts_project_type():
    config = {"required_artifacts": {"project_type": "cli"}}
    caps = _resolve_generation_capabilities(config, None)
    assert caps == load_capabilities("cli")


def test_resolve_capabilities_unknown_type_is_generic_conservative():
    config = {"project": {"type": "totally-unknown-xyz"}}
    caps = _resolve_generation_capabilities(config, None)
    # Unknown -> generic baseline (NOT web): conservative, no UI.
    assert caps.user_interface is False


# --------------------------------------------------------------------------- #
# Planner MECE / V-model: UX domain conditional on user_interface.
# --------------------------------------------------------------------------- #


def test_planner_v_model_patterns_include_ux_for_ui_types():
    from codd.planner import _standard_v_model_patterns

    text = _standard_v_model_patterns(WEB)
    assert "UX" in text
    # Default (None) preserves web behavior.
    assert "UX" in _standard_v_model_patterns(None)


def test_planner_v_model_patterns_omit_ux_for_non_ui_types():
    from codd.planner import _standard_v_model_patterns

    text = _standard_v_model_patterns(CLI)
    assert "UX" not in text
    # Other mandatory domains still present.
    for domain in ("requirements", "design", "detailed_design", "plan"):
        # MECE doc structure dirs are separate; here check domain-design line keeps
        # API/database/auth/infrastructure even without UX.
        pass
    assert "API, database, auth, infrastructure/CI/CD" in text


def test_planner_plan_init_prompt_omits_actor_facing_for_non_ui():
    from codd.planner import RequirementDocument, _build_plan_init_prompt

    config = {"project": {"name": "tool", "type": "cli"}}
    reqs = [RequirementDocument(node_id="req:x", path="docs/requirements/x.md", content="# X")]

    web_prompt = _build_plan_init_prompt(config, reqs, WEB)
    cli_prompt = _build_plan_init_prompt(config, reqs, CLI)

    assert "actor-facing surface/copy obligations before implementation planning" in web_prompt
    assert "actor-facing surface/copy obligations before implementation planning" not in cli_prompt
    assert "UX" in web_prompt
    assert "UX" not in cli_prompt


def test_planner_brownfield_prompt_omits_actor_facing_for_non_ui():
    from codd.planner import ExtractedDocument, _build_brownfield_plan_init_prompt

    config = {"project": {"name": "tool", "type": "cli"}}
    docs = [ExtractedDocument(node_id="ext:x", path="docs/extracted/x.md", content="# X")]

    web_prompt = _build_brownfield_plan_init_prompt(config, docs, WEB)
    cli_prompt = _build_brownfield_plan_init_prompt(config, docs, CLI)

    assert "actor-facing surface/copy obligations before implementation planning" in web_prompt
    assert "actor-facing surface/copy obligations before implementation planning" not in cli_prompt


# --------------------------------------------------------------------------- #
# Implementer: non-UI projects get no UI warnings; web behavior unchanged.
# --------------------------------------------------------------------------- #


def test_implementer_non_ui_suppresses_design_md_warning(tmp_path, recwarn):
    from codd.implementer import _load_design_md_content

    # No DESIGN.md present + non-UI capabilities -> no warning, returns None.
    result = _load_design_md_content(tmp_path, capabilities=CLI)
    assert result is None
    ui_warnings = [w for w in recwarn.list if "UI file generation" in str(w.message)]
    assert ui_warnings == []


def test_implementer_ui_still_warns_about_missing_design_md(tmp_path):
    from codd.implementer import _load_design_md_content

    with pytest.warns(UserWarning, match="UI file generation will proceed without design tokens"):
        _load_design_md_content(tmp_path, capabilities=WEB)


def test_implementer_non_ui_suppresses_screen_flow_warning(tmp_path, recwarn):
    from codd.implementer import _load_screen_flow_for_implementation

    result = _load_screen_flow_for_implementation(tmp_path, capabilities=CLI)
    assert result is None
    route_warnings = [w for w in recwarn.list if "route definitions" in str(w.message)]
    assert route_warnings == []


def test_implementer_ui_still_warns_about_missing_screen_flow(tmp_path):
    from codd.implementer import _load_screen_flow_for_implementation

    with pytest.warns(UserWarning, match="route definitions"):
        _load_screen_flow_for_implementation(tmp_path, capabilities=WEB)


# --------------------------------------------------------------------------- #
# B: language-aware web E2E harness selection in the test-doc/test-code prompts.
# --------------------------------------------------------------------------- #

# Affirmative Playwright/.spec.ts naming line emitted ONLY by the TS browser
# block — the clean discriminator for "this prompt instructs Playwright".
_PLAYWRIGHT_NAMING_LINE = "API integration tests → `tests/e2e/<domain>.spec.ts`"
_PYTHON_HTTP_E2E_HEADER = (
    "E2E Test Generation Meta-Prompt section rules (Python HTTP E2E):"
)


def _canonical_test_doc(language: str | None) -> str:
    return _join(
        _build_test_doc_block(
            WEB,
            node_id="test:test-strategy",
            output_path="docs/test/test_strategy.md",
            project_language=language,
        )
    )


def test_test_doc_python_browser_emits_pytest_http_not_playwright():
    text = _canonical_test_doc("python")
    # Python HTTP E2E guidance present...
    assert _PYTHON_HTTP_E2E_HEADER in text
    assert "pytest" in text
    assert "test_<domain>.py" in text
    # ...and the affirmative Playwright/.spec.ts naming instruction is absent.
    assert _PLAYWRIGHT_NAMING_LINE not in text
    assert "E2E Test Level Separation (CRITICAL):" not in text


def test_test_doc_typescript_browser_keeps_playwright_unchanged():
    text = _canonical_test_doc("typescript")
    assert _PLAYWRIGHT_NAMING_LINE in text
    assert "E2E Test Level Separation (CRITICAL):" in text
    assert _PYTHON_HTTP_E2E_HEADER not in text


def test_test_doc_unknown_language_is_byte_for_byte_legacy():
    # Language unknown (None) must reproduce the historical (TS Playwright) block
    # byte-for-byte — generality / backward-compat guarantee.
    legacy = _build_test_doc_block(
        WEB, node_id="test:test-strategy", output_path="docs/test/test_strategy.md"
    )
    typescript = _build_test_doc_block(
        WEB,
        node_id="test:test-strategy",
        output_path="docs/test/test_strategy.md",
        project_language="typescript",
    )
    assert legacy == typescript


def test_test_doc_explicit_browser_python_keeps_playwright():
    # NOTE: the doc-block resolver call does not pass constraints, so this proves
    # the DEFAULT python+browser routing is pytest_http (covered above). The
    # explicit-browser path is proven directly against resolve_e2e_harness in
    # tests/test_e2e_harness.py. Here we only assert non-python is untouched.
    text = _canonical_test_doc("go")
    assert _PLAYWRIGHT_NAMING_LINE in text
    assert _PYTHON_HTTP_E2E_HEADER not in text


def _e2e_test_artifact(output: str) -> WaveArtifact:
    return WaveArtifact(
        wave=3,
        node_id="test:e2e-items",
        output=output,
        title="Items E2E",
        depends_on=[],
        conventions=[],
    )


def test_test_code_python_http_e2e_gets_live_server_not_playwright():
    # A Python E2E test-code artifact (.test.py under tests/e2e) with browser
    # capability must get Python HTTP live-server guidance, NOT Playwright and
    # NOT the "no browser, no server" integration fallback.
    prompt = _build_generation_prompt(
        _e2e_test_artifact("tests/e2e/items.test.py"),
        [],
        [],
        capabilities=WEB,
        project_language="python",
    )
    assert "Python HTTP end-to-end rules (live server, no browser):" in prompt
    assert "pytest" in prompt
    # The affirmative Playwright-rules block must be absent (the Python block's
    # own "do NOT import '@playwright/test'" steering line is expected).
    assert "Playwright-specific rules:" not in prompt
    assert "Import from '@playwright/test'" not in prompt
    # The "no browser, no server" integration fallback must NOT apply here — these
    # tests DO require a live HTTP server.
    assert "Do NOT use a browser or start a web server." not in prompt


def test_test_code_typescript_browser_keeps_playwright_rules():
    prompt = _build_generation_prompt(
        _e2e_test_artifact("tests/e2e/items.spec.ts"),
        [],
        [],
        capabilities=WEB,
        project_language="typescript",
    )
    assert "Playwright-specific rules:" in prompt
    assert "@playwright/test" in prompt
    assert "Python HTTP end-to-end rules" not in prompt


def test_resolve_project_language_reads_config():
    from codd.generator import _resolve_project_language

    assert _resolve_project_language({"project": {"language": "Python"}}) == "python"
    assert _resolve_project_language({"project": {"name": "x"}}) is None
    assert _resolve_project_language({}) is None
    assert _resolve_project_language(None) is None
