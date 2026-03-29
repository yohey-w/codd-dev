"""Tests for R9 — Inheritance chain analysis."""

import pytest

from codd.inheritance import (
    InheritanceEdge,
    build_inheritance_tree,
    get_overrides,
    get_inherited_methods,
)
from codd.extractor import Symbol, ModuleInfo, ProjectFacts


def _make_facts(modules_dict):
    """Helper to build ProjectFacts from a dict of module definitions."""
    facts = ProjectFacts(language="python", source_dirs=["."])
    for mod_name, symbols in modules_dict.items():
        mod = ModuleInfo(name=mod_name)
        mod.symbols = symbols
        mod.files = [f"{mod_name}/__init__.py"]
        facts.modules[mod_name] = mod
    return facts


# ── build_inheritance_tree ────────────────────────────────────


class TestBuildInheritanceTree:
    def test_resolves_parent_in_same_module(self):
        facts = _make_facts({
            "animals": [
                Symbol(name="Animal", kind="class", file="animals/base.py", line=1),
                Symbol(name="Dog", kind="class", file="animals/dog.py", line=1, bases=["Animal"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 1
        edge = facts.inheritance_edges[0]
        assert edge.child_class == "animals.Dog"
        assert edge.parent_class == "animals.Animal"

    def test_resolves_parent_across_modules(self):
        facts = _make_facts({
            "base": [
                Symbol(name="BaseHandler", kind="class", file="base/handler.py", line=5),
            ],
            "api": [
                Symbol(name="UserHandler", kind="class", file="api/users.py", line=10,
                       bases=["BaseHandler"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 1
        edge = facts.inheritance_edges[0]
        assert edge.child_class == "api.UserHandler"
        assert edge.parent_class == "base.BaseHandler"
        assert edge.child_module == "api"
        assert edge.parent_module == "base"

    def test_skips_builtin_bases(self):
        facts = _make_facts({
            "models": [
                Symbol(name="User", kind="class", file="models/user.py", line=1,
                       bases=["Model", "ABC"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 0

    def test_skips_unresolved_bases(self):
        facts = _make_facts({
            "api": [
                Symbol(name="MyView", kind="class", file="api/views.py", line=1,
                       bases=["ThirdPartyView"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 0

    def test_no_self_loop(self):
        facts = _make_facts({
            "core": [
                Symbol(name="Singleton", kind="class", file="core/single.py", line=1,
                       bases=["Singleton"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 0

    def test_multiple_parents(self):
        facts = _make_facts({
            "mixins": [
                Symbol(name="LogMixin", kind="class", file="mixins/log.py", line=1),
                Symbol(name="CacheMixin", kind="class", file="mixins/cache.py", line=5),
            ],
            "service": [
                Symbol(name="UserService", kind="class", file="service/users.py", line=1,
                       bases=["LogMixin", "CacheMixin"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 2
        parents = {e.parent_class for e in facts.inheritance_edges}
        assert parents == {"mixins.LogMixin", "mixins.CacheMixin"}

    def test_ignores_non_class_symbols(self):
        facts = _make_facts({
            "utils": [
                Symbol(name="helper", kind="function", file="utils/h.py", line=1, bases=["something"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 0

    def test_qualified_base_resolution(self):
        facts = _make_facts({
            "core": [
                Symbol(name="Engine", kind="class", file="core/engine.py", line=1),
            ],
            "ext": [
                Symbol(name="FastEngine", kind="class", file="ext/fast.py", line=1,
                       bases=["core.Engine"]),
            ],
        })
        build_inheritance_tree(facts)
        assert len(facts.inheritance_edges) == 1
        assert facts.inheritance_edges[0].parent_class == "core.Engine"


# ── get_overrides ─────────────────────────────────────────────


class TestGetOverrides:
    def test_detects_overridden_method(self):
        facts = _make_facts({
            "base": [
                Symbol(name="Handler", kind="class", file="base/b.py", line=1),
                Symbol(name="Handler.run", kind="function", file="base/b.py", line=5),
                Symbol(name="Handler.stop", kind="function", file="base/b.py", line=10),
            ],
            "impl": [
                Symbol(name="Impl", kind="class", file="impl/i.py", line=1, bases=["Handler"]),
                Symbol(name="Impl.run", kind="function", file="impl/i.py", line=5),
            ],
        })
        build_inheritance_tree(facts)
        overrides = get_overrides(facts)
        assert "impl.Impl" in overrides
        assert "run" in overrides["impl.Impl"]
        assert "stop" not in overrides["impl.Impl"]

    def test_no_overrides(self):
        facts = _make_facts({
            "base": [
                Symbol(name="Handler", kind="class", file="base/b.py", line=1),
                Symbol(name="Handler.run", kind="function", file="base/b.py", line=5),
            ],
            "impl": [
                Symbol(name="Impl", kind="class", file="impl/i.py", line=1, bases=["Handler"]),
                Symbol(name="Impl.other", kind="function", file="impl/i.py", line=5),
            ],
        })
        build_inheritance_tree(facts)
        overrides = get_overrides(facts)
        assert overrides.get("impl.Impl") is None


# ── get_inherited_methods ─────────────────────────────────────


class TestGetInheritedMethods:
    def test_detects_inherited_methods(self):
        facts = _make_facts({
            "base": [
                Symbol(name="Handler", kind="class", file="base/b.py", line=1),
                Symbol(name="Handler.run", kind="function", file="base/b.py", line=5),
                Symbol(name="Handler.stop", kind="function", file="base/b.py", line=10),
            ],
            "impl": [
                Symbol(name="Impl", kind="class", file="impl/i.py", line=1, bases=["Handler"]),
                Symbol(name="Impl.run", kind="function", file="impl/i.py", line=5),
            ],
        })
        build_inheritance_tree(facts)
        inherited = get_inherited_methods(facts)
        assert "impl.Impl" in inherited
        assert "stop" in inherited["impl.Impl"]
        assert "run" not in inherited["impl.Impl"]

    def test_no_inherited_when_all_overridden(self):
        facts = _make_facts({
            "base": [
                Symbol(name="Handler", kind="class", file="base/b.py", line=1),
                Symbol(name="Handler.run", kind="function", file="base/b.py", line=5),
            ],
            "impl": [
                Symbol(name="Impl", kind="class", file="impl/i.py", line=1, bases=["Handler"]),
                Symbol(name="Impl.run", kind="function", file="impl/i.py", line=5),
            ],
        })
        build_inheritance_tree(facts)
        inherited = get_inherited_methods(facts)
        assert inherited.get("impl.Impl") is None


# ── synth.py integration: inherits relation ───────────────────


class TestInheritsRelation:
    def test_module_depends_on_includes_inherits(self):
        from codd.synth import _module_depends_on

        facts = _make_facts({
            "base": [
                Symbol(name="Handler", kind="class", file="base/b.py", line=1),
            ],
            "child": [
                Symbol(name="Child", kind="class", file="child/c.py", line=1, bases=["Handler"]),
            ],
        })
        build_inheritance_tree(facts)
        depends = _module_depends_on(facts, facts.modules["child"])
        inherits_deps = [d for d in depends if d["relation"] == "inherits"]
        assert len(inherits_deps) == 1
        assert "base" in inherits_deps[0]["id"]
