"""Extract user journeys for generated E2E tests from project documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class UserScenario:
    """A user-facing scenario that can become one E2E test case."""

    name: str
    steps: list[str]
    routes: list[str]
    acceptance_criteria: list[str]
    priority: str = "medium"


@dataclass
class ScenarioCollection:
    scenarios: list[UserScenario] = field(default_factory=list)
    source_screen_flow: Optional[str] = None
    source_requirements: Optional[str] = None


_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>.+?)\s*$")
_ROUTE_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:(?:route|screen|page|path|ルート|画面)\s*[:：]\s*)?"
    r"(?P<route>/[^\s`#]+)(?:\s+[-:–]\s*(?P<title>.+))?\s*$",
    re.IGNORECASE,
)
_FIELD_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\*\*)?(?P<key>[^:*：]+?)(?:\*\*)?\s*[:：]\s*(?P<value>.+?)\s*$"
)
_ROUTE_TOKEN_RE = re.compile(r"(?<![:\w])/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*")
_REQ_ID_RE = re.compile(r"\b(?P<id>(?:FR|NFR|REQ|AC)-[A-Za-z0-9_-]+)\b", re.IGNORECASE)
_USER_STORY_RE = re.compile(r"\bAs\s+an?\s+.+?\bI\s+want\s+.+", re.IGNORECASE)
_PRIORITY_RE = re.compile(r"\bpriority\s*[:：]\s*(high|medium|low)\b", re.IGNORECASE)

_COMPONENT_KEYS = {"component", "view", "pagecomponent", "コンポーネント"}
_ACTION_KEYS = {"action", "actions", "useraction", "useractions", "operation", "操作", "ユーザー操作"}
_TRANSITION_KEYS = {"transition", "transitions", "next", "nextroute", "遷移", "遷移先"}
_LOW_VALUE_WORDS = {"none", "n/a", "na", "-"}
_HIGH_KEYWORDS = {"auth", "login", "signup", "checkout", "payment", "admin", "security"}


class ScenarioExtractor:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def extract(
        self,
        screen_flow_path: Optional[Path] = None,
        requirements_path: Optional[Path] = None,
    ) -> ScenarioCollection:
        """Extract scenarios from screen-flow.md and requirements.md.

        Missing inputs are valid: the result records only existing sources and
        returns an empty scenario list when there is no route data.
        """
        sf_path = screen_flow_path or self.project_root / "docs" / "extracted" / "screen-flow.md"
        req_path = requirements_path or self.project_root / "docs" / "requirements" / "requirements.md"

        collection = ScenarioCollection(
            source_screen_flow=str(sf_path) if sf_path.exists() else None,
            source_requirements=str(req_path) if req_path.exists() else None,
        )

        routes = self._parse_screen_flow(sf_path) if sf_path.exists() else []
        requirements = self._parse_requirements(req_path) if req_path.exists() else []
        collection.scenarios = self._generate_scenarios(routes, requirements)
        return collection

    def _parse_screen_flow(self, path: Path) -> list[dict]:
        """Parse route sections, user actions, and route tokens from screen-flow.md."""
        text = path.read_text(encoding="utf-8")
        routes_by_path: dict[str, dict] = {}
        current: dict | None = None

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            route_match = _ROUTE_HEADING_RE.match(line)
            if route_match:
                route = _normalize_route(route_match.group("route"))
                if not route:
                    current = None
                    continue
                current = routes_by_path.setdefault(route, _empty_route(route))
                title = route_match.group("title")
                if title and not current.get("title"):
                    current["title"] = title.strip()
                continue

            if current is None:
                continue

            field_match = _FIELD_RE.match(line)
            if not field_match:
                continue

            key = _normalize_key(field_match.group("key"))
            value = field_match.group("value").strip()
            if not value or value.lower() in _LOW_VALUE_WORDS:
                continue

            if key in _COMPONENT_KEYS:
                current["component"] = value
            elif key in _ACTION_KEYS:
                current["actions"].extend(_split_phrase_values(value))
            elif key in _TRANSITION_KEYS:
                current["transitions"].extend(_extract_routes_or_values(value))

        for route in _extract_route_tokens(text):
            routes_by_path.setdefault(route, _empty_route(route))

        return [_dedupe_route(route) for route in routes_by_path.values()]

    def _parse_requirements(self, path: Path) -> list[dict]:
        """Parse user stories and acceptance criteria from requirements.md."""
        text = _strip_frontmatter(path.read_text(encoding="utf-8"))
        requirements = []

        for title, body in _iter_markdown_sections(text):
            requirement = _requirement_from_section(title, body)
            if requirement is not None:
                requirements.append(requirement)

        if requirements:
            return requirements

        story_requirements = []
        for index, line in enumerate(text.splitlines(), start=1):
            cleaned = _clean_list_marker(line)
            if _USER_STORY_RE.search(cleaned):
                story_requirements.append(
                    {
                        "id": f"story-{index}",
                        "title": cleaned,
                        "user_story": cleaned,
                        "acceptance_criteria": [cleaned],
                        "priority": "medium",
                    }
                )
        return story_requirements

    def _generate_scenarios(self, routes: list[dict], requirements: list[dict]) -> list[UserScenario]:
        """Combine screen-flow routes with matching requirement criteria."""
        screen_routes = [route for route in routes if _is_user_screen_route(route.get("route", ""))]
        if not screen_routes:
            return []

        scenarios = []
        for route in screen_routes:
            matched_requirement = _best_requirement_for_route(route, requirements)
            criteria = list(matched_requirement.get("acceptance_criteria", [])) if matched_requirement else []
            scenario_routes = _ordered_routes([route["route"], *route.get("transitions", [])])
            steps = _steps_for_route(route, criteria)

            scenarios.append(
                UserScenario(
                    name=_scenario_name(route, matched_requirement),
                    steps=steps,
                    routes=scenario_routes,
                    acceptance_criteria=criteria,
                    priority=_scenario_priority(route, matched_requirement),
                )
            )

        return scenarios

    def save_scenarios(
        self,
        collection: ScenarioCollection,
        output_path: Optional[Path] = None,
    ) -> Path:
        """Write extracted scenarios to docs/e2e/scenarios.md."""
        out_path = output_path or self.project_root / "docs" / "e2e" / "scenarios.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_render_scenarios_markdown(collection), encoding="utf-8")
        return out_path


def _empty_route(route: str) -> dict:
    return {"route": route, "title": "", "component": "", "actions": [], "transitions": []}


def _normalize_key(key: str) -> str:
    return re.sub(r"[\s_\-]+", "", key.strip().strip("*").lower())


def _normalize_route(route: str) -> str:
    normalized = route.strip().strip("`\"'")
    normalized = normalized.rstrip(".,;。、)")
    if not normalized or not normalized.startswith("/"):
        return ""
    normalized = normalized.rstrip("/") or "/"
    return normalized


def _extract_route_tokens(text: str) -> list[str]:
    return _ordered_routes(_normalize_route(match.group(0)) for match in _ROUTE_TOKEN_RE.finditer(text))


def _split_phrase_values(value: str) -> list[str]:
    normalized = value.replace("→", ",").replace("=>", ",")
    parts = re.split(r"\s*(?:,|;|\||\n)\s*", normalized)
    return [part.strip().strip("- ") for part in parts if part.strip().strip("- ")]


def _extract_routes_or_values(value: str) -> list[str]:
    routes = _extract_route_tokens(value)
    if routes:
        return routes
    return _split_phrase_values(value)


def _dedupe_route(route: dict) -> dict:
    route["actions"] = _dedupe_strings(route.get("actions", []))
    route["transitions"] = _ordered_routes(route.get("transitions", []))
    return route


def _dedupe_strings(values: list[str]) -> list[str]:
    seen = set()
    deduped = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in seen:
            deduped.append(cleaned)
            seen.add(cleaned)
    return deduped


def _ordered_routes(values) -> list[str]:
    seen = set()
    routes = []
    for value in values:
        route = _normalize_route(str(value))
        if route and route not in seen:
            routes.append(route)
            seen.add(route)
    return routes


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    after = text.find("\n", end + 4)
    return text[after + 1 :] if after != -1 else ""


def _iter_markdown_sections(text: str):
    title = ""
    body: list[str] = []
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            if title:
                yield title, body
            title = heading.group("title").strip()
            body = []
        elif title:
            body.append(line)
    if title:
        yield title, body


def _requirement_from_section(title: str, body: list[str]) -> dict | None:
    body_text = "\n".join(body)
    req_id_match = _REQ_ID_RE.search(title)
    story = _extract_user_story(body)
    criteria = _extract_acceptance_criteria(body)

    if not req_id_match and not story and not criteria:
        return None

    req_id = req_id_match.group("id") if req_id_match else _slug_id(title)
    clean_title = _clean_requirement_title(title)
    return {
        "id": req_id,
        "title": clean_title,
        "user_story": story,
        "acceptance_criteria": criteria or ([story] if story else []),
        "priority": _extract_priority(title, body_text),
    }


def _extract_user_story(lines: list[str]) -> str | None:
    for line in lines:
        cleaned = _clean_list_marker(line)
        lowered = cleaned.lower()
        if lowered.startswith("user story:"):
            return cleaned.split(":", 1)[1].strip()
        if cleaned.startswith("ユーザーストーリー:") or cleaned.startswith("ユーザーストーリー："):
            return re.split(r"[:：]", cleaned, maxsplit=1)[1].strip()
        story_match = _USER_STORY_RE.search(cleaned)
        if story_match:
            return story_match.group(0).strip()
    return None


def _extract_acceptance_criteria(lines: list[str]) -> list[str]:
    criteria = []
    in_acceptance_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading = stripped.strip("*").strip().lower()
        if heading.startswith("user story:") or heading.startswith("priority:") or heading.startswith("ユーザーストーリー"):
            continue
        if "acceptance" in heading or "受入" in heading or "受け入れ" in heading:
            in_acceptance_block = True
            continue

        cleaned = _clean_list_marker(stripped)
        if not cleaned or cleaned == stripped and not in_acceptance_block:
            if not _looks_like_requirement_sentence(cleaned):
                continue

        if in_acceptance_block or _looks_like_requirement_sentence(cleaned):
            criteria.append(cleaned)

    return _dedupe_strings(criteria)


def _clean_list_marker(line: str) -> str:
    return re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()


def _looks_like_requirement_sentence(text: str) -> bool:
    lowered = text.lower()
    return any(
        token in lowered
        for token in (
            " shall ",
            " must ",
            " should ",
            " can ",
            " acceptance",
            "display",
            "navigate",
            "submit",
            "verify",
            "ログイン",
            "表示",
            "入力",
            "遷移",
            "完了",
            "できる",
        )
    )


def _clean_requirement_title(title: str) -> str:
    title = re.sub(r"^\s*(?:FR|NFR|REQ|AC)-[A-Za-z0-9_-]+\s*[:：-]\s*", "", title, flags=re.IGNORECASE)
    return title.strip()


def _extract_priority(title: str, body_text: str) -> str:
    match = _PRIORITY_RE.search(f"{title}\n{body_text}")
    if match:
        return match.group(1).lower()
    combined = f"{title}\n{body_text}".lower()
    if "critical" in combined or "must" in combined:
        return "high"
    if "may " in combined or "optional" in combined:
        return "low"
    return "medium"


def _slug_id(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"req:{slug or 'untitled'}"


def _best_requirement_for_route(route: dict, requirements: list[dict]) -> dict | None:
    if not requirements:
        return None

    keywords = _route_keywords(route)
    best_score = 0
    best_requirement = None
    for requirement in requirements:
        haystack = " ".join(
            [
                requirement.get("title", ""),
                requirement.get("user_story") or "",
                " ".join(requirement.get("acceptance_criteria", [])),
            ]
        ).lower()
        score = sum(1 for keyword in keywords if keyword and keyword in haystack)
        if score > best_score:
            best_score = score
            best_requirement = requirement

    return best_requirement if best_score > 0 else None


def _route_keywords(route: dict) -> set[str]:
    values = [route.get("route", ""), route.get("title", ""), route.get("component", "")]
    keywords = set()
    for value in values:
        for token in re.split(r"[^A-Za-z0-9]+", value.lower()):
            if token and token not in {"id", "route", "page", "screen"}:
                keywords.add(token)
    return keywords


def _is_user_screen_route(route: str) -> bool:
    return route != "/api" and not route.startswith("/api/")


def _steps_for_route(route: dict, criteria: list[str]) -> list[str]:
    route_path = route["route"]
    steps = [f"Open {route_path}."]

    actions = route.get("actions", [])
    if actions:
        steps.extend(_sentence(action) for action in actions)
    else:
        label = route.get("component") or route.get("title") or route_path
        steps.append(f"Review the {label} screen.")

    for transition in route.get("transitions", []):
        steps.append(f"Navigate to {transition}.")

    for criterion in criteria[:3]:
        steps.append(f"Verify: {criterion}")

    return _dedupe_strings(steps)


def _sentence(value: str) -> str:
    cleaned = value.strip()
    if cleaned.endswith((".", "!", "?", "。")):
        return cleaned
    return f"{cleaned}."


def _scenario_name(route: dict, requirement: dict | None) -> str:
    if requirement and requirement.get("title"):
        return f"{requirement['title']} via {route['route']}"
    label = route.get("component") or route.get("title") or route["route"]
    return f"{label} user journey"


def _scenario_priority(route: dict, requirement: dict | None) -> str:
    if requirement and requirement.get("priority") in {"high", "medium", "low"}:
        return requirement["priority"]
    route_text = " ".join([route.get("route", ""), route.get("component", ""), route.get("title", "")]).lower()
    if any(keyword in route_text for keyword in _HIGH_KEYWORDS):
        return "high"
    return "medium"


def _render_scenarios_markdown(collection: ScenarioCollection) -> str:
    lines = ["# E2E Scenarios", ""]
    lines.append(f"- Source screen flow: {collection.source_screen_flow or 'not found'}")
    lines.append(f"- Source requirements: {collection.source_requirements or 'not found'}")
    lines.append("")

    if not collection.scenarios:
        lines.append("_No scenarios extracted._")
        lines.append("")
        return "\n".join(lines)

    for index, scenario in enumerate(collection.scenarios, start=1):
        lines.append(f"## {index}. {scenario.name}")
        lines.append(f"- Priority: {scenario.priority}")
        lines.append(f"- Routes: {' -> '.join(f'`{route}`' for route in scenario.routes) or 'none'}")
        lines.append("")
        lines.append("### Steps")
        for step_index, step in enumerate(scenario.steps, start=1):
            lines.append(f"{step_index}. {step}")
        lines.append("")
        lines.append("### Acceptance Criteria")
        if scenario.acceptance_criteria:
            for criterion in scenario.acceptance_criteria:
                lines.append(f"- {criterion}")
        else:
            lines.append("- No matching requirement criteria found.")
        lines.append("")

    return "\n".join(lines)
