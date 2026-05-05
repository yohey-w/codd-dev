"""Prompt construction helpers for LLM-derived verification planning."""

from __future__ import annotations

from pathlib import Path
import re


_PLACEHOLDER_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


class PromptBuilder:
    """Build prompts from the bundled neutral meta-instruction template."""

    DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("templates") / "meta_instruction.md"

    def __init__(self, template_path: str | Path | None = None) -> None:
        self.template_path = Path(template_path) if template_path is not None else self.DEFAULT_TEMPLATE_PATH

    def build(
        self,
        design_doc_content: str,
        domain_guidance: str | None = None,
        means_catalog_hint: str | None = None,
    ) -> str:
        """Return a prompt with optional guidance blocks omitted cleanly."""

        template = self.template_path.read_text(encoding="utf-8")
        return (
            template.replace("{domain_guidance_block}", _optional_block("DOMAIN GUIDANCE", domain_guidance))
            .replace("{means_catalog_hint}", _optional_block("VERIFICATION MEANS CATALOG", means_catalog_hint))
            .replace("{design_doc_content}", design_doc_content)
        )

    @staticmethod
    def extract_parameter_placeholders(prompt: str) -> list[str]:
        """Extract unique ``${VAR_NAME}`` placeholders in first-seen order."""

        seen: set[str] = set()
        placeholders: list[str] = []
        for match in _PLACEHOLDER_RE.finditer(prompt):
            token = match.group(0)
            if token in seen:
                continue
            seen.add(token)
            placeholders.append(token)
        return placeholders


def _optional_block(title: str, content: str | None) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    return f"{title}:\n{text}"


__all__ = ["PromptBuilder"]
