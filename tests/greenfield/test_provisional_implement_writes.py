"""Keep the human-edited requirements appendix through implementation."""

import os
from pathlib import Path
import shlex
import subprocess
import sys

from click.testing import CliRunner
import pytest

from codd import generator, implementer
from codd.cli import main
from codd.implementer.chunked_runner import ChunkedRunner, _ChunkTimeout
from codd.llm.impl_step_deriver import ImplStep, ImplStepCacheRecord, impl_step_cache_path, write_impl_step_cache
from tests.greenfield.test_provisional_decisions import DECISIONS, DESIGN, make_project


EDITED_APPENDIX = b"<!-- codd:provisional-decisions -->\r\n\r\n## Duration\r\n\r\nReserve for half an hour.  \r\n"
SOURCE = Path("src/reservations.py")
WRITE_REJECTION = "AI attempted to write the managed requirements appendix"
TARGET_REJECTION = "Cannot overwrite the managed requirements appendix"


@pytest.fixture
def project(tmp_path):
    project = make_project(tmp_path / "project")
    (project / DESIGN).parent.mkdir()
    (project / DESIGN).write_text(
        "---\ncodd:\n  node_id: design:reservations\n  type: design\n---\n\n# Reservations\n",
        encoding="utf-8",
    )
    (project / DECISIONS).write_bytes(EDITED_APPENDIX)
    return project


def _ordinary_command(project, *args):
    return CliRunner().invoke(main, [
        "implement", "--path", str(project), "--design", DESIGN.as_posix(),
        "--use-derived-steps", "false", *args,
    ])


def _script_command(source):
    return shlex.join([sys.executable, "-c", source])


@pytest.mark.parametrize("alias", [None, "symlink", "hardlink", "absolute"])
def test_implement_retries_after_ai_targets_the_appendix(project, monkeypatch, alias):
    target = Path("docs/alias.md")
    if alias == "symlink":
        (project / target).symlink_to(project / DECISIONS)
    elif alias == "hardlink":
        (project / target).hardlink_to(project / DECISIONS)
    else:
        target = project / DECISIONS if alias == "absolute" else DECISIONS
    prompts = []

    def invoke(command, prompt, *, project_root):
        prompts.append(prompt)
        if len(prompts) == 1:
            return f"=== FILE: {target} ===\n# Replace it\n=== FILE: docs/rejected.md ===\n# Rejected\n"
        return "=== FILE: docs/generated.md ===\n# Accepted"

    monkeypatch.setattr(implementer._legacy, "DEFAULT_NO_USABLE_FILE_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
    result = _ordinary_command(project, "--output", "docs")

    assert result.exit_code == 0, result.output
    assert len(prompts) == 2
    assert TARGET_REJECTION in prompts[1]
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX
    assert not (project / "docs/rejected.md").exists()
    assert (project / "docs/generated.md").is_file()


@pytest.mark.parametrize("target", [DECISIONS, DECISIONS.parent, Path("docs")])
def test_implement_clean_rejects_paths_containing_the_appendix(project, monkeypatch, target):
    (project / SOURCE).parent.mkdir()
    (project / SOURCE).write_text("kept = True\n", encoding="utf-8")
    monkeypatch.setattr(generator, "_invoke_ai_command", lambda *args, **kwargs: pytest.fail("unexpected AI call"))
    result = _ordinary_command(project, "--clean", "--output", str(SOURCE), "--output", str(target))

    assert result.exit_code == 1, result.output
    assert str(DECISIONS) in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX
    assert (project / SOURCE).read_text(encoding="utf-8") == "kept = True\n"


def test_implement_rejects_an_absolute_appendix_output_before_invoking_ai(project, monkeypatch):
    monkeypatch.setattr(generator, "_invoke_ai_command", lambda *args, **kwargs: pytest.fail("unexpected AI call"))
    result = _ordinary_command(project, "--output", str(project / DECISIONS))

    assert result.exit_code == 1, result.output
    assert TARGET_REJECTION in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX


def test_implement_keeps_a_relative_stdout_command(project, tmp_path, monkeypatch):
    fake = tmp_path / "fake-ai"
    fake.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        f"assert Path.cwd() == Path({str(project)!r})\n"
        "assert 'half an hour' in sys.stdin.read()\n"
        f"print('=== FILE: {SOURCE} ===\\nlimit = 30')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    result = _ordinary_command(project, "--output", str(SOURCE), "--ai-cmd", "../fake-ai")

    assert result.exit_code == 0, result.output
    assert "limit = 30\n" in (project / SOURCE).read_text(encoding="utf-8")
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX


@pytest.mark.parametrize("action", ["appendix.write_bytes(b'Other')", "appendix.unlink(); sys.exit(7)"])
def test_implement_retries_after_a_direct_appendix_change(project, monkeypatch, action):
    fake = (
        "from pathlib import Path\nimport sys\n"
        f"assert Path.cwd() == Path({str(project)!r})\n"
        f"appendix = Path({str(DECISIONS)!r})\n"
        f"assert appendix.read_bytes() == {EDITED_APPENDIX!r}\n"
        f"if {WRITE_REJECTION!r} not in sys.stdin.read():\n"
        f"    {action}\n"
        f"    print('=== FILE: {SOURCE} ===\\nlimit = 90')\n"
        "else:\n"
        f"    print('=== FILE: {SOURCE} ===\\nlimit = 30')\n"
    )
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(implementer._legacy, "DEFAULT_NO_USABLE_FILE_BACKOFF_SECONDS", 0)
    result = _ordinary_command(project, "--output", str(SOURCE), "--ai-cmd", _script_command(fake))

    assert result.exit_code == 0, result.output
    assert result.output.count(WRITE_REJECTION) == 1
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX
    assert "limit = 30\n" in (project / SOURCE).read_text(encoding="utf-8")


@pytest.mark.parametrize("violation", ["appendix.unlink()", f"print('=== FILE: {DECISIONS} ===')"])
def test_implement_file_writing_agent_detects_the_appendix_before_file_capture(project, tmp_path, monkeypatch, violation):
    subprocess.run(["git", "init", "--quiet", "--template="], cwd=project, check=True)
    fake = tmp_path / "codex-fake"
    fake.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"assert Path.cwd() == Path({str(project)!r})\n"
        f"appendix = Path({str(DECISIONS)!r})\n"
        f"assert appendix.read_bytes() == {EDITED_APPENDIX!r}\n"
        f"output = Path({str(SOURCE)!r})\n"
        "output.parent.mkdir(exist_ok=True)\noutput.write_text('limit = 30\\n')\n"
        f"{violation}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(implementer._legacy, "DEFAULT_NO_USABLE_FILE_RETRIES", 0)
    result = _ordinary_command(project, "--output", ".", "--ai-cmd", str(fake))

    assert result.exit_code == 1, result.output
    assert (WRITE_REJECTION if violation == "appendix.unlink()" else TARGET_REJECTION) in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX
    assert not (project / SOURCE).exists()


@pytest.mark.parametrize("action", ["appendix.write_bytes(b'Other')", "appendix.unlink(); sys.exit(7)"])
def test_chunked_implement_protects_the_appendix_from_direct_ai_writes(project, monkeypatch, action):
    monkeypatch.setenv("PATH", "")
    os.mkfifo(project / "unrelated-pipe")
    fake = (
        "from pathlib import Path\nimport sys\n"
        f"assert Path.cwd() == Path({str(project)!r})\n"
        "assert 'half an hour' in sys.stdin.read()\n"
        f"source = Path({str(SOURCE)!r})\n"
        "if source.exists():\n"
        f"    appendix = Path({str(DECISIONS)!r})\n"
        f"    {action}\n"
        "else:\n"
        "    source.parent.mkdir()\n"
        "    source.write_text('limit = 30\\n')\n"
    )
    result = _run_chunked(project, fake, chunks=2)

    assert result.exit_code == 1, result.output
    assert "PARTIAL: 1/2 chunks" in result.output
    assert WRITE_REJECTION in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX

    history = next((project / ".codd/chunked_run_history").iterdir())
    result = _run_chunked(
        project, fake.replace(action, f"assert appendix.read_bytes() == {EDITED_APPENDIX!r}"),
        chunks=2, history=history,
    )
    assert result.exit_code == 0, result.output
    assert result.output.count("Chunk ") == 1
    assert "SUCCESS: 2/2 chunks" in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX
    assert (project / SOURCE).read_text(encoding="utf-8") == "limit = 30\n"


def test_chunked_implement_timeout_keeps_original_appendix(project, monkeypatch):
    def invoke(self, command, prompt, project_root):
        (project_root / DECISIONS).unlink()
        raise _ChunkTimeout("Timed out.")

    monkeypatch.setattr(ChunkedRunner, "_invoke", invoke)
    result = _run_chunked(project, "")

    assert result.exit_code == 1, result.output
    assert "PARTIAL: 0/1 chunks" in result.output
    assert WRITE_REJECTION in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX


@pytest.mark.parametrize("record", ["relative", "absolute", "symlink"])
def test_chunked_implement_rejects_an_appendix_named_in_the_reply(project, record):
    if record == "symlink":
        target = project / "docs/alias.md"
        target.symlink_to(project / DECISIONS)
    else:
        target = project / DECISIONS if record == "absolute" else f"./{DECISIONS}"
    response = f"=== FILE: {target} ===\n# Replace it"
    result = _run_chunked(project, f"print({response!r})")

    assert result.exit_code == 1, result.output
    assert "PARTIAL: 0/1 chunks" in result.output
    assert TARGET_REJECTION in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX


@pytest.mark.parametrize("field", ["expected_outputs", "target_path_hint"])
def test_chunked_implement_rejects_an_absolute_appendix_step_output(project, monkeypatch, field):
    monkeypatch.setattr(ChunkedRunner, "_invoke", lambda *args, **kwargs: pytest.fail("unexpected AI call"))
    appendix = str(project / DECISIONS)
    declared = {field: [appendix] if field == "expected_outputs" else appendix}
    result = _run_chunked(project, "", step_fields=declared)

    assert result.exit_code == 1, result.output
    assert TARGET_REJECTION in result.output
    assert (project / DECISIONS).read_bytes() == EDITED_APPENDIX


def _run_chunked(project, fake, *, chunks=1, step_fields=None, history=None):
    task = "design:reservations"
    steps = [ImplStep.from_dict({
        "id": f"reservation_limit_{index}",
        "kind": "edit",
        "rationale": "Limit reservation duration.",
        "source_design_section": "Reservations",
        "expected_outputs": [SOURCE.as_posix()],
        "approved": True,
        **(step_fields or {}),
    }) for index in range(chunks)]
    write_impl_step_cache(
        impl_step_cache_path(task, {"project_root": project}),
        ImplStepCacheRecord("scripted", "key", task, "design", "template", "now", [DESIGN.as_posix()], steps),
    )
    return CliRunner().invoke(main, [
        "implement", "run" if history is None else "resume", "--path", str(project),
        "--task", task, "--chunk-size", "1", "--timeout-per-chunk", "10",
        "--ai-cmd", _script_command(fake), *(["--history", str(history)] if history is not None else []),
    ])
