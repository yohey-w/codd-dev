"""Sidecar declarations for verification tests (cmd_466 #1 + #2).

The sidecar file is ``<test_path>.codd.yaml`` next to the test source. It
opts the test into explicit DAG metadata that the test source language
itself can't carry (TypeScript .spec.ts has no markdown frontmatter).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.deployment import EDGE_VERIFIED_BY, VerificationKind
from codd.deployment.extractor import (
    _add_verification_test,
    infer_deployment_edges,
)


def _make_test_file(path: Path, *, sidecar: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import { test } from '@playwright/test';\n"
        "test('smoke', async () => {});\n",
        encoding="utf-8",
    )
    if sidecar is not None:
        sidecar_path = path.with_suffix(path.suffix + ".codd.yaml")
        sidecar_path.write_text(
            yaml.safe_dump(sidecar, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def test_sidecar_verified_by_attaches_to_verification_node(tmp_path: Path) -> None:
    test_path = tmp_path / "tests/smoke/build_check.test.ts"
    _make_test_file(
        test_path,
        sidecar={
            "verified_by": ["runtime:file_present:build_artifact"],
        },
    )

    tests: dict = {}
    _add_verification_test(tests, tmp_path, test_path, VerificationKind.SMOKE)

    node = next(iter(tests.values()))
    assert node.verified_by == ["runtime:file_present:build_artifact"]


def test_sidecar_axis_matrix_attaches_to_verification_node(tmp_path: Path) -> None:
    test_path = tmp_path / "tests/e2e/coverage.spec.ts"
    _make_test_file(
        test_path,
        sidecar={
            "axis_matrix": [
                {
                    "journey": "tenant_admin_course_creation",
                    "axis_type": "viewport",
                    "variant_id": "smartphone_se",
                },
            ],
        },
    )

    tests: dict = {}
    _add_verification_test(tests, tmp_path, test_path, VerificationKind.E2E)

    node = next(iter(tests.values()))
    assert node.axis_matrix == [
        {
            "journey": "tenant_admin_course_creation",
            "axis_type": "viewport",
            "variant_id": "smartphone_se",
        }
    ]


def test_sidecar_missing_falls_back_to_empty_declarations(tmp_path: Path) -> None:
    test_path = tmp_path / "tests/smoke/no_sidecar.test.ts"
    _make_test_file(test_path)

    tests: dict = {}
    _add_verification_test(tests, tmp_path, test_path, VerificationKind.SMOKE)
    node = next(iter(tests.values()))
    assert node.verified_by == []
    assert node.axis_matrix == []


def test_sidecar_invalid_yaml_falls_back_to_empty(tmp_path: Path) -> None:
    test_path = tmp_path / "tests/smoke/broken.test.ts"
    _make_test_file(test_path)
    sidecar_path = test_path.with_suffix(test_path.suffix + ".codd.yaml")
    sidecar_path.write_text("not: [valid", encoding="utf-8")

    tests: dict = {}
    _add_verification_test(tests, tmp_path, test_path, VerificationKind.SMOKE)
    node = next(iter(tests.values()))
    assert node.verified_by == []


def test_infer_deployment_edges_uses_sidecar_verified_by(tmp_path: Path) -> None:
    from codd.deployment import RuntimeStateKind, RuntimeStateNode

    test_path = tmp_path / "tests/smoke/build_check.test.ts"
    _make_test_file(
        test_path,
        sidecar={"verified_by": ["runtime:file_present:build_artifact"]},
    )
    tests: dict = {}
    _add_verification_test(tests, tmp_path, test_path, VerificationKind.SMOKE)
    verification_tests = list(tests.values())

    runtime_state = RuntimeStateNode(
        identifier="runtime:file_present:build_artifact",
        kind=RuntimeStateKind.FILE_PRESENT,
        target="build_artifact",
    )

    edges = infer_deployment_edges(
        project_root=tmp_path,
        deployment_docs=[],
        runtime_states=[runtime_state],
        verification_tests=verification_tests,
        impl_files=[],
    )

    verified_by_edges = [edge for edge in edges if edge[2] == EDGE_VERIFIED_BY]
    assert any(
        from_id == runtime_state.identifier
        and to_id == verification_tests[0].identifier
        and attrs.get("source") == "sidecar_declaration"
        for from_id, to_id, _, attrs in verified_by_edges
    )
