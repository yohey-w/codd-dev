from __future__ import annotations

import subprocess

import pytest

from codd.dag import DAG, Edge, Node
from codd.repair.engine import get_repair_engine
from codd.repair.git_patcher import GitPatcher
from codd.repair.llm_repair_engine import LlmRepairEngine, RepairFailed, resolve_repair_ai_command
from codd.repair.schema import FilePatch, RepairProposal, RootCauseAnalysis, VerificationFailureReport


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
        return self.responses.pop(0)


def _failure() -> VerificationFailureReport:
    return VerificationFailureReport(
        check_name="node_completeness",
        failed_nodes=["design:login"],
        error_messages=["implementation node missing"],
        dag_snapshot={"nodes": [{"id": "design:login"}], "edges": []},
        timestamp="2026-05-06T00:00:00Z",
    )


def _dag() -> DAG:
    dag = DAG()
    dag.add_node(Node("design:login", "design_doc", "docs/design.md", {"capability": "sign_in"}))
    dag.add_node(Node("impl:login", "impl_file", "src/login.py", {}))
    dag.add_edge(Edge("design:login", "impl:login", "expects"))
    return dag


def _rca(strategy: str = "unified_diff") -> RootCauseAnalysis:
    return RootCauseAnalysis(
        probable_cause="implementation does not match the expected behavior",
        affected_nodes=["impl:login"],
        repair_strategy=strategy,  # type: ignore[arg-type]
        confidence=0.8,
        analysis_timestamp="2026-05-06T00:00:01Z",
    )


def _proposal(*patches: FilePatch) -> RepairProposal:
    return RepairProposal(
        list(patches),
        "align file content",
        0.8,
        "2026-05-06T00:00:02Z",
        "2026-05-06T00:00:01Z",
    )


def _valid_diff(old: str = "one", new: str = "two") -> str:
    return (
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _init_repo(tmp_path):
    root = tmp_path
    (root / "sample.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "add", "sample.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def test_llm_repair_engine_registers_default_engine():
    assert get_repair_engine("llm") is LlmRepairEngine


@pytest.mark.parametrize("template_name", ["analyze_meta.md", "propose_meta.md"])
def test_repair_templates_are_domain_neutral(template_name):
    text = (LlmRepairEngine.__module__.replace(".", "/"), template_name)
    path = __import__("pathlib").Path("codd/repair/templates") / text[1]
    content = path.read_text(encoding="utf-8")

    assert not any(term in content for term in FORBIDDEN_TERMS)


def test_analyze_returns_root_cause_from_mock_ai_command():
    ai = FakeAiCommand(
        '{"probable_cause":"missing artifact","affected_nodes":["impl:login"],'
        '"repair_strategy":"unified_diff","confidence":0.7}'
    )
    engine = LlmRepairEngine(ai_command={"repair_analyze": ai})

    rca = engine.analyze(_failure(), _dag())

    assert rca.probable_cause == "missing artifact"
    assert rca.affected_nodes == ["impl:login"]
    assert rca.repair_strategy == "unified_diff"
    assert rca.analysis_timestamp


def test_analyze_accepts_markdown_json_fence():
    ai = FakeAiCommand(
        '```json\n{"probable_cause":"drift","affected_nodes":[],"repair_strategy":"unified_diff","confidence":0.5}\n```'
    )

    assert LlmRepairEngine(ai_command=ai).analyze(_failure(), _dag()).probable_cause == "drift"


def test_analyze_invalid_json_logs_warning_and_raises(caplog):
    ai = FakeAiCommand("not json")
    engine = LlmRepairEngine(ai_command=ai)

    with caplog.at_level("WARNING"), pytest.raises(RepairFailed):
        engine.analyze(_failure(), _dag())

    assert "not valid JSON" in caplog.text


def test_propose_fix_returns_unified_diff_proposal_from_mock_ai_command():
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"unified_diff","content":"diff --git a/sample.txt b/sample.txt"}],'
        '"rationale":"small patch","confidence":0.8}'
    )
    proposal = LlmRepairEngine(ai_command={"repair_propose": ai}).propose_fix(_rca(), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "unified_diff"
    assert proposal.patches[0].file_path == "sample.txt"
    assert proposal.rca_reference == _rca().analysis_timestamp


def test_propose_fix_returns_full_file_replacement_proposal():
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace file","confidence":0.8}'
    )
    proposal = LlmRepairEngine(ai_command=ai).propose_fix(_rca("full_file_replacement"), {"sample.txt": "one\n"})

    assert proposal.patches[0].patch_mode == "full_file_replacement"
    assert proposal.patches[0].content == "two\n"


def test_propose_fix_validates_unified_diff_when_project_root_exists(tmp_path):
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


def test_ai_command_resolves_named_repair_command_before_default():
    config = {"ai_commands": {"repair_analyze": "ai analyze"}, "ai_command": "ai default"}

    assert resolve_repair_ai_command(config, "repair_analyze") == "ai analyze"


def test_ai_command_resolves_project_default_before_environment(monkeypatch):
    monkeypatch.setenv("CODD_AI_COMMAND", "ai env")

    assert resolve_repair_ai_command({"ai_command": "ai project"}, "repair_propose") == "ai project"


def test_ai_command_resolves_environment_when_project_has_no_command(monkeypatch):
    monkeypatch.setenv("CODD_AI_COMMAND", "ai env")

    assert resolve_repair_ai_command({}, "repair_propose") == "ai env"


def test_ai_command_missing_raises_repair_failed(monkeypatch):
    monkeypatch.delenv("CODD_AI_COMMAND", raising=False)

    with pytest.raises(RepairFailed, match="not configured"):
        resolve_repair_ai_command({}, "repair_analyze")


def test_analyze_prompt_replaces_failure_dag_and_project_placeholders(tmp_path):
    (tmp_path / "context.md").write_text("Project invariant: keep artifacts aligned.", encoding="utf-8")
    ai = FakeAiCommand(
        '{"probable_cause":"gap","affected_nodes":["design:login"],"repair_strategy":"unified_diff","confidence":0.6}'
    )
    engine = LlmRepairEngine(
        project_root=tmp_path,
        config={"repair": {"context_path": "context.md"}},
        ai_command=ai,
    )

    engine.analyze(_failure(), _dag())
    prompt = ai.prompts[0]

    assert "{failure_report}" not in prompt
    assert "{dag_context}" not in prompt
    assert "{project_context}" not in prompt
    assert "node_completeness" in prompt
    assert "design:login" in prompt
    assert "Project invariant" in prompt


def test_propose_prompt_replaces_rca_file_and_context_placeholders(tmp_path):
    (tmp_path / "context.md").write_text("Project invariant: minimal edits.", encoding="utf-8")
    ai = FakeAiCommand(
        '{"patches":[{"file_path":"sample.txt","patch_mode":"full_file_replacement","content":"two\\n"}],'
        '"rationale":"replace","confidence":0.8}'
    )
    engine = LlmRepairEngine(
        project_root=tmp_path,
        config={"repair": {"context_path": "context.md"}},
        ai_command=ai,
    )

    engine.propose_fix(_rca("full_file_replacement"), {"sample.txt": "one\n"})
    prompt = ai.prompts[0]

    assert "{root_cause_analysis}" not in prompt
    assert "{file_contents}" not in prompt
    assert "{project_context}" not in prompt
    assert "sample.txt" in prompt
    assert "minimal edits" in prompt


def test_git_patcher_validate_returns_true_for_valid_diff(tmp_path):
    root = _init_repo(tmp_path)

    assert GitPatcher().validate(FilePatch("sample.txt", "unified_diff", _valid_diff()), root)


def test_git_patcher_validate_returns_false_for_invalid_diff(tmp_path):
    root = _init_repo(tmp_path)

    assert not GitPatcher().validate(FilePatch("sample.txt", "unified_diff", "not a diff"), root)


def test_git_patcher_apply_dry_run_does_not_modify_file(tmp_path):
    root = _init_repo(tmp_path)
    result = GitPatcher().apply(FilePatch("sample.txt", "unified_diff", _valid_diff()), root, dry_run=True)

    assert result.success
    assert (root / "sample.txt").read_text(encoding="utf-8") == "one\n"


def test_git_patcher_apply_unified_diff_modifies_file(tmp_path):
    root = _init_repo(tmp_path)
    result = GitPatcher().apply(FilePatch("sample.txt", "unified_diff", _valid_diff()), root)

    assert result.success
    assert (root / "sample.txt").read_text(encoding="utf-8") == "two\n"


def test_git_patcher_apply_full_file_replacement_overwrites_file(tmp_path):
    root = _init_repo(tmp_path)
    result = GitPatcher().apply(FilePatch("sample.txt", "full_file_replacement", "two\n"), root)

    assert result.success
    assert (root / "sample.txt").read_text(encoding="utf-8") == "two\n"


def test_git_patcher_apply_retries_three_way_once(tmp_path):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        three_way_calls = sum(1 for call in calls if "--3way" in call)
        if "--check" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if three_way_calls == 1:
            return subprocess.CompletedProcess(command, 1, "", "first failure")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = GitPatcher(runner=runner).apply(FilePatch("sample.txt", "unified_diff", _valid_diff()), tmp_path)

    assert result.success
    assert sum(1 for call in calls if "--3way" in call) == 2


def test_git_patcher_rejects_absolute_full_file_path(tmp_path):
    result = GitPatcher().apply(FilePatch(str(tmp_path / "sample.txt"), "full_file_replacement", "two\n"), tmp_path)

    assert not result.success
    assert "relative path" in result.error_message


def test_engine_apply_aggregates_patch_results(tmp_path):
    engine = LlmRepairEngine(project_root=tmp_path)
    proposal = _proposal(
        FilePatch("one.txt", "full_file_replacement", "one\n"),
        FilePatch("two.txt", "full_file_replacement", "two\n"),
    )

    result = engine.apply(proposal)

    assert result.success
    assert sorted(result.applied_patches) == ["one.txt", "two.txt"]
    assert (tmp_path / "one.txt").read_text(encoding="utf-8") == "one\n"
    assert (tmp_path / "two.txt").read_text(encoding="utf-8") == "two\n"


def test_engine_apply_without_project_root_returns_apply_result_failure():
    proposal = _proposal(FilePatch("sample.txt", "full_file_replacement", "two\n"))
    result = LlmRepairEngine().apply(proposal)

    assert not result.success
    assert result.failed_patches == ["sample.txt"]
    assert "project_root" in result.error_message
