"""Project-level framework wiring seam: resolve_project_stack / verify_project_stack.

The framework layer is OPT-IN: a project with no ``stack:`` block in its codd.yaml
is completely unaffected (both functions return None). A project that declares a
stack resolves to a contract and has its obligations enforced.
"""
from __future__ import annotations

from codd.stack.project import resolve_project_stack, verify_project_stack

_STACK_YAML = (
    "stack:\n"
    "  language: typescript\n"
    "  frameworks: [nextjs]\n"
    "  addons: [prisma, playwright]\n"
)


def _make_project(tmp_path, *, stack: bool, next_config: str | None = None):
    codd_dir = tmp_path / "codd"
    codd_dir.mkdir()
    (codd_dir / "codd.yaml").write_text(_STACK_YAML if stack else "project_name: t\n")
    if next_config is not None:
        (tmp_path / "next.config.js").write_text(next_config)
    return tmp_path


def test_no_stack_block_is_noop(tmp_path):
    proj = _make_project(tmp_path, stack=False)
    assert resolve_project_stack(proj) is None
    assert verify_project_stack(proj) is None


def test_missing_config_is_noop(tmp_path):
    # No codd.yaml at all -> None, never an error.
    assert resolve_project_stack(tmp_path) is None
    assert verify_project_stack(tmp_path) is None


def test_stack_block_resolves_to_contract(tmp_path):
    proj = _make_project(tmp_path, stack=True)
    contract = resolve_project_stack(proj)
    assert contract is not None
    assert contract.stack_id == "typescript+nextjs+prisma+playwright"
    assert contract.is_clean


def test_verify_project_stack_blocks_on_ignore_build_errors(tmp_path):
    proj = _make_project(
        tmp_path,
        stack=True,
        next_config="module.exports = { typescript: { ignoreBuildErrors: true } };\n",
    )
    result = verify_project_stack(proj, report_data={"stats": {"expected": 3}})
    assert result is not None
    assert not result.passed
    assert any(
        v.obligation.id == "no_ignore_build_errors_as_typecheck"
        for v in result.blocking_violations
    )


def test_verify_project_stack_passes_clean(tmp_path):
    proj = _make_project(
        tmp_path,
        stack=True,
        next_config="module.exports = { typescript: { ignoreBuildErrors: false } };\n",
    )
    result = verify_project_stack(proj, report_data={"stats": {"expected": 3, "unexpected": 0}})
    assert result is not None
    assert result.passed
