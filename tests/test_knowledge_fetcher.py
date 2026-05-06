from datetime import datetime, timedelta, timezone
UTC = timezone.utc
import json
import re

from codd.knowledge_fetcher import (
    KnowledgeEntry,
    KnowledgeFetcher,
    _load_cache,
    _project_has_auth_ui,
    _save_cache,
    _slugify,
    suggest_ux_required_routes,
)


def test_fetch_returns_fresh_cache_without_search(tmp_path, monkeypatch):
    fetcher = KnowledgeFetcher(tmp_path)
    cached = KnowledgeEntry(
        query="Next.js route conventions",
        result="cached answer",
        sources=["https://nextjs.org/docs"],
        confidence=0.9,
    )
    _save_cache(fetcher._cache_dir / f"{_slugify(cached.query)}.json", cached)

    def fail_search(query):
        raise AssertionError(f"unexpected search for {query}")

    monkeypatch.setattr(fetcher, "_search_web", fail_search)

    assert fetcher.fetch(cached.query) == cached


def test_fetch_miss_and_force_refresh_call_search(tmp_path, monkeypatch):
    fetcher = KnowledgeFetcher(tmp_path)
    calls = []

    def fake_search(query):
        calls.append(query)
        return KnowledgeEntry(query=query, result=f"fresh: {query}", confidence=0.7)

    monkeypatch.setattr(fetcher, "_search_web", fake_search)

    first = fetcher.fetch("React suspense")
    assert first.result == "fresh: React suspense"
    assert calls == ["React suspense"]

    second = fetcher.fetch("React suspense", force_refresh=True)
    assert second.result == "fresh: React suspense"
    assert calls == ["React suspense", "React suspense"]


def test_knowledge_entry_is_stale_when_ttl_exceeded():
    entry = KnowledgeEntry(
        query="old",
        result="old",
        fetched_at=_utc_iso(datetime.now(UTC) - timedelta(days=31)),
    )

    assert entry.is_stale(ttl_days=30)


def test_knowledge_entry_is_not_stale_when_fresh():
    entry = KnowledgeEntry(
        query="fresh",
        result="fresh",
        fetched_at=_utc_iso(datetime.now(UTC) - timedelta(days=1)),
    )

    assert not entry.is_stale(ttl_days=30)


def test_detect_tech_stack_detects_python_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

    assert "Python" in KnowledgeFetcher(tmp_path).detect_tech_stack()


def test_slugify_converts_special_characters_to_safe_cache_key():
    slug = _slugify("Next.js routing: app/[id]? useEffect!")

    assert re.fullmatch(r"[A-Za-z0-9_-]+", slug)
    assert " " not in slug
    assert ":" not in slug
    assert len(slug) <= 64


def test_cache_save_load_roundtrip_preserves_entry(tmp_path):
    entry = KnowledgeEntry(
        query="Django URL routing",
        result="Use path() and include().",
        sources=["https://docs.djangoproject.com/"],
        confidence=0.85,
        provenance="official_doc",
    )
    cache_file = tmp_path / "cache.json"

    _save_cache(cache_file, entry)

    assert _load_cache(cache_file) == entry


def _utc_iso(value):
    return value.isoformat().replace("+00:00", "Z")


def test_project_has_auth_ui_with_next_auth_package(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next-auth": "^5.0.0"}}),
        encoding="utf-8",
    )

    assert _project_has_auth_ui(tmp_path, "React")


def test_project_has_auth_ui_with_auth_dir(tmp_path):
    (tmp_path / "src" / "auth").mkdir(parents=True)

    assert _project_has_auth_ui(tmp_path, None)


def test_project_has_auth_ui_no_auth(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "^19.0.0"}}),
        encoding="utf-8",
    )

    assert not _project_has_auth_ui(tmp_path, "React")


def test_suggest_ux_routes_with_auth():
    routes = suggest_ux_required_routes("React", has_auth_ui=True)

    assert routes == {"root": "/", "signin": "/login"}


def test_suggest_ux_routes_no_auth():
    routes = suggest_ux_required_routes("React", has_auth_ui=False)

    assert routes == {"root": "/"}


def test_suggest_ux_routes_returns_generics():
    routes = suggest_ux_required_routes("React", has_auth_ui=True)
    rendered = " ".join(routes.values()).lower()

    assert "next" not in rendered
    assert "clerk" not in rendered
    assert "supabase" not in rendered


def test_codd_yaml_ux_override_respected(tmp_path):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(
        "ux:\n"
        "  required_routes:\n"
        "    signin: /sign-in\n"
        "    root: /\n"
        "    signup: /sign-up\n",
        encoding="utf-8",
    )

    routes = KnowledgeFetcher(tmp_path).suggest_ux_required_routes("React")

    assert routes == {"signin": "/sign-in", "root": "/", "signup": "/sign-up"}
