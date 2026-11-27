"""Exercise test environment guidance and validation through document generation."""

import pytest
import yaml

from codd import generator
from codd.greenfield.pipeline import (
    EnvironmentProvisionError,
    GreenfieldPipeline,
    load_session,
    new_session,
    save_session,
)
from codd.languages.adapters import env_python
from codd.project_types import EnvProvisionResult, provision_project_env
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


def test_noncanonical_environment_is_rejected(author):
    project, _, prompts = author(*[BODY + environment(packages=["test-helpers"])] * 3, entries=[artifact(
        ARCHIVE, "test:archive",
    )])
    with pytest.raises(ValueError, match="declaration is in a noncanonical test document"):
        generator.generate_wave(project, 1)
    assert len(prompts) == 3
    assert all(NO_DECLARATION in prompt and INSTRUCTION not in prompt for prompt in prompts)
    assert not (project / ARCHIVE).exists()


@pytest.mark.parametrize("pins", [
    ["docs/qa/smoke.md", "docs/qa/integration.md"],
    ["docs/qa"],
], ids=["multiple-files", "directory"])
def test_pinned_noncanonical_environment_matches_verification(author, pins):
    candidate = "docs/qa/smoke.md"
    body = BODY + environment(packages=["demo-helper>=1"])
    project, config, prompts = author(
        body, body, body, entries=[artifact(candidate, "design:smoke")],
        test_coverage={"docs": pins},
    )
    path = project / candidate
    path.parent.mkdir(parents=True)
    (path.parent / "integration.md").write_text(BODY, encoding="utf-8")
    assert_rejection_matches_verification(project, config, path, body, "noncanonical test document")
    assert len(prompts) == 3 and all(NO_DECLARATION in prompt for prompt in prompts)


@pytest.mark.parametrize(("has_strategy", "reason"), [
    (True, "noncanonical test document"),
    (False, "version must be the integer 1"),
], ids=["active-strategy", "malformed-archive-only"])
def test_malformed_noncanonical_environment_matches_verification(author, has_strategy, reason):
    archive = "docs/test/z_archive/test_strategy.md"
    body = BODY + environment(version=2)
    project, config, prompts = author(body, body, body, entries=[artifact(archive, "test:archive")])
    path = project / archive
    path.parent.mkdir(parents=True)
    if has_strategy:
        (project / STRATEGY).write_text(BODY + environment(packages=["demo-helper>=1"]), encoding="utf-8")
    assert_rejection_matches_verification(project, config, path, body, reason)
    assert len(prompts) == 3 and all(NO_DECLARATION in prompt for prompt in prompts)


def assert_rejection_matches_verification(project, config, path, body, reason):
    [planned] = generator._load_wave_artifacts(config)
    path.write_text(generator._render_document(planned, [], [], body), encoding="utf-8")
    with pytest.raises(ValueError, match=reason) as verify:
        read_test_environment(project, config)
    path.unlink()
    with pytest.raises(ValueError) as generate:
        generator.generate_wave(project, 1)
    assert str(generate.value) == str(verify.value)
    assert not path.exists()


def test_strategy_wave_path_trailing_space_matches_verification(author):
    project, config, prompts = author(
        BODY + environment(packages=["demo-helper>=1"]), entries=[artifact(STRATEGY + " ")],
    )
    [result] = generator.generate_wave(project, 1)
    assert result.path == project / STRATEGY
    assert not (project / (STRATEGY + " ")).exists()
    assert len(prompts) == 1 and INSTRUCTION in prompts[0]
    declaration = read_test_environment(project, config)
    assert declaration.packages == ("demo-helper>=1",)
    assert declaration.location.startswith(f"{STRATEGY}:")


@pytest.mark.parametrize("existing", [True, False], ids=["existing-archive", "generated-archive"])
def test_empty_noncanonical_environment_matches_verification(author, existing):
    archive = BODY + environment()
    valid = BODY + environment(packages=["demo-helper>=1"])
    project, config, prompts = author(
        valid, archive, entries=[artifact(), artifact(ARCHIVE, "test:archive")],
    )
    path = project / ARCHIVE
    if existing:
        path.parent.mkdir(parents=True)
        path.write_text(archive, encoding="utf-8")
        assert read_test_environment(project, config) is None
    canonical, archived = generator.generate_wave(project, 1)
    assert canonical.status == "generated"
    assert archived.status == ("skipped" if existing else "generated")
    assert len(prompts) == (1 if existing else 2)
    assert "packages: []" in path.read_text(encoding="utf-8")
    assert read_test_environment(project, config).packages == ("demo-helper>=1",)


@pytest.mark.parametrize(("configured", "saved"), [("python", "go"), ("go", "python")])
def test_saved_language_matches_verification(author, monkeypatch, configured, saved):
    packages = ("demo-helper>=1",)
    body = BODY + environment(packages=packages)
    project, config, prompts = author(body, body, body, language=configured)
    save_session(project, new_session({"language": saved}))
    installed = []
    success = EnvProvisionResult(True, "provisioned", "")

    def prepare(root, *_):
        state = root / ".codd/verify/exec_env.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}", encoding="utf-8")
        return success

    def install(root, declaration):
        installed.append(declaration.packages)
        return success

    monkeypatch.setattr(env_python, "_provision_python_base_env", prepare)
    monkeypatch.setattr(env_python, "_install_test_packages", install)
    pipeline = GreenfieldPipeline(echo=lambda _: None)
    pipeline._restore_session_options(load_session(project))

    if saved == "go":
        [planned] = generator._load_wave_artifacts(config)
        path = project / STRATEGY
        path.parent.mkdir(parents=True)
        path.write_text(generator._render_document(planned, [], [], body), encoding="utf-8")
        with pytest.raises(EnvironmentProvisionError, match="for language 'go'") as verify:
            pipeline._provision_env(project)
        path.unlink()
        with pytest.raises(ValueError) as generate:
            generator.generate_wave(project, 1)
        assert str(verify.value).endswith(str(generate.value))
        assert not installed
        assert len(prompts) == 3 and "Keep `packages` empty" in prompts[0]
    else:
        generator.generate_wave(project, 1)
        pipeline._provision_env(project)
        assert installed == [packages]
        assert len(prompts) == 1 and "list registry packages" in prompts[0]


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
