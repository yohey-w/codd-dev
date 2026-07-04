"""Tests for the deterministic test-root re-key (B1 task-load + B2 fence-side).

A task whose declared output is test-SHAPED (``.test.js`` etc.) but sits outside
every configured scan root — the JS greenfield's ``test/errors.test.js`` while the
harness owns ``tests/`` — is a self-contradiction: the kind gate demands a produced
'test' deliverable, but the output-path fence drops the misplaced file, so the
deliverable reads as never produced → a hard ``StageError``.

B1 normalizes such a declared output on READ (``test/errors.test.js`` →
``tests/errors.test.js``) so every consumer sees the corrected path through one
chokepoint; B2 is the fence-side backstop that replants a misplaced test-shaped
PAYLOAD under the owned test root instead of dropping it. Both are conservative,
deterministic re-keys to the OWNED root — never a fence relaxation (accepting a
test file in place would false-green the kind gate for a file the runner never
executes).
"""

from __future__ import annotations

from codd.implementer import (
    _normalize_declared_test_outputs,
    _parse_file_payloads,
    _test_root_from_config,
)


def _js_config() -> dict:
    return {"scan": {"test_dirs": ["tests"], "source_dirs": ["src"]}}


# ---------------------------------------------------------------------------
# B1 — declared-output normalization at task-load
# ---------------------------------------------------------------------------


def test_b1_rekeys_singular_test_dir():
    assert _normalize_declared_test_outputs(["test/errors.test.js"], _js_config()) == [
        "tests/errors.test.js"
    ]


def test_b1_rekeys_glob():
    assert _normalize_declared_test_outputs(["test/**/*.test.js"], _js_config()) == [
        "tests/**/*.test.js"
    ]


def test_b1_rekeys_bare_basename():
    assert _normalize_declared_test_outputs(["errors.test.js"], _js_config()) == [
        "tests/errors.test.js"
    ]


def test_b1_rekeys_nested_subdir_preserving_tail():
    assert _normalize_declared_test_outputs(["test/unit/x.test.js"], _js_config()) == [
        "tests/unit/x.test.js"
    ]


def test_b1_noop_already_under_test_root():
    outs = ["tests/errors.test.js"]
    assert _normalize_declared_test_outputs(list(outs), _js_config()) == outs


def test_b1_noop_under_source_root():
    # A genuinely misplaced test under src/ is LEFT there — verify catches it (and
    # re-keying a src-colocated test would be wrong). Anti-false-green.
    outs = ["src/foo.test.js"]
    assert _normalize_declared_test_outputs(list(outs), _js_config()) == outs


def test_b1_noop_non_test_shaped():
    outs = ["errors.js"]
    assert _normalize_declared_test_outputs(list(outs), _js_config()) == outs


def test_b1_noop_go_root_module_dot_guard():
    # Go: scan roots are '.' — the under-root check cannot distinguish in-root from
    # out-of-root, so the WHOLE normalization no-ops (protects root-module stacks).
    go = {"scan": {"test_dirs": ["."], "source_dirs": ["."]}}
    outs = ["errors_test.go"]
    assert _normalize_declared_test_outputs(list(outs), go) == outs


def test_b1_noop_brownfield_test_dir():
    # A brownfield stack whose real test dir IS ``test`` leaves ``test/...`` alone.
    bf = {"scan": {"test_dirs": ["test"], "source_dirs": ["src"]}}
    outs = ["test/errors.test.js"]
    assert _normalize_declared_test_outputs(list(outs), bf) == outs


def test_b1_noop_empty_test_dirs():
    outs = ["test/errors.test.js"]
    assert _normalize_declared_test_outputs(list(outs), {"scan": {}}) == outs


def test_b1_mixed_list_rekeys_only_misplaced_test():
    got = _normalize_declared_test_outputs(
        ["src/errors.js", "test/errors.test.js"], _js_config()
    )
    assert got == ["src/errors.js", "tests/errors.test.js"]


def test_test_root_from_config():
    assert _test_root_from_config(_js_config()) == "tests"
    assert _test_root_from_config({"scan": {"test_dirs": ["."]}}) is None
    assert _test_root_from_config({"scan": {}}) is None


# ---------------------------------------------------------------------------
# B2 — fence-side re-key backstop (in _parse_file_payloads)
# ---------------------------------------------------------------------------


def _file_block(header: str) -> str:
    return f"=== FILE: {header} ===\n```js\nexport const e = 1;\ntest('x', () => {{}});\n```\n"


def _blocks(*headers: str) -> str:
    return "".join(_file_block(h) for h in headers)


def test_b2_rekeys_misplaced_test_payload():
    raw = _blocks("src/errors.js", "test/errors.test.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src", "tests"], "javascript", test_root="tests")}
    assert got == {"src/errors.js", "tests/errors.test.js"}
    assert "test/errors.test.js" not in got


def test_b2_collision_keeps_drop():
    # Model emitted BOTH test/x and tests/x → the in-root file wins, the misplaced
    # dup drops (its re-key target collides), staying deterministic.
    raw = _blocks("tests/errors.test.js", "test/errors.test.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src", "tests"], "javascript", test_root="tests")}
    assert got == {"tests/errors.test.js"}


def test_b2_in_prefix_untouched():
    raw = _blocks("src/errors.js", "tests/errors.test.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src", "tests"], "javascript", test_root="tests")}
    assert got == {"src/errors.js", "tests/errors.test.js"}


def test_b2_non_test_config_file_still_dropped(capsys):
    # vitest.config.js is NOT test-shaped → B2 never touches it; it stays dropped.
    raw = _blocks("src/errors.js", "vitest.config.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src", "tests"], "javascript", test_root="tests")}
    assert got == {"src/errors.js"}


def test_b2_noop_when_test_root_not_in_fence():
    # The task's fence does not reach under the test root (no ``tests`` prefix) →
    # B2 does not fire; the misplaced test drops (unchanged behavior).
    raw = _blocks("src/errors.js", "test/errors.test.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src"], "javascript", test_root="tests")}
    assert got == {"src/errors.js"}


def test_b2_noop_without_test_root():
    # No test_root threaded (default) → strictly the pre-fix drop behavior.
    raw = _blocks("src/errors.js", "test/errors.test.js")
    got = {p for p, _ in _parse_file_payloads(raw, ["src", "tests"], "javascript")}
    assert got == {"src/errors.js"}


def test_b2_emits_loud_warning(capsys):
    raw = _blocks("src/errors.js", "test/errors.test.js")
    _parse_file_payloads(raw, ["src", "tests"], "javascript", test_root="tests")
    err = capsys.readouterr().err
    assert "RE-KEYED" in err
    assert "tests/errors.test.js" in err
