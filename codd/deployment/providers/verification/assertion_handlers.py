"""Assertion handler registry for CDP journey steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping


@dataclass(frozen=True)
class AssertionResult:
    """Result emitted by a CDP assertion handler."""

    passed: bool
    output: str = ""


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


@register_assertion_handler("expect_url")
class ExpectUrlAssertion(AssertionHandler):
    action_name = "expect_url"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        raise NotImplementedError("expect_url assertion is implemented in a later phase")


@register_assertion_handler("expect_browser_state")
class ExpectBrowserStateAssertion(AssertionHandler):
    action_name = "expect_browser_state"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        raise NotImplementedError("expect_browser_state assertion is implemented in a later phase")


@register_assertion_handler("expect_dom_visible")
class ExpectDomVisibleAssertion(AssertionHandler):
    action_name = "expect_dom_visible"

    def assert_(self, cdp_session: Any, step: Mapping[str, Any]) -> AssertionResult:
        raise NotImplementedError("expect_dom_visible assertion is implemented in a later phase")


__all__ = [
    "ASSERTION_HANDLERS",
    "AssertionHandler",
    "AssertionResult",
    "ExpectBrowserStateAssertion",
    "ExpectDomVisibleAssertion",
    "ExpectUrlAssertion",
    "register_assertion_handler",
]
