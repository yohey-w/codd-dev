"""v3.35.0 Item ① — regeneration parity: the mechanical contract flows to the
REPAIR regeneration prompt, so a single repair cannot undo v3.33.0's signature
convergence (re-opening Root A / TS2835 module-specifier, Root C / TS2307
runtime-dep).

The monotonicity invariant: a repair regeneration prompt's mechanical-contract
context ⊇ the first-pass implement prompt's (at SIGNATURE granularity). Four
acceptance checks (Fable5 検収 ``fable5_reply_2026-07-12_codd-hardening.md`` §Item①):

  (a) a consumer (SOURCE) repair prompt carries the distance-1 producer's SIGNATURE
      slice + the declared mechanical blocks (module-specifier / runtime-dep);
  (b) the producer BODY is NOT carried (signature is floor AND ceiling);
  (c) a test-unit repair stays SUT-blind — mechanical blocks only, no SUT surface;
  (d) a renderer-less language degrades to NAMES (the implement ladder).

Every assertion binds to the mechanical/profile DATA path; no ``language ==`` and
no domain literal is exercised here — the only language-specific tokens are the
TypeScript fixture's own contents (the language ZONE), never the core logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.repair.llm_repair_engine import LlmRepairEngine
from codd.repair.schema import RootCauseAnalysis


#: A stable substring of ``REGENERATION_CONTRACT_RULE`` — asserted directly (rather
#: than importing the constant) so the RED baseline manifests as a per-test
#: assertion failure, not a module import error.
_REGENERATION_RULE_MARKER = "MECHANICAL CONTRACT (regeneration parity"


# ── fixture: a TypeScript producer + a SOURCE consumer + a TEST consumer ────────

#: Producer surface: an interface (a signature) + a function whose SIGNATURE the
#: consumer must bind, and whose BODY carries a sentinel that must NEVER leak.
_PRODUCER = (
    "export interface AppResponse { status: number; body: string; }\n"
    "export function request(method: string, path: string, opts?: object): "
    "Promise<AppResponse> {\n"
    '  const PRODUCER_BODY_SENTINEL = "do-not-leak-42";\n'
    "  return Promise.resolve({ status: 200, body: PRODUCER_BODY_SENTINEL });\n"
    "}\n"
)
#: SOURCE consumer — imports the producer (its distance-1 edge). Calls ``request``
#: WITHOUT restating the signature, so any signature text in the prompt can only
#: come from the injected producer surface, never from this file's own contents.
_SOURCE_CONSUMER = (
    'import { request, AppResponse } from "./http.js";\n'
    "export function boot(): Promise<AppResponse> { return request(\"GET\", \"/\"); }\n"
)
#: TEST consumer — its distance-1 producer IS the SUT; injecting the SUT signature
#: here would break impl-blindness (a false-green vector).
_TEST_CONSUMER = (
    'import { request } from "../src/http.js";\n'
    'import { describe, it, expect } from "vitest";\n'
    'describe("server", () => {\n'
    '  it("boots", async () => { expect(await request("GET", "/")).toBeDefined(); });\n'
    "});\n"
)

_PRODUCER_SIGNATURE = "request(method: string, path: string, opts?: object)"
_MODULE_SPECIFIER_MARKER = "MODULE-SPECIFIER COHERENCE"
_RUNTIME_DEP_MARKER = "RUNTIME DEPENDENCY DECLARATION"


class FakeAiCommand:
    """Captures every prompt and returns a canned propose response."""

    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


def _propose_response(file_path: str) -> str:
    # full_file_replacement skips unified-diff dry-run validation, so propose_fix
    # returns on the first invoke and we can read the captured prompt.
    return json.dumps(
        {
            "patches": [
                {"file_path": file_path, "patch_mode": "full_file_replacement", "content": "x"}
            ],
            "rationale": "align impl",
            "confidence": 0.5,
        }
    )


def _rca(affected: list[str]) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="impl drift",
        affected_nodes=list(affected),
        repair_strategy="full_file_replacement",
        confidence=0.6,
        analysis_timestamp="2026-07-12T00:00:00Z",
    )


def _seed(root: Path) -> None:
    (root / "codd").mkdir(parents=True, exist_ok=True)
    (root / "codd" / "codd.yaml").write_text(
        yaml.safe_dump(
            {
                "project": {"name": "demo", "language": "typescript"},
                "scan": {"source_dirs": ["src"], "test_dirs": ["tests"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "http.ts").write_text(_PRODUCER, encoding="utf-8")
    (root / "src" / "server.ts").write_text(_SOURCE_CONSUMER, encoding="utf-8")
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "server.test.ts").write_text(_TEST_CONSUMER, encoding="utf-8")


def _propose_prompt(root: Path, target: str, *, monkeypatch=None) -> str:
    _seed(root)
    ai = FakeAiCommand(_propose_response(target))
    engine = LlmRepairEngine(
        project_root=root,
        ai_command={"repair_propose": ai},
        max_strategy_attempts=1,
    )
    contents = {target: (root / target).read_text(encoding="utf-8")}
    engine.propose_fix(_rca([target]), contents)
    return ai.prompts[0]


# ── (a) consumer repair carries producer SIGNATURE + declared mechanical blocks ─


def test_source_repair_prompt_carries_producer_signature_and_mechanical_blocks(tmp_path: Path):
    prompt = _propose_prompt(tmp_path, "src/server.ts")

    # The distance-1 producer's SIGNATURE slice (only source is the injected surface).
    assert _PRODUCER_SIGNATURE in prompt
    # The declared mechanical blocks (module-specifier + runtime-dep for TS).
    assert _MODULE_SPECIFIER_MARKER in prompt
    assert _RUNTIME_DEP_MARKER in prompt
    # The regeneration-parity rule header states the monotonicity contract.
    assert _REGENERATION_RULE_MARKER in prompt


# ── (b) the producer BODY is never carried (signature is floor AND ceiling) ─────


def test_source_repair_prompt_omits_producer_body(tmp_path: Path):
    prompt = _propose_prompt(tmp_path, "src/server.ts")

    # Signature IS carried (the positive rung — red without the injection)...
    assert _PRODUCER_SIGNATURE in prompt
    # ...but the producer's function BODY (and its sentinel) is NEVER rendered.
    assert "PRODUCER_BODY_SENTINEL" not in prompt
    assert "do-not-leak-42" not in prompt


# ── (c) a test-unit repair stays SUT-blind: mechanical blocks only ──────────────


def test_test_unit_repair_is_sut_blind(tmp_path: Path):
    prompt = _propose_prompt(tmp_path, "tests/server.test.ts")

    # Mechanical blocks still flow (they are design/profile-derived, not SUT-behaviour).
    assert _MODULE_SPECIFIER_MARKER in prompt
    # But NO SUT signature surface reaches an impl-blind test repair.
    assert "DISTANCE-1 PRODUCER" not in prompt
    assert _PRODUCER_SIGNATURE not in prompt


# ── (d) a renderer-less language degrades to NAMES (the implement ladder) ───────


def test_renderer_less_language_degrades_to_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Simulate a language whose SIGNATURE renderer has no arm (returns None) while a
    # NAME extractor still exists — the middle rung of signature → names → paths.
    monkeypatch.setattr(
        "codd.implement_oracle_scope.render_public_surface", lambda *a, **k: None
    )
    prompt = _propose_prompt(tmp_path, "src/server.ts")

    # Degraded to the name-level surface, never the signature form, never a body.
    assert "Exported symbols: AppResponse, request" in prompt
    assert "public surface names" in prompt
    assert _PRODUCER_SIGNATURE not in prompt
    assert "PRODUCER_BODY_SENTINEL" not in prompt
