"""Generate E2E test stubs from extracted user scenarios."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import yaml

from codd.e2e_extractor import ScenarioCollection, UserScenario


_DEFAULT_E2E_RUNNER = "playwright"
SUPPORTED_FRAMEWORKS = {"playwright", "cypress"}


@dataclass
class GeneratedTest:
    """Metadata for a generated E2E test file."""

    scenario_name: str
    file_name: str
    content: str
    routes: list[str]
    steps_count: int
    framework: str = "playwright"


class TestGenerator:
    """Render Playwright or Cypress test stubs from ``UserScenario`` objects."""

    __test__ = False

    def __init__(
        self,
        project_root: Path,
        base_url: str = "http://localhost:3000",
        framework: str = "playwright",
    ):
        self.project_root = Path(project_root)
        self.base_url = base_url.rstrip("/") or "http://localhost:3000"
        self.framework = _normalize_framework(framework)

    def generate(
        self,
        collection: ScenarioCollection,
        output_dir: Optional[Path] = None,
    ) -> list[GeneratedTest]:
        """Write generated test files for all scenarios in ``collection``."""
        out_dir = output_dir or self.project_root / "docs" / "e2e" / "tests"
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        generated: list[GeneratedTest] = []
        used_names: dict[str, int] = {}
        context = _load_generation_context(self.project_root)
        for scenario in collection.scenarios:
            test = self._generate_test(scenario, context=context)
            file_name = _dedupe_file_name(test.file_name, used_names)
            if file_name != test.file_name:
                test = GeneratedTest(
                    scenario_name=test.scenario_name,
                    file_name=file_name,
                    content=test.content,
                    routes=test.routes,
                    steps_count=test.steps_count,
                    framework=test.framework,
                )
            (out_dir / test.file_name).write_text(test.content, encoding="utf-8")
            generated.append(test)

        return generated

    def _generate_test(self, scenario: UserScenario, context: "GenerationContext | None" = None) -> GeneratedTest:
        """Convert one ``UserScenario`` into one generated test file."""
        file_name = self._scenario_to_filename(scenario.name)
        render_context = context or _load_generation_context(self.project_root)
        if self.framework == "cypress":
            content = self._render_cypress_test(scenario, render_context)
        else:
            content = self._render_playwright_test(scenario, render_context)
        return GeneratedTest(
            scenario_name=scenario.name,
            file_name=file_name,
            content=content,
            routes=list(scenario.routes),
            steps_count=len(scenario.steps),
            framework=self.framework,
        )

    def _scenario_to_filename(self, name: str) -> str:
        """Convert a scenario name to a stable test file name."""
        slug = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip().lower()
        slug = re.sub(r"[-\s]+", "_", slug).strip("_")
        suffix = "cy.ts" if self.framework == "cypress" else "spec.ts"
        return f"test_{slug or 'scenario'}.{suffix}"

    def _render_playwright_test(self, scenario: UserScenario, context: "GenerationContext | None" = None) -> str:
        """Render a Playwright TypeScript test."""
        render_context = context or _load_generation_context(self.project_root)
        comments = _render_header_comments(scenario, render_context, self.framework)
        steps_code = _render_playwright_steps(scenario)
        first_url = self._initial_url(scenario)

        return (
            'import { test, expect } from "@playwright/test";\n'
            "\n"
            f"{comments}\n"
            f"test({_ts_string(scenario.name)}, async ({{ page }}) => {{\n"
            f"  await page.goto({_ts_string(first_url)});\n"
            '  await expect(page).not.toHaveURL("about:blank");\n'
            f"{steps_code}"
            "\n"
            "  // TODO: Add assertions based on acceptance criteria.\n"
            "});\n"
        )

    def _render_cypress_test(self, scenario: UserScenario, context: "GenerationContext | None" = None) -> str:
        """Render a Cypress TypeScript test."""
        render_context = context or _load_generation_context(self.project_root)
        comments = _render_header_comments(scenario, render_context, self.framework)
        steps_code = _render_cypress_steps(scenario)
        first_url = self._initial_url(scenario)

        return (
            f"{comments}\n"
            f"describe({_ts_string(scenario.name)}, () => {{\n"
            f"  it({_ts_string('completes the user journey')}, () => {{\n"
            f"    cy.visit({_ts_string(first_url)});\n"
            '    cy.location("href").should("not.eq", "about:blank");\n'
            f"{steps_code}"
            "\n"
            "    // TODO: Add assertions based on acceptance criteria.\n"
            "  });\n"
            "});\n"
        )

    def _initial_url(self, scenario: UserScenario) -> str:
        route = scenario.routes[0] if scenario.routes else ""
        return urljoin(f"{self.base_url}/", route.lstrip("/")) if route else self.base_url


class TransitionTestGenerator:
    """Generate E2E tests for screen transitions from screen-transitions.yaml."""

    __test__ = False

    def __init__(self, project_root: Path, config: dict | None = None):
        self.project_root = Path(project_root)
        self.config = config if config is not None else _load_optional_project_config(self.project_root)
        e2e_config = self.config.get("e2e", {})
        if not isinstance(e2e_config, dict):
            e2e_config = {}
        runner = e2e_config.get("test_runner", _DEFAULT_E2E_RUNNER)
        self.runner = str(runner or _DEFAULT_E2E_RUNNER).strip().lower()

    def load_transitions(self) -> list[dict]:
        """Load edges from docs/extracted/screen-transitions.yaml."""
        path = self.project_root / "docs" / "extracted" / "screen-transitions.yaml"
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return []
        edges = data.get("edges", [])
        if not isinstance(edges, list):
            return []
        return [edge for edge in edges if isinstance(edge, dict)]

    def generate_test_file(self, transitions: list[dict]) -> str:
        """Generate spec file content for all transitions."""
        if not transitions:
            return ""
        renderer = _TRANSITION_TEST_RENDERERS.get(
            self.runner,
            _TRANSITION_TEST_RENDERERS[_DEFAULT_E2E_RUNNER],
        )
        return renderer(transitions)

    def write_tests(self, output_path: Path | None = None) -> Path:
        """Write generated test file. Default: tests/e2e/screen_transitions.spec.ts."""
        transitions = self.load_transitions()
        content = self.generate_test_file(transitions)
        if output_path is None:
            output_path = self.project_root / "tests" / "e2e" / "screen_transitions.spec.ts"
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        return output_path


@dataclass
class GenerationContext:
    design_token_hints: list[str]
    lexicon_hints: list[str]


def load_scenarios_from_markdown(path: Path) -> ScenarioCollection:
    """Parse the ``docs/e2e/scenarios.md`` format emitted by ScenarioExtractor."""
    path = Path(path)
    collection = ScenarioCollection()
    if not path.exists():
        return collection

    text = path.read_text(encoding="utf-8")
    collection.source_screen_flow = _extract_source_line(text, "Source screen flow")
    collection.source_requirements = _extract_source_line(text, "Source requirements")
    collection.scenarios = [_scenario_from_block(name, block) for name, block in _iter_scenario_blocks(text)]
    return collection


def _scenario_from_block(name: str, block: str) -> UserScenario:
    priority = _extract_field(block, "Priority") or "medium"
    routes = _parse_routes(_extract_field(block, "Routes") or "")
    steps = _extract_markdown_list_section(block, "Steps")
    acceptance = [
        item
        for item in _extract_markdown_list_section(block, "Acceptance Criteria")
        if item != "No matching requirement criteria found."
    ]
    if not steps and routes:
        steps = [f"Open {routes[0]}."]

    return UserScenario(
        name=name,
        steps=steps,
        routes=routes,
        acceptance_criteria=acceptance,
        priority=priority if priority in {"high", "medium", "low"} else "medium",
    )


def _iter_scenario_blocks(text: str):
    matches = list(re.finditer(r"^##\s+(?:\d+\.\s*)?(?P<name>.+?)\s*$", text, flags=re.MULTILINE))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        yield match.group("name").strip(), text[start:end]


def _extract_source_line(text: str, label: str) -> str | None:
    match = re.search(rf"^-\s+{re.escape(label)}:\s*(?P<value>.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    value = match.group("value").strip()
    return None if value == "not found" else value


def _extract_field(block: str, label: str) -> str | None:
    match = re.search(rf"^-\s+{re.escape(label)}:\s*(?P<value>.+?)\s*$", block, flags=re.MULTILINE)
    return match.group("value").strip() if match else None


def _parse_routes(value: str) -> list[str]:
    quoted = re.findall(r"`([^`]+)`", value)
    if quoted:
        return [_normalize_route(route) for route in quoted if _normalize_route(route)]
    return [_normalize_route(part) for part in re.split(r"\s*(?:->|,|\|)\s*", value) if _normalize_route(part)]


def _extract_markdown_list_section(block: str, heading: str) -> list[str]:
    heading_match = re.search(rf"^###\s+{re.escape(heading)}\s*$", block, flags=re.MULTILINE)
    if not heading_match:
        return []

    section = block[heading_match.end() :]
    next_heading = re.search(r"^###\s+", section, flags=re.MULTILINE)
    if next_heading:
        section = section[: next_heading.start()]

    items: list[str] = []
    for line in section.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
        if cleaned:
            items.append(cleaned)
    return items


def _render_header_comments(scenario: UserScenario, context: GenerationContext, framework: str) -> str:
    lines = [
        f"// Scenario: {_clean_comment(scenario.name)}",
        f"// Framework: {framework}",
        f"// Routes: {', '.join(scenario.routes) if scenario.routes else 'unknown'}",
        f"// Priority: {scenario.priority}",
        "// AUTO-GENERATED by codd e2e generate. Replace selectors before running.",
    ]

    lines.append("// Acceptance:")
    if scenario.acceptance_criteria:
        lines.extend(f"// - {_clean_comment(item)}" for item in scenario.acceptance_criteria)
    else:
        lines.append("// - N/A")

    if context.design_token_hints:
        lines.append("// DESIGN.md assertion candidates:")
        lines.extend(f"// - {_clean_comment(item)}" for item in context.design_token_hints)

    if context.lexicon_hints:
        lines.append("// Project lexicon hints:")
        lines.extend(f"// - {_clean_comment(item)}" for item in context.lexicon_hints)

    return "\n".join(lines)


def _render_playwright_steps(scenario: UserScenario) -> str:
    if not scenario.steps:
        return "\n  // TODO: Implement user journey steps.\n  await page.waitForLoadState(\"networkidle\");\n"

    chunks = []
    for index, step in enumerate(scenario.steps, start=1):
        chunks.append(
            f"\n  // Step {index}: {_clean_comment(step)}\n"
            "  // TODO: Replace with role, aria-label, or data-testid selectors.\n"
            '  await page.waitForLoadState("networkidle");\n'
        )
    return "".join(chunks)


def _render_cypress_steps(scenario: UserScenario) -> str:
    if not scenario.steps:
        return "\n    // TODO: Implement user journey steps.\n"

    chunks = []
    for index, step in enumerate(scenario.steps, start=1):
        chunks.append(
            f"\n    // Step {index}: {_clean_comment(step)}\n"
            "    // TODO: Replace with role, aria-label, or data-testid selectors.\n"
        )
    return "".join(chunks)


def _load_generation_context(project_root: Path) -> GenerationContext:
    return GenerationContext(
        design_token_hints=_load_design_token_hints(project_root),
        lexicon_hints=_load_lexicon_hints(project_root),
    )


def _load_optional_project_config(project_root: Path) -> dict:
    try:
        from codd.config import load_project_config

        return load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        return {}


def _render_playwright_transition_test_file(transitions: list[dict]) -> str:
    lines = [
        'import { test, expect } from "@playwright/test";',
        "",
        "// Auto-generated by CoDD e2e_generator transition mode",
        "// Source: docs/extracted/screen-transitions.yaml",
        "",
    ]
    for edge in transitions:
        from_route = str(edge.get("from", "") or "")
        to_route = str(edge.get("to", "") or "")
        trigger = str(edge.get("trigger", "") or "").strip()
        kind = str(edge.get("type", "link") or "link")
        test_name = f"transition: {from_route} -> {to_route} ({kind})"
        lines.extend(
            [
                f"test({_ts_string(test_name)}, async ({{ page }}) => {{",
                f"  await page.goto({_ts_string(from_route)});",
            ]
        )
        if trigger:
            lines.append(f"  await page.click({_ts_string(trigger)});")
        lines.extend(
            [
                f"  await expect(page).toHaveURL({_ts_string(to_route)});",
                "});",
                "",
            ]
        )
    return "\n".join(lines)


_TRANSITION_TEST_RENDERERS = {
    _DEFAULT_E2E_RUNNER: _render_playwright_transition_test_file,
}


def _load_design_token_hints(project_root: Path) -> list[str]:
    hints: list[str] = []
    design_md_path = Path(project_root) / "DESIGN.md"
    if design_md_path.exists():
        from codd.design_md import DesignMdExtractor

        result = DesignMdExtractor().extract(design_md_path)
        for token in result.tokens[:6]:
            hints.append(f"{token.id} = {_display_token_value(token.value)}")

    tokens_json_path = Path(project_root) / "tokens.json"
    if tokens_json_path.exists():
        try:
            data = json.loads(tokens_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        for token_id, value in _flatten_token_json(data)[:6]:
            hint = f"{token_id} = {value}"
            if hint not in hints:
                hints.append(hint)

    return hints[:6]


def _load_lexicon_hints(project_root: Path) -> list[str]:
    from codd.generator import _inject_lexicon

    sentinel = "__CODD_E2E_GENERATION_PROMPT__"
    injected = _inject_lexicon(sentinel, project_root)
    if injected == sentinel:
        return []

    context = injected.split("---", 1)[0]
    hints = []
    for line in context.splitlines():
        cleaned = line.strip().lstrip("-").strip()
        if cleaned and not cleaned.startswith("#"):
            hints.append(cleaned)
        if len(hints) >= 6:
            break
    return hints


def _flatten_token_json(data, prefix: str = "") -> list[tuple[str, object]]:
    if not isinstance(data, dict):
        return []

    flattened: list[tuple[str, object]] = []
    for key, value in data.items():
        token_id = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and "value" in value:
            flattened.append((token_id, value["value"]))
        elif isinstance(value, dict):
            flattened.extend(_flatten_token_json(value, token_id))
    return flattened


def _display_token_value(value) -> object:
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _dedupe_file_name(file_name: str, used_names: dict[str, int]) -> str:
    count = used_names.get(file_name, 0) + 1
    used_names[file_name] = count
    if count == 1:
        return file_name
    for suffix in (".spec.ts", ".cy.ts"):
        if file_name.endswith(suffix):
            return f"{file_name[: -len(suffix)]}_{count}{suffix}"
    path = Path(file_name)
    return f"{path.stem}_{count}{path.suffix}"


def _normalize_framework(framework: str) -> str:
    normalized = framework.strip().lower()
    if normalized not in SUPPORTED_FRAMEWORKS:
        raise ValueError(f"unsupported E2E framework: {framework}")
    return normalized


def _normalize_route(route: str) -> str:
    cleaned = route.strip().strip("`\"'")
    if cleaned in {"", "none", "unknown"}:
        return ""
    return cleaned if cleaned.startswith("/") else f"/{cleaned}"


def _ts_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clean_comment(value: str) -> str:
    return " ".join(str(value).replace("*/", "* /").split())
