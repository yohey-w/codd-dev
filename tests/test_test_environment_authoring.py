"""Exercise test environment guidance and validation through document generation."""

import pytest
import yaml

from codd import generator
from codd.languages.adapters import env_python
from codd.project_types import provision_project_env
from codd.test_environment import read_test_environment


STRATEGY = "docs/test/test_strategy.md"
ARCHIVE = "docs/test/archive/test_strategy.md"
BODY = "# Test Strategy\n\n## Overview\n\nThe suite tests public outcomes.\n"
INSTRUCTION = "Write exactly one `codd-test-environment`"
NO_DECLARATION = "- Do not write a `codd-test-environment` block in this document."


def environment(*, version=1, packages=(), assets=()):
    values = {"version": version, "packages": list(packages), "assets": list(assets)}
    return f"\n```codd-test-environment\n{yaml.safe_dump(values, sort_keys=False)}```\n"


def artifact(output=STRATEGY, node_id="test:test-strategy"):
    return {"node_id": node_id, "output": output, "title": "Test Strategy"}


@pytest.fixture
def author(tmp_path, monkeypatch):
    prompts = []

    def setup(*responses, entries=None, language="python", **settings):
        config = {
            "project": {"type": "cli", "language": language},
            "wave_config": {1: entries or [artifact()]},
            "ai_command": "fake-test-author",
            **settings,
        }
        config_dir = tmp_path / "codd"
        config_dir.mkdir()
        (config_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        replies = iter(responses)

        def invoke(command, prompt):
            assert command == "fake-test-author"
            prompts.append(prompt)
            return next(replies)

        monkeypatch.setattr(generator, "_invoke_ai_command", invoke)
        return tmp_path, config, prompts

    return setup


@pytest.mark.parametrize("language", ["python", "go", None], ids=["python", "go", "unknown"])
def test_canonical_environment_prompt(author, language):
    project, _, prompts = author(BODY + environment(), language=language)
    generator.generate_wave(project, 1)

    [prompt] = prompts
    assert INSTRUCTION in prompt
    assert "```codd-test-environment\nversion: 1\npackages: []\nassets: []\n```" in prompt
    assert "Use `[]` for either list when nothing is needed." in prompt
    if language == "python":
        assert "list registry packages imported by tests or helper processes they start" in prompt
        assert "Keep runtime dependencies in the project's dependency manifest." in prompt
        assert "Version constraints are optional. Do not use `===`, paths, or URLs." in prompt
        assert "`playwright-chromium` — Chromium browser binaries used by Playwright tests." in prompt
    else:
        assert "Keep `packages` empty. Put test dependencies in the development dependencies" in prompt
        assert "Keep `assets` empty. This language profile lists no assets." in prompt
        assert "playwright-chromium" not in prompt


@pytest.mark.parametrize(("language", "declaration", "reason"), [
    ("python", environment(version=2), "version must be the integer 1"),
    ("python", environment(assets=["missing-asset"], packages=["./invalid"]), "unknown test asset"),
    ("go", environment(packages=["test-helpers"]), "does not install declared test packages"),
    ("python", environment(packages=["demo===archive.zip"]), "the === version operator is not accepted"),
    ("python", environment(packages=["./invalid"]), "invalid test package requirement"),
], ids=["malformed", "unknown-asset", "unsupported-packages", "arbitrary-equality", "package-path"])
@pytest.mark.parametrize("repair", [True, False], ids=["retry-succeeds", "retry-exhausted"])
def test_invalid_environment_retries_with_e1_diagnostic(author, monkeypatch, language, declaration, reason, repair):
    bad = BODY + declaration
    good = BODY + environment()
    project, config, prompts = author(bad, bad, good if repair else bad, language=language)
    planned = generator._load_wave_artifacts(config)[0]
    path = project / STRATEGY
    path.parent.mkdir(parents=True)
    path.write_text(generator._render_document(planned, [], [], bad), encoding="utf-8")

    def fail_if_provisioned(*args, **kwargs):
        pytest.fail("declaration checks must not provision an environment")

    monkeypatch.setattr(env_python, "_provision_python_base_env", fail_if_provisioned)
    try:
        parsed = read_test_environment(project, config)
    except ValueError as exc:
        expected = str(exc)
    else:
        result = provision_project_env(project, language=language, project_name="demo", test_environment=parsed)
        assert not result.ok
        expected = result.detail
    path.unlink()
    assert reason in expected

    if repair:
        generator.generate_wave(project, 1)
        assert path.is_file()
        assert read_test_environment(project, config) is None
    else:
        with pytest.raises(ValueError) as rejected:
            generator.generate_wave(project, 1)
        assert str(rejected.value) == expected
        assert not path.exists()
    assert len(prompts) == 3
    assert "REJECTED by CoDD" not in prompts[0]
    for prompt in prompts[1:]:
        assert "REJECTED by CoDD" in prompt
        assert expected in prompt


@pytest.mark.parametrize("repair", [True, False], ids=["retry-succeeds", "warn-and-continue"])
def test_missing_environment_retries_then_continues(author, capsys, repair):
    project, _, prompts = author(BODY, BODY, BODY + environment() if repair else BODY)
    [result] = generator.generate_wave(project, 1)

    assert result.status == "generated"
    assert ("codd-test-environment" in result.path.read_text(encoding="utf-8")) is repair
    assert len(prompts) == 3
    diagnostic = f"{STRATEGY}: write exactly one codd-test-environment block"
    for prompt in prompts[1:]:
        assert diagnostic in prompt
    assert capsys.readouterr().err.splitlines() == ([] if repair else [f"Warning: {diagnostic}"])


def test_valid_environment_succeeds_first_attempt(author, capsys):
    project, config, prompts = author(BODY + environment(
        packages=["test-helpers>=1"], assets=["playwright-chromium"],
    ))
    [result] = generator.generate_wave(project, 1)

    assert result.status == "generated"
    assert len(prompts) == 1
    declaration = read_test_environment(project, config)
    assert declaration.packages == ("test-helpers>=1",)
    assert declaration.assets == ("playwright-chromium",)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("packages", [[], ["test-helpers"]], ids=["empty", "nonempty"])
def test_noncanonical_environment_is_rejected(author, packages):
    project, _, prompts = author(*[BODY + environment(packages=packages)] * 3, entries=[artifact(
        ARCHIVE, "test:archive",
    )])
    with pytest.raises(ValueError, match="declaration is in a noncanonical test document"):
        generator.generate_wave(project, 1)
    assert len(prompts) == 3
    assert all(NO_DECLARATION in prompt and INSTRUCTION not in prompt for prompt in prompts)
    assert not (project / ARCHIVE).exists()


def test_wave_strategy_wins_over_same_basename_and_pin(author):
    alternate = "docs/qa/test_strategy.md"
    project, config, prompts = author(
        BODY, BODY + environment(packages=["test-helpers"]), entries=[
            artifact(ARCHIVE, "test:archive"), artifact(alternate),
        ], test_coverage={"docs": [ARCHIVE]},
    )
    generator.generate_wave(project, 1)

    archive, canonical = prompts
    assert NO_DECLARATION in archive and INSTRUCTION not in archive
    assert INSTRUCTION in canonical and NO_DECLARATION not in canonical
    assert read_test_environment(project, config).location.startswith(f"{alternate}:")


@pytest.mark.parametrize("alternate", ["docs/qa/verification.MD", "docs/qa/verification.markdown"])
def test_single_coverage_pin_gets_canonical_prompt(author, alternate):
    project, config, prompts = author(
        BODY + environment(packages=["test-helpers"]), entries=[artifact(alternate, "design:quality")],
        test_coverage={"docs": [alternate]},
    )
    generator.generate_wave(project, 1)

    assert len(prompts) == 1 and INSTRUCTION in prompts[0]
    assert read_test_environment(project, config).location.startswith(f"{alternate}:")


def test_environment_and_behavior_contract_are_both_checked(author):
    behavior = "\n```codd-behavior\nowns: {}\ncases: []\nunresolved: []\n```\n"
    project, _, prompts = author(
        BODY + environment(), BODY + behavior, BODY + behavior + environment(),
        generate={"design_consistency": True},
    )
    generator.generate_wave(project, 1)

    assert len(prompts) == 3
    assert "missing codd-behavior declarations" in prompts[1]
    assert "write exactly one codd-test-environment block" in prompts[2]


@pytest.mark.parametrize("remove", [False, True], ids=["invalid", "missing"])
def test_rewrite_checks_test_environment(author, capsys, remove):
    before = BODY + environment()
    after = BODY.rstrip() if remove else before.replace("version: 1", "version: 2")
    project, config, prompts = author(after)
    artifacts = generator._load_wave_artifacts(config)
    generate = generator._artifact_generator(project, config, artifacts, None)
    scope = (set(range(1, len(before.splitlines()) + 1)), set())
    rewrite = generator._DesignRewrite(before, "Resolve the test obligations.", scope=scope)
    drafts = {}

    if remove:
        generate(artifact=artifacts[0], feedback=None, rewrite=rewrite, drafts=drafts)
        assert "codd-test-environment" not in drafts[STRATEGY]
        assert capsys.readouterr().err.splitlines() == [
            f"Warning: {STRATEGY}: write exactly one codd-test-environment block",
        ]
    else:
        with pytest.raises(ValueError, match="version must be the integer 1"):
            generate(artifact=artifacts[0], feedback=None, rewrite=rewrite, drafts=drafts)
        assert not drafts
    assert len(prompts) == 1 and INSTRUCTION in prompts[0]


def test_existing_strategy_is_skipped_and_explicit_regeneration_is_checked(author):
    project, _, prompts = author(BODY + environment())
    path = project / STRATEGY
    path.parent.mkdir(parents=True)
    path.write_text(BODY, encoding="utf-8")

    [existing] = generator.generate_wave(project, 1)
    assert existing.status == "skipped"
    assert path.read_text(encoding="utf-8") == BODY
    assert not prompts

    replacement = generator.regenerate_artifact(project, node_id="test:test-strategy")
    assert replacement.status == "generated"
    assert "codd-test-environment" in path.read_text(encoding="utf-8")
    assert len(prompts) == 1
