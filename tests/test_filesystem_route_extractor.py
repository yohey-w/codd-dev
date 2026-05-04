"""Tests for configurable filesystem route extraction."""

from pathlib import Path

import pytest

from codd.parsing import FileSystemRouteExtractor, FilesystemRouteInfo


def _write_fixture_files(project_root: Path, file_paths: list[str]) -> None:
    for file_path in file_paths:
        target = project_root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// route fixture\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture_files", "route_config", "expected_routes"),
    [
        pytest.param(
            [
                "app/page.tsx",
                "app/(auth)/login/page.tsx",
                "app/central-admin/page.tsx",
                "app/central-admin/courses/page.tsx",
                "app/[id]/page.tsx",
                "app/api/health/route.ts",
            ],
            {
                "base_dir": "app/",
                "page_pattern": "page.{tsx,jsx}",
                "api_pattern": "route.{ts,js}",
                "url_template": "/{relative_dir}",
                "dynamic_segment": {"from": r"\[(.+)\]", "to": r":$1"},
                "ignore_segment": [r"\(.*\)"],
                "base_url": "",
            },
            {
                ("/", "page"),
                ("/login", "page"),
                ("/central-admin", "page"),
                ("/central-admin/courses", "page"),
                ("/:id", "page"),
                ("/api/health", "api"),
            },
            id="nextjs-app-router",
        ),
        pytest.param(
            [
                "src/routes/+page.svelte",
                "src/routes/about/+page.svelte",
                "src/routes/[slug]/+page.svelte",
                "src/routes/api/health/+server.ts",
            ],
            {
                "base_dir": "src/routes/",
                "page_pattern": "+page.svelte",
                "api_pattern": "+server.ts",
                "url_template": "/{relative_dir}",
                "dynamic_segment": {"from": r"\[(.+)\]", "to": r":$1"},
            },
            {
                ("/", "page"),
                ("/about", "page"),
                ("/:slug", "page"),
                ("/api/health", "api"),
            },
            id="sveltekit",
        ),
        pytest.param(
            [
                "pages/index.vue",
                "pages/users/index.vue",
                "pages/users/[id].vue",
            ],
            {
                "base_dir": "pages/",
                "page_pattern": "*.vue",
                "api_pattern": "",
                "url_template": "/{relative_dir}",
                "dynamic_segment": {"from": r"\[(.+)\]\.vue$", "to": r":$1"},
            },
            {
                ("/", "page"),
                ("/users", "page"),
                ("/users/:id", "page"),
            },
            id="nuxt3",
        ),
        pytest.param(
            [
                "src/pages/index.astro",
                "src/pages/blog/[...slug].astro",
                "src/pages/api/posts.ts",
            ],
            {
                "base_dir": "src/pages/",
                "page_pattern": "*.astro",
                "api_pattern": "api/*.ts",
                "url_template": "/{relative_dir}",
                "dynamic_segment": {"from": r"\[\.{3}(.+)\]\.astro$", "to": r":$1"},
            },
            {
                ("/", "page"),
                ("/blog/:slug", "page"),
                ("/api/posts", "api"),
            },
            id="astro",
        ),
        pytest.param(
            [
                "app/routes/_index.tsx",
                "app/routes/about.tsx",
                "app/routes/users.$id.tsx",
                "app/routes/api.health.ts",
            ],
            {
                "base_dir": "app/routes/",
                "page_pattern": "*.tsx",
                "api_pattern": "api.*.ts",
                "url_template": "/{relative_dir}",
                "dynamic_segment": {"from": r"^\$(.+)$", "to": r":$1"},
                "ignore_segment": [r"^_.*"],
            },
            {
                ("/", "page"),
                ("/about", "page"),
                ("/users/:id", "page"),
                ("/api/health", "api"),
            },
            id="remix",
        ),
    ],
)
def test_extracts_filesystem_routes_for_framework_conventions(
    tmp_path,
    fixture_files,
    route_config,
    expected_routes,
):
    _write_fixture_files(tmp_path, fixture_files)

    info = FileSystemRouteExtractor().extract_routes(tmp_path, [route_config])

    assert isinstance(info, FilesystemRouteInfo)
    assert {(route["url"], route["kind"]) for route in info.routes} == expected_routes
    assert len(info.routes) == len(expected_routes)
    assert all(Path(route["file"]).exists() for route in info.routes)
