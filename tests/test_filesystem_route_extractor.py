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


# --- path-escape jail (round-9) -------------------------------------------------
#
# ``base_dir`` comes from codd.yaml ``filesystem_routes[*].base_dir`` — a
# user-controllable config value. An absolute/``../``-escaping base_dir, or an
# in-root symlink whose target leaves the tree, must NOT let the extractor
# walk/read files from outside the project root. Out-of-root files must never
# become routes (a path-escape leak). In-root behaviour is unchanged (regression).


def _route_config(base_dir: str) -> dict:
    return {
        "base_dir": base_dir,
        "page_pattern": "page.tsx",
        "api_pattern": "route.ts",
        "url_template": "/{relative_dir}",
        "base_url": "",
    }


def test_filesystem_routes_absolute_base_dir_outside_root_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    _write_fixture_files(outside, ["secret/page.tsx"])

    info = FileSystemRouteExtractor().extract_routes(
        project_root, [_route_config(str(outside))]
    )

    assert info.routes == []


def test_filesystem_routes_parent_traversal_base_dir_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    _write_fixture_files(outside, ["secret/page.tsx"])

    info = FileSystemRouteExtractor().extract_routes(
        project_root, [_route_config("../outside")]
    )

    assert info.routes == []


def test_filesystem_routes_symlinked_base_dir_escaping_root_is_excluded(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    _write_fixture_files(outside, ["secret/page.tsx"])
    link = project_root / "linked_app"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        import pytest as _pytest

        _pytest.skip("symlinks unsupported on this platform")

    info = FileSystemRouteExtractor().extract_routes(
        project_root, [_route_config("linked_app")]
    )

    assert info.routes == []


def test_filesystem_routes_symlinked_file_escaping_root_is_excluded(tmp_path):
    """An in-root base_dir containing a symlink whose target leaves the tree must
    not yield that off-root file as a route, even though base_dir itself is in-root."""
    project_root = tmp_path / "project"
    app = project_root / "app"
    app.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "page.tsx"
    real.write_text("// off-root\n", encoding="utf-8")
    link = app / "page.tsx"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - platform guard
        import pytest as _pytest

        _pytest.skip("symlinks unsupported on this platform")

    info = FileSystemRouteExtractor().extract_routes(project_root, [_route_config("app/")])

    assert all(
        Path(route["file"]).resolve().is_relative_to(project_root.resolve())
        for route in info.routes
    )
    assert real.resolve().as_posix() not in {
        Path(route["file"]).resolve().as_posix() for route in info.routes
    }


def test_filesystem_routes_in_root_base_dir_unchanged_regression(tmp_path):
    """In-root base_dir keeps producing its routes (anti-false-red)."""
    _write_fixture_files(tmp_path, ["app/page.tsx", "app/api/health/route.ts"])

    info = FileSystemRouteExtractor().extract_routes(tmp_path, [_route_config("app/")])

    assert {(route["url"], route["kind"]) for route in info.routes} == {
        ("/", "page"),
        ("/api/health", "api"),
    }
