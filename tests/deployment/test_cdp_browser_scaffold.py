from __future__ import annotations

import json

import pytest

from codd.config import load_project_config
from codd.deployment import RuntimeStateKind, RuntimeStateNode
from codd.deployment.providers import (
    VERIFICATION_TEMPLATES,
    VerificationTemplate,
)
from codd.deployment.providers.verification.assertion_handlers import (
    ASSERTION_HANDLERS,
    AssertionHandler,
    AssertionResult,
    ExpectBrowserStateAssertion,
    ExpectDomVisibleAssertion,
    ExpectUrlAssertion,
    register_assertion_handler,
)
from codd.deployment.providers.verification.cdp_browser import CdpBrowser
from codd.deployment.providers.verification.cdp_engines import (
    BROWSER_ENGINES,
    BrowserEngine,
    register_browser_engine,
)
from codd.deployment.providers.verification.cdp_launchers import (
    CDP_LAUNCHERS,
    CdpLauncher,
    register_cdp_launcher,
)
from codd.deployment.providers.verification.curl import CurlTemplate
from codd.deployment.providers.verification.form_strategies import (
    FORM_STRATEGIES,
    FormInteractionStrategy,
    register_form_strategy,
)
from codd.deployment.providers.verification.playwright import PlaywrightTemplate


@pytest.fixture(autouse=True)
def restore_registries():
    templates = dict(VERIFICATION_TEMPLATES)
    engines = dict(BROWSER_ENGINES)
    launchers = dict(CDP_LAUNCHERS)
    strategies = dict(FORM_STRATEGIES)
    assertions = dict(ASSERTION_HANDLERS)
    yield
    VERIFICATION_TEMPLATES.clear()
    VERIFICATION_TEMPLATES.update(templates)
    BROWSER_ENGINES.clear()
    BROWSER_ENGINES.update(engines)
    CDP_LAUNCHERS.clear()
    CDP_LAUNCHERS.update(launchers)
    FORM_STRATEGIES.clear()
    FORM_STRATEGIES.update(strategies)
    ASSERTION_HANDLERS.clear()
    ASSERTION_HANDLERS.update(assertions)


def test_cdp_browser_template_registers():
    assert VERIFICATION_TEMPLATES["cdp_browser"] is CdpBrowser


def test_cdp_browser_satisfies_verification_template_contract():
    assert isinstance(CdpBrowser(), VerificationTemplate)


def test_cdp_browser_generate_test_command_returns_json_plan():
    runtime_state = RuntimeStateNode(
        identifier="runtime:server:app",
        kind=RuntimeStateKind.SERVER_RUNNING,
        target="https://example.test/app",
    )

    plan = json.loads(CdpBrowser().generate_test_command(runtime_state, "E2E"))

    assert plan == {
        "identifier": "runtime:server:app",
        "journey": None,
        "steps": [],
        "target": "https://example.test/app",
        "template": "cdp_browser",
        "test_kind": "e2e",
    }


def test_cdp_browser_execute_is_scaffold_only():
    with pytest.raises(NotImplementedError):
        CdpBrowser().execute("{}")


def test_browser_engine_is_abstract():
    with pytest.raises(TypeError):
        BrowserEngine()


def test_register_browser_engine_adds_class():
    @register_browser_engine("dummy")
    class DummyEngine(BrowserEngine):
        engine_name = "dummy"

        def cdp_endpoint(self, config):
            return str(config["endpoint"])

        def normalized_capabilities(self):
            return {"journey"}

    assert BROWSER_ENGINES["dummy"] is DummyEngine
    assert DummyEngine().cdp_endpoint({"endpoint": "ws://example.test"}) == "ws://example.test"
    assert DummyEngine().normalized_capabilities() == {"journey"}


def test_cdp_launcher_is_abstract():
    with pytest.raises(TypeError):
        CdpLauncher()


def test_register_cdp_launcher_adds_class():
    @register_cdp_launcher("dummy")
    class DummyLauncher(CdpLauncher):
        launcher_name = "dummy"

        def launch_command(self, browser_config):
            return ["start", str(browser_config["port"])]

        def teardown_command(self):
            return ["stop"]

        def is_alive(self, browser_config):
            return bool(browser_config.get("alive"))

    assert CDP_LAUNCHERS["dummy"] is DummyLauncher
    assert DummyLauncher().launch_command({"port": 9000}) == ["start", "9000"]
    assert DummyLauncher().teardown_command() == ["stop"]
    assert DummyLauncher().is_alive({"alive": True}) is True


def test_form_interaction_strategy_is_abstract():
    with pytest.raises(TypeError):
        FormInteractionStrategy()


def test_register_form_strategy_adds_class():
    @register_form_strategy("dummy")
    class DummyStrategy(FormInteractionStrategy):
        strategy_name = "dummy"

        def fill_input_js(self, selector: str, value: str) -> str:
            return f"fill {selector} {value}"

        def click_js(self, selector: str) -> str:
            return f"click {selector}"

        def submit_form_js(self, selector: str | None = None) -> str:
            return f"submit {selector or ''}".strip()

    assert FORM_STRATEGIES["dummy"] is DummyStrategy
    assert DummyStrategy().fill_input_js("#email", "a@example.test") == "fill #email a@example.test"
    assert DummyStrategy().click_js("button") == "click button"
    assert DummyStrategy().submit_form_js("form") == "submit form"


def test_assertion_handler_is_abstract():
    with pytest.raises(TypeError):
        AssertionHandler()


def test_builtin_assertion_handlers_register():
    assert ASSERTION_HANDLERS["expect_url"] is ExpectUrlAssertion
    assert ASSERTION_HANDLERS["expect_browser_state"] is ExpectBrowserStateAssertion
    assert ASSERTION_HANDLERS["expect_dom_visible"] is ExpectDomVisibleAssertion


def test_register_assertion_handler_adds_class():
    @register_assertion_handler("expect_dummy")
    class DummyAssertion(AssertionHandler):
        action_name = "expect_dummy"

        def assert_(self, cdp_session, step):
            return AssertionResult(passed=bool(step["passed"]), output="checked")

    result = DummyAssertion().assert_(None, {"passed": True})

    assert ASSERTION_HANDLERS["expect_dummy"] is DummyAssertion
    assert result == AssertionResult(passed=True, output="checked")


def test_verification_package_import_keeps_existing_templates_registered():
    import codd.deployment.providers.verification as verification_package

    assert verification_package.cdp_browser.CdpBrowser is CdpBrowser
    assert VERIFICATION_TEMPLATES["playwright"] is PlaywrightTemplate
    assert VERIFICATION_TEMPLATES["curl"] is CurlTemplate
    assert VERIFICATION_TEMPLATES["cdp_browser"] is CdpBrowser


def test_project_config_without_cdp_browser_section_still_loads(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text("project:\n  frameworks: []\n", encoding="utf-8")

    config = load_project_config(tmp_path)

    assert config["project"]["frameworks"] == []
    assert "scan" in config


def test_project_config_accepts_cdp_browser_passthrough(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        "verification:\n"
        "  templates:\n"
        "    cdp_browser:\n"
        "      launcher:\n"
        "        kind: external\n"
        "      timeout_seconds: 45\n",
        encoding="utf-8",
    )

    config = load_project_config(tmp_path)

    assert config["verification"]["templates"]["cdp_browser"] == {
        "launcher": {"kind": "external"},
        "timeout_seconds": 45,
    }
