from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from codd.repair.git_patcher import GitPatcher
from codd.repair.llm_repair_engine import LlmRepairEngine, RepairFailed
from codd.repair.schema import FilePatch, RootCauseAnalysis


FORBIDDEN_TERMS = (
    "Cookie",
    "NextAuth",
    "OAuth",
    "Safari",
    "iPhone",
    "React",
    "LMS",
    "osato",
    "NextJS",
    "PowerShell",
)


class FakeAiCommand:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("unexpected extra AI invocation")
        return self.responses.pop(0)


def _rca(strategy: str = "unified_diff") -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="implementation does not match the expected behavior",
        affected_nodes=["sample.txt"],
        repair_strategy=strategy,  # type: ignore[arg-type]
        confidence=0.8,
        analysis_timestamp="2026-05-07T00:00:01Z",
    )


def _init_repo(root: Path) -> Path:
    (root / "sample.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def _valid_diff(old: str = "one", new: str = "two") -> str:
    return (
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def test_repair_strategy_retry_template_is_domain_neutral():
    content = Path("codd/repair/templates/repair_strategy_meta.md").read_text(encoding="utf-8")

    assert not any(term in content for term in FORBIDDEN_TERMS)


def test_validate_failure_retries_and_accepts_full_file_replacement(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}',
        '{"patch_mode":"full_file_replacement",'
        '"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace entire file after validation feedback","confidence":0.8}',
    )

    proposal = LlmRepairEngine(project_root=root, ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "full_file_replacement"
    assert proposal.patches[0].content == "two\n"
    assert len(ai.prompts) == 2
    assert "The following patch failed validation" in ai.prompts[1]
    assert "Options:" in ai.prompts[1]
    assert (root / "sample.txt").read_text(encoding="utf-8") == "one\n"


def test_validate_failure_retries_and_accepts_corrected_unified_diff(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}',
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":'
        + repr(_valid_diff()).replace("'", '"')
        + '}],"rationale":"correct context","confidence":0.8}',
    )

    proposal = LlmRepairEngine(project_root=root, ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "unified_diff"
    assert proposal.patches[0].content == _valid_diff()
    assert len(ai.prompts) == 2


def test_no_patch_top_level_selection_is_unrepairable():
    ai = FakeAiCommand('{"patch_mode":"no-patch","patches":[],"rationale":"no safe patch","confidence":0.6}')

    with pytest.raises(RepairFailed, match="no-patch"):
        LlmRepairEngine(ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})


def test_no_patch_patch_entry_selection_is_unrepairable():
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"no-patch","content":""}],'
        '"rationale":"no safe patch","confidence":0.6}'
    )

    with pytest.raises(RepairFailed, match="no-patch"):
        LlmRepairEngine(ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})


def test_max_strategy_attempts_one_does_not_retry(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}'
    )

    with pytest.raises(RepairFailed, match="patch validation"):
        LlmRepairEngine(project_root=root, ai_command=ai, max_strategy_attempts=1).propose_fix(
            _rca(),
            {"sample.txt": "one\n"},
        )

    assert len(ai.prompts) == 1


def test_configured_max_strategy_attempts_allows_third_strategy(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}',
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"still not a diff"}],'
        '"rationale":"bad retry","confidence":0.8}',
        '{"patch_mode":"full_file_replacement",'
        '"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace after repeated validation feedback","confidence":0.8}',
    )

    proposal = LlmRepairEngine(
        project_root=root,
        config={"repair": {"max_strategy_attempts": 3}},
        ai_command=ai,
    ).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "full_file_replacement"
    assert len(ai.prompts) == 3


def test_invalid_max_strategy_attempts_falls_back_to_default(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}',
        '{"patch_mode":"full_file_replacement",'
        '"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace after validation feedback","confidence":0.8}',
    )

    proposal = LlmRepairEngine(
        project_root=root,
        config={"repair": {"max_strategy_attempts": "invalid"}},
        ai_command=ai,
    ).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "full_file_replacement"
    assert len(ai.prompts) == 2


def test_project_root_absent_skips_patch_validation():
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}'
    )

    proposal = LlmRepairEngine(ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].content == "not a diff"
    assert len(ai.prompts) == 1


def test_validate_result_exposes_generic_patch_error(tmp_path):
    root = _init_repo(tmp_path)
    result = GitPatcher().validate_result(FilePatch("sample.txt", "unified_diff", "not a diff"), root)

    assert not result.success
    assert result.failed_patches == ["sample.txt"]
    assert result.error_message


def test_retry_prompt_does_not_introduce_stack_specific_terms(tmp_path):
    root = _init_repo(tmp_path)
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"not a diff"}],'
        '"rationale":"bad patch","confidence":0.8}',
        '{"patch_mode":"full_file_replacement",'
        '"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace after validation feedback","confidence":0.8}',
    )

    LlmRepairEngine(project_root=root, ai_command=ai).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert not any(term in ai.prompts[1] for term in FORBIDDEN_TERMS)
