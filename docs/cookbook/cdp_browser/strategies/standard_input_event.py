"""Standard DOM input event strategy for the CDP browser cookbook."""

from __future__ import annotations

import json
from typing import ClassVar

from codd.deployment.providers.verification.form_strategies import (
    FormInteractionStrategy,
    register_form_strategy,
)


@register_form_strategy("standard_input_event")
class StandardInputEventStrategy(FormInteractionStrategy):
    """Fill controls with DOM value assignment and input/change events."""

    strategy_name: ClassVar[str] = "standard_input_event"

    def fill_input_js(self, selector: str, value: str) -> str:
        selector_js = json.dumps(selector)
        value_js = json.dumps(value)
        return (
            "(() => {"
            f"const element = document.querySelector({selector_js});"
            "if (!element) throw new Error('input not found');"
            f"element.value = {value_js};"
            "element.dispatchEvent(new Event('input', { bubbles: true }));"
            "element.dispatchEvent(new Event('change', { bubbles: true }));"
            "return true;"
            "})()"
        )

    def click_js(self, selector: str) -> str:
        selector_js = json.dumps(selector)
        return (
            "(() => {"
            f"const element = document.querySelector({selector_js});"
            "if (!element) throw new Error('element not found');"
            "element.click();"
            "return true;"
            "})()"
        )

    def submit_form_js(self, selector: str | None = None) -> str:
        if selector:
            selector_js = json.dumps(selector)
            form_lookup = f"document.querySelector({selector_js})"
        else:
            form_lookup = "document.activeElement && document.activeElement.closest('form')"
        return (
            "(() => {"
            f"const form = {form_lookup};"
            "if (!form) throw new Error('form not found');"
            "if (typeof form.requestSubmit === 'function') { form.requestSubmit(); }"
            "else { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); }"
            "return true;"
            "})()"
        )
