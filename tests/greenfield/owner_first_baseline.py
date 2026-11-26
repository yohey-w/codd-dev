"""Run each side of a comparison in its own clean interpreter and project."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from types import SimpleNamespace

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
    return work


def measure_both(tmp_path, main_reference, kind, **options):
    repo = Path(__file__).resolve().parents[2]
    results = []
    for name, package in (("current", repo), ("main", main_reference)):
        work = tmp_path / name
        work.mkdir()
        subprocess.run(
            [sys.executable, "-I", "-B", __file__, str(package), str(repo), kind, json.dumps(options)],
            cwd=work, check=True,
        )
        data = json.loads((work / "observation.json").read_text())
        if "root" in data:
            data["root"] = Path(data["root"])
        if "events" in data:
            data["events"] = [tuple(event) for event in data["events"]]
        results.append(SimpleNamespace(**data))
    return results


def _measure(package, repo, kind, options):
    sys.path[:0] = [str(package), str(repo)]
    import codd

    assert Path(codd.__file__).resolve().parent == package / "codd"
    project = Path.cwd() / "case"
    if kind == "doctor":
        return {"results": doctor_and_check(project, **options)}
    with pytest.MonkeyPatch.context() as patches:
        if kind == "oracle":
            from tests.greenfield.test_oracle_owner_first import observe_oracle
            return observe_oracle(project, patches, **options)
        if kind == "auth":
            from tests.greenfield.test_auth_owner_first import observe_auth
            return observe_auth(project, patches, **options)
    raise ValueError(kind)


if __name__ == "__main__":
    package, repo, kind, options = sys.argv[1:]
    data = _measure(Path(package), Path(repo), kind, json.loads(options))
    Path("observation.json").write_text(json.dumps(data))
