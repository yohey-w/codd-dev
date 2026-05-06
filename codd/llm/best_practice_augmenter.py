"""Suggest inferred implementation steps omitted by design documents."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
from pathlib import Path
from typing import Any, ClassVar, Mapping

from codd.dag import Node
from codd.deployment.providers.ai_command import AiCommandError, SubprocessAiCommand
from codd.llm.impl_step_deriver import (
    ImplStep,
    parse_impl_steps,
    render_impl_steps_for_prompt,
    task_yaml,
    utc_timestamp,
)
from codd.llm.plan_deriver import design_doc_bundle


DEFAULT_PROVIDER_NAME = "subprocess_ai_command"
DEFAULT_TEMPLATE_PATH = Path(__file__).with_name("templates") / "best_practice_augment_meta.md"
BEST_PRACTICE_AUGMENTERS: dict[str, type["BestPracticeAugmenter"]] = {}
LOGGER = logging.getLogger(__name__)


class BestPracticeAugmenter(ABC):
    provider_name: ClassVar[str]

    @abstractmethod
    def suggest_implicit_steps(
        self,
        task: Any,
        design_docs: list[Node],
        explicit_steps: list[Any],
        project_context: dict,
    ) -> list[Any]:
        ...


def register_best_practice_augmenter(name: str):
    registry_name = name.strip()
    if not registry_name:
        raise ValueError("best practice augmenter name is required")

    def decorator(cls: type[BestPracticeAugmenter]) -> type[BestPracticeAugmenter]:
        if not issubclass(cls, BestPracticeAugmenter):
            raise TypeError("registered best practice augmenter must subclass BestPracticeAugmenter")
        BEST_PRACTICE_AUGMENTERS[registry_name] = cls
        cls.provider_name = registry_name
        return cls

    return decorator


@register_best_practice_augmenter(DEFAULT_PROVIDER_NAME)
class SubprocessAiCommandBestPracticeAugmenter(BestPracticeAugmenter):
    provider_name = DEFAULT_PROVIDER_NAME

    def __init__(
        self,
        ai_command: Any | None = None,
        *,
        provider_id: str | None = None,
        template_path: Path | str | None = None,
        model: str | None = None,
    ) -> None:
        self.ai_command = ai_command
        self._provider_id = provider_id
        self.template_path = Path(template_path) if template_path is not None else DEFAULT_TEMPLATE_PATH
        self.model = model

    def suggest_implicit_steps(
        self,
        task: Any,
        design_docs: list[Node],
        explicit_steps: list[Any],
        project_context: dict,
    ) -> list[Any]:
        if not design_docs:
            return []

        project_root = Path(str(project_context.get("project_root") or Path.cwd())).resolve()
        template_text = self.template_path.read_text(encoding="utf-8")
        prompt = template_text.replace("{design_doc_bundle}", design_doc_bundle(design_docs, {"project_root": project_root}))
        prompt = prompt.replace("{task_yaml}", task_yaml(task))
        prompt = prompt.replace("{explicit_steps}", render_impl_steps_for_prompt(explicit_steps))
        prompt = prompt.replace(
            "{project_context}",
            json.dumps(project_context.get("project_context", {}), sort_keys=True),
        )

        try:
            raw_output = self._invoke(prompt)
        except AiCommandError as exc:
            LOGGER.warning("Best practice augmentation command failed: %s", exc)
            return []

        provider_id = self._resolve_provider_id(project_context)
        steps = parse_impl_steps(
            raw_output,
            provider_id=provider_id,
            generated_at=utc_timestamp(),
            default_source_design_section="best_practice_augmenter",
            inferred=True,
        )
        return [_coerce_implicit_step(step) for step in steps]

    def _invoke(self, prompt: str) -> str:
        command = self.ai_command or SubprocessAiCommand()
        if hasattr(command, "invoke"):
            try:
                return str(command.invoke(prompt, model=self.model))
            except TypeError:
                return str(command.invoke(prompt))
        if callable(command):
            return str(command(prompt))
        raise TypeError("ai_command must be callable or expose invoke()")

    def _resolve_provider_id(self, project_context: Mapping[str, Any]) -> str:
        if self._provider_id:
            return self._provider_id
        command = self.ai_command
        if command is not None and hasattr(command, "provider_id"):
            provider_id = command.provider_id
            if callable(provider_id):
                try:
                    return str(provider_id(model=self.model))
                except TypeError:
                    return str(provider_id())
        context_provider = project_context.get("provider_id")
        return str(context_provider or self.provider_name)


def _coerce_implicit_step(step: Any) -> Any:
    data = step.to_dict()
    data["inferred"] = True
    data["confidence"] = _bounded_confidence(data.get("confidence"))
    data["best_practice_category"] = str(data.get("best_practice_category") or "general")
    return ImplStep.from_dict(data)


def _bounded_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 1.0
    return min(1.0, max(0.0, confidence))


__all__ = [
    "BEST_PRACTICE_AUGMENTERS",
    "BestPracticeAugmenter",
    "SubprocessAiCommandBestPracticeAugmenter",
    "register_best_practice_augmenter",
]
