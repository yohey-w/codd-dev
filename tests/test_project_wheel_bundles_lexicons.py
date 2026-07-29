"""Regression coverage for built-wheel lexicon payloads."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "codd_plugins"
LEXICON_ROOT = PLUGIN_ROOT / "lexicons"


def _relative_paths(paths: list[Path]) -> set[str]:
    return {path.relative_to(REPO_ROOT).as_posix() for path in paths}


def test_built_wheel_bundles_every_builtin_lexicon_file(tmp_path: Path) -> None:
    """The distributable wheel must retain the top-level plug-in data tree."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path),
            str(REPO_ROOT),
        ],
        check=True,
        cwd=REPO_ROOT,
    )

    source_manifests = _relative_paths(sorted(LEXICON_ROOT.rglob("manifest.yaml")))
    api_rest_openapi_files = _relative_paths(
        sorted(path for path in (LEXICON_ROOT / "api_rest_openapi").rglob("*") if path.is_file())
    )
    stack_map = "codd_plugins/stack_map.yaml"

    assert len(source_manifests) == 39
    assert len(api_rest_openapi_files) == 6
    assert f"codd_plugins/lexicons/api_rest_openapi/manifest.yaml" in source_manifests
    assert (REPO_ROOT / stack_map).is_file()

    wheels = list(tmp_path.glob("codd_dev-3.37.0-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_files = set(wheel.namelist())

    wheel_manifests = {
        path
        for path in wheel_files
        if path.startswith("codd_plugins/lexicons/") and path.endswith("/manifest.yaml")
    }
    assert wheel_manifests == source_manifests
    assert len(wheel_manifests) == 39
    assert api_rest_openapi_files <= wheel_files
    assert stack_map in wheel_files
