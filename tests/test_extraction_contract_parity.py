"""STEP-0 byte-identical parity oracle for the CEG extraction engine.

Contract Kernel Cut Condition A — the PARSING/EXTRACTION zone. This pins the
CURRENT extraction output (symbols, imports, code-patterns, schema, call-graph,
module-name mapping, test-target guessing) for a representative source file in
EACH language whose behavior is produced by the inline ``if language == X`` /
``if language in (...)`` ladders being de-literalized in:

  * ``codd/extractor.py`` core functions (``_extract_symbols``,
    ``_extract_imports``, ``_detect_code_patterns``, ``_common_stdlib``,
    ``_file_to_module``, ``_guess_test_target``, ``_language_extensions``)
  * ``codd/parsing/__init__.py:get_extractor`` selection
  * ``codd/scanner.py:_extract_imports_basic`` TS/JS relative resolution

The de-literalization is STRUCTURAL (polymorphic methods + registry-data
selection), NOT a behavior change: every assertion here MUST stay byte-identical
before and after the refactor. The values are snapshotted from the pre-refactor
engine; if any of them changes, the refactor altered behavior and is WRONG.

These exercise BOTH seams that carry the language ladders:
  1. the public extractor objects returned by ``get_extractor`` (the AST/regex
     path actually used by the pipeline), and
  2. the module-level regex helpers in ``extractor.py`` (the verbatim bodies of
     the ``if language ==`` blocks), so the moved-onto-the-object logic is
     proven identical to its old inline form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codd import extractor as extractor_module
from codd.extractor import (
    _common_stdlib,
    _detect_code_patterns,
    _extract_imports,
    _extract_symbols,
    _file_to_module,
    _guess_test_target,
    _language_extensions,
    ModuleInfo,
)
from codd.parsing import RegexExtractor, get_extractor


# ─────────────────────────────────────────────────────────────────────────────
# Representative sources per language (exercise the language== branches)
# ─────────────────────────────────────────────────────────────────────────────

PY_SRC = '''\
import os
import json
from db.models import User
from utils.crypto import hash_password
import jwt


@app.get("/users")
def list_users(limit: int) -> list:
    return []


class UserService(Base):
    def create(self, name: str) -> int:
        return list_users(10)


@celery_app.task
def send_email(to: str):
    return None
'''

TS_SRC = '''\
import { Foo } from "./foo";
import { Bar } from "../shared/bar";
import express from "express";
import type { Baz } from "@scope/pkg";

export class UserController extends BaseEntity {
    handle(): void {}
}

export interface UserShape {
    id: number;
}

export function listUsers(limit: number): User[] {
    return [];
}

export const makeUser = (name: string) => ({ name });

router.get("/users", listUsers);
'''

JS_SRC = '''\
import { Foo } from "./foo";
import express from "express";

export class Widget {
    render() {}
}

export function build(a, b) {
    return a + b;
}

app.post("/widgets", build);
'''

GO_SRC = '''\
package main

import (
    "fmt"
    "github.com/gin-gonic/gin"
)

type Server struct {
    Addr string
}

func NewServer(addr string) *Server {
    return &Server{Addr: addr}
}

func (s *Server) Run() error {
    fmt.Println(s.Addr)
    return nil
}
'''

JAVA_SRC = '''\
package com.example;

public class UserService {
    public int createUser(String name) {
        return 0;
    }

    private void helper() {}
}
'''

SQL_SRC = '''\
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    org_id INTEGER,
    FOREIGN KEY (org_id) REFERENCES orgs(id)
);

CREATE INDEX idx_users_name ON users(name);
'''

PRISMA_SRC = '''\
model User {
  id    Int    @id @default(autoincrement())
  name  String
  posts Post[]
}

enum Role {
  ADMIN
  USER
}
'''


def _symbol_tuples(symbols):
    return [
        (s.name, s.kind, s.line, s.params, s.return_type, s.is_async,
         tuple(s.bases), tuple(s.implements))
        for s in symbols
    ]


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


# ─────────────────────────────────────────────────────────────────────────────
# extract_symbols — the module-level _extract_symbols (regex ladder body) AND
# the get_extractor object path must both stay byte-identical.
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED_REGEX_SYMBOLS = {
    "python": [
        ("list_users", "function", 9, "limit: int", "", False, (), ()),
        ("UserService", "class", 13, "", "", False, (), ()),
        ("create", "function", 14, "self, name: str", "", False, (), ()),
        ("send_email", "function", 19, "to: str", "", False, (), ()),
    ],
    "typescript": [
        ("UserController", "class", 6, "", "", False, (), ()),
        ("listUsers", "function", 14, "limit: number", "", False, (), ()),
        ("makeUser", "function", 18, "", "", False, (), ()),
    ],
    "javascript": [
        ("Widget", "class", 4, "", "", False, (), ()),
        ("build", "function", 8, "a, b", "", False, (), ()),
    ],
    "go": [
        ("Server", "class", 8, "", "", False, (), ()),
        ("NewServer", "function", 12, "addr string", "", False, (), ()),
        ("Run", "function", 16, "", "", False, (), ()),
    ],
    "java": [
        ("UserService", "class", 3, "", "", False, (), ()),
        ("createUser", "function", 4, "String name", "", False, (), ()),
    ],
}

_SRC_BY_LANG = {
    "python": PY_SRC,
    "typescript": TS_SRC,
    "javascript": JS_SRC,
    "go": GO_SRC,
    "java": JAVA_SRC,
}


@pytest.mark.parametrize("language", sorted(EXPECTED_REGEX_SYMBOLS))
def test_module_level_extract_symbols_byte_identical(language):
    """The verbatim regex ladder body in extractor._extract_symbols is pinned."""
    got = _symbol_tuples(_extract_symbols(_SRC_BY_LANG[language], "f", language))
    assert got == EXPECTED_REGEX_SYMBOLS[language]


@pytest.mark.parametrize("language", sorted(EXPECTED_REGEX_SYMBOLS))
def test_regex_extractor_object_symbols_match_module_level(language):
    """RegexExtractor.extract_symbols delegates to the same ladder body."""
    obj = RegexExtractor(language, "source")
    got = _symbol_tuples(obj.extract_symbols(_SRC_BY_LANG[language], "f"))
    assert got == EXPECTED_REGEX_SYMBOLS[language]


def test_get_extractor_python_ast_symbols_byte_identical():
    """The Python AST extractor (the path the pipeline actually uses)."""
    ext = get_extractor("python", "source")
    got = _symbol_tuples(ext.extract_symbols(PY_SRC, "f"))
    assert got == [
        ("list_users", "function", 9, "limit: int", "list", False, (), ()),
        ("UserService", "class", 13, "", "", False, ("Base",), ()),
        ("create", "function", 14, "self, name: str", "int", False, (), ()),
        ("send_email", "function", 19, "to: str", "", False, (), ()),
    ]


def test_get_extractor_typescript_ast_symbols_byte_identical():
    ext = get_extractor("typescript", "source")
    got = _symbol_tuples(ext.extract_symbols(TS_SRC, "f.ts"))
    assert got == [
        ("UserController", "class", 6, "", "", False, ("BaseEntity",), ()),
        ("UserShape", "interface", 10, "", "", False, (), ()),
        ("listUsers", "function", 14, "limit: number", "User[]", False, (), ()),
        ("makeUser", "function", 18, "name: string", "", False, (), ()),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# extract_imports — the module-level _extract_imports ladder body (internal /
# external classification across python/ts/js/go).
# ─────────────────────────────────────────────────────────────────────────────

def _imports_via_module(tmp_path, language, src, rel):
    src_dir = tmp_path / "src"
    full = _write(tmp_path, f"src/{rel}", src)
    # Create sibling modules so python internal-detection resolves.
    return _extract_imports(src, language, tmp_path, src_dir, full)


def test_module_level_extract_imports_python(tmp_path):
    src_dir = tmp_path / "src"
    (src_dir / "db").mkdir(parents=True)
    (src_dir / "db" / "models.py").write_text("", encoding="utf-8")
    (src_dir / "utils").mkdir(parents=True)
    (src_dir / "utils" / "crypto.py").write_text("", encoding="utf-8")
    full = _write(tmp_path, "src/app.py", PY_SRC)
    internal, external = _extract_imports(PY_SRC, "python", tmp_path, src_dir, full)
    assert {k: sorted(v) for k, v in internal.items()} == {
        "db": ["from db.models import User"],
        "utils": ["from utils.crypto import hash_password"],
    }
    assert external == {"jwt"}


def test_module_level_extract_imports_typescript(tmp_path):
    # The module-level regex seam (the verbatim ``if language in
    # ("typescript","javascript")`` block) does NOT probe file existence: it
    # ``.resolve()``s the relative path as-is, so ``./foo`` keys on ``foo`` (no
    # ``.ts`` suffix) and ``../shared/bar`` (which escapes ``src`` after resolve)
    # falls to external. This asymmetry vs. the AST seam below is PRE-EXISTING
    # behavior and is pinned exactly.
    src_dir = tmp_path / "src"
    _write(tmp_path, "src/foo.ts", "export const x = 1;")
    _write(tmp_path, "src/shared/bar.ts", "export const y = 2;")
    full = _write(tmp_path, "src/app.ts", TS_SRC)
    internal, external = _extract_imports(TS_SRC, "typescript", tmp_path, src_dir, full)
    assert {k: sorted(v) for k, v in internal.items()} == {
        "foo": ['import { Foo } from "./foo";'],
    }
    assert external == {"express", "@scope/pkg", "../shared/bar"}


def test_module_level_extract_imports_go(tmp_path):
    src_dir = tmp_path / "src"
    full = _write(tmp_path, "src/main.go", GO_SRC)
    internal, external = _extract_imports(GO_SRC, "go", tmp_path, src_dir, full)
    assert internal == {}
    assert external == {"fmt", "gin"}


def test_get_extractor_python_ast_imports_byte_identical(tmp_path):
    src_dir = tmp_path / "src"
    (src_dir / "db").mkdir(parents=True)
    (src_dir / "db" / "models.py").write_text("", encoding="utf-8")
    (src_dir / "utils").mkdir(parents=True)
    (src_dir / "utils" / "crypto.py").write_text("", encoding="utf-8")
    full = _write(tmp_path, "src/app.py", PY_SRC)
    ext = get_extractor("python", "source")
    internal, external = ext.extract_imports(PY_SRC, full, tmp_path, src_dir)
    assert {k: sorted(v) for k, v in internal.items()} == {
        "db": ["from db.models import User"],
        "utils": ["from utils.crypto import hash_password"],
    }
    assert external == {"jwt"}


def test_get_extractor_typescript_ast_imports_byte_identical(tmp_path):
    src_dir = tmp_path / "src"
    _write(tmp_path, "src/foo.ts", "export const x = 1;")
    _write(tmp_path, "src/shared/bar.ts", "export const y = 2;")
    full = _write(tmp_path, "src/app.ts", TS_SRC)
    ext = get_extractor("typescript", "source")
    internal, external = ext.extract_imports(TS_SRC, full, tmp_path, src_dir)
    # The AST seam (TreeSitterExtractor → ``_record_js_import``) DOES probe
    # suffixes, so ``./foo`` resolves to the existing ``foo.ts`` and keys on
    # ``foo.ts``; ``../shared/bar`` still escapes ``src`` after resolve and falls
    # to external. Pinned exactly (note the key differs from the regex seam).
    assert {k: sorted(v) for k, v in internal.items()} == {
        "foo.ts": ['import { Foo } from "./foo";'],
    }
    assert external == {"express", "@scope/pkg", "../shared/bar"}


# ─────────────────────────────────────────────────────────────────────────────
# _common_stdlib — the only language with a non-empty set is python.
# ─────────────────────────────────────────────────────────────────────────────

def test_common_stdlib_python_pinned():
    s = _common_stdlib("python")
    assert "os" in s and "asyncio" in s and "json" in s
    assert len(s) == 82


@pytest.mark.parametrize("language", ["typescript", "javascript", "go", "java", "unknown"])
def test_common_stdlib_nonpython_empty(language):
    assert _common_stdlib(language) == set()


# ─────────────────────────────────────────────────────────────────────────────
# _detect_code_patterns — the regex ladder body for python/ts/js.
# ─────────────────────────────────────────────────────────────────────────────

def test_module_level_detect_code_patterns_python():
    mod = ModuleInfo(name="m")
    _detect_code_patterns(mod, PY_SRC, "python")
    assert mod.patterns == {
        "api_routes": "HTTP route handlers",
        "db_models": "ORM models",
        "background_tasks": "Async task handlers",
    }


def test_module_level_detect_code_patterns_typescript():
    mod = ModuleInfo(name="m")
    _detect_code_patterns(mod, TS_SRC, "typescript")
    # TS_SRC has ``router.get(...)`` (api_routes) but no ``schema(``/``model(``
    # call, so db_models is NOT set by the regex ladder. Pinned exactly.
    assert mod.patterns == {"api_routes": "HTTP route handlers"}


def test_module_level_detect_code_patterns_typescript_db_models_branch():
    """Cover the ts/js db_models + NestJS + page/middleware/auth branches."""
    src = (
        "const userModel = model('User');\n"
        "@Controller('/x')\n"
        "export default async function ProfilePage() {}\n"
        "export async function middleware() { NextResponse.redirect('/login'); }\n"
        "router.push('/home');\n"
        "import { getServerSession } from 'next-auth';\n"
    )
    mod = ModuleInfo(name="m")
    _detect_code_patterns(mod, src, "typescript")
    assert mod.patterns == {
        "api_routes": "NestJS controller",
        "db_models": "Database models",
        "page_routes": "Page route components",
        "auth_redirects": "Server-side redirects",
        "middleware": "Request middleware",
        "client_redirects": "Client-side navigation",
        "auth_provider": "Authentication provider",
    }


def test_get_extractor_python_ast_detect_code_patterns_byte_identical():
    mod = ModuleInfo(name="m")
    get_extractor("python", "source").detect_code_patterns(mod, PY_SRC)
    assert mod.patterns == {
        "api_routes": "HTTP route handlers: /users",
        "db_models": "ORM models: UserService",
        "background_tasks": "Async task handlers",
    }


# ─────────────────────────────────────────────────────────────────────────────
# extract_schema — sql / prisma selection via get_extractor("...", "schema").
# ─────────────────────────────────────────────────────────────────────────────

def test_get_extractor_sql_schema_byte_identical(tmp_path):
    ext = get_extractor("sql", "schema")
    info = ext.extract_schema(SQL_SRC, tmp_path / "s.sql")
    assert [t["name"] for t in info.tables] == ["users"]
    assert [fk["table"] for fk in info.foreign_keys] == ["users"]
    assert [ix["name"] for ix in info.indexes] == ["idx_users_name"]


def test_get_extractor_prisma_schema_byte_identical(tmp_path):
    ext = get_extractor("prisma", "schema")
    info = ext.extract_schema(PRISMA_SRC, tmp_path / "schema.prisma")
    assert [m["name"] for m in info.models] == ["User"]
    assert [e["name"] for e in info.enums] == ["Role"]


def test_get_extractor_unknown_schema_falls_back_to_regex(tmp_path):
    """Unknown schema language → RegexExtractor, returns None (best-effort)."""
    ext = get_extractor("toml", "schema")
    assert isinstance(ext, RegexExtractor)
    assert ext.extract_schema("x = 1", tmp_path / "f.toml") is None


# ─────────────────────────────────────────────────────────────────────────────
# get_extractor selection identity — which class for which (language, category).
# ─────────────────────────────────────────────────────────────────────────────

def test_get_extractor_selection_identity():
    from codd.parsing import (
        PrismaSchemaExtractor,
        PythonAstExtractor,
        SqlDdlExtractor,
        TreeSitterExtractor,
    )

    # PythonAst (stdlib ast) + Prisma are ALWAYS available; tree-sitter (ts/js)
    # + SqlDdl are OPTIONAL deps — the selector gracefully DEGRADES to RegexExtractor
    # when they are absent (e.g. a CI matrix job without the [scan]/[tree-sitter]
    # extras). Assert the preferred extractor IFF available, else the regex fallback,
    # so the test verifies the degradation CONTRACT instead of assuming an environment
    # (the v2.87 regression: CI py3.11 lacked the SqlDdl dep and got RegexExtractor).
    assert isinstance(get_extractor("python", "source"), PythonAstExtractor)
    assert isinstance(get_extractor("prisma", "schema"), PrismaSchemaExtractor)

    def _assert_selected(language: str, category: str, preferred: type, available: bool) -> None:
        got = get_extractor(language, category)
        expected = preferred if available else RegexExtractor
        assert isinstance(got, expected), (
            f"{language}/{category}: got {type(got).__name__}, "
            f"expected {expected.__name__} (available={available})"
        )

    _assert_selected("typescript", "source", TreeSitterExtractor, TreeSitterExtractor.is_available("typescript"))
    _assert_selected("javascript", "source", TreeSitterExtractor, TreeSitterExtractor.is_available("javascript"))
    _assert_selected("sql", "schema", SqlDdlExtractor, SqlDdlExtractor.is_available())
    # unknown language / category → regex fallback (best-effort analysis)
    assert isinstance(get_extractor("ruby", "source"), RegexExtractor)
    assert isinstance(get_extractor("go", "source"), RegexExtractor)
    assert isinstance(get_extractor("python", "schema"), RegexExtractor)


# ─────────────────────────────────────────────────────────────────────────────
# _file_to_module — language-keyed module-name mapping.
# ─────────────────────────────────────────────────────────────────────────────

def test_file_to_module_byte_identical(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    cases = [
        ("python", "src/auth/service.py", "auth"),
        ("python", "src/auth/__init__.py", "auth"),
        ("python", "src/main.py", "main"),
        ("typescript", "src/components/Button.ts", "components"),
        ("javascript", "src/lib/util.js", "lib"),
        ("java", "src/main/java/com/Foo.java", "com"),
        ("go", "src/cmd/server.go", "cmd"),
        ("ruby", "src/app/thing.rb", "app"),
    ]
    for language, rel, expected in cases:
        assert _file_to_module(rel, tmp_path, src, language) == expected, (language, rel)


# ─────────────────────────────────────────────────────────────────────────────
# _guess_test_target / _language_extensions — language-keyed lookups.
# ─────────────────────────────────────────────────────────────────────────────

def test_guess_test_target_byte_identical():
    assert _guess_test_target("test_auth.py", "python") == "auth"
    assert _guess_test_target("auth.py", "python") is None
    assert _guess_test_target("auth.test.ts", "typescript") == "auth"
    assert _guess_test_target("auth.spec.ts", "typescript") == "auth"
    assert _guess_test_target("auth.test.js", "javascript") == "auth"
    assert _guess_test_target("auth_test.go", "go") is None
    assert _guess_test_target("AuthTest.java", "java") is None


def test_language_extensions_byte_identical():
    assert _language_extensions("python") == {".py"}
    assert _language_extensions("typescript") == {".ts", ".tsx"}
    assert _language_extensions("javascript") == {".js", ".jsx"}
    assert _language_extensions("java") == {".java"}
    assert _language_extensions("go") == {".go"}
    assert _language_extensions("ruby") == set()


# ─────────────────────────────────────────────────────────────────────────────
# scanner._extract_imports_basic — the TS/JS relative-resolution ladder.
# ─────────────────────────────────────────────────────────────────────────────

def _import_edges(ceg):
    return [
        (e["source_id"], e["target_id"], e["relation"])
        for e in ceg.edges
        if e.get("relation") == "imports"
    ]


def test_scanner_extract_imports_basic_typescript(tmp_path):
    from codd.graph import CEG
    from codd.scanner import _extract_imports_basic

    src_dir = tmp_path / "src"
    _write(tmp_path, "src/foo.ts", "export const x = 1;")
    full = _write(tmp_path, "src/app.ts", 'import { Foo } from "./foo";\n')
    ceg = CEG(tmp_path / ".codd-scan")
    ceg.upsert_node("file:src/app.ts", "file", path="src/app.ts", name="app.ts")
    _extract_imports_basic(ceg, tmp_path, src_dir, full, "src/app.ts", "typescript")
    edges = _import_edges(ceg)
    ceg.close()
    assert ("file:src/app.ts", "file:src/foo.ts", "imports") in edges


def test_scanner_extract_imports_basic_python(tmp_path):
    from codd.graph import CEG
    from codd.scanner import _extract_imports_basic

    src_dir = tmp_path / "src"
    (src_dir / "db").mkdir(parents=True)
    (src_dir / "db" / "__init__.py").write_text("", encoding="utf-8")
    full = _write(tmp_path, "src/app.py", "from db import models\n")
    ceg = CEG(tmp_path / ".codd-scan")
    ceg.upsert_node("file:src/app.py", "file", path="src/app.py", name="app.py")
    _extract_imports_basic(ceg, tmp_path, src_dir, full, "src/app.py", "python")
    edges = _import_edges(ceg)
    ceg.close()
    assert ("file:src/app.py", "module:db", "imports") in edges
