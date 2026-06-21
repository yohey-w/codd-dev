"""CoDD template generator driven by wave_config."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess  # noqa: F401 — kept: tests monkeypatch ``generator.subprocess.run``
from typing import Any

import yaml

from codd.ai_invoke import (
    DEFAULT_AI_COMMAND,
    invoke_ai,
    invoke_file_writing_agent,
    is_file_writing_agent,
    resolve_ai_command,
)
from codd.config import load_project_config
from codd.e2e_harness import resolve_e2e_harness
from codd.project_types import (
    ProjectCapabilities,
    load_capabilities,
    resolve_project_type,
)
from codd.requirements_meta import normalize_operation_flow


# Full-capability profile used as the backward-compatible default when a project
# does NOT declare a project type. Historically the generation pipeline assumed a
# web application unconditionally (UI + HTTP + browser E2E + long-running server),
# so an untyped/legacy project must keep producing exactly that output. Only when
# a project explicitly configures a known type do we switch to type-appropriate
# capabilities (e.g. a `cli` project drops browser E2E and server startup).
WEB_FALLBACK_CAPABILITIES = ProjectCapabilities(
    user_interface=True,
    network_surface="http",
    e2e_modality="browser",
    long_running_service=True,
)


# DEFAULT_AI_COMMAND moved to codd.ai_invoke (re-exported above for compat).
DEFAULT_RELATION = "depends_on"
DEFAULT_SEMANTIC = "governance"
# Bounded regeneration attempts when a generated body fails output validation.
# Overridable per project via `generate.body_retry.max_retries` in codd.yaml.
DEFAULT_BODY_MAX_RETRIES = 2
DOC_TYPE_BY_DIR = {
    "requirements": "requirement",
    "design": "design",
    "detailed_design": "design",
    "plan": "plan",
    "governance": "governance",
    "test": "test",
    "operations": "operations",
}
TYPE_SECTIONS = {
    "requirement": ["Overview", "Scope", "Open Questions"],
    "design": ["Overview", "Architecture", "Open Questions"],
    "plan": ["Overview", "Milestones", "Risks"],
    "governance": ["Overview", "Decision Log", "Follow-ups"],
    "test": ["Overview", "Acceptance Criteria", "Failure Criteria", "E2E Test Generation Meta-Prompt"],
    "operations": ["Overview", "Runbook", "Monitoring", "CI/CD Pipeline Generation Meta-Prompt"],
    "document": ["Overview", "Details", "Open Questions"],
}
DETAILED_DESIGN_SECTIONS = [
    "Overview",
    "Mermaid Diagrams",
    "Ownership Boundaries",
    "Implementation Implications",
    "Open Questions",
]

# Shared design-time guidance injected into design-document prompts. Both the
# greenfield generator and the brownfield restorer import this single definition
# so restored docs carry the same operation_flow / operational-behavior structure
# as generated ones (single source of truth — never copy-paste these lines).
OPERATIONAL_BEHAVIOR_MODEL_BLOCK = [
    "",
    "Operational Behavior Model (DESIGN-TIME, CRITICAL):",
    "- Do not postpone operational workflow discovery to E2E generation. The design document must define the actor/action/state/outcome model before implementation planning.",
    "- If dependency documents describe actors, permissions, mutable commands, measured/observed events, automatic triggers, thresholds, timers, callbacks, derived or aggregate read models, latest/last/resume state, cross-actor visibility, lifecycle states, or external side effects, include a `### Operational Behavior Model` subsection in the appropriate required section.",
    "- In that subsection, include one fenced YAML block with top-level `operation_flow:`. CoDD will lift this block into document metadata so downstream implementation and E2E generation share the same source of truth.",
    "- The `operation_flow.operations` entries must be generic and implementation-ready: `id`, `actor`, `verb`, `target`, `trigger`, `preconditions`, `expected_outcomes`, and, when applicable, `forbidden_actors`, `visible_to`, `route`/`path`, `ui_pattern`, lifecycle `from_state`/`to_state`, `measurement_source`, `durable_state`, `readback`, `consumer_surfaces`, `threshold`, `boundary_cases`, and `dod_obligations`.",
    "- For each non-trivial operation, include machine-checkable `dod_obligations` with stable `id` and concrete text derived from the requirements/design. Each obligation must be assertable by an E2E runner without human judgment; avoid vague wording such as 'works correctly', 'reasonable', or 'properly'.",
    "- For passive or automatic behavior, name the actor-facing public trigger (for example a user action, stream event, timer, callback, or system observation). A manual admin shortcut or direct storage write is not the same operation unless the requirements declare it as the public surface.",
    "- Enumerate operational obligations across these MECE axes before coding: happy path, persistence/readback, permission boundary, terminal-state guard, cross-actor reflection, derived-state/read-model chain, and threshold/boundary behavior.",
    "- This is not an E2E scenario list. E2E tests are only evidence generated later from the design-time operation model.",
    "",
    "Actor-Facing Surface/Copy Obligations (DESIGN-TIME, CRITICAL):",
    "- If dependency documents describe user-facing surfaces, roles/actors, navigation, onboarding/authentication, or visible user copy, define the actor-facing surface/copy obligations before implementation planning.",
    "- For each relevant surface, state its purpose, primary audience/actor, allowed actions/navigation, forbidden actions/navigation, required visible copy intent, and forbidden copy patterns.",
    "- User-visible copy must use the audience's job-to-be-done language. Do not expose implementation rationale, internal process notes, demo/test/sample labels, environment assumptions, or hidden authority-boundary explanations unless the audience's explicit task is to administer those boundaries.",
    "- Entry or pre-authentication surfaces must not expose ambiguous role-resolved or protected navigation; navigation must match the surface purpose and current access state.",
    "- When requirements use internal role identifiers and business/user-facing role names, prefer the business/user-facing labels in visible copy and document the mapping.",
]


def _operation_flow_field_list(capabilities: ProjectCapabilities) -> str:
    """Build the optional operation-flow field enumeration for a capability set.

    `route`/`path` & `ui_pattern` only make sense when the project exposes an
    HTTP surface or a user interface; a CLI operation's surface is a command, so
    we substitute generic `entry_point`/`invocation` fields instead.
    """

    if capabilities.network_surface == "http" or capabilities.user_interface:
        surface_fields = "`route`/`path`, `ui_pattern`, "
    else:
        surface_fields = "`entry_point`/`invocation`, "
    return (
        "- The `operation_flow.operations` entries must be generic and "
        "implementation-ready: `id`, `actor`, `verb`, `target`, `trigger`, "
        "`preconditions`, `expected_outcomes`, and, when applicable, "
        "`forbidden_actors`, `visible_to`, "
        f"{surface_fields}lifecycle `from_state`/`to_state`, "
        "`measurement_source`, `durable_state`, `readback`, "
        "`consumer_surfaces`, `threshold`, `boundary_cases`, and "
        "`dod_obligations`."
    )


def build_operational_behavior_model_block(
    capabilities: ProjectCapabilities | None = None,
) -> list[str]:
    """Build the design-time Operational Behavior Model guidance for a capability set.

    The universal operation_flow contract (id/actor/verb/target/trigger/
    preconditions/expected_outcomes/lifecycle/measurement/durable_state/readback/
    boundary_cases/dod) is ALWAYS emitted. UI/route-specific guidance is
    conditional:

    * route/path & ui_pattern emphasis only when ``network_surface == "http"`` or
      ``user_interface`` (otherwise described as generic entry_point/invocation).
    * the "Actor-Facing Surface/Copy Obligations" block only when
      ``user_interface`` is true.

    When ``capabilities`` is ``None`` the full backward-compatible (web) block is
    returned — identical to the legacy ``OPERATIONAL_BEHAVIOR_MODEL_BLOCK``.
    """

    if capabilities is None:
        capabilities = WEB_FALLBACK_CAPABILITIES

    block = [
        "",
        "Operational Behavior Model (DESIGN-TIME, CRITICAL):",
        "- Do not postpone operational workflow discovery to E2E generation. The design document must define the actor/action/state/outcome model before implementation planning.",
        "- If dependency documents describe actors, permissions, mutable commands, measured/observed events, automatic triggers, thresholds, timers, callbacks, derived or aggregate read models, latest/last/resume state, cross-actor visibility, lifecycle states, or external side effects, include a `### Operational Behavior Model` subsection in the appropriate required section.",
        "- In that subsection, include one fenced YAML block with top-level `operation_flow:`. CoDD will lift this block into document metadata so downstream implementation and E2E generation share the same source of truth.",
        _operation_flow_field_list(capabilities),
        "- For each non-trivial operation, include machine-checkable `dod_obligations` with stable `id` and concrete text derived from the requirements/design. Each obligation must be assertable by an E2E runner without human judgment; avoid vague wording such as 'works correctly', 'reasonable', or 'properly'.",
        "- For passive or automatic behavior, name the actor-facing public trigger (for example a user action, stream event, timer, callback, or system observation). A manual admin shortcut or direct storage write is not the same operation unless the requirements declare it as the public surface.",
        "- Enumerate operational obligations across these MECE axes before coding: happy path, persistence/readback, permission boundary, terminal-state guard, cross-actor reflection, derived-state/read-model chain, and threshold/boundary behavior.",
        "- This is not an E2E scenario list. E2E tests are only evidence generated later from the design-time operation model.",
    ]

    if capabilities.user_interface:
        block.extend(
            [
                "",
                "Actor-Facing Surface/Copy Obligations (DESIGN-TIME, CRITICAL):",
                "- If dependency documents describe user-facing surfaces, roles/actors, navigation, onboarding/authentication, or visible user copy, define the actor-facing surface/copy obligations before implementation planning.",
                "- For each relevant surface, state its purpose, primary audience/actor, allowed actions/navigation, forbidden actions/navigation, required visible copy intent, and forbidden copy patterns.",
                "- User-visible copy must use the audience's job-to-be-done language. Do not expose implementation rationale, internal process notes, demo/test/sample labels, environment assumptions, or hidden authority-boundary explanations unless the audience's explicit task is to administer those boundaries.",
                "- Entry or pre-authentication surfaces must not expose ambiguous role-resolved or protected navigation; navigation must match the surface purpose and current access state.",
                "- When requirements use internal role identifiers and business/user-facing role names, prefer the business/user-facing labels in visible copy and document the mapping.",
            ]
        )

    return block


def _resolve_generation_capabilities(
    config: dict[str, Any] | None,
    project_root: Path | None,
) -> ProjectCapabilities:
    """Resolve the capabilities the generation prompts should adapt to.

    Reads the configured project type from ``required_artifacts.project_type`` or
    ``project.type`` (the same keys the artifact deriver / completeness auditor
    consult) and loads its capability profile. When NO project type is
    configured we return the full-capability web fallback so legacy/untyped
    projects keep producing today's web-style prompts (backward compatibility).
    """

    if not isinstance(config, dict):
        return WEB_FALLBACK_CAPABILITIES

    configured = ""
    required_artifacts = config.get("required_artifacts")
    if isinstance(required_artifacts, dict):
        configured = str(required_artifacts.get("project_type") or "").strip().lower()
    if not configured:
        project_section = config.get("project")
        if isinstance(project_section, dict):
            configured = str(project_section.get("type") or "").strip().lower()
    if not configured:
        configured = str(config.get("project_type") or "").strip().lower()

    if not configured:
        # Untyped project: preserve the historical web-application behavior.
        return WEB_FALLBACK_CAPABILITIES

    resolved, _reason = resolve_project_type(configured, None, project_root)
    return load_capabilities(resolved, project_root)


def _resolve_project_language(config: dict[str, Any] | None) -> str | None:
    """Resolve the project's host language for language-aware harness selection.

    Reads ``project.language`` from ``codd.yaml`` (the same key the scanner
    consults, where it defaults to ``python``). Returns ``None`` when no config /
    language is present so callers keep legacy (language-unknown) behavior — a
    ``None`` language never switches the browser E2E harness away from Playwright.
    """

    if not isinstance(config, dict):
        return None
    project_section = config.get("project")
    if not isinstance(project_section, dict):
        return None
    language = project_section.get("language")
    if not isinstance(language, str):
        return None
    normalized = language.strip().lower()
    return normalized or None


# Test-doc traceability guidance — applies to every project type regardless of
# capabilities (unit + integration coverage, traceability, operation_flow). It
# is ROLE-AWARE: exactly one generated test document (the canonical doc,
# ``docs/test/test_strategy.md`` / node ``test:test-strategy``) owns the
# verifiable-behavior id namespace and declares every VB once in a first-column
# ``VB-*`` table. Every other test document (e.g. acceptance_criteria.md) is
# reference-only: it must NOT mint a parallel ``VB-*`` table — it references the
# canonical ids in a later column. This prevents two docs from declaring the
# same numeric id with different semantics, which makes 100% VB coverage
# structurally impossible (the implementer can only mark one id per behavior).
_TEST_DOC_CANONICAL_VB_HEAD = [
    "",
    "Design-to-test traceability (CRITICAL — this is the CANONICAL verifiable-behavior registry):",
    "- This document is the single canonical owner of the verifiable-behavior (VB) id namespace for the project. Declare EVERY verifiable behavior here exactly once; no other test document may declare VB ids.",
    "- Before defining test scenarios, enumerate ALL verifiable behaviors from the dependency design documents. A verifiable behavior is any system action, state transition, or output that the design specifies and that can be asserted in a test.",
    "- Every verifiable behavior must map to at least one test scenario — if a design document specifies a transition chain (e.g. action → intermediate state → final state), each link in the chain requires a separate assertion.",
    "- Include a traceability section that lists each verifiable behavior and its corresponding test scenario(s). Flag any behavior that lacks coverage.",
    "- Write that traceability section as a Markdown table whose FIRST column is a stable verifiable-behavior id of the form `VB-<id>` (e.g. `VB-01`, `VB-AUTH-02`), one row per verifiable behavior. Header wording and language are free; CoDD machine-parses the `VB-` id in the first cell (`codd test audit`), so never merge multiple behaviors into one row and never omit the id column.",
    "- Each VB id must be ATOMIC — name exactly one behavior. Never use range or list shorthand in an id (no `VB-EVAL-02..06`, no `VB-02,03`, no `VB-02〜06`); write one row with a distinct atomic id per behavior instead. A range id cannot be honestly covered by a single test marker, and its members would read as permanently uncovered.",
    "- Declare each behavior under exactly one stable id; do not split one behavior across two ids or reuse an id for two different behaviors.",
    "- Treat design-time `operation_flow` records as the authoritative source for operational test obligations. Do not invent E2E-only behavior that is absent from requirements or design; instead flag missing design obligations.",
]

_TEST_DOC_REFERENCE_VB_HEAD = [
    "",
    "Design-to-test traceability (CRITICAL — this document is REFERENCE-ONLY for verifiable behaviors):",
    "- The canonical verifiable-behavior (VB) registry lives in `docs/test/test_strategy.md`. This document MUST NOT declare VB ids and MUST NOT contain any Markdown table whose FIRST column is a `VB-*` id.",
    "- Use your own first-column ids appropriate to this document (e.g. `AC-01` for acceptance criteria, or a requirement id). Reference the canonical VB ids only in a LATER column, e.g. a column headed `Canonical VBs` listing `VB-07, VB-29`.",
    "- Concretely: a traceability/mapping table here looks like `| AC ID | Acceptance criterion | Canonical VBs |`, never `| VB-01 | ... |`. CoDD machine-parses any first-cell `VB-*` as a declaration, so a `VB-*` first column here would collide with the canonical registry.",
    "- Do not restate or renumber the canonical VB table; map your acceptance criteria / requirements to the existing canonical VB ids instead.",
    "- Treat design-time `operation_flow` records as the authoritative source for operational test obligations. Do not invent behavior that is absent from requirements or design; instead flag missing design obligations.",
]


def _python_http_e2e_doc_lines() -> list[str]:
    """Test-doc meta-prompt guidance for a Python ``pytest`` HTTP E2E harness.

    Emitted for a Python web project (HTTP surface) when no EXPLICIT browser
    automation is required — the language-native counterpart of the TS Playwright
    browser block. This is NOT a weakening of the browser block: it keeps every
    web-E2E obligation (server readiness, <500 health baseline, response bodies,
    server-rendered HTML content, JSON API contracts, persistence/readback,
    teardown), but drives them through a Python HTTP client against a live server
    instead of a browser/node toolchain. Browser-only obligations (clicking,
    JS-rendered UI, visual layout) are out of scope here by design; a project
    that truly needs them must declare an explicit browser harness (then the TS
    Playwright block applies). Output files are pytest-convention ``.py`` names.
    """

    return [
        "",
        "E2E Test Generation Meta-Prompt section rules (Python HTTP E2E):",
        "- The final section '## E2E Test Generation Meta-Prompt' serves as a machine-readable instruction for `codd propagate` to auto-generate Python HTTP end-to-end tests.",
        "- This is a Python web project with an HTTP surface and NO explicit browser-automation requirement. End-to-end tests MUST be written in Python with `pytest` and drive the application over HTTP. Do NOT use Playwright/Cypress, do NOT write TypeScript/JavaScript, and do NOT emit `.spec.ts`/`.ts` files.",
        "- HTTP client: Use a real HTTP client against a live server — either start the app's real server as a background subprocess and hit it with `httpx`/`requests`/stdlib `urllib`/`http.client`, OR use the framework's live-server test client (e.g. Flask `app.test_client()` / a `live_server` fixture) so requests exercise the real WSGI/ASGI stack end-to-end. Prefer a live server bound to a loopback port for true E2E.",
        "- MECE domain decomposition: Split E2E tests into non-overlapping behavioral domains. Each file owns exactly one domain.",
        "- Scenario derivation: First derive test obligations from design-time `operation_flow` and verifiable behaviors, then derive concrete HTTP E2E evidence candidates. Cover positive, negative, persistence/readback, permission-boundary, terminal-state, cross-actor-reflection, derived-state/read-model chain, and threshold/boundary cases when the design declares those axes.",
        "- Server health baseline (CRITICAL): Every HTTP request assertion MUST first verify the response status is < 500 before checking business-logic status codes (200, 302, 401, etc.). A 5xx is a server error (unhandled exception, DB down) — categorically different from a 4xx (auth failure, not found). Without this, a DB failure silently passes when tests only check for specific success codes.",
        "- Response-body & contract coverage: Assert on response bodies, not just status codes — for HTML endpoints assert required server-rendered content/copy is present and forbidden content/links are absent; for JSON APIs assert the response contract (keys, types, values) the design declares.",
        "- Actor-facing surface/copy coverage: Derive HTTP E2E obligations from design-time surface/copy obligations. Assert required visible labels/copy appear in the rendered response, assert forbidden actions/links/copy patterns are absent, and cover actor-specific wording where the design declares different audiences.",
        "- Persistence/readback: For any scenario that creates or updates state, assert the change is observable on a subsequent request (write -> read-back over HTTP), and for measured/derived values assert the producer -> durable state -> derived value -> consumer-surface chain. If a value has a threshold, percentage, count, duration, score, or latest/last/resume rule, include below/at/above-boundary assertions where feasible.",
        "- Architecture adaptation: Test generation MUST scan the actual route/endpoint structure and mark unimplemented endpoints with `pytest.mark.skip`/`xfail(strict=True)` ONLY where the design declares them not-yet-implemented; never silently omit a declared obligation.",
        "- Server lifecycle & teardown (CRITICAL): The meta-prompt MUST specify how to start the application under test (build/import the app, bind a loopback port, wait-for-ready via a health probe) before assertions, and MUST tear it down so NO background server process survives the test session. Use a pytest fixture for start/stop and bind an ephemeral port to avoid collisions.",
        "- Quality gate: Define pass criteria — all PASS, zero SKIP, operation_flow/verifiable-behavior coverage, and any release-blocking constraints from conventions.",
        "- Execution policy: Run the whole selected suite, collect every failure, and only then start repair so related failures can be fixed coherently.",
        "- Output file mapping: Specify a table mapping each domain to its output file path under `tests/e2e/`, using pytest-convention Python filenames `test_<domain>.py` (NOT `.spec.ts`).",
        "- Shared helpers: Mandate a `tests/e2e/helpers/` (or `conftest.py` fixtures) location for the live-server fixture, auth flows, test-data setup, and common assertions to avoid duplication across test modules.",
        "- Mutating test data: Any E2E scenario that creates or updates records MUST use per-run unique identifiers and explicit cleanup/idempotent teardown so repeated runs cannot fail from stale data or uniqueness constraints.",
        "- Scenario fixtures: Any E2E scenario that depends on pre-existing records MUST establish or idempotently reset those preconditions inside the scenario/fixture before assertions; do not trust mutable shared seed state unless the test recreates it or proves it unchanged.",
        "- Generation markers: All generated files must include `# @generated-from:` and `# @generated-by: codd propagate` header comments (Python comment syntax). Manual tests marked with `# @manual` must be preserved on regeneration.",
    ]


def _build_test_doc_block(
    capabilities: ProjectCapabilities,
    *,
    node_id: str | None = None,
    output_path: str | None = None,
    project_language: str | None = None,
) -> list[str]:
    """Build the test-document meta-prompt guidance, branched on e2e_modality.

    Unit + integration guidance is universal. The traceability head is
    ROLE-AWARE: the canonical VB doc (``test:test-strategy`` /
    ``docs/test/test_strategy.md``) gets the declaration head; every other test
    doc gets the reference-only head (no first-column ``VB-*`` table). When the
    role is unknown (no ``node_id``/``output_path`` — e.g. single-doc projects
    or legacy callers), default to canonical so a lone test doc still declares
    VBs and back-compat holds. The end-to-end layer adapts:

    * ``browser``  → LANGUAGE-AWARE web E2E. A Python project with an HTTP surface
      and no EXPLICIT browser requirement gets a Python ``pytest`` HTTP E2E
      harness (live server + HTTP client, ``tests/e2e/test_*.py``); every other
      browser project keeps the TS Playwright/Cypress UI-flow harness + API split.
    * ``cli``      → end-to-end tests that invoke the built CLI as a subprocess
      and assert exit codes / stdout / stderr / produced files. No browser, no server.
    * ``device``   → on-device / emulator / hardware-in-the-loop E2E. No web server.
    * ``none``     → integration tests only; no end-to-end UI/browser layer applies.

    The web-E2E harness is resolved DETERMINISTICALLY via
    :func:`codd.e2e_harness.resolve_e2e_harness` (no LLM prose inference). When
    ``project_language`` is ``None`` (unknown / legacy caller), the browser branch
    keeps the historical TS Playwright guidance unchanged.

    Server-startup language is only emitted when ``long_running_service`` is true.
    """

    from codd.verifiable_behavior_audit import is_canonical_vb_doc

    modality = capabilities.e2e_modality
    if node_id is None and output_path is None:
        is_canonical = True
    else:
        is_canonical = is_canonical_vb_doc(node_id=node_id, output_path=output_path)
    head = _TEST_DOC_CANONICAL_VB_HEAD if is_canonical else _TEST_DOC_REFERENCE_VB_HEAD
    lines = list(head)

    harness = resolve_e2e_harness(
        project_language=project_language, capabilities=capabilities
    )

    if modality == "browser" and harness.runner == "pytest_http":
        lines.extend(_python_http_e2e_doc_lines())
    elif modality == "browser":
        lines.extend(
            [
                "",
                "E2E Test Generation Meta-Prompt section rules:",
                "- The final section '## E2E Test Generation Meta-Prompt' serves as a machine-readable instruction for `codd propagate` to auto-generate E2E tests.",
                "- MECE domain decomposition: Split E2E tests into non-overlapping behavioral domains. Each file owns exactly one domain.",
                "- Scenario derivation: First derive test obligations from design-time `operation_flow` and verifiable behaviors, then derive concrete E2E evidence candidates. Cover positive, negative, persistence/readback, permission-boundary, terminal-state, cross-actor-reflection, derived-state/read-model chain, and threshold/boundary cases when the design declares those axes.",
                "- Actor-facing surface/copy coverage: Derive browser E2E obligations from design-time surface/copy obligations. Assert required visible labels/copy, assert forbidden actions/links/copy patterns are absent, and cover actor-specific wording where the design declares different audiences.",
                "- For measured or observed behavior, test the producer -> durable state/event -> derived value/read model -> consumer surface chain. If a value has a threshold, percentage, count, duration, score, or latest/last/resume rule, include below/at/above-boundary assertions where feasible.",
                "- Architecture adaptation: Include a rule that test generation must scan the actual route/endpoint structure and mark unimplemented endpoints with `test.fixme()` instead of skipping.",
                "- Quality gate: Define pass criteria — all PASS, zero SKIP, operation_flow/verifiable-behavior coverage, and any release-blocking constraints from conventions.",
                "- Execution policy: Run the whole selected suite, collect every failure, and only then start repair so related failures can be fixed coherently.",
                "- Output file mapping: Specify a table mapping each domain to its output file path under `tests/e2e/`.",
                "- Shared helpers: Mandate a `tests/e2e/helpers/` directory for auth flows, test data setup, and common assertions to avoid duplication across spec files.",
                "- Mutating test data: Any E2E scenario that creates or updates records MUST use per-run unique identifiers and explicit cleanup/idempotent teardown so repeated runs cannot fail from stale data or uniqueness constraints.",
                "- Scenario fixtures: Any E2E scenario that depends on pre-existing records MUST establish or idempotently reset those preconditions inside the scenario/helper before assertions; do not trust mutable shared seed state unless the test recreates it or proves it unchanged.",
                "- Generation markers: All generated files must include `// @generated-from:` and `// @generated-by: codd propagate` headers. Manual tests marked with `// @manual` must be preserved on regeneration.",
                "",
                "E2E Test Level Separation (CRITICAL):",
                "- E2E tests MUST be split into two distinct levels: API integration tests and browser tests. These are NOT interchangeable.",
                "- API integration tests use HTTP client mode (e.g. Playwright `request` context, `supertest`, `fetch`) to verify endpoint responses, status codes, and data contracts. These test the server, not the user experience.",
                "- Browser tests use real browser automation (e.g. Playwright `page`, Cypress `cy`) to simulate actual user interactions: clicking buttons, filling forms, navigating pages, and verifying visible UI state.",
                "- For web applications with authentication, browser tests MUST include a login-redirect-render flow: (1) navigate to login page, (2) fill credentials and submit, (3) assert redirect to the correct post-login URL, (4) assert the target page renders expected content. This catches redirect misconfigurations and route mismatches that API tests cannot detect.",
                "- For any page transition triggered by a user action (form submit, link click, button click), browser tests MUST verify both the resulting URL (via URL assertion) and at least one visible content element on the destination page. Checking only the HTTP status is insufficient — a 200 with wrong content or a silent redirect to a 404 page will be missed.",
                "- Server health baseline: Every HTTP request assertion MUST first verify the response status is < 500 before checking business-logic status codes (200, 302, 401, etc.). A 5xx is a server error (unhandled exception, DB down) — categorically different from a 4xx (auth failure, not found). Without this, a DB failure silently passes when tests only check for specific success codes.",
                "- Output file naming: API integration tests → `tests/e2e/<domain>.spec.ts`, browser tests → `tests/e2e/<domain>.browser.spec.ts`. This makes the test level immediately visible from the filename.",
                "",
                "E2E Runtime Environment rules:",
                "- E2E tests for web applications require a running server. The meta-prompt MUST specify how to start the application under test before running E2E tests.",
                "- Detect the project type from package.json scripts, framework config, or entry points. Include the appropriate startup sequence (e.g., build → start → wait-for-ready) in the E2E instructions.",
                "- For CI environments, specify that the server must run in the background with a health-check wait before test execution begins.",
                "- Browser tests require a headed or headless browser. Specify the browser launch configuration (e.g. `use: { headless: true }`) in the test config.",
            ]
        )
    elif modality == "cli":
        lines.extend(
            [
                "",
                "E2E Test Generation Meta-Prompt section rules (CLI):",
                "- The final section '## E2E Test Generation Meta-Prompt' serves as a machine-readable instruction for `codd propagate` to auto-generate end-to-end CLI tests.",
                "- This project has NO browser and NO long-running web server. End-to-end tests MUST invoke the built/installed CLI as a subprocess and assert on its observable contract: process exit code, stdout, stderr, and any files or artifacts produced. Do NOT use Playwright/Cypress, do NOT launch a browser, and do NOT start a web server.",
                "- MECE domain decomposition: Split end-to-end CLI tests into non-overlapping behavioral domains (one command or coherent command-group per domain file).",
                "- Scenario derivation: First derive obligations from design-time `operation_flow` and verifiable behaviors, then derive concrete CLI-invocation evidence candidates. Cover positive runs, invalid-argument/usage errors (non-zero exit codes), idempotency/re-run behavior, and boundary inputs the design declares.",
                "- For each scenario: build/locate the CLI entry point, run it with explicit arguments and a controlled working directory, then assert the exit code, the relevant stdout/stderr substrings or structured output, and the on-disk side effects (created/updated/removed files, their contents).",
                "- For measured or derived behavior, assert the command's output value chain (input -> processing -> emitted result) rather than any UI surface.",
                "- Quality gate: Define pass criteria — all PASS, zero SKIP, operation_flow/verifiable-behavior coverage, and any release-blocking constraints from conventions.",
                "- Execution policy: Run the whole selected suite, collect every failure, and only then start repair so related failures can be fixed coherently.",
                "- Output file mapping: Specify a table mapping each domain to its output file path under `tests/e2e/`.",
                "- Shared helpers: Mandate a helpers directory for CLI-invocation wrappers, temp-directory/workspace setup, and common assertions to avoid duplication across files.",
                "- Mutating test data: Any scenario that writes files or persistent state MUST use per-run unique paths/identifiers and explicit cleanup (or an isolated temp workspace) so repeated runs cannot fail from stale data.",
                "- Generation markers: All generated files must include `// @generated-from:` (or language-appropriate comment) and `// @generated-by: codd propagate` headers. Manual tests marked `// @manual` must be preserved on regeneration.",
            ]
        )
    elif modality == "device":
        lines.extend(
            [
                "",
                "E2E Test Generation Meta-Prompt section rules (on-device):",
                "- The final section '## E2E Test Generation Meta-Prompt' serves as a machine-readable instruction for `codd propagate` to auto-generate on-device end-to-end tests.",
                "- This project runs on a device or emulator, NOT a web server in a browser. End-to-end tests MUST drive the application on an emulator/simulator or real hardware-in-the-loop and assert on observable device behavior (UI elements via the platform's UI-automation framework, on-device state, sensor/peripheral effects, emitted events). Do NOT assume a browser-on-a-web-server.",
                "- MECE domain decomposition: Split on-device E2E tests into non-overlapping behavioral domains. Each file owns exactly one domain.",
                "- Scenario derivation: First derive obligations from design-time `operation_flow` and verifiable behaviors, then derive concrete on-device evidence candidates. Cover positive, negative, persistence/readback, permission-boundary, terminal-state, and threshold/boundary cases when the design declares those axes.",
                "- Actor-facing surface/copy coverage: When the design declares user-facing surfaces/copy, assert required visible labels/copy and forbidden actions/copy on the device UI using the platform's UI-automation locators.",
                "- For measured or observed behavior (sensors, callbacks, network), test the producer -> durable state/event -> derived value -> consumer surface chain on the device.",
                "- Provisioning: Specify the emulator/simulator or device-farm configuration and any build/install step required to deploy the app under test before the suite runs.",
                "- Quality gate: Define pass criteria — all PASS, zero SKIP, operation_flow/verifiable-behavior coverage, and any release-blocking constraints from conventions.",
                "- Execution policy: Run the whole selected suite, collect every failure, and only then start repair so related failures can be fixed coherently.",
                "- Output file mapping: Specify a table mapping each domain to its output file path under `tests/e2e/`.",
                "- Generation markers: All generated files must include `// @generated-from:` (or language-appropriate comment) and `// @generated-by: codd propagate` headers. Manual tests marked `// @manual` must be preserved on regeneration.",
            ]
        )
    else:  # "none"
        lines.extend(
            [
                "",
                "Integration test rules (no end-to-end layer):",
                "- This project has NO user-facing surface and NO end-to-end UI/browser layer; an end-to-end UI/browser test layer does NOT apply. Do NOT generate Playwright/Cypress browser tests, do NOT launch a browser, and do NOT start a web server.",
                "- Provide thorough unit and integration coverage instead: exercise public functions/APIs and module boundaries directly, asserting return values, raised errors, and persisted/observable state.",
                "- Scenario derivation: Derive obligations from design-time `operation_flow` and verifiable behaviors. Cover positive, negative, persistence/readback, boundary, and error-handling cases the design declares.",
                "- Quality gate: Define pass criteria — all PASS, zero SKIP, operation_flow/verifiable-behavior coverage, and any release-blocking constraints from conventions.",
                "- Execution policy: Run the whole selected suite, collect every failure, and only then start repair so related failures can be fixed coherently.",
                "- Generation markers: All generated files must include a `@generated-from:` and `@generated-by: codd propagate` header (language-appropriate comment). Manual tests marked `@manual` must be preserved on regeneration.",
            ]
        )

    return lines


def _build_operations_doc_block(capabilities: ProjectCapabilities) -> list[str]:
    """Build the operations-document meta-prompt guidance, branched on capabilities.

    Server-startup / running-service CI steps are emitted only when
    ``long_running_service`` is true. For non-services, operations are framed as
    release / distribution / packaging plus type-appropriate monitoring. Env-var
    examples use neutral phrasing rather than web-stack-specific names.
    """

    is_service = capabilities.long_running_service
    is_browser_e2e = capabilities.e2e_modality == "browser"

    if is_service:
        pipeline_intro = [
            "",
            "CI/CD Pipeline Generation Meta-Prompt section rules:",
            "- The final section '## CI/CD Pipeline Generation Meta-Prompt' serves as a machine-readable instruction for generating `.github/workflows/ci.yml`.",
            "- Derive CI jobs from the test strategy document: for each test level (unit, integration, E2E, performance), create a corresponding CI job.",
            "- Include build verification: `npm run build` (or equivalent) must pass before tests run.",
            "- Database setup: If the project uses a database, include a service container (e.g. PostgreSQL) with seed step.",
            "- Environment variables: List required project-required secrets/credentials and configuration env vars from the project config and mark which should be GitHub Secrets.",
            "- Merge gate: All test jobs must pass before PR merge is allowed. Specify branch protection rule recommendations.",
            "- Output file: `.github/workflows/ci.yml`. Include `// @generated-by: codd propagate` as a YAML comment.",
            "- Trigger: `on: pull_request` to main/develop branches.",
            "- Caching: Include dependency caching (node_modules, pip cache, etc.) for faster CI runs.",
            "- Failure notification: Recommend but do not require Slack/email notification on failure.",
        ]
    else:
        pipeline_intro = [
            "",
            "CI/CD Pipeline Generation Meta-Prompt section rules:",
            "- The final section '## CI/CD Pipeline Generation Meta-Prompt' serves as a machine-readable instruction for generating `.github/workflows/ci.yml`.",
            "- This project is NOT a long-running service. Frame operations as build → test → release/distribution/packaging, not server deployment. Do NOT generate server-startup, health-check, or running-service steps.",
            "- Derive CI jobs from the test strategy document: for each test level (unit, integration, etc.), create a corresponding CI job.",
            "- Include build verification: the project's build/packaging command must pass before tests run.",
            "- Release/distribution: include a release job appropriate to the artifact type (e.g. publish a package/binary/library to its registry or attach build artifacts to a release) rather than deploying a server.",
            "- Environment variables: List the project-required secrets/credentials and configuration env vars from the project config, and mark which should be GitHub Secrets.",
            "- Merge gate: All test jobs must pass before PR merge is allowed. Specify branch protection rule recommendations.",
            "- Output file: `.github/workflows/ci.yml`. Include `# @generated-by: codd propagate` as a YAML comment.",
            "- Trigger: `on: pull_request` to main/develop branches.",
            "- Caching: Include dependency caching (node_modules, pip cache, etc.) for faster CI runs.",
            "- Monitoring: frame monitoring around release health / error reporting / usage telemetry appropriate to the artifact, not server uptime checks.",
            "- Failure notification: Recommend but do not require Slack/email notification on failure.",
        ]

    common_tail = [
        "",
        "Prerequisite Validation rules:",
        "- Before referencing any tool or package in a CI step (e.g., a linter, test runner, build tool), verify it exists in the project's dependency manifest (package.json, requirements.txt, pyproject.toml, etc.).",
        "- If a required tool is missing, either add an install step in CI or note it as a prerequisite that must be added to the project's dev dependencies.",
        "- Do not generate CI steps that invoke tools the project has not installed.",
        "",
        "Runtime Compatibility rules:",
        "- When generating configuration files or CI steps, detect the project's existing tool versions (framework, linter, test runner) and produce version-compatible output.",
        "- Avoid generating config formats or flags that require a newer version than what the project uses (e.g., flat config for ESLint <9, or module syntax for older Node.js).",
        "- If version information is available in package.json, requirements.txt, or lock files, use it to guide config format choices.",
    ]

    lines = pipeline_intro + common_tail

    if is_service and is_browser_e2e:
        lines.extend(
            [
                "",
                "E2E Job Server Startup rules:",
                "- If the CI includes E2E tests for a web application, the E2E job MUST include steps to build and start the application server before running tests.",
                "- Detect the project type (web app, CLI, library) from the project structure and only add server startup for web applications.",
                "- Include a readiness check (e.g., wait-on, curl health endpoint) between server start and test execution to avoid race conditions.",
            ]
        )

    return lines
MARKDOWN_FENCE_RE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(?P<body>.*)\n```\s*$", re.IGNORECASE | re.DOTALL)
FENCE_LINE_RE = re.compile(r"^\s*```(?:[a-zA-Z0-9_-]+)?\s*$")
TITLE_HEADING_RE = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$")
SECTION_HEADING_RE = re.compile(r"^##\s+.+$", re.MULTILINE)
MERMAID_FENCE_RE = re.compile(r"```mermaid\b", re.IGNORECASE)
H1_HEADING_RE = re.compile(r"^#\s+(.+)$")
H3_HEADING_RE = re.compile(r"^###\s+(.+)$")
BOLD_HEADING_RE = re.compile(r"^\*\*(\d+\.\s+.+?)\*\*\s*$")
META_PREAMBLE_PATTERNS = (
    re.compile(r"^\s*the\s+docs?(?:/[a-z0-9._-]+)*\s+directory\b.*$", re.IGNORECASE),
    re.compile(r"^\s*the\s+dependency\s+documents\s+provided\s+inline\b.*$", re.IGNORECASE),
    re.compile(r"^\s*the\s+existing\s+(?:file|document|content)\b.*$", re.IGNORECASE),
    re.compile(r"^\s*now\s+i\s+have\s+enough\s+context\b.*$", re.IGNORECASE),
    re.compile(r"^\s*no\s+existing\s+file\s+found\b.*$", re.IGNORECASE),
    re.compile(r"^\s*since the user\b.*$", re.IGNORECASE),
    re.compile(r"^\s*i\s+need\s+to\s+write\s+just\s+the\s+document\s+body\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*.*\b(?:i(?:'|’)ll\s+(?:now\s+)?(?:output|write|create)|let me(?:\s+now)?\s+write)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*let me(?:\s+(?:review|verify|check|compare))\b.*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:here is|here(?:'|’)s)\b.*\b(?:document|markdown|body|content)\b.*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*[-*]\s+.+→\s+covered\b.*$", re.IGNORECASE),
    re.compile(r"^\s*`[^`]+`\s+を(?:作成|生成)しました。?\s*$"),
    re.compile(r"^\s*(?:主要|主な)な?構成[:：]\s*$"),
    re.compile(r"^\s*(?:以下|上記)の(?:内容|構成|設計)で(?:作成|生成)しました。?\s*$"),
)


@dataclass(frozen=True)
class WaveArtifact:
    """Normalized wave_config entry."""

    wave: int
    node_id: str
    output: str
    title: str
    depends_on: list[dict[str, Any]]
    conventions: list[dict[str, Any]]
    modules: list[str] = ()


@dataclass(frozen=True)
class GenerationResult:
    """Result of rendering one artifact."""

    node_id: str
    path: Path
    status: str


@dataclass(frozen=True)
class DependencyDocument:
    """Resolved dependency document used as AI context."""

    node_id: str
    path: Path
    content: str


def generate_wave(
    project_root: Path,
    wave: int,
    force: bool = False,
    ai_command: str | None = None,
    feedback: str | None = None,
) -> list[GenerationResult]:
    """Generate or skip all documents configured for a wave."""
    from codd.scanner import build_document_node_path_map

    config = _load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)
    selected = [artifact for artifact in artifacts if artifact.wave == wave]
    if not selected:
        raise ValueError(f"wave_config has no entries for wave {wave}")

    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="generate")
    global_conventions = _normalize_conventions(config.get("conventions", []))
    depended_by_map = _build_depended_by_map(artifacts)
    document_node_paths = build_document_node_path_map(project_root, config)
    body_max_retries = _body_max_retries(config)
    capabilities = _resolve_generation_capabilities(config, project_root)
    project_language = _resolve_project_language(config)

    results: list[GenerationResult] = []
    for artifact in selected:
        output_path = project_root / artifact.output
        if output_path.exists() and not force:
            results.append(GenerationResult(node_id=artifact.node_id, path=output_path, status="skipped"))
            continue
        results.append(
            _generate_one_artifact(
                artifact=artifact,
                project_root=project_root,
                global_conventions=global_conventions,
                depended_by_map=depended_by_map,
                document_node_paths=document_node_paths,
                resolved_ai_command=resolved_ai_command,
                body_max_retries=body_max_retries,
                capabilities=capabilities,
                project_language=project_language,
                feedback=feedback,
            )
        )

    _enforce_vb_declaration_coherence(project_root, config, results)
    return results


def _generate_one_artifact(
    *,
    artifact: WaveArtifact,
    project_root: Path,
    global_conventions: list[dict[str, Any]],
    depended_by_map: dict[str, list[dict[str, Any]]],
    document_node_paths: dict[str, Path],
    resolved_ai_command: str,
    body_max_retries: int,
    capabilities: "ProjectCapabilities | None",
    project_language: str | None,
    feedback: str | None,
) -> GenerationResult:
    """Render ONE artifact's document and write it (overwriting any prior file).

    The shared per-artifact rendering step used by both :func:`generate_wave`
    (the wave loop) and :func:`regenerate_artifact` (the single-doc repair seam).
    Always (re)writes the output; skip-if-exists is the wave loop's decision, not
    this helper's, so a scoped repair can overwrite exactly one doc.
    """

    output_path = project_root / artifact.output
    dependency_documents = _load_dependency_documents(
        project_root, artifact.depends_on, document_node_paths
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined_conventions = deepcopy(global_conventions) + deepcopy(artifact.conventions)
    content = _render_document(
        artifact=artifact,
        global_conventions=global_conventions,
        depended_by=depended_by_map.get(artifact.node_id, []),
        body=_generate_document_body(
            artifact=artifact,
            dependency_documents=dependency_documents,
            conventions=combined_conventions,
            ai_command=resolved_ai_command,
            project_root=project_root,
            feedback=feedback,
            max_retries=body_max_retries,
            capabilities=capabilities,
            project_language=project_language,
        ),
    )
    output_path.write_text(content, encoding="utf-8")
    return GenerationResult(node_id=artifact.node_id, path=output_path, status="generated")


def regenerate_artifact(
    project_root: Path,
    *,
    node_id: str | None = None,
    output_path: str | None = None,
    feedback: str | None = None,
    ai_command: str | None = None,
) -> GenerationResult:
    """Regenerate a SINGLE wave artifact in place, scoped to one document.

    The thin seam the greenfield generate-time VB-registry repair uses: it
    rewrites ONLY the canonical VB doc (``docs/test/test_strategy.md``) with
    repair feedback, WITHOUT re-running the whole wave (which would clobber the
    sibling governance/design docs already generated). Reuses the exact wave
    plumbing (:func:`_generate_one_artifact` → ``_load_dependency_documents`` /
    ``_render_document`` / ``_generate_document_body``); only the artifact
    selection (by ``node_id`` or ``output``) and the forced overwrite differ.

    Identify the target by ``node_id`` (preferred) or ``output_path`` (the
    artifact's ``output``). Raises ``ValueError`` if no/!=1 wave artifact matches
    — an ambiguous or absent target must fail loudly, never silently no-op.
    """

    from codd.scanner import build_document_node_path_map

    if not node_id and not output_path:
        raise ValueError("regenerate_artifact requires node_id or output_path")

    config = _load_project_config(project_root)
    artifacts = _load_wave_artifacts(config)

    def _matches(candidate: WaveArtifact) -> bool:
        if node_id is not None and candidate.node_id == node_id:
            return True
        if output_path is not None:
            want = output_path.replace("\\", "/").strip("/")
            have = str(candidate.output).replace("\\", "/").strip("/")
            if want == have:
                return True
        return False

    matched = [artifact for artifact in artifacts if _matches(artifact)]
    if not matched:
        target = node_id or output_path
        raise ValueError(f"wave_config has no artifact matching {target!r}")
    if len(matched) > 1:
        target = node_id or output_path
        raise ValueError(f"wave_config has {len(matched)} artifacts matching {target!r}; ambiguous")

    artifact = matched[0]
    resolved_ai_command = _resolve_ai_command(config, ai_command, command_name="generate")
    global_conventions = _normalize_conventions(config.get("conventions", []))
    depended_by_map = _build_depended_by_map(artifacts)
    document_node_paths = build_document_node_path_map(project_root, config)
    body_max_retries = _body_max_retries(config)
    capabilities = _resolve_generation_capabilities(config, project_root)
    project_language = _resolve_project_language(config)

    return _generate_one_artifact(
        artifact=artifact,
        project_root=project_root,
        global_conventions=global_conventions,
        depended_by_map=depended_by_map,
        document_node_paths=document_node_paths,
        resolved_ai_command=resolved_ai_command,
        body_max_retries=body_max_retries,
        capabilities=capabilities,
        project_language=project_language,
        feedback=feedback,
    )


def _enforce_vb_declaration_coherence(
    project_root: Path,
    config: dict[str, Any],
    results: list[GenerationResult],
) -> None:
    """Fail generation if the generated test docs declare an incoherent VB space.

    Deterministic cross-document post-check (defaults-must-not-flake): after the
    test docs are written, parse every discovered VB table and reject:

    * a non-canonical test doc with first-column ``VB-*`` rows (a model that
      ignored the reference-only instruction), and
    * two docs declaring the same normalized VB id with DIFFERENT descriptions
      (an incoherent namespace where one id means two things).

    Identical duplicates (same id+description) only warn. Treated as a
    generation defect — NOT a coverage gap — so it is never "fixed" by stuffing
    duplicate markers. Runs only when this wave generated at least one test doc
    so unrelated waves are unaffected.
    """

    if not any(
        result.status == "generated" and _infer_doc_type(str(result.path)) == "test"
        for result in results
    ):
        return

    from codd.verifiable_behavior_audit import parse_vb_tables_by_doc, validate_vb_declarations

    behaviors_by_doc = parse_vb_tables_by_doc(project_root, config=config)
    # Newly generated output must be canonical by construction → strict.
    issues = validate_vb_declarations(behaviors_by_doc, strict=True)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        detail = "\n".join(f"  - {issue.message}" for issue in errors)
        raise ValueError(
            "Generated test documents declare an incoherent verifiable-behavior namespace:\n"
            f"{detail}\n"
            "Exactly one document (docs/test/test_strategy.md) must declare VB ids; all other "
            "test docs must reference canonical VB ids in a later column."
        )


def _load_project_config(project_root: Path) -> dict[str, Any]:
    return load_project_config(project_root)


def _body_max_retries(config: dict[str, Any]) -> int:
    """Read `generate.body_retry.max_retries` (0 disables the retry loop)."""
    generate_cfg = config.get("generate")
    if isinstance(generate_cfg, dict):
        body_retry = generate_cfg.get("body_retry")
        if isinstance(body_retry, dict):
            value = body_retry.get("max_retries")
            if isinstance(value, int) and value >= 0:
                return value
    return DEFAULT_BODY_MAX_RETRIES


def _load_wave_artifacts(config: dict[str, Any]) -> list[WaveArtifact]:
    wave_config = config.get("wave_config")
    if not isinstance(wave_config, dict) or not wave_config:
        raise ValueError(
            "codd.yaml is missing wave_config. "
            "Run 'codd plan --init' to generate it from your requirements, "
            "or 'codd generate' will auto-generate it for you."
        )

    artifacts: list[WaveArtifact] = []
    for wave_key, entries in wave_config.items():
        try:
            wave = int(wave_key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"wave_config key must be an integer wave number, got {wave_key!r}") from exc

        if not isinstance(entries, list):
            raise ValueError(f"wave_config[{wave_key!r}] must be a list of artifacts")

        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"wave_config[{wave_key!r}] entries must be mappings")

            missing = [field for field in ("node_id", "output", "title") if not entry.get(field)]
            if missing:
                raise ValueError(
                    f"wave_config[{wave_key!r}] entry is missing required fields: {', '.join(missing)}"
                )

            artifacts.append(
                WaveArtifact(
                    wave=wave,
                    node_id=str(entry["node_id"]),
                    output=str(entry["output"]),
                    title=str(entry["title"]),
                    depends_on=_normalize_dependencies(entry.get("depends_on", [])),
                    conventions=_normalize_conventions(entry.get("conventions", [])),
                    modules=_normalize_modules(entry.get("modules", [])),
                )
            )

    return artifacts


def _normalize_dependencies(entries: Any) -> list[dict[str, Any]]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("depends_on must be a list")

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            data: dict[str, Any] = {"id": entry}
        elif isinstance(entry, dict):
            data = deepcopy(entry)
        else:
            raise ValueError(f"depends_on entries must be strings or mappings, got {type(entry).__name__}")

        node_id = data.get("id") or data.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("depends_on entries require a non-empty id")

        data["id"] = node_id
        data.setdefault("relation", DEFAULT_RELATION)
        data.setdefault("semantic", DEFAULT_SEMANTIC)
        normalized.append(data)

    return normalized


def _normalize_conventions(entries: Any) -> list[dict[str, Any]]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("conventions must be a list")

    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append({"targets": [entry], "reason": ""})
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"conventions entries must be strings or mappings, got {type(entry).__name__}")

        data = deepcopy(entry)
        targets = data.get("targets", [])
        if isinstance(targets, str):
            data["targets"] = [targets]
        elif isinstance(targets, list):
            data["targets"] = [target for target in targets if isinstance(target, str)]
        else:
            raise ValueError("convention targets must be a string or list of strings")
        data.setdefault("reason", "")
        normalized.append(data)

    return normalized


def _normalize_modules(entries: Any) -> list[str]:
    if not entries:
        return []
    if not isinstance(entries, list):
        raise ValueError("modules must be a list of strings")
    return [str(m) for m in entries if isinstance(m, str) and m.strip()]


def _build_depended_by_map(artifacts: list[WaveArtifact]) -> dict[str, list[dict[str, Any]]]:
    depended_by: dict[str, list[dict[str, Any]]] = {artifact.node_id: [] for artifact in artifacts}

    for artifact in artifacts:
        for dependent in artifacts:
            if dependent.wave <= artifact.wave:
                continue

            for dependency in dependent.depends_on:
                if dependency["id"] != artifact.node_id:
                    continue

                reverse = {"id": dependent.node_id}
                for key, value in dependency.items():
                    if key == "id":
                        continue
                    reverse[key] = deepcopy(value)
                depended_by[artifact.node_id].append(reverse)

    return depended_by


def _render_document(
    artifact: WaveArtifact,
    global_conventions: list[dict[str, Any]],
    depended_by: list[dict[str, Any]],
    body: str,
    restoration_meta: dict[str, Any] | None = None,
) -> str:
    # For test code files, use comment-style headers instead of YAML frontmatter
    if _is_test_code_output(artifact.output):
        dep_paths = [d.get("id", "") for d in artifact.depends_on]
        header_lines = [f"// @generated-from: {path}" for path in dep_paths]
        header_lines.append("// @generated-by: codd generate")
        header_lines.append(f"// @codd-node-id: {artifact.node_id}")
        header = "\n".join(header_lines)
        # Strip any markdown fences the AI might have wrapped the code in
        cleaned = body.strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
            cleaned = cleaned[first_newline + 1:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return f"{header}\n\n{cleaned.strip()}\n"

    doc_type = _infer_doc_type(artifact.output)
    codd_block = {
        "node_id": artifact.node_id,
        "type": doc_type,
        "depends_on": deepcopy(artifact.depends_on),
        "depended_by": deepcopy(depended_by),
        "conventions": deepcopy(global_conventions) + deepcopy(artifact.conventions),
    }
    if artifact.modules:
        codd_block["modules"] = list(artifact.modules)
    operation_flow = _extract_operation_flow_contract(body)
    if operation_flow is not None:
        codd_block["operation_flow"] = operation_flow
    # Restoration-only, additive provenance / confidence / open-questions blocks.
    # Greenfield generation passes restoration_meta=None, so generated docs are
    # byte-for-byte unchanged. The DAG scanner reads only known codd: keys and
    # tolerates these extra sibling keys (see scanner._extract_frontmatter).
    if restoration_meta:
        for key, value in restoration_meta.items():
            if value:
                codd_block[key] = value
    frontmatter = yaml.safe_dump(
        {"codd": codd_block},
        allow_unicode=True,
        sort_keys=False,
    )
    return f"---\n{frontmatter}---\n\n{body.rstrip()}\n"


def _extract_operation_flow_contract(body: str) -> dict[str, Any] | None:
    """Find a design-time operation_flow YAML block in a generated document body."""
    for match in re.finditer(r"```(?:yaml|yml)\s*\n(?P<body>.*?)\n```", body, flags=re.IGNORECASE | re.DOTALL):
        try:
            payload = yaml.safe_load(match.group("body")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(payload, dict):
            continue
        flow = payload.get("operation_flow")
        codd_meta = payload.get("codd")
        if flow is None and isinstance(codd_meta, dict):
            flow = codd_meta.get("operation_flow")
        normalized = normalize_operation_flow(flow, source="generated_document.operation_flow")
        if normalized is not None:
            return normalized
    return None


def extract_restoration_meta(body: str) -> dict[str, Any] | None:
    """Lift a restoration evidence block (``codd_restoration:``) from a body.

    Brownfield restoration asks the AI to emit one fenced YAML block carrying the
    machine-readable provenance / confidence-band / open-questions metadata that
    the *deterministic* facts cannot by themselves express (mapping prose claims
    to the evidence that backs them). CoDD lifts that block out of the prose into
    document frontmatter (the same lift pattern used for ``operation_flow``), so
    a downstream report (R3) can inspect "how far restoration got" without
    re-parsing prose. Returns the recognized sub-keys only, or ``None``.

    Greenfield generation never injects this block, so generated docs are
    unaffected.
    """

    recognized = ("provenance", "confidence_bands", "open_questions", "assumptions")
    for match in re.finditer(
        r"```(?:yaml|yml)\s*\n(?P<body>.*?)\n```", body, flags=re.IGNORECASE | re.DOTALL
    ):
        try:
            payload = yaml.safe_load(match.group("body")) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(payload, dict):
            continue
        block = payload.get("codd_restoration")
        if not isinstance(block, dict):
            # Also tolerate the keys living directly at top level of the block.
            if not any(k in payload for k in recognized):
                continue
            block = payload
        meta: dict[str, Any] = {}
        for key in recognized:
            value = block.get(key)
            if value:
                meta[key] = value
        if meta:
            return meta
    return None


def _infer_doc_type(output_path: str) -> str:
    parts = PurePosixPath(output_path).parts
    if len(parts) >= 3 and parts[0] == "docs":
        return DOC_TYPE_BY_DIR.get(parts[1], "document")
    return "document"


# RF4: resolution lives in codd.ai_invoke. The historical private name is kept
# because many modules (require, restore, propagator, fixer, planner, cli, ...)
# import it from here.
_resolve_ai_command = resolve_ai_command


def _load_dependency_documents(
    project_root: Path,
    dependencies: list[dict[str, Any]],
    document_node_paths: dict[str, Path],
) -> list[DependencyDocument]:
    documents: list[DependencyDocument] = []
    missing_node_ids: list[str] = []
    seen_node_ids: set[str] = set()

    for dependency in dependencies:
        node_id = dependency["id"]
        if node_id in seen_node_ids:
            continue
        seen_node_ids.add(node_id)

        rel_path = document_node_paths.get(node_id)
        if rel_path is None:
            missing_node_ids.append(node_id)
            continue

        file_path = project_root / rel_path
        if not file_path.exists():
            raise ValueError(
                f"dependency document {node_id!r} maps to {rel_path.as_posix()}, but the file does not exist"
            )

        documents.append(
            DependencyDocument(
                node_id=node_id,
                path=rel_path,
                content=file_path.read_text(encoding="utf-8"),
            )
        )

    if missing_node_ids:
        raise ValueError(f"unable to resolve dependency document paths for: {', '.join(missing_node_ids)}")

    return documents


def _inject_lexicon(base_prompt: str, project_root: str | Path | None) -> str:
    """Prepend project lexicon context to a prompt if project_lexicon.yaml exists."""
    if project_root is None:
        return base_prompt
    from codd.lexicon import load_lexicon

    lexicon = load_lexicon(project_root)
    if lexicon is None:
        return base_prompt
    return lexicon.as_context_string() + "\n\n---\n\n" + base_prompt


def _generate_document_body(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    ai_command: str,
    project_root: Path | None = None,
    feedback: str | None = None,
    max_retries: int = DEFAULT_BODY_MAX_RETRIES,
    capabilities: ProjectCapabilities | None = None,
    project_language: str | None = None,
) -> str:
    """Generate one document body with a bounded validation-feedback retry loop.

    Output fluctuation (empty body, TODO scaffold, unstructured summary, meta
    commentary, missing Mermaid in detailed design) raises ``ValueError`` from
    sanitization/validation. Each retry feeds that error back to the model as
    review feedback. Infra failures (``AI command failed``) and exhausted
    retries propagate the original ``ValueError`` unchanged (backward compat).
    """

    current_feedback = feedback
    last_error: ValueError | None = None
    for attempt in range(max(0, max_retries) + 1):
        prompt = _build_generation_prompt(
            artifact, dependency_documents, conventions,
            feedback=current_feedback, capabilities=capabilities,
            project_language=project_language,
        )
        prompt = _inject_lexicon(prompt, project_root)
        try:
            return _sanitize_generated_body(
                artifact.title,
                _invoke_ai_command(ai_command, prompt),
                output_path=artifact.output,
            )
        except ValueError as exc:
            if str(exc).startswith("AI command failed"):
                raise  # infra failure: retrying the same broken command cannot help
            last_error = exc
            current_feedback = _combine_retry_feedback(feedback, exc)
    assert last_error is not None
    raise last_error


def _combine_retry_feedback(original_feedback: str | None, error: ValueError) -> str:
    """Merge the caller-provided feedback with the validation error for a retry."""

    retry_note = (
        "The previous generation attempt was REJECTED by CoDD output validation "
        f"with this error: {error}. "
        "Regenerate the COMPLETE document body and fix that problem: start directly "
        "with the document content, use the required `## ` section headings, and "
        "never emit meta commentary, TODO placeholders, or a summary of the document."
    )
    if original_feedback and original_feedback.strip():
        return f"{original_feedback.rstrip()}\n\n{retry_note}"
    return retry_note


def _is_test_code_output(output_path: str) -> bool:
    """Check if the output target is an executable test file (not a design doc)."""
    return output_path.endswith((
        '.spec.ts', '.test.ts', '.e2e.ts',
        '.spec.js', '.test.js', '.e2e.js',
        '.spec.py', '.test.py',
    ))


def _is_e2e_output(output_path: str) -> bool:
    """Check if the output target is an end-to-end test file (under ``tests/e2e/``)."""
    parts = PurePosixPath(output_path).parts
    return len(parts) >= 2 and parts[0] == "tests" and parts[1] == "e2e"


def _build_generation_prompt(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    feedback: str | None = None,
    capabilities: ProjectCapabilities | None = None,
    project_language: str | None = None,
) -> str:
    if capabilities is None:
        capabilities = WEB_FALLBACK_CAPABILITIES
    # Test code generation mode: output executable test code, not a Markdown document
    if _is_test_code_output(artifact.output):
        return _build_test_code_prompt(
            artifact,
            dependency_documents,
            conventions,
            feedback=feedback,
            capabilities=capabilities,
            project_language=project_language,
        )

    doc_type = _infer_doc_type(artifact.output)
    is_detailed_design = _is_detailed_design_output(artifact.output)
    section_names = DETAILED_DESIGN_SECTIONS if is_detailed_design else TYPE_SECTIONS.get(doc_type, TYPE_SECTIONS["document"])
    preferred_sections = ", ".join(section_names)
    required_section_headings = [f"## {index}. {name}" for index, name in enumerate(section_names, start=1)]

    lines = [
        f"You are writing a CoDD {doc_type} document.",
        f"Node ID: {artifact.node_id}",
        f"Title: {artifact.title}",
        "Use the dependency documents below as the primary context, synthesize them, and write a complete Markdown document body.",
        (
            "ABSOLUTE PROHIBITION: **Do not emit** YAML frontmatter, implementation notes, "
            "TODO placeholders, or any meta-commentary about the writing process "
            "(e.g. 'I'll write...', 'No existing file found...', 'Here is...', "
            "'Let me...', 'Now I have enough context...'). **Start directly with the document content.** "
            "Violating this instruction is a **CRITICAL ERROR** and breaks a release-blocking constraint."
        ),
        "Treat requirement documents as the source of truth and reflect every feature, screen, workflow, API, integration, and operational rule they describe.",
        "Before finalizing, self-check that every capability and constraint mentioned in the depends_on documents is represented in the document body.",
        "Use concrete tool names, framework names, services, table names, endpoints, thresholds, counts, and timelines wherever applicable.",
        "Never use vague placeholders such as '推奨なし', '要検討', or 'TBD'.",
        f"Prefer a structure that covers: {preferred_sections}.",
        "After the title, immediately continue with section headings such as '## Overview' or '## 1. Overview'; do not acknowledge that you created the file.",
        "Do not write summary phrases like '`docs/...` を作成しました。', '本設計書は以下を網羅しています:', or '主な構成:'. Write the actual sections instead.",
    ]

    if is_detailed_design:
        lines.extend(
            [
                "This artifact lives under docs/detailed_design/ and must serve as a downstream-ready detailed design document.",
                "Use Mermaid diagrams when they clarify ownership, dependencies, sequences, states, CRUD boundaries, or module/component structure.",
                "Choose only the diagram types justified by the dependency documents; do not force every possible diagram.",
                "For every diagram, add concise prose that explains canonical ownership, reuse/import expectations, and implementation boundaries.",
                "If a shared type, module, or workflow should have a single owner, state that ownership explicitly to prevent reimplementation drift.",
                "Include at least one Mermaid diagram and at least three section headings in the final document body.",
            ]
        )

    if doc_type == "design":
        lines.extend(build_operational_behavior_model_block(capabilities))

    lines.extend(
        [
            "",
            "Output contract:",
            "- Write the finished document body now, not a summary of what it would contain.",
            "- The first content line after the title must be the first required section heading below.",
            "- Use these section headings exactly once and in this order:",
        ]
    )
    lines.extend(required_section_headings)
    if is_detailed_design:
        lines.extend(
            [
                "- Under '## 2. Mermaid Diagrams', include at least one ```mermaid``` fenced block.",
                "- Use prose after each Mermaid block to explain ownership boundaries and implementation consequences.",
            ]
        )

    if doc_type == "test":
        lines.extend(
            _build_test_doc_block(
                capabilities,
                node_id=artifact.node_id,
                output_path=artifact.output,
                project_language=project_language,
            )
        )

    if doc_type == "operations":
        lines.extend(_build_operations_doc_block(capabilities))

    if conventions:
        lines.extend(
            [
                "",
                "Non-negotiable conventions:",
                "- These are release-blocking constraints. Reflect them explicitly in the document body.",
                "- Explicitly state how the document complies with each convention and invariant listed below.",
                "- For security or access-control constraints, state the concrete controls in architecture, security, data, or workflow sections.",
                "- For legal/privacy constraints, add explicit compliance or data-handling requirements.",
                "- For SLA/performance constraints, include measurable thresholds in non-functional sections.",
            ]
        )
        for index, convention in enumerate(conventions, start=1):
            targets = ", ".join(str(target) for target in convention.get("targets", []) if isinstance(target, str))
            reason = str(convention.get("reason") or "").strip() or "(no reason provided)"
            lines.append(f"{index}. Targets: {targets or '(no explicit targets)'}")
            lines.append(f"   Reason: {reason}")

        lines.extend(
            [
                "- Example reflections: tenant isolation in security/data model sections, auth requirements in access control, privacy rules in compliance, performance thresholds in non-functional requirements.",
            ]
        )

    lines.extend(
        [
            "",
            "Dependency documents:",
        ]
    )

    for document in dependency_documents:
        lines.extend(
            [
                f"--- BEGIN DEPENDENCY {document.path.as_posix()} ({document.node_id}) ---",
                document.content.rstrip(),
                f"--- END DEPENDENCY {document.path.as_posix()} ---",
                "",
            ]
        )

    if feedback:
        lines.extend([
            "",
            "--- REVIEW FEEDBACK (from previous generation attempt) ---",
            "A reviewer found issues with a previous version of this document.",
            "You MUST address ALL of the following feedback in this generation:",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
            "",
        ])

    lines.extend(
        [
            "Final instruction: output the real Markdown document body now using the required section headings above. "
            "Do not describe the document. Do not announce completion. Do not provide a summary list.",
        ]
    )

    return "\n".join(lines).rstrip() + "\n"


def _build_test_code_prompt(
    artifact: WaveArtifact,
    dependency_documents: list[DependencyDocument],
    conventions: list[dict[str, Any]],
    feedback: str | None = None,
    capabilities: ProjectCapabilities | None = None,
    project_language: str | None = None,
) -> str:
    """Build a prompt that generates executable test code (not a Markdown document)."""
    if capabilities is None:
        capabilities = WEB_FALLBACK_CAPABILITIES
    # Detect test framework from output filename. ``.py`` already maps to pytest,
    # so a Python HTTP-E2E artifact (``tests/e2e/test_*.py``) gets pytest guidance.
    ext = PurePosixPath(artifact.output).suffix
    if ext in ('.ts', '.js'):
        framework = "Playwright"
        lang = "TypeScript"
    else:
        framework = "pytest"
        lang = "Python"
    # Browser-E2E guidance (Playwright @cdp-only tagging, '@playwright/test'
    # imports) is only correct when the harness is actually the TS Playwright
    # browser harness. A Python project whose browser modality resolves to the
    # language-native ``pytest_http`` harness writes ``.py`` files and must NOT
    # receive Playwright-specific rules — gate ``is_browser_e2e`` on BOTH the
    # declared modality AND a TS/JS output extension so the deterministic harness
    # choice (:func:`codd.e2e_harness.resolve_e2e_harness`) governs.
    is_browser_e2e = capabilities.e2e_modality == "browser" and ext in ('.ts', '.js')
    # Python HTTP-E2E: a web (HTTP-surface) project whose harness resolves to
    # ``pytest_http`` and whose artifact is a ``.py`` E2E file. Such a file needs
    # live-server HTTP-E2E guidance (NOT the "no browser, no server" integration
    # fallback, which would forbid the server startup these tests require).
    harness = resolve_e2e_harness(
        project_language=project_language, capabilities=capabilities
    )
    is_python_http_e2e = (
        harness.runner == "pytest_http"
        and ext == ".py"
        and _is_e2e_output(artifact.output)
    )

    conv_text = ""
    for c in conventions:
        targets = ", ".join(c.get("targets", []))
        reason = c.get("reason", "")
        conv_text += f"  - [{targets}]: {reason}\n"

    lines = [
        f"You are generating executable {framework} test code in {lang}.",
        f"Node ID: {artifact.node_id}",
        f"Title: {artifact.title}",
        f"Output file: {artifact.output}",
        "",
        "CRITICAL: Output ONLY executable test code. Do NOT output Markdown, frontmatter, design prose, or commentary.",
        "The output must be a valid, runnable test file that can be executed directly by the test runner.",
        "",
        "Conventions to enforce in tests:",
        conv_text,
        "",
    ]

    if is_browser_e2e:
        lines.extend([
            "Test separation rules:",
            "- Tests that can run in CI (headless browser + test DB) must NOT be tagged.",
            "- Tests that require a deployed environment (VPS, staging) must be tagged with @cdp-only in the describe block name.",
            "  Example: test.describe('Deploy Smoke @cdp-only', () => { ... })",
            "- The Playwright config uses `grepInvert: /@cdp-only/` in CI to exclude deploy-only tests.",
            "- CI tests: login flow, redirect checks, route protection, role-based access.",
            "- CDP-only tests: visual layout checks, mobile viewport, deployed URL smoke tests.",
            "",
            "Server health baseline (CRITICAL):",
            "- Every test that makes an HTTP request MUST assert the response status is < 500 BEFORE any business-logic assertions.",
            "  Example: expect(response.status()).toBeLessThan(500);",
            "- 5xx = server broke (unhandled exception, DB down). 4xx = business logic rejection (auth failure, not found). These are categorically different.",
            "- Without this assertion, a DB connection failure silently passes when the test only checks for specific success codes like [200, 302].",
            "- For browser tests after page.goto() or form submission, check response?.status() < 500 before asserting page content.",
            "- For API tests, assert < 500 first, then assert the specific expected status code.",
            "",
            "Actor-facing surface/copy coverage (CRITICAL):",
            "- If dependency documents declare actor-facing surface/copy obligations, browser tests MUST assert the required visible labels/copy and MUST assert forbidden actions, links, or copy patterns are absent.",
            "- For role-specific surfaces, test the audience-specific wording and available navigation for that actor instead of only checking that the route returns 200.",
            "- Do not accept generic smoke assertions when the design declares concrete visible copy, role labels, or forbidden navigation.",
            "",
        ])
    elif is_python_http_e2e:
        lines.extend([
            "Python HTTP end-to-end rules (live server, no browser):",
            "- This is a Python web project E2E test. Write `pytest` tests that drive the application over HTTP against a live server. Do NOT use Playwright/Cypress, do NOT import '@playwright/test', and do NOT write any TypeScript/JavaScript.",
            "- Start the application under test (import/build the app, bind a loopback ephemeral port, wait-for-ready via a health probe) in a pytest fixture, hit it with a real HTTP client (`httpx`/`requests`/stdlib `urllib`/`http.client`) or the framework's live-server test client, and tear it down so NO background server process survives.",
            "- Server health baseline (CRITICAL): Every request MUST assert the response status is < 500 BEFORE any business-logic assertion. 5xx = server broke (unhandled exception, DB down); 4xx = business rejection (auth failure, not found) — categorically different. Without this, a DB failure silently passes when the test only checks for specific success codes.",
            "- Assert response bodies, not just status: for HTML endpoints assert required server-rendered content/copy is present and forbidden content/links are absent; for JSON APIs assert the response contract (keys, types, values).",
            "- Cover positive flows, negative/permission-boundary flows, and persistence/readback (write -> read-back over HTTP). Use per-run unique identifiers and explicit teardown so repeated runs cannot fail from stale data.",
            "",
        ])
    elif capabilities.e2e_modality == "cli":
        lines.extend([
            "CLI end-to-end rules (no browser, no server):",
            "- This project has NO browser and NO web server. Invoke the built/installed CLI as a subprocess and assert on its exit code, stdout, stderr, and the files/artifacts it produces. Do NOT use a browser, page objects, or HTTP server startup.",
            "- Cover positive runs, invalid-usage runs (assert non-zero exit code and the error message), and re-run/idempotency behavior.",
            "- Use an isolated temporary working directory per test and assert on-disk side effects (created/updated/removed files and their contents).",
            "",
        ])
    else:
        lines.extend([
            "Integration test rules (no end-to-end UI/browser layer):",
            "- This project has no user-facing browser surface. Exercise the public API/functions and module boundaries directly; assert return values, raised errors, and persisted/observable state. Do NOT use a browser or start a web server.",
            "",
        ])

    lines.extend([
        "Verifiable-behavior traceability markers:",
        "- If a dependency test document declares verifiable-behavior ids (a traceability table whose first column is `VB-<id>`), annotate each test with a comment marker `codd: covers vb=<id>` for every behavior that test proves (`codd test audit` reconciles these markers).",
        "- If a declared behavior cannot be tested yet, add an explicit `codd: blocked vb=<id> reason=<short_reason>` comment marker instead of leaving it silently uncovered.",
        "",
    ])

    # Cut Condition B (framework-pluggable): the browser-e2e harness decision —
    # NOT a framework NAME — gates these codegen rules. ``framework == "Playwright"``
    # was an algebraically REDUNDANT conjunct: ``framework`` is the ext-derived
    # display string ("Playwright" iff ext in .ts/.js) and ``is_browser_e2e`` already
    # requires ext in .ts/.js, so ``framework == "Playwright" and is_browser_e2e``
    # is byte-identical to ``is_browser_e2e`` alone. Collapsed to the harness gate so
    # the generation core branches on NO framework name (the framework label survives
    # only as prompt-display content below).
    if is_browser_e2e:
        lines.extend([
            "Playwright-specific rules:",
            "- Import from '@playwright/test': test, expect, Page",
            "- Use page object for browser tests, NOT playwrightRequest for API tests.",
            "- For login forms: detect the actual form structure from dependency documents.",
            "  Look for input labels, button text, tab switching if the form has multiple modes.",
            "- Use getByRole, getByLabel, getByText for selectors (accessibility-first).",
            "- For redirects: use page.waitForURL() with regex pattern and reasonable timeout.",
            "- Assert both URL and visible content after navigation.",
            "- Use process.env.BASE_URL for the server URL.",
            "",
            "File header format:",
            f"// @generated-from: <dependency doc paths>",
            "// @generated-by: codd generate",
            "",
        ])

    lines.append("Use the following dependency documents as context for what to test:")
    lines.append("")
    for doc in dependency_documents:
        lines.append(f"--- {doc.node_id} ({doc.path.as_posix()}) ---")
        lines.append(doc.content[:8000])
        lines.append("--- END ---")
        lines.append("")

    if feedback:
        lines.extend([
            "--- REVIEW FEEDBACK ---",
            feedback.rstrip(),
            "--- END REVIEW FEEDBACK ---",
            "",
        ])

    lines.append(f"Output the complete {lang} test file now. No markdown fences. No prose. Just code.")
    return "\n".join(lines).rstrip() + "\n"


def _is_detailed_design_output(output_path: str) -> bool:
    parts = PurePosixPath(output_path).parts
    return len(parts) >= 2 and parts[0] == "docs" and parts[1] == "detailed_design"


# RF4: invocation lives in codd.ai_invoke. The historical private names are
# kept as aliases — generator-internal call sites and downstream modules
# (fixer, ai_patch, propagator, require, restore, planner, implementer,
# assembler, ...) reference ``_invoke_ai_command`` by these names, and tests
# monkeypatch them to intercept generation.
_is_file_writing_agent = is_file_writing_agent
_invoke_file_writing_agent = invoke_file_writing_agent
_invoke_ai_command = invoke_ai


def _sanitize_generated_body(title: str, body: str, *, output_path: str | None = None) -> str:
    # For test code output, skip Markdown-specific sanitization
    if output_path and _is_test_code_output(output_path):
        cleaned = body.strip()
        if not cleaned:
            raise ValueError("AI command returned empty output")
        return cleaned + "\n"

    normalized = body.lstrip()
    if normalized.startswith("---"):
        match = re.match(r"^---\s*\n.*?\n---\s*\n?", normalized, re.DOTALL)
        if match:
            normalized = normalized[match.end():]

    normalized = _strip_meta_preamble(normalized)
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("AI command returned empty output")
    if re.search(r"\bTODO\b", normalized):
        raise ValueError("AI command returned scaffold content containing TODO")
    if not normalized.startswith("# "):
        normalized = f"# {title}\n\n{normalized}"
    normalized = _normalize_title_heading_block(title, normalized)
    normalized = _normalize_section_headings(normalized)
    normalized = _collapse_blank_line_runs(normalized)
    _validate_generated_body(title, normalized, output_path=output_path)

    return normalized.rstrip() + "\n"


def _strip_meta_preamble(body: str) -> str:
    fenced = MARKDOWN_FENCE_RE.match(body)
    if fenced:
        body = fenced.group("body")

    lines = [line for line in body.splitlines() if not _is_meta_preamble_line(line)]
    _trim_outer_non_content_lines(lines)

    return "\n".join(lines)


def _is_meta_preamble_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return any(pattern.match(stripped) for pattern in META_PREAMBLE_PATTERNS)


def _trim_outer_non_content_lines(lines: list[str]) -> None:
    while lines:
        stripped = lines[0].strip()
        if not stripped or stripped == "---":
            lines.pop(0)
            continue
        break

    while lines:
        stripped = lines[-1].strip()
        if not stripped or stripped == "---":
            lines.pop()
            continue
        break


def _collapse_blank_line_runs(body: str) -> str:
    lines = body.splitlines()
    collapsed: list[str] = []
    in_fence = False
    blank_run = 0

    for line in lines:
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            blank_run = 0
            collapsed.append(line)
            continue

        if not in_fence and not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0

        collapsed.append(line)

    return "\n".join(collapsed)


def _normalize_title_heading_block(title: str, body: str) -> str:
    lines = body.splitlines()
    if not lines:
        return body

    expected = re.sub(r"\s+", " ", title).strip().casefold()
    if _normalize_heading_text(lines[0]) != expected:
        return body

    retained: list[str] = [lines[0]]
    index = 1
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped == "---" or FENCE_LINE_RE.match(stripped):
            index += 1
            continue
        if _is_meta_preamble_line(lines[index]):
            index += 1
            continue
        if _normalize_heading_text(lines[index]) == expected:
            index += 1
            continue
        break

    if index < len(lines):
        retained.extend(["", *lines[index:]])

    return "\n".join(retained)


def _normalize_heading_text(line: str) -> str | None:
    match = TITLE_HEADING_RE.match(line)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group("title")).strip().casefold()


def _normalize_section_headings(body: str) -> str:
    """Promote or demote misleveled headings so ``## `` section headings exist.

    AI models sometimes emit ``###`` or bare ``#`` (non-title) headings instead
    of the required ``## `` level.  This function detects the mismatch and
    adjusts heading levels *outside* fenced code blocks.  Bold pseudo-headings
    (``**1. Name**``) are also promoted.

    If ``## `` headings already exist the body is returned unchanged.
    """
    if SECTION_HEADING_RE.search(body):
        return body

    lines = body.splitlines()
    has_title = bool(lines and TITLE_HEADING_RE.match(lines[0]))

    # Tally heading-like patterns (outside fences) to decide the strategy.
    h1_non_title = 0
    h3_count = 0
    bold_count = 0
    in_fence = False
    for idx, line in enumerate(lines):
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if idx == 0 and has_title:
            continue
        if H3_HEADING_RE.match(line):
            h3_count += 1
        elif H1_HEADING_RE.match(line):
            h1_non_title += 1
        elif BOLD_HEADING_RE.match(line):
            bold_count += 1

    if h3_count == 0 and h1_non_title == 0 and bold_count == 0:
        return body  # Nothing we can safely fix

    result: list[str] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if FENCE_LINE_RE.match(line.strip()):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence:
            result.append(line)
            continue

        # Skip the title heading
        if idx == 0 and has_title:
            result.append(line)
            continue

        # Strategy: promote/demote to ##
        if h3_count > 0 and H3_HEADING_RE.match(line):
            result.append(re.sub(r"^###", "##", line))
        elif h1_non_title > 0 and H1_HEADING_RE.match(line) and not (idx == 0 and has_title):
            result.append(re.sub(r"^#\s+", "## ", line))
        elif bold_count > 0:
            m = BOLD_HEADING_RE.match(line)
            if m:
                result.append(f"## {m.group(1)}")
            else:
                result.append(line)
        else:
            result.append(line)

    return "\n".join(result)


def _validate_generated_body(title: str, body: str, *, output_path: str | None = None) -> None:
    if not SECTION_HEADING_RE.search(body):
        raise ValueError(f"AI command returned unstructured summary for {title!r}; missing section headings")

    first_content_line = _first_content_line_after_title(body)
    if first_content_line and any(pattern.match(first_content_line) for pattern in META_PREAMBLE_PATTERNS):
        raise ValueError(f"AI command returned meta commentary instead of document content for {title!r}")

    if output_path and _is_detailed_design_output(output_path):
        if not MERMAID_FENCE_RE.search(body):
            raise ValueError(f"AI command returned detailed design without Mermaid diagrams for {title!r}")

    _validate_test_doc_vb_declaration(body, output_path=output_path)


def _validate_test_doc_vb_declaration(body: str, *, output_path: str | None) -> None:
    """Reject a reference-only test doc that mints a first-column ``VB-*`` table.

    Only the canonical VB document (``docs/test/test_strategy.md``) may declare
    VB ids. A non-canonical test doc (e.g. acceptance_criteria.md) that emits
    first-column ``VB-*`` rows would collide with the canonical registry and make
    100% coverage structurally impossible, so this raises ``ValueError`` —
    feeding deterministic feedback into the bounded generation-retry loop rather
    than relying on the prompt alone (defaults-must-not-flake). Non-test docs and
    the canonical doc are unaffected.
    """

    if not output_path or _infer_doc_type(output_path) != "test":
        return
    from codd.verifiable_behavior_audit import is_canonical_vb_doc, parse_vb_table

    if is_canonical_vb_doc(output_path=output_path):
        return
    declared = parse_vb_table(body)
    if not declared:
        return
    sample = ", ".join(sorted({behavior.vb_id for behavior in declared})[:5])
    raise ValueError(
        f"Non-canonical test document {output_path!r} declares first-column verifiable-behavior "
        f"id(s) ({sample}). Only docs/test/test_strategy.md may declare VB ids. Remove the "
        "first-column `VB-*` table from this document: use AC-*/requirement ids in the first "
        "column and reference the canonical VB ids (from docs/test/test_strategy.md) in a later "
        "column instead."
    )


def _first_content_line_after_title(body: str) -> str | None:
    lines = body.splitlines()
    start_index = 1 if lines and TITLE_HEADING_RE.match(lines[0]) else 0
    for line in lines[start_index:]:
        stripped = line.strip()
        if stripped:
            return stripped
    return None
