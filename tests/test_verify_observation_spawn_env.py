"""AST invariant: every verify OBSERVATION-surface subprocess spawn threads env.

The env-channel projection (v3.15.0) makes an unchanged bare ``argv[0]`` resolve
against a harness-provisioned interpreter by prepending recorded dirs to the spawn
``PATH``. It must reach EVERY surface that observes the SUT by spawning a shell/
non-absolute command — not just the two the first cut covered. Enumerating those
surfaces once by hand is exactly what missed the third one (``template.execute``)
and shipped a gap the dogfood caught.

This test fixes the enumeration as a machine-checked invariant: any
``subprocess.run(...)`` in the observation modules (verify runner, contract
executor, and EVERY verification template — the glob auto-includes new ones) must
pass an explicit ``env=`` keyword. A new template or a new spawn that forgets the
env-channel turns this RED before it can ship. Producers (the provisioner, which
pins absolute argv) and harness tools (git) live in OTHER modules and are out of
scope by construction — this scans only the observation surface.
"""

from __future__ import annotations

import ast
from pathlib import Path

import codd

_CODD_ROOT = Path(codd.__file__).parent

_OBSERVATION_FILES = [
    _CODD_ROOT / "repair" / "verify_runner.py",
    _CODD_ROOT / "languages" / "verify_executor.py",
    *sorted((_CODD_ROOT / "deployment" / "providers" / "verification").glob("*.py")),
]


def _is_subprocess_run(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _passes_env(call: ast.Call) -> bool:
    # explicit ``env=`` keyword, or ``**kwargs`` forwarding (which may carry env).
    return any(kw.arg == "env" or kw.arg is None for kw in call.keywords)


def test_all_verify_observation_subprocess_runs_thread_env():
    violations: list[str] = []
    scanned = 0
    for path in _OBSERVATION_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_subprocess_run(node):
                scanned += 1
                if not _passes_env(node):
                    violations.append(f"{path.name}:{node.lineno}")
    # Guard against a silent zero-match (e.g. a refactor that renamed the call):
    # the invariant is only meaningful if it actually inspected real spawns.
    assert scanned >= 5, f"expected to scan the known observation spawns, saw {scanned}"
    assert not violations, (
        "verify observation-surface subprocess.run(...) without an explicit env= "
        "keyword — the env-channel is not threaded to this spawn, so a bare argv[0] "
        f"will not resolve against a provisioned interpreter: {violations}"
    )
