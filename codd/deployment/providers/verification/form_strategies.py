"""Form interaction strategy registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class FormInteractionStrategy(ABC):
    """Build script snippets for declarative journey form steps."""

    strategy_name: ClassVar[str]

    @abstractmethod
    def fill_input_js(self, selector: str, value: str) -> str:
        """Return a script snippet that fills an input."""

    @abstractmethod
    def click_js(self, selector: str) -> str:
        """Return a script snippet that clicks an element."""

    @abstractmethod
    def submit_form_js(self, selector: str | None = None) -> str:
        """Return a script snippet that submits a form."""


FORM_STRATEGIES: dict[str, type[FormInteractionStrategy]] = {}


def register_form_strategy(name: str):
    """Register a form interaction strategy class under ``name``."""

    def decorator(cls: type[FormInteractionStrategy]) -> type[FormInteractionStrategy]:
        FORM_STRATEGIES[name] = cls
        return cls

    return decorator


__all__ = [
    "FORM_STRATEGIES",
    "FormInteractionStrategy",
    "register_form_strategy",
]
