from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from codd.elicit.engine import ElicitEngine


@dataclass(frozen=True)
class LexiconStub:
    lexicon_name: str
    prompt_extension_content: str
    recommended_kinds: list[str] = field(default_factory=list)
    coverage_axes: list[dict] = field(default_factory=list)
    severity_rules: dict = field(default_factory=dict)


class RoutingAiCommand:
    def __init__(self):
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "LEXICON_TWO" in prompt:
            return json.dumps(
                {
                    "lexicon_coverage_report": {"axis_two": "covered"},
                    "findings": [
                        {
                            "id": "F-2",
                            "kind": "gap",
                            "severity": "medium",
                            "details": {"dimension": "axis_two"},
                        }
                    ],
                }
            )
        return json.dumps(
            {
                "lexicon_coverage_report": {"axis_one": "gap"},
                "findings": [
                    {
                        "id": "F-1",
                        "kind": "gap",
                        "severity": "medium",
                        "details": {"dimension": "axis_one"},
                    }
                ],
            }
        )


class SequencedAiCommand:
    def __init__(self, outputs: list[dict]):
        self.outputs = list(outputs)

    def invoke(self, prompt: str) -> str:
        return json.dumps(self.outputs.pop(0))


def _lexicon(name: str, marker: str, axis: str) -> LexiconStub:
    return LexiconStub(
        lexicon_name=name,
        prompt_extension_content=f"{marker} {{requirements_content}} {{project_lexicon}}",
        recommended_kinds=[f"{name}_gap"],
        coverage_axes=[{"axis_type": axis}],
    )


def test_engine_accepts_list_of_lexicon_configs(tmp_path: Path) -> None:
    ai = RoutingAiCommand()
    result = ElicitEngine(ai_command=ai).run(
        tmp_path,
        lexicon_config=[
            _lexicon("lexicon_one", "LEXICON_ONE", "axis_one"),
            _lexicon("lexicon_two", "LEXICON_TWO", "axis_two"),
        ],
    )

    assert [finding.id for finding in result.findings] == ["F-1", "F-2"]
    assert len(ai.prompts) == 2
    assert result.lexicon_coverage_report == {"axis_one": "gap", "axis_two": "covered"}


def test_engine_attaches_lexicon_source_to_findings(tmp_path: Path) -> None:
    ai = RoutingAiCommand()
    result = ElicitEngine(ai_command=ai).run(
        tmp_path,
        lexicon_config=[
            _lexicon("lexicon_one", "LEXICON_ONE", "axis_one"),
            _lexicon("lexicon_two", "LEXICON_TWO", "axis_two"),
        ],
    )

    assert [finding.details["lexicon_source"] for finding in result.findings] == [
        "lexicon_one",
        "lexicon_two",
    ]


def test_engine_dedups_duplicate_dimensions_with_warning(tmp_path: Path) -> None:
    ai = SequencedAiCommand(
        [
            {
                "lexicon_coverage_report": {"shared_axis": "gap"},
                "findings": [
                    {
                        "id": "F-1",
                        "kind": "gap",
                        "severity": "medium",
                        "details": {"dimension": "shared_axis"},
                    }
                ],
            },
            {
                "lexicon_coverage_report": {"shared_axis": "gap"},
                "findings": [
                    {
                        "id": "F-2",
                        "kind": "gap",
                        "severity": "medium",
                        "details": {"dimension": "shared_axis"},
                    }
                ],
            },
        ]
    )
    first = _lexicon("lexicon_one", "LEXICON_ONE", "shared_axis")
    second = _lexicon("lexicon_two", "LEXICON_TWO", "shared_axis")

    with pytest.warns(RuntimeWarning, match="duplicate lexicon dimension"):
        result = ElicitEngine(ai_command=ai).run(tmp_path, lexicon_config=[first, second])

    assert [finding.id for finding in result.findings] == ["F-1"]
