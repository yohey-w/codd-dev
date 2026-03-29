"""Tests for R4.2 — Feature clustering."""

from codd.clustering import (
    _common_prefix,
    _connected_components,
    _group_by_prefix,
    _resolve_callee_module,
    build_feature_clusters,
)
from codd.extractor import CallEdge, FeatureCluster, ModuleInfo, ProjectFacts, Symbol


def test_resolve_callee_module_exact():
    modules = ["auth", "api", "db"]
    assert _resolve_callee_module("auth", modules) == "auth"


def test_resolve_callee_module_dotted():
    modules = ["auth", "api", "db"]
    assert _resolve_callee_module("auth.verify_token", modules) == "auth"


def test_resolve_callee_module_unknown():
    modules = ["auth", "api"]
    assert _resolve_callee_module("unknown.func", modules) is None


def test_connected_components():
    nodes = ["a", "b", "c", "d"]
    adj = {"a": {"b"}, "b": {"a"}}
    comps = _connected_components(nodes, adj)
    # a-b connected, c and d isolated
    sizes = sorted(len(c) for c in comps)
    assert sizes == [1, 1, 2]


def test_group_by_prefix():
    names = ["auth_service", "auth_middleware", "db_models", "db_session", "utils"]
    groups = _group_by_prefix(names)
    assert "auth" in groups
    assert "db" in groups
    assert len(groups["auth"]) == 2
    assert "utils" not in groups  # only 1 member


def test_common_prefix():
    assert _common_prefix({"auth_service", "auth_middleware"}) == "auth"
    assert _common_prefix({"foo", "bar"}) == ""


def test_build_feature_clusters_by_calls():
    facts = ProjectFacts(language="python", source_dirs=["src"])
    a = ModuleInfo(name="a")
    a.symbols = [Symbol(name="fn_a", kind="function", file="a.py", line=1)]
    a.call_edges = [CallEdge(caller="fn_a", callee="b.fn_b", call_site="a.py:2")]
    facts.modules["a"] = a

    b = ModuleInfo(name="b")
    b.symbols = [Symbol(name="fn_b", kind="function", file="b.py", line=1)]
    b.call_edges = [CallEdge(caller="fn_b", callee="a.fn_a", call_site="b.py:2")]
    facts.modules["b"] = b

    c = ModuleInfo(name="c")
    c.symbols = [Symbol(name="fn_c", kind="function", file="c.py", line=1)]
    facts.modules["c"] = c

    build_feature_clusters(facts)

    # a and b should be clustered (mutual calls)
    assert len(facts.feature_clusters) >= 1
    cluster_with_ab = [cl for cl in facts.feature_clusters if "a" in cl.modules and "b" in cl.modules]
    assert len(cluster_with_ab) == 1


def test_build_feature_clusters_by_prefix():
    facts = ProjectFacts(language="python", source_dirs=["src"])
    for name in ["auth_login", "auth_register", "db_models"]:
        mod = ModuleInfo(name=name)
        mod.symbols = [Symbol(name=f"fn_{name}", kind="function", file=f"{name}.py", line=1)]
        facts.modules[name] = mod

    build_feature_clusters(facts)

    auth_clusters = [cl for cl in facts.feature_clusters if "auth_login" in cl.modules]
    assert len(auth_clusters) == 1
    assert "auth_register" in auth_clusters[0].modules


def test_build_feature_clusters_single_module():
    """Single module should produce no clusters."""
    facts = ProjectFacts(language="python", source_dirs=["src"])
    mod = ModuleInfo(name="solo")
    mod.symbols = [Symbol(name="fn", kind="function", file="solo.py", line=1)]
    facts.modules["solo"] = mod

    build_feature_clusters(facts)
    assert facts.feature_clusters == []
