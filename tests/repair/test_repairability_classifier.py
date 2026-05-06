from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess
from pathlib import Path

from codd.repair.repairability_classifier import NullClassifier, RepairabilityClassifier


@dataclass
class Violation:
    id: str
    affected_files: list[str]
    detail: str = "artifact mismatch"


class FakeLlm:
    def __init__(self, *responses: str):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.responses.pop(0)


class InvokeLlm:
    def __init__(self, response: str):
        self.response = response
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-m", message)


def _write(root: Path, path: str, text: str = "value\n") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, *, changed: tuple[str, ...] = ("src/changed.py",)) -> tuple[Path, str]:
    root = tmp_path
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test User")
    _write(root, "src/old.py", "old = True\n")
    _commit(root, "base")
    baseline = _git(root, "rev-parse", "HEAD")
    for index, path in enumerate(changed):
        _write(root, path, f"changed_{index} = True\n")
    if changed:
        _commit(root, "change")
    return root, baseline


def test_stage_one_changed_affected_file_is_repairable(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/changed.py",))
    llm = FakeLlm("{}")

    result = RepairabilityClassifier(llm, root).classify(
        [Violation("issue", ["src/changed.py"])],
        baseline,
    )

    assert result.repairable == [Violation("issue", ["src/changed.py"])]
    assert result.pre_existing == []
    assert result.unrepairable == []
    assert llm.prompts == []


def test_stage_one_unchanged_file_goes_to_stage_two(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/changed.py",))
    llm = FakeLlm('{"issue": "pre_existing"}')

    result = RepairabilityClassifier(llm, root).classify([Violation("issue", ["src/old.py"])], baseline)

    assert result.repairable == []
    assert result.pre_existing == [Violation("issue", ["src/old.py"])]
    assert result.unrepairable == []
    assert len(llm.prompts) == 1


def test_stage_two_marks_pre_existing(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"old": "pre_existing"}'), root).classify([violation], baseline)

    assert result.pre_existing == [violation]


def test_stage_two_marks_unrepairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("wide_gap", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"wide_gap": "unrepairable"}'), root).classify([violation], baseline)

    assert result.unrepairable == [violation]


def test_stage_two_can_upgrade_unchanged_file_to_repairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("minor", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"minor": "repairable"}'), root).classify([violation], baseline)

    assert result.repairable == [violation]
    assert result.pre_existing == []
    assert result.unrepairable == []


def test_all_changed_violations_skip_stage_two(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/a.py", "src/b.py"))
    llm = FakeLlm("{}")
    violations = [Violation("a", ["src/a.py"]), Violation("b", ["src/b.py"])]

    result = RepairabilityClassifier(llm, root).classify(violations, baseline)

    assert result.repairable == violations
    assert llm.prompts == []


def test_all_unchanged_violations_use_one_stage_two_prompt(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/changed.py",))
    violations = [Violation("old", ["src/old.py"]), Violation("missing", ["docs/spec.md"])]
    llm = FakeLlm('{"old": "pre_existing", "missing": "unrepairable"}')

    result = RepairabilityClassifier(llm, root).classify(violations, baseline)

    assert result.pre_existing == [violations[0]]
    assert result.unrepairable == [violations[1]]
    assert len(llm.prompts) == 1


def test_malformed_stage_two_output_falls_back_to_unrepairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm("not json"), root).classify([violation], baseline)

    assert result.unrepairable == [violation]


def test_markdown_json_fence_is_accepted(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('```json\n{"old": "pre_existing"}\n```'), root).classify(
        [violation],
        baseline,
    )

    assert result.pre_existing == [violation]


def test_missing_stage_two_key_is_unrepairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"other": "repairable"}'), root).classify([violation], baseline)

    assert result.unrepairable == [violation]


def test_unknown_stage_two_label_is_unrepairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"old": "manual"}'), root).classify([violation], baseline)

    assert result.unrepairable == [violation]


def test_null_classifier_keeps_all_violations_repairable():
    violations = [Violation("one", ["a.py"]), Violation("two", ["b.py"])]

    result = NullClassifier().classify(violations, baseline_ref=None)

    assert result.repairable == violations
    assert result.pre_existing == []
    assert result.unrepairable == []


def test_mapping_affected_files_are_supported(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/from_dict.py",))
    violation = {"id": "dict", "affected_files": ["src/from_dict.py"]}

    result = RepairabilityClassifier(FakeLlm("{}"), root).classify([violation], baseline)

    assert result.repairable == [violation]


def test_singular_affected_file_is_supported(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/single.py",))
    violation = {"id": "single", "affected_file": "src/single.py"}

    result = RepairabilityClassifier(FakeLlm("{}"), root).classify([violation], baseline)

    assert result.repairable == [violation]


def test_failed_nodes_can_act_as_affected_files(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/node.py",))
    violation = {"id": "node", "failed_nodes": ["src/node.py"]}

    result = RepairabilityClassifier(FakeLlm("{}"), root).classify([violation], baseline)

    assert result.repairable == [violation]


def test_absolute_paths_inside_repo_are_normalized(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/absolute.py",))
    violation = Violation("absolute", [str(root / "src" / "absolute.py")])

    result = RepairabilityClassifier(FakeLlm("{}"), root).classify([violation], baseline)

    assert result.repairable == [violation]


def test_directory_affected_path_matches_changed_child(tmp_path: Path):
    root, baseline = _repo(tmp_path, changed=("src/nested/file.py",))
    violation = Violation("directory", ["src/nested"])

    result = RepairabilityClassifier(FakeLlm("{}"), root).classify([violation], baseline)

    assert result.repairable == [violation]


def test_missing_git_baseline_falls_back_to_stage_two(tmp_path: Path):
    root, _baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(FakeLlm('{"old": "pre_existing"}'), root).classify([violation], "missing")

    assert result.pre_existing == [violation]


def test_missing_llm_marks_pending_violations_unrepairable(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violation = Violation("old", ["src/old.py"])

    result = RepairabilityClassifier(None, root).classify([violation], baseline)

    assert result.unrepairable == [violation]


def test_llm_keyword_alias_is_supported(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    llm_client = FakeLlm('{"old": "pre_existing"}')

    result = RepairabilityClassifier(llm_client=llm_client, repo_path=root).classify(
        [Violation("old", ["src/old.py"])],
        baseline,
    )

    assert result.pre_existing == [Violation("old", ["src/old.py"])]
    assert len(llm_client.prompts) == 1


def test_invoke_style_llm_is_supported(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    llm = InvokeLlm('{"old": "pre_existing"}')

    result = RepairabilityClassifier(llm, root).classify([Violation("old", ["src/old.py"])], baseline)

    assert result.pre_existing == [Violation("old", ["src/old.py"])]
    assert len(llm.prompts) == 1


def test_duplicate_violation_ids_are_made_unique_for_stage_two(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    violations = [Violation("same", ["src/old.py"]), Violation("same", ["docs/spec.md"])]

    result = RepairabilityClassifier(FakeLlm('{"same": "pre_existing", "same_1": "repairable"}'), root).classify(
        violations,
        baseline,
    )

    assert result.pre_existing == [violations[0]]
    assert result.repairable == [violations[1]]


def test_prompt_replaces_placeholders_and_includes_baseline(tmp_path: Path):
    root, baseline = _repo(tmp_path)
    llm = FakeLlm('{"old": "pre_existing"}')

    RepairabilityClassifier(llm, root).classify([Violation("old", ["src/old.py"])], baseline)
    prompt = llm.prompts[0]

    assert "{{violations_json}}" not in prompt
    assert "{{baseline_ref}}" not in prompt
    assert baseline in prompt
    assert '"id": "old"' in prompt


def test_repairability_code_generality_gate_has_zero_hits():
    pattern = re.compile(
        r"lms|osato|web app|mobile app|cli|backend|embedded|node_completeness|"
        r"deployment_completeness|vitest|c1|c2|c3|c4|c5|c6|c7|c8|c9",
        re.IGNORECASE,
    )
    text = Path("codd/repair/repairability_classifier.py").read_text(encoding="utf-8")

    assert pattern.search(text) is None


def test_repairability_template_generality_gate_has_zero_hits():
    pattern = re.compile(
        r"lms|osato|web|mobile|node_completeness|deployment_completeness|vitest",
        re.IGNORECASE,
    )
    text = Path("codd/repair/templates/repairability_meta.md").read_text(encoding="utf-8")

    assert pattern.search(text) is None
