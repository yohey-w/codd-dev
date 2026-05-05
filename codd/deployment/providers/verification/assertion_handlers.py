"""Assertion handler registry for CDP journey steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import re
from typing import Any, ClassVar, Mapping


@dataclass(frozen=True)
class AssertionResult:
    """Result emitted by a CDP assertion handler."""

    passed: bool
    output: str = ""
    observed: str = ""


class AssertionHandler(ABC):
    """Validate one declarative journey assertion step."""

    action_name: ClassVar[str]

    @abstractmethod
    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        """Return the assertion result for ``step``."""


ASSERTION_HANDLERS: dict[str, type[AssertionHandler]] = {}


def register_assertion_handler(action: str):
    """Register an assertion handler class under ``action``."""

    def decorator(cls: type[AssertionHandler]) -> type[AssertionHandler]:
        ASSERTION_HANDLERS[action] = cls
        return cls

    return decorator


def _evaluate(cdp_session: Any, expression: str) -> Any:
    response = cdp_session.send_command("Runtime.evaluate", {"expression": expression})
    return _remote_value(response)


def _remote_value(response: Any) -> Any:
    value = response
    if isinstance(value, Mapping) and "result" in value:
        value = value["result"]
    if isinstance(value, Mapping) and "result" in value:
        value = value["result"]
    if isinstance(value, Mapping) and "value" in value:
        return value["value"]
    if isinstance(value, Mapping) and "description" in value:
        return value["description"]
    return value


def _parse_cookie_line(raw_value: Any) -> dict[str, str]:
    entries: dict[str, str] = {}
    for item in str(raw_value or "").split(";"):
        name, separator, value = item.strip().partition("=")
        if separator and name:
            entries[name] = value
    return entries


def _state_observed(target: str, key: str, value: Any) -> str:
    value_text = "<missing>" if value is None else str(value)
    return f"{target}[{key}]={value_text}"


def _dom_state(raw_value: Any) -> Mapping[str, Any]:
    if isinstance(raw_value, Mapping):
        return raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return {"exists": False, "raw": raw_value}
        if isinstance(parsed, Mapping):
            return parsed
    return {"exists": False, "raw": raw_value}


def _dom_observed(selector: str, state: Mapping[str, Any]) -> str:
    if not state.get("exists"):
        return f"selector={selector} exists=false"
    display = state.get("display", "")
    visibility = state.get("visibility", "")
    opacity = state.get("opacity", "")
    return (
        f"selector={selector} exists=true "
        f"display={display} visibility={visibility} opacity={opacity}"
    )


def _opacity_visible(raw_value: Any) -> bool:
    try:
        return float(raw_value) > 0
    except (TypeError, ValueError):
        return str(raw_value) not in {"", "0", "0.0"}


@register_assertion_handler("expect_url")
class ExpectUrlAssertion(AssertionHandler):
    action_name = "expect_url"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        expected = str(step["value"])
        mode = str(step.get("mode", "startsWith"))
        observed = str(_evaluate(cdp_session, "location.href") or "")

        if mode == "startsWith":
            passed = observed.startswith(expected)
        elif mode == "contains":
            passed = expected in observed
        elif mode == "regex":
            passed = re.search(expected, observed) is not None
        elif mode == "exact":
            passed = observed == expected
        else:
            return AssertionResult(
                passed=False,
                output=f"expected: url mode startsWith/contains/regex/exact, got: {mode}",
                observed=observed,
            )

        if passed:
            return AssertionResult(passed=True, observed=observed)
        return AssertionResult(
            passed=False,
            output=f"expected: url {mode} {expected}, got: {observed}",
            observed=observed,
        )


@register_assertion_handler("expect_browser_state")
class ExpectBrowserStateAssertion(AssertionHandler):
    action_name = "expect_browser_state"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        target = str(step["target"])
        key = str(step["key"])

        if target == "cookie":
            entries = _parse_cookie_line(_evaluate(cdp_session, "document.cookie"))
            actual = entries.get(key)
        elif target == "localStorage":
            actual = _evaluate(cdp_session, f"localStorage.getItem({json.dumps(key)})")
        elif target == "sessionStorage":
            actual = _evaluate(cdp_session, f"sessionStorage.getItem({json.dumps(key)})")
        else:
            return AssertionResult(
                passed=False,
                output=(
                    "expected: target cookie/localStorage/sessionStorage, "
                    f"got: {target}"
                ),
                observed=f"target={target}",
            )

        exists = actual is not None
        observed = _state_observed(target, key, actual)
        if "exists" in step:
            expected_exists = bool(step["exists"])
            if exists == expected_exists:
                return AssertionResult(passed=True, observed=observed)
            expected_text = "to exist" if expected_exists else "to be absent"
            got_text = "present" if exists else "missing"
            return AssertionResult(
                passed=False,
                output=f"expected: {target}[{key}] {expected_text}, got: {got_text}",
                observed=observed,
            )

        if "value" in step:
            expected = step["value"]
            if exists and str(actual) == str(expected):
                return AssertionResult(passed=True, observed=observed)
            return AssertionResult(
                passed=False,
                output=f"expected: {target}[{key}]={expected}, got: {actual}",
                observed=observed,
            )

        if exists:
            return AssertionResult(passed=True, observed=observed)
        return AssertionResult(
            passed=False,
            output=f"expected: {target}[{key}] to exist, got: missing",
            observed=observed,
        )


@register_assertion_handler("expect_dom_visible")
class ExpectDomVisibleAssertion(AssertionHandler):
    action_name = "expect_dom_visible"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        selector = str(step["selector"])
        selector_expr = json.dumps(selector)
        expression = (
            "(() => {"
            f"const element = document.querySelector({selector_expr});"
            "if (!element) return JSON.stringify({exists: false});"
            "const style = window.getComputedStyle(element);"
            "return JSON.stringify({"
            "exists: true,"
            "display: style.display,"
            "visibility: style.visibility,"
            "opacity: style.opacity"
            "});"
            "})()"
        )
        state = _dom_state(_evaluate(cdp_session, expression))
        observed = _dom_observed(selector, state)
        visible = (
            bool(state.get("exists"))
            and state.get("display") != "none"
            and state.get("visibility") not in {"hidden", "collapse"}
            and _opacity_visible(state.get("opacity"))
        )
        if visible:
            return AssertionResult(passed=True, observed=observed)
        return AssertionResult(
            passed=False,
            output=f"expected: selector {selector} visible, got: {observed}",
            observed=observed,
        )


__all__ = [
    "ASSERTION_HANDLERS",
    "AssertionHandler",
    "AssertionResult",
    "ExpectBrowserStateAssertion",
    "ExpectDomVisibleAssertion",
    "ExpectUrlAssertion",
    "register_assertion_handler",
]
