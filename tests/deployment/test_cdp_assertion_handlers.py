from __future__ import annotations

import json

from codd.deployment.providers.verification.assertion_handlers import (
    ASSERTION_HANDLERS,
    AssertionResult,
    ExpectBrowserStateAssertion,
    ExpectDomVisibleAssertion,
    ExpectUrlAssertion,
)


class FakeCdpSession:
    def __init__(self, *values):
        self.values = list(values)
        self.calls = []

    def send_command(self, method, params):
        self.calls.append((method, params))
        value = self.values.pop(0)
        return {"result": {"result": {"value": value}}}


def _dom_value(**state):
    return json.dumps(state)


def test_builtin_handlers_are_registered():
    assert ASSERTION_HANDLERS["expect_url"] is ExpectUrlAssertion
    assert ASSERTION_HANDLERS["expect_browser_state"] is ExpectBrowserStateAssertion
    assert ASSERTION_HANDLERS["expect_dom_visible"] is ExpectDomVisibleAssertion


def test_expect_url_starts_with_passes_and_reads_current_location():
    session = FakeCdpSession("https://example.test/dashboard")

    result = ExpectUrlAssertion().assert_(
        session,
        {"value": "https://example.test"},
    )

    assert result == AssertionResult(
        passed=True,
        observed="https://example.test/dashboard",
    )
    assert session.calls == [
        ("Runtime.evaluate", {"expression": "location.href"}),
    ]


def test_expect_url_contains_passes():
    session = FakeCdpSession("https://example.test/account/settings")

    result = ExpectUrlAssertion().assert_(
        session,
        {"value": "/account", "mode": "contains"},
    )

    assert result.passed is True


def test_expect_url_regex_passes():
    session = FakeCdpSession("https://example.test/users/42")

    result = ExpectUrlAssertion().assert_(
        session,
        {"value": r"/users/\d+$", "mode": "regex"},
    )

    assert result.passed is True


def test_expect_url_exact_passes():
    session = FakeCdpSession("https://example.test/home")

    result = ExpectUrlAssertion().assert_(
        session,
        {"value": "https://example.test/home", "mode": "exact"},
    )

    assert result.passed is True


def test_expect_url_failure_reports_expected_and_observed():
    session = FakeCdpSession("https://example.test/settings")

    result = ExpectUrlAssertion().assert_(
        session,
        {"value": "https://example.test/home", "mode": "exact"},
    )

    assert result.passed is False
    assert "expected: url exact https://example.test/home" in result.output
    assert "got: https://example.test/settings" in result.output
    assert result.observed == "https://example.test/settings"


def test_expect_browser_state_cookie_exists_passes():
    session = FakeCdpSession("sid=abc; theme=dark")

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "cookie", "key": "sid", "exists": True},
    )

    assert result == AssertionResult(passed=True, observed="cookie[sid]=abc")
    assert session.calls == [
        ("Runtime.evaluate", {"expression": "document.cookie"}),
    ]


def test_expect_browser_state_cookie_value_passes():
    session = FakeCdpSession("sid=abc; theme=dark")

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "cookie", "key": "theme", "value": "dark"},
    )

    assert result.passed is True
    assert result.observed == "cookie[theme]=dark"


def test_expect_browser_state_local_storage_exists_passes():
    session = FakeCdpSession("enabled")

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "localStorage", "key": "flag", "exists": True},
    )

    assert result.passed is True
    assert session.calls == [
        ("Runtime.evaluate", {"expression": 'localStorage.getItem("flag")'}),
    ]


def test_expect_browser_state_local_storage_value_passes():
    session = FakeCdpSession("compact")

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "localStorage", "key": "layout", "value": "compact"},
    )

    assert result.passed is True
    assert result.observed == "localStorage[layout]=compact"


def test_expect_browser_state_session_storage_value_passes():
    session = FakeCdpSession("step-2")

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "sessionStorage", "key": "wizard", "value": "step-2"},
    )

    assert result.passed is True
    assert session.calls == [
        ("Runtime.evaluate", {"expression": 'sessionStorage.getItem("wizard")'}),
    ]


def test_expect_browser_state_missing_value_fails():
    session = FakeCdpSession(None)

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "localStorage", "key": "token", "value": "present"},
    )

    assert result.passed is False
    assert "expected: localStorage[token]=present" in result.output
    assert result.observed == "localStorage[token]=<missing>"


def test_expect_browser_state_exists_false_passes_when_missing():
    session = FakeCdpSession(None)

    result = ExpectBrowserStateAssertion().assert_(
        session,
        {"target": "sessionStorage", "key": "flash", "exists": False},
    )

    assert result.passed is True
    assert result.observed == "sessionStorage[flash]=<missing>"


def test_expect_dom_visible_passes_for_visible_element():
    session = FakeCdpSession(
        _dom_value(exists=True, display="block", visibility="visible", opacity="1")
    )

    result = ExpectDomVisibleAssertion().assert_(session, {"selector": "#submit"})

    assert result.passed is True
    assert "selector=#submit" in result.observed
    assert "display=block" in result.observed
    assert 'document.querySelector("#submit")' in session.calls[0][1]["expression"]


def test_expect_dom_visible_fails_when_display_hidden():
    session = FakeCdpSession(
        _dom_value(exists=True, display="none", visibility="visible", opacity="1")
    )

    result = ExpectDomVisibleAssertion().assert_(session, {"selector": "#panel"})

    assert result.passed is False
    assert "selector #panel visible" in result.output
    assert "display=none" in result.output


def test_expect_dom_visible_fails_when_visibility_hidden():
    session = FakeCdpSession(
        _dom_value(exists=True, display="block", visibility="hidden", opacity="1")
    )

    result = ExpectDomVisibleAssertion().assert_(session, {"selector": ".notice"})

    assert result.passed is False
    assert "visibility=hidden" in result.output


def test_expect_dom_visible_fails_when_opacity_zero():
    session = FakeCdpSession(
        _dom_value(exists=True, display="block", visibility="visible", opacity="0")
    )

    result = ExpectDomVisibleAssertion().assert_(session, {"selector": ".fade"})

    assert result.passed is False
    assert "opacity=0" in result.output


def test_expect_dom_visible_fails_when_element_is_missing():
    session = FakeCdpSession(_dom_value(exists=False))

    result = ExpectDomVisibleAssertion().assert_(session, {"selector": "#missing"})

    assert result.passed is False
    assert "selector #missing visible" in result.output
    assert "selector=#missing exists=false" in result.output
    assert result.observed == "selector=#missing exists=false"
