"""The two WARN obligation checkers behind the Next.js + Prisma profiles:
``nextjs_adapter:check_route_coverage`` (route_handler_must_be_exercised) and
``prisma_adapter:check_schema_sync`` (client_in_sync_with_schema).

They are advisory (WARN) and best-effort, but they must be REAL enforcement, not
no-op theater: each must fire on the genuinely-bad case and stay quiet on the good
case (and be biased toward anti-false-RED on the undeterminable case).
"""

from __future__ import annotations

from pathlib import Path

from codd.stack.adapters import OBLIGATION_CHECKERS, resolve_checker
from codd.stack.adapters.nextjs import check_route_coverage
from codd.stack.adapters.prisma import check_schema_sync


def _write(p: Path, text: str = "x\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ── registry wiring (the profile refs must resolve to real callables) ─────────

def test_both_warn_checkers_are_registered() -> None:
    assert resolve_checker("nextjs_adapter:check_route_coverage") is check_route_coverage
    assert resolve_checker("prisma_adapter:check_schema_sync") is check_schema_sync
    assert "nextjs_adapter:check_route_coverage" in OBLIGATION_CHECKERS
    assert "prisma_adapter:check_schema_sync" in OBLIGATION_CHECKERS


# ── check_route_coverage ──────────────────────────────────────────────────────

def test_route_coverage_clean_when_handler_is_exercised(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "api" / "health" / "route.ts", "export async function GET() {}\n")
    _write(
        tmp_path / "e2e" / "health.spec.ts",
        "test('h', async ({request}) => { await request.get('/api/health'); });\n",
    )
    assert check_route_coverage(tmp_path) == []


def test_route_coverage_flags_an_unexercised_handler(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "api" / "orphan" / "route.ts", "export async function GET() {}\n")
    _write(tmp_path / "e2e" / "other.spec.ts", "test('x', async () => {});\n")
    findings = check_route_coverage(tmp_path)
    assert len(findings) == 1
    assert findings[0].obligation_id == "route_handler_must_be_exercised"
    assert "orphan" in findings[0].location


def test_route_coverage_quiet_when_no_handlers(tmp_path: Path) -> None:
    _write(tmp_path / "app" / "page.tsx", "export default function P(){}\n")
    assert check_route_coverage(tmp_path) == []


def test_route_coverage_finds_src_app_and_pages_api(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app" / "api" / "ping" / "route.ts", "export async function GET(){}\n")
    _write(tmp_path / "pages" / "api" / "legacy.ts", "export default function h(){}\n")
    _write(tmp_path / "tests" / "unrelated.spec.ts", "test('x', () => {});\n")  # exercises neither
    assert len(check_route_coverage(tmp_path)) == 2  # both handlers (src/app + pages/api) flagged


def test_route_coverage_no_false_red_without_a_test_tree(tmp_path: Path) -> None:
    # Handlers but no test directory at all: undeterminable against → no finding (WARN,
    # anti-false-RED). Route coverage is only asserted once the project has e2e tests.
    _write(tmp_path / "app" / "api" / "health" / "route.ts", "export async function GET(){}\n")
    assert check_route_coverage(tmp_path) == []


# ── check_schema_sync ─────────────────────────────────────────────────────────

_SCHEMA = "model User {\n  id Int @id\n}\n"


def test_schema_sync_clean_when_client_matches(tmp_path: Path) -> None:
    _write(tmp_path / "prisma" / "schema.prisma", _SCHEMA)
    _write(tmp_path / "node_modules" / ".prisma" / "client" / "schema.prisma", _SCHEMA)
    assert check_schema_sync(tmp_path) == []


def test_schema_sync_flags_drift(tmp_path: Path) -> None:
    _write(tmp_path / "prisma" / "schema.prisma", _SCHEMA + "model Post {\n  id Int @id\n}\n")
    _write(tmp_path / "node_modules" / ".prisma" / "client" / "schema.prisma", _SCHEMA)
    findings = check_schema_sync(tmp_path)
    assert len(findings) == 1
    assert findings[0].obligation_id == "client_in_sync_with_schema"
    assert "out of sync" in findings[0].detail


def test_schema_sync_flags_ungenerated_client(tmp_path: Path) -> None:
    _write(tmp_path / "prisma" / "schema.prisma", _SCHEMA)
    findings = check_schema_sync(tmp_path)
    assert len(findings) == 1
    assert "not generated" in findings[0].detail


def test_schema_sync_quiet_when_no_schema(tmp_path: Path) -> None:
    assert check_schema_sync(tmp_path) == []


def test_schema_sync_no_false_red_when_copy_absent(tmp_path: Path) -> None:
    # client dir exists but exposes no embedded schema copy -> undeterminable, no finding.
    _write(tmp_path / "prisma" / "schema.prisma", _SCHEMA)
    (tmp_path / "node_modules" / ".prisma" / "client").mkdir(parents=True)
    _write(tmp_path / "node_modules" / ".prisma" / "client" / "index.js", "// generated\n")
    assert check_schema_sync(tmp_path) == []
