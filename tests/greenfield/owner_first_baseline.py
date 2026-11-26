"""Measure ordinary retry with the origin/main package in a fresh interpreter."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


def doctor_and_check(root, enabled):
    from click.testing import CliRunner
    from codd.cli import main

    config = root / "codd/codd.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("implement:\n  implement_oracle: true\n"
                      + ("  owner_first_retry_on_copy: false\n" if enabled is False else ""))
    runner = CliRunner()
    results = []
    for command in (["doctor"], ["check", "--format", "json"]):
        result = runner.invoke(main, [*command, "--project-path", str(root)])
        results.append([result.exit_code, result.output.replace(str(root), "<project>")])
    return results


@pytest.fixture(scope="session")
def main_reference(tmp_path_factory):
    repo = Path(__file__).resolve().parents[2]
    work = tmp_path_factory.mktemp("owner-first-main")
    # Extract only the runtime package. No checkout, index, tests or venv is copied.
    archive = subprocess.check_output(["git", "archive", "origin/main", "codd"], cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(work, filter="data")
    subprocess.run([sys.executable, "-I", "-B", __file__, str(work), str(repo)], cwd=work, check=True)
    data = json.loads((work / "observations.json").read_text())
    for result in data["oracle"].values():
        result["events"] = [tuple(event) for event in result["events"]]
    return data


def _measure(work, repo):
    sys.path[:0] = [str(work), str(repo)]
    import codd
    from tests.greenfield.test_auth_owner_first import _run_stage
    from tests.greenfield.test_oracle_owner_first import oracle_scenario, StageError

    assert Path(codd.__file__).resolve().parent == work / "codd"
    oracle_cases = {}
    for name in ("None", "False", "multiple", "mixed", "orphan", "narrow", "budget", "global_error"):
        with pytest.MonkeyPatch.context() as patches:
            enabled = None if name == "None" else name != "False"
            project = oracle_scenario(work / f"oracle-{name}", patches, name, enabled=enabled)
            if name in {"multiple", "orphan", "budget"}:
                with pytest.raises(StageError):
                    project.run()
            else:
                project.run()
            oracle_cases[name] = {"events": project.events, "echo": project.echo,
                                  "feedback": project.feedback, "root": str(project.root)}
    auth_cases = {}
    for name, options in (("None", {"enabled": None}), ("False", {"enabled": False}),
                          ("zero", {"rounds": 0}), ("all", {"failed": tuple("abc")})):
        with pytest.MonkeyPatch.context() as patches:
            root, writes, checks, messages = _run_stage(work / f"auth-{name}", patches, "clean", **options)
            auth_cases[name] = {"project": str(root), "writes": writes, "checks": checks, "messages": messages}
    data = {
        "oracle": oracle_cases, "auth": auth_cases,
        "doctor": {str(flag): doctor_and_check(work / f"doctor-{flag}", flag) for flag in (None, False)},
    }
    (work / "observations.json").write_text(json.dumps(data))


if __name__ == "__main__":
    _measure(*map(Path, sys.argv[1:]))
