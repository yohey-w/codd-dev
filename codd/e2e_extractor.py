"""Extract user journeys for generated E2E tests from project documents."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from codd.action_outcome import canonical_action_verb
from codd.requirements_meta import normalize_operation_flow, operation_flow_operations


@dataclass
class DodObligation:
    """One machine-checkable Definition of Done obligation for E2E evidence."""

    id: str
    text: str


@dataclass
class UserScenario:
    """A user-facing scenario that can become one E2E test case."""

    name: str
    steps: list[str]
    routes: list[str]
    acceptance_criteria: list[str]
    priority: str = "medium"
    kind: str = "user_journey"
    actor: str | None = None
    coverage_axis: str | None = None
    preconditions: list[str] = field(default_factory=list)
    trigger: str | None = None
    observable_outcomes: list[str] = field(default_factory=list)
    dod_obligations: list[DodObligation] = field(default_factory=list)
    source: str | None = None
    operation_id: str | None = None


@dataclass
class ScenarioCollection:
    scenarios: list[UserScenario] = field(default_factory=list)
    source_screen_flow: Optional[str] = None
    source_requirements: Optional[str] = None
    source_operation_flows: list[str] = field(default_factory=list)


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
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*", re.DOTALL)
_DOC_SUFFIXES = {".md", ".yaml", ".yml"}
_MUTATING_VERBS = {
    "create",
    "update",
    "delete",
    "submit",
    "approve",
    "assign",
    "publish",
    "revoke",
    "import",
    "send",
    "enable",
    "disable",
    "complete",
    "archive",
    "restore",
}
_TERMINAL_VERBS = {"complete", "delete", "disable", "archive", "revoke"}
_ACTOR_KEYS = ("actor", "actors", "role", "roles", "user", "users", "persona", "personas")
_DENIED_ACTOR_KEYS = (
    "denied_actors",
    "forbidden_actors",
    "unauthorized_actors",
    "not_allowed_actors",
    "not_allowed",
)
_OBSERVER_KEYS = (
    "observer",
    "observers",
    "visible_to",
    "visible_for",
    "affected_actor",
    "affected_actors",
    "handoff_to",
    "handoff",
)
_PRECONDITION_KEYS = ("preconditions", "precondition", "requires", "given", "setup", "state", "from_state")
_OUTCOME_KEYS = (
    "observable_outcomes",
    "expected_outcomes",
    "expected_outcome",
    "outcomes",
    "outcome",
    "postconditions",
    "postcondition",
    "result",
    "results",
    "to_state",
)
_ROUTE_KEYS = ("routes", "route", "screens", "screen", "paths", "path", "urls", "url")
_TRIGGER_KEYS = ("trigger", "control", "button", "command", "action")
_DERIVED_STATE_KEYS = (
    "measurement_source",
    "measurement",
    "observed_event",
    "observed_events",
    "durable_state",
    "durable_event",
    "durable_events",
    "readback",
    "read_model",
    "read_models",
    "derived_state",
    "derived_value",
    "derived_values",
    "aggregation",
    "aggregate",
    "consumer_surface",
    "consumer_surfaces",
    "consumers",
)
_THRESHOLD_KEYS = (
    "threshold",
    "thresholds",
    "boundary_case",
    "boundary_cases",
    "boundary",
    "boundaries",
    "timer",
    "timers",
    "duration",
    "durations",
    "score",
    "scores",
    "percentage",
    "percent",
    "completion_rule",
    "completion_rules",
)
_PUBLIC_BOUNDARY_ACCEPTANCE = (
    "Evidence exercises the actor-facing public trigger; direct storage writes, "
    "seed-only setup, or lower-layer helper/API shortcuts alone do not satisfy "
    "this scenario unless that lower layer is the declared public surface."
)
_CHAIN_READBACK_ACCEPTANCE = (
    "Evidence verifies producer -> durable state/event -> readback/consumer reflection, "
    "not only immediate request success."
)
_DERIVED_STATE_ACCEPTANCE = (
    "Evidence verifies measured or observed input -> durable state/event -> derived value/read model "
    "-> consumer surface, including latest/last readback when declared."
)
_THRESHOLD_BOUNDARY_ACCEPTANCE = (
    "Evidence covers behavior below, at, and above the declared threshold/timer/duration boundary "
    "where feasible, or records an explicit public-surface simulation rationale."
)
_PARTIAL_SIGNAL_ACCEPTANCE = (
    "Evidence exercises a minimal or partially unavailable source signal (for example missing, null, "
    "omitted, timeout, fallback, or provider-degraded values) instead of only an all-fields-present ideal "
    "stub; then verifies durable readback, downstream reflection, or an explicit failure outcome."
)
_SCENARIO_STATE_ISOLATION_ACCEPTANCE = (
    "Evidence establishes scenario-owned or idempotently reset preconditions before assertions; "
    "mutable shared seed state is not trusted unless it is recreated or proven unchanged, and "
    "test-created state is cleaned up."
)
_EVENTFUL_SOURCE_WORDS = {
    "adapter",
    "callback",
    "cron",
    "ended",
    "event",
    "external",
    "fallback",
    "iframe",
    "message",
    "notification",
    "pause",
    "player",
    "queue",
    "scheduler",
    "seeked",
    "sensor",
    "stream",
    "timeupdate",
    "timeout",
    "unload",
    "visibilitychange",
    "webhook",
    "worker",
}
_SOURCE_SIGNAL_KEYS = (
    "callback_payload",
    "degraded_inputs",
    "edge_cases",
    "event_payload",
    "fallback",
    "input_variants",
    "message_payload",
    "message_schema",
    "nullable_fields",
    "optional_fields",
    "payload",
    "source_signal",
    "source_signals",
)
_DOD_OBLIGATION_KEYS = (
    "dod_obligations",
    "definition_of_done",
    "e2e_dod",
    "machine_dod",
)


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

    def extract_operational(self) -> ScenarioCollection:
        """Extract MECE operational E2E scenarios from generic operation metadata."""
        flows = _operation_flows_from_project(self.project_root)
        collection = ScenarioCollection(source_operation_flows=[source for source, _flow in flows])
        collection.scenarios = self._generate_operational_scenarios(flows)
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

    def _generate_operational_scenarios(self, flows: list[tuple[str, Any]]) -> list[UserScenario]:
        """Generate scenario candidates across operational coverage axes."""
        scenarios: list[UserScenario] = []
        for source, raw_flow in flows:
            flow = normalize_operation_flow(raw_flow, source=source) or {}
            flow_actors = _actor_values(flow)
            for index, operation in enumerate(operation_flow_operations(flow)):
                operation_id = str(operation.get("id") or operation.get("name") or f"operation[{index}]")
                primary_actors = _actor_values(operation) or flow_actors or ["primary actor"]
                denied_actors = _values_from_keys(operation, _DENIED_ACTOR_KEYS)
                observers = _values_from_keys(operation, _OBSERVER_KEYS)
                verb = canonical_action_verb(operation.get("verb")) or canonical_action_verb(operation_id)
                target = _operation_target(operation)
                routes = _routes_from_operation(operation)
                trigger = _trigger_from_operation(operation, verb=verb, target=target)
                preconditions = _operation_preconditions(flow, operation)
                outcomes = _operation_outcomes(operation)
                priority = _operation_priority(operation, verb)

                for actor in primary_actors:
                    scenarios.append(
                        _operational_scenario(
                            name=f"{actor} {operation_id} success",
                            actor=actor,
                            axis="happy_path",
                            operation=operation,
                            source=source,
                            operation_id=operation_id,
                            routes=routes,
                            trigger=trigger,
                            preconditions=preconditions,
                            outcomes=outcomes,
                            priority=priority,
                            acceptance=[
                                f"{actor} can complete {operation_id}.",
                                _PUBLIC_BOUNDARY_ACCEPTANCE,
                                *_visible_outcome_acceptance(outcomes),
                            ],
                        )
                    )

                    if verb in _MUTATING_VERBS:
                        scenarios.append(
                            _operational_scenario(
                                name=f"{actor} {operation_id} readback",
                                actor=actor,
                                axis="persistence_readback",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=preconditions,
                                outcomes=outcomes,
                                priority=priority,
                                extra_steps=["Reload or reopen the relevant user surface.", "Verify the outcome is still visible."],
                                acceptance=[
                                    f"{operation_id} state change is still observable after readback.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    _CHAIN_READBACK_ACCEPTANCE,
                                    *_visible_outcome_acceptance(outcomes),
                                ],
                            )
                        )

                    if verb in _TERMINAL_VERBS:
                        scenarios.append(
                            _operational_scenario(
                                name=f"{actor} {operation_id} terminal guard",
                                actor=actor,
                                axis="terminal_state_guard",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=preconditions,
                                outcomes=outcomes,
                                priority=priority,
                                extra_steps=["Attempt the same terminal operation again."],
                                acceptance=[
                                    "The completed terminal state cannot be repeated inconsistently.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    "The UI or API exposes a clear blocked/disabled/no-op outcome.",
                                ],
                            )
                        )

                    if _has_derived_state_contract(operation, outcomes):
                        derived_contract = _derived_state_contract_values(operation)
                        derived_outcomes = _dedupe_strings([*outcomes, *derived_contract])
                        scenarios.append(
                            _operational_scenario(
                                name=f"{actor} {operation_id} derived state chain",
                                actor=actor,
                                axis="derived_state_chain",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=preconditions,
                                outcomes=derived_outcomes,
                                priority=priority,
                                extra_steps=[
                                    "Exercise the declared measurement or observation through the public trigger.",
                                    "Reload or reopen the declared read model or consumer surface.",
                                ],
                                acceptance=[
                                    f"{operation_id} derives the declared state from the measured or observed source.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    _DERIVED_STATE_ACCEPTANCE,
                                    *_visible_outcome_acceptance(derived_outcomes),
                                ],
                            )
                        )

                    if _has_threshold_contract(operation, outcomes):
                        threshold_contract = _threshold_contract_values(operation)
                        threshold_outcomes = _dedupe_strings([*outcomes, *threshold_contract])
                        scenarios.append(
                            _operational_scenario(
                                name=f"{actor} {operation_id} threshold boundary",
                                actor=actor,
                                axis="threshold_boundary",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=preconditions,
                                outcomes=threshold_outcomes,
                                priority="high",
                                extra_steps=[
                                    "Exercise a value below the declared threshold or boundary.",
                                    "Exercise a value at the declared threshold or boundary.",
                                    "Exercise a value above the declared threshold or boundary.",
                                ],
                                acceptance=[
                                    f"{operation_id} changes behavior only at the declared threshold or boundary.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    _THRESHOLD_BOUNDARY_ACCEPTANCE,
                                    *_visible_outcome_acceptance(threshold_outcomes),
                                ],
                            )
                        )

                    if _has_source_signal_contract(operation, trigger, outcomes):
                        signal_contract = _source_signal_contract_values(operation)
                        signal_outcomes = _dedupe_strings([*outcomes, *signal_contract])
                        scenarios.append(
                            _operational_scenario(
                                name=f"{actor} {operation_id} partial source signal",
                                actor=actor,
                                axis="partial_signal_contract",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=preconditions,
                                outcomes=signal_outcomes,
                                priority="high",
                                extra_steps=[
                                    "Exercise the declared trigger with a minimal, missing, null, omitted, timeout, fallback, or provider-degraded source signal.",
                                    "Verify the operation does not silently pass with an all-fields-present ideal stub only.",
                                    "Reload or reopen the durable read model or consumer surface.",
                                ],
                                acceptance=[
                                    f"{operation_id} handles partial or unavailable source signals from the declared trigger.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    _PARTIAL_SIGNAL_ACCEPTANCE,
                                    _CHAIN_READBACK_ACCEPTANCE,
                                    *_visible_outcome_acceptance(signal_outcomes),
                                ],
                            )
                        )

                for denied_actor in denied_actors:
                    scenarios.append(
                        _operational_scenario(
                            name=f"{denied_actor} cannot {operation_id}",
                            actor=denied_actor,
                            axis="permission_boundary",
                            operation=operation,
                            source=source,
                            operation_id=operation_id,
                            routes=routes,
                            trigger=trigger,
                            preconditions=preconditions,
                            outcomes=[],
                            priority="high",
                            acceptance=[
                                f"{denied_actor} cannot complete {operation_id}.",
                                _PUBLIC_BOUNDARY_ACCEPTANCE,
                                "The forbidden action produces no persisted state change.",
                            ],
                        )
                    )

                for observer in observers:
                    for actor in primary_actors[:1]:
                        scenarios.append(
                            _operational_scenario(
                                name=f"{operation_id} reflected for {observer}",
                                actor=observer,
                                axis="cross_actor_reflection",
                                operation=operation,
                                source=source,
                                operation_id=operation_id,
                                routes=routes,
                                trigger=trigger,
                                preconditions=[*preconditions, f"{actor} has completed {operation_id}."],
                                outcomes=outcomes,
                                priority=priority,
                                acceptance=[
                                    f"{observer} observes the result of {actor} completing {operation_id}.",
                                    _PUBLIC_BOUNDARY_ACCEPTANCE,
                                    _CHAIN_READBACK_ACCEPTANCE,
                                    *_visible_outcome_acceptance(outcomes),
                                ],
                            )
                        )

        return _dedupe_operational_scenarios(scenarios)

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

    def save_operational_scenarios(
        self,
        collection: ScenarioCollection,
        output_path: Optional[Path] = None,
    ) -> Path:
        """Write extracted operational scenarios to docs/e2e/operational-scenarios.md."""
        out_path = output_path or self.project_root / "docs" / "e2e" / "operational-scenarios.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_render_operational_scenarios_markdown(collection), encoding="utf-8")
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


def _operation_flows_from_project(project_root: Path) -> list[tuple[str, Any]]:
    try:
        from codd.config import load_project_config

        config = load_project_config(project_root)
    except (FileNotFoundError, ValueError):
        config = {}

    flows: list[tuple[str, Any]] = []
    if isinstance(config.get("operation_flow"), Mapping):
        flows.append(("codd.yaml.operation_flow", config["operation_flow"]))

    for path in _configured_doc_files(project_root, config):
        payload = _frontmatter_or_yaml_payload(path)
        if not isinstance(payload, Mapping):
            continue
        source = _display_path(path, project_root)
        if isinstance(payload.get("operation_flow"), Mapping):
            flows.append((f"{source}.operation_flow", payload["operation_flow"]))
        codd_meta = payload.get("codd")
        if isinstance(codd_meta, Mapping) and isinstance(codd_meta.get("operation_flow"), Mapping):
            flows.append((f"{source}.codd.operation_flow", codd_meta["operation_flow"]))
    return flows


def _configured_doc_files(project_root: Path, config: Mapping[str, Any]) -> list[Path]:
    scan = config.get("scan", {})
    raw_dirs = scan.get("doc_dirs", ["docs/"]) if isinstance(scan, Mapping) else ["docs/"]
    dirs = raw_dirs if isinstance(raw_dirs, list) else ["docs/"]
    files: list[Path] = []
    for raw_dir in dirs:
        if not isinstance(raw_dir, str) or not raw_dir.strip():
            continue
        root = Path(raw_dir).expanduser()
        if not root.is_absolute():
            root = project_root / root
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix in _DOC_SUFFIXES:
                files.append(root)
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in _DOC_SUFFIXES:
                files.append(path)
    deduped: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def _frontmatter_or_yaml_payload(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if path.suffix == ".md":
        match = _FRONTMATTER_RE.search(text)
        if not match:
            return None
        raw = match.group(1)
    else:
        raw = text
    try:
        payload = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _actor_values(mapping: Mapping[str, Any]) -> list[str]:
    return _values_from_keys(mapping, _ACTOR_KEYS)


def _values_from_keys(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        if key in mapping:
            values.extend(_coerce_text_list(mapping.get(key)))
    return _dedupe_strings(values)


def _coerce_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return _split_phrase_values(value)
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                cleaned = item.strip()
                if cleaned:
                    items.append(cleaned)
                continue
            items.extend(_coerce_text_list(item))
        return items
    return [str(value).strip()] if str(value).strip() else []


def _operation_target(operation: Mapping[str, Any]) -> str:
    for key in ("target", "resource", "entity", "object", "subject", "item"):
        value = operation.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _routes_from_operation(operation: Mapping[str, Any]) -> list[str]:
    routes: list[str] = []
    for key in _ROUTE_KEYS:
        for value in _coerce_text_list(operation.get(key)):
            normalized = _normalize_route(value)
            if normalized:
                routes.append(normalized)
    return _ordered_routes(routes)


def _trigger_from_operation(operation: Mapping[str, Any], *, verb: str | None, target: str) -> str:
    for key in _TRIGGER_KEYS:
        value = operation.get(key)
        if value not in (None, ""):
            return _sentence(str(value))
    bits = [part for part in (verb, target) if part]
    return _sentence(" ".join(bits) if bits else "perform the declared operation")


def _operation_preconditions(flow: Mapping[str, Any], operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _PRECONDITION_KEYS:
        values.extend(_coerce_text_list(flow.get(key)))
        values.extend(_coerce_text_list(operation.get(key)))
    return _dedupe_strings(values)


def _operation_outcomes(operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _OUTCOME_KEYS:
        values.extend(_coerce_text_list(operation.get(key)))
    return _dedupe_strings(values)


def _operation_priority(operation: Mapping[str, Any], verb: str | None) -> str:
    raw = str(operation.get("priority") or "").strip().lower()
    if raw in {"high", "medium", "low"}:
        return raw
    if verb in {"delete", "disable", "revoke", "approve", "publish"}:
        return "high"
    return "medium"


def _visible_outcome_acceptance(outcomes: list[str]) -> list[str]:
    if outcomes:
        return [f"Visible outcome: {outcome}" for outcome in outcomes]
    return ["The result is observable without inspecting implementation internals."]


def _derived_state_contract_values(operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _DERIVED_STATE_KEYS:
        values.extend(_coerce_text_list(operation.get(key)))
    return _dedupe_strings(values)


def _threshold_contract_values(operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _THRESHOLD_KEYS:
        values.extend(_coerce_text_list(operation.get(key)))
    return _dedupe_strings(values)


def _source_signal_contract_values(operation: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _SOURCE_SIGNAL_KEYS:
        values.extend(_coerce_text_list(operation.get(key)))
    return _dedupe_strings(values)


def _has_derived_state_contract(operation: Mapping[str, Any], outcomes: list[str]) -> bool:
    if _derived_state_contract_values(operation):
        return True
    text = _operation_contract_text(operation, outcomes)
    return any(
        token in text
        for token in (
            "measure",
            "measured",
            "measurement",
            "observed",
            "telemetry",
            "derive",
            "derived",
            "aggregate",
            "aggregation",
            "read model",
            "read_model",
            "consumer",
            "percentage",
            "percent",
            "duration",
            "score",
            "latest",
            "last",
            "resume",
            "restore",
        )
    )


def _has_threshold_contract(operation: Mapping[str, Any], outcomes: list[str]) -> bool:
    if _threshold_contract_values(operation):
        return True
    text = _operation_contract_text(operation, outcomes)
    return any(
        token in text
        for token in (
            "threshold",
            "boundary",
            "timer",
            "duration",
            "score",
            "percentage",
            "percent",
        )
    )


def _has_source_signal_contract(operation: Mapping[str, Any], trigger: str, outcomes: list[str]) -> bool:
    if _source_signal_contract_values(operation):
        return True
    return _has_eventful_source_terms(trigger)


def _has_eventful_source_terms(value: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9_]+", value.lower().replace("-", "_")))
    return bool(tokens & _EVENTFUL_SOURCE_WORDS)


def _operation_contract_text(operation: Mapping[str, Any], outcomes: list[str]) -> str:
    values: list[str] = []
    for key in (
        "id",
        "name",
        "verb",
        "target",
        "trigger",
        "preconditions",
        "expected_outcomes",
        "outcomes",
        *_SOURCE_SIGNAL_KEYS,
        *_DERIVED_STATE_KEYS,
        *_THRESHOLD_KEYS,
        *_DOD_OBLIGATION_KEYS,
    ):
        values.extend(_coerce_text_list(operation.get(key)))
    values.extend(outcomes)
    return " ".join(values).lower().replace("-", "_")


def _default_dod_obligations(
    *,
    axis: str,
    operation: Mapping[str, Any],
    trigger: str,
    outcomes: list[str],
) -> list[DodObligation]:
    obligations = [
        DodObligation(
            id="scenario_state",
            text=(
                "The test establishes scenario-owned or idempotently reset preconditions before assertions; "
                "mutable shared seed state alone is not accepted."
            ),
        ),
        DodObligation(
            id="public_trigger",
            text=(
                f"The test exercises the declared actor-facing trigger ({trigger}); direct storage writes or "
                "helper/API shortcuts alone are accepted only when that lower layer is the declared public surface."
            ),
        ),
    ]

    if outcomes:
        obligations.append(
            DodObligation(
                id="observable_outcome",
                text="The test asserts the declared observable outcome from a user or consumer surface.",
            )
        )

    if axis in {"persistence_readback", "cross_actor_reflection", "derived_state_chain"}:
        obligations.append(
            DodObligation(
                id="durable_readback",
                text=(
                    "The test proves producer -> durable state/event -> readback, reload, or downstream "
                    "consumer reflection, not only immediate request success."
                ),
            )
        )

    if axis == "permission_boundary":
        obligations.append(
            DodObligation(
                id="no_forbidden_mutation",
                text="The denied actor cannot complete the operation and no forbidden state mutation is persisted.",
            )
        )

    if axis == "terminal_state_guard":
        obligations.append(
            DodObligation(
                id="terminal_guard",
                text="Repeating the terminal operation is blocked, disabled, or no-op without creating inconsistent state.",
            )
        )

    if axis == "threshold_boundary":
        obligations.append(
            DodObligation(
                id="boundary_values",
                text="The test covers below, at, and above the declared threshold or records an explicit public-surface simulation rationale.",
            )
        )

    if axis == "partial_signal_contract":
        obligations.append(
            DodObligation(
                id="partial_source_signal",
                text=(
                    "The test exercises a minimal, missing, null, omitted, timeout, fallback, or provider-degraded "
                    "source signal; an all-fields-present ideal stub is insufficient."
                ),
            )
        )

    return _dedupe_dod_obligations([*obligations, *_operation_dod_obligations(operation)])


def _operation_dod_obligations(operation: Mapping[str, Any]) -> list[DodObligation]:
    obligations: list[DodObligation] = []
    for key in _DOD_OBLIGATION_KEYS:
        obligations.extend(_coerce_dod_obligations(operation.get(key)))
    return _dedupe_dod_obligations(obligations)


def _coerce_dod_obligations(value: Any) -> list[DodObligation]:
    if value is None:
        return []
    if isinstance(value, str):
        return _parse_dod_obligations(_split_phrase_values(value))
    if isinstance(value, Mapping):
        obligations: list[DodObligation] = []
        for raw_id, raw_text in value.items():
            obligation_id = _normalize_obligation_id(str(raw_id))
            text = str(raw_text).strip()
            if obligation_id and text:
                obligations.append(DodObligation(id=obligation_id, text=text))
        return obligations
    if isinstance(value, (list, tuple, set)):
        obligations: list[DodObligation] = []
        for item in value:
            if isinstance(item, Mapping):
                raw_id = item.get("id") or item.get("name") or item.get("key")
                raw_text = item.get("text") or item.get("description") or item.get("criteria") or item.get("criterion")
                if raw_text is None:
                    raw_text = " ".join(_coerce_text_list(item))
                text = str(raw_text).strip()
                obligation_id = _normalize_obligation_id(str(raw_id or _slug_fragment(text)))
                if obligation_id and text:
                    obligations.append(DodObligation(id=obligation_id, text=text))
            else:
                obligations.extend(_parse_dod_obligations(_coerce_text_list(item)))
        return obligations
    return _parse_dod_obligations([str(value)])


def _parse_dod_obligations(items: list[str]) -> list[DodObligation]:
    obligations: list[DodObligation] = []
    for item in items:
        cleaned = item.strip()
        if not cleaned or cleaned == "No explicit DoD obligations declared.":
            continue
        if ":" in cleaned:
            raw_id, text = cleaned.split(":", 1)
        elif " - " in cleaned:
            raw_id, text = cleaned.split(" - ", 1)
        else:
            raw_id, text = _slug_fragment(cleaned), cleaned
        obligation_id = _normalize_obligation_id(raw_id)
        text = text.strip()
        if obligation_id and text:
            obligations.append(DodObligation(id=obligation_id, text=text))
    return _dedupe_dod_obligations(obligations)


def _normalize_obligation_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.:-]+", "_", value.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized


def _slug_fragment(value: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", value.lower())
    return "_".join(tokens[:5]) or "obligation"


def _dedupe_dod_obligations(obligations: list[DodObligation]) -> list[DodObligation]:
    seen: set[str] = set()
    deduped: list[DodObligation] = []
    for obligation in obligations:
        obligation_id = _normalize_obligation_id(obligation.id)
        text = obligation.text.strip()
        if not obligation_id or not text or obligation_id in seen:
            continue
        deduped.append(DodObligation(id=obligation_id, text=text))
        seen.add(obligation_id)
    return deduped


def _operational_scenario(
    *,
    name: str,
    actor: str,
    axis: str,
    operation: Mapping[str, Any],
    source: str,
    operation_id: str,
    routes: list[str],
    trigger: str,
    preconditions: list[str],
    outcomes: list[str],
    priority: str,
    acceptance: list[str],
    extra_steps: list[str] | None = None,
) -> UserScenario:
    steps = [f"Act as {actor}."]
    steps.append("Establish scenario-owned or idempotently reset test state before assertions.")
    steps.extend(f"Establish precondition: {item}" for item in preconditions)
    if routes:
        steps.append(f"Open {routes[0]}.")
    steps.append(f"Trigger {trigger}")
    steps.extend(f"Verify: {item}" for item in outcomes)
    if extra_steps:
        steps.extend(extra_steps)

    return UserScenario(
        name=name,
        steps=_dedupe_strings(steps),
        routes=routes,
        acceptance_criteria=_dedupe_strings([_SCENARIO_STATE_ISOLATION_ACCEPTANCE, *acceptance]),
        priority=priority,
        kind="operational",
        actor=actor,
        coverage_axis=axis,
        preconditions=preconditions,
        trigger=trigger,
        observable_outcomes=outcomes,
        dod_obligations=_default_dod_obligations(
            axis=axis,
            operation=operation,
            trigger=trigger,
            outcomes=outcomes,
        ),
        source=source,
        operation_id=operation_id,
    )


def _dedupe_operational_scenarios(scenarios: list[UserScenario]) -> list[UserScenario]:
    seen: set[tuple[str | None, str | None, str | None, str]] = set()
    deduped: list[UserScenario] = []
    for scenario in scenarios:
        key = (scenario.operation_id, scenario.actor, scenario.coverage_axis, "|".join(scenario.routes))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(scenario)
    return deduped


def _render_operational_scenarios_markdown(collection: ScenarioCollection) -> str:
    lines = [
        "# Operational E2E Scenarios",
        "",
        "These scenarios are generated from generic operation metadata. Run the whole suite, collect all failures, then repair after the full campaign finishes.",
        "Each scenario must establish scenario-owned or idempotently reset state before assertions so prior test runs cannot pollute the result.",
        "",
        "## MECE Coverage Axes",
        "- happy_path: the declared actor can complete the operation.",
        "- persistence_readback: mutating outcomes remain visible after reload or readback.",
        "- permission_boundary: denied actors cannot complete the operation or persist state.",
        "- terminal_state_guard: terminal actions cannot be repeated into inconsistent state.",
        "- cross_actor_reflection: observers see the result of another actor's operation.",
        "- derived_state_chain: measured or observed input flows through durable state/event into a derived read model or consumer surface.",
        "- threshold_boundary: thresholds, timers, durations, scores, percentages, or latest/last rules behave correctly around their boundary values.",
        "- partial_signal_contract: event, callback, queue, webhook, scheduler, adapter, provider, or external-source triggers handle minimal or partially unavailable source signals instead of only ideal full-payload stubs.",
        "",
    ]
    if collection.source_operation_flows:
        lines.append("## Sources")
        for source in collection.source_operation_flows:
            lines.append(f"- {source}")
        lines.append("")

    if not collection.scenarios:
        lines.append("_No operational scenarios extracted._")
        lines.append("")
        return "\n".join(lines)

    for index, scenario in enumerate(collection.scenarios, start=1):
        lines.append(f"## {index}. {scenario.name}")
        lines.append(f"- Kind: {scenario.kind}")
        lines.append(f"- Priority: {scenario.priority}")
        lines.append(f"- Actor: {scenario.actor or 'unspecified'}")
        lines.append(f"- Coverage Axis: {scenario.coverage_axis or 'unspecified'}")
        lines.append(f"- Source Operation: {scenario.source or 'unknown'}#{scenario.operation_id or 'unknown'}")
        lines.append(f"- Trigger: {scenario.trigger or 'unspecified'}")
        lines.append(f"- Routes: {' -> '.join(f'`{route}`' for route in scenario.routes) or 'none'}")
        lines.append("")
        lines.append("### Preconditions")
        if scenario.preconditions:
            lines.extend(f"- {item}" for item in scenario.preconditions)
        else:
            lines.append("- No explicit preconditions declared.")
        lines.append("")
        lines.append("### Steps")
        for step_index, step in enumerate(scenario.steps, start=1):
            lines.append(f"{step_index}. {step}")
        lines.append("")
        lines.append("### Observable Outcomes")
        if scenario.observable_outcomes:
            lines.extend(f"- {item}" for item in scenario.observable_outcomes)
        else:
            lines.append("- No explicit observable outcomes declared.")
        lines.append("")
        lines.append("### Acceptance Criteria")
        if scenario.acceptance_criteria:
            lines.extend(f"- {criterion}" for criterion in scenario.acceptance_criteria)
        else:
            lines.append("- No matching requirement criteria found.")
        lines.append("")
        lines.append("### DoD Obligations")
        if scenario.dod_obligations:
            lines.extend(f"- {obligation.id}: {obligation.text}" for obligation in scenario.dod_obligations)
        else:
            lines.append("- No explicit DoD obligations declared.")
        lines.append("")

    return "\n".join(lines)


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return str(path)
