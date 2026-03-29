"""Tests for R4.3 — Interface contract detection."""

import textwrap
from pathlib import Path

import pytest

from codd.contracts import InterfaceContract, build_interface_contracts, detect_init_exports
from codd.extractor import ModuleInfo, ProjectFacts, Symbol


def test_detect_init_exports_all():
    content = "__all__ = ['Foo', 'Bar', 'baz']"
    assert detect_init_exports(content) == ["Foo", "Bar", "baz"]


def test_detect_init_exports_reexports():
    content = textwrap.dedent("""\
        from .models import User, Post
        from .service import AuthService as Auth
    """)
    result = detect_init_exports(content)
    assert "User" in result
    assert "Post" in result
    assert "Auth" in result


def test_detect_init_exports_all_takes_priority():
    content = textwrap.dedent("""\
        __all__ = ['Foo']
        from .models import User, Post
    """)
    assert detect_init_exports(content) == ["Foo"]


def test_detect_init_exports_empty():
    assert detect_init_exports("") == []
    assert detect_init_exports("# just a comment") == []


def test_build_interface_contracts_with_init(tmp_path):
    """Module with __init__.py splits public/internal correctly."""
    init_dir = tmp_path / "src" / "auth"
    init_dir.mkdir(parents=True)
    (init_dir / "__init__.py").write_text("__all__ = ['AuthService']")

    facts = ProjectFacts(language="python", source_dirs=["src"])
    mod = ModuleInfo(name="auth")
    mod.files = ["src/auth/__init__.py", "src/auth/service.py"]
    mod.symbols = [
        Symbol(name="AuthService", kind="class", file="src/auth/service.py", line=1),
        Symbol(name="_hash_pw", kind="function", file="src/auth/service.py", line=10),
    ]
    facts.modules["auth"] = mod

    build_interface_contracts(facts, tmp_path)

    ic = mod.interface_contract
    assert ic is not None
    assert ic.public_symbols == ["AuthService"]
    assert ic.internal_symbols == ["_hash_pw"]
    assert ic.api_surface_ratio == 0.5


def test_build_interface_contracts_no_init():
    """Module without __init__.py treats all symbols as public."""
    facts = ProjectFacts(language="python", source_dirs=["src"])
    mod = ModuleInfo(name="utils")
    mod.files = ["src/utils/helpers.py"]
    mod.symbols = [
        Symbol(name="foo", kind="function", file="src/utils/helpers.py", line=1),
        Symbol(name="bar", kind="function", file="src/utils/helpers.py", line=5),
    ]
    facts.modules["utils"] = mod

    build_interface_contracts(facts, Path("/fake"))

    ic = mod.interface_contract
    assert ic is not None
    assert sorted(ic.public_symbols) == ["bar", "foo"]
    assert ic.internal_symbols == []
    assert ic.api_surface_ratio == 1.0


def test_encapsulation_violations(tmp_path):
    """Detect when module A uses internal symbols from module B."""
    init_dir = tmp_path / "src" / "core"
    init_dir.mkdir(parents=True)
    (init_dir / "__init__.py").write_text("__all__ = ['Engine']")

    facts = ProjectFacts(language="python", source_dirs=["src"])
    core = ModuleInfo(name="core")
    core.files = ["src/core/__init__.py", "src/core/engine.py"]
    core.symbols = [
        Symbol(name="Engine", kind="class", file="src/core/engine.py", line=1),
        Symbol(name="_internal_fn", kind="function", file="src/core/engine.py", line=20),
    ]
    facts.modules["core"] = core

    consumer = ModuleInfo(name="api")
    consumer.files = ["src/api/handler.py"]
    consumer.symbols = [
        Symbol(name="handler", kind="function", file="src/api/handler.py", line=1),
    ]
    consumer.internal_imports = {
        "core": ["from core.engine import _internal_fn"],
    }
    facts.modules["api"] = consumer

    build_interface_contracts(facts, tmp_path)

    ic = consumer.interface_contract
    assert ic is not None
    assert len(ic.encapsulation_violations) == 1
    assert "_internal_fn" in ic.encapsulation_violations[0]
