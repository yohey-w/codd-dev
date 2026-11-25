import subprocess

import pytest

from codd.repair.git_patcher import GitPatcher
from codd.repair.schema import FilePatch


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    tracked = tmp_path / "sample1.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    _git(tmp_path, "add", tracked.name)
    _git(tmp_path, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "--no-gpg-sign", "-qm", "baseline")
    tracked.write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", tracked.name)
    return tmp_path


def _git_state(root):
    return (
        _git(root, "status", "--porcelain"),
        _git(root, "diff", "--cached"),
        _git(root, "rev-parse", "HEAD"),
    )


def _patch(name):
    return FilePatch(
        name, "unified_diff",
        f"--- a/{name}\n+++ b/{name}\n@@ -1,3 +1,3 @@\n first\n-one\n+two\n last\n",
    )


def test_apply_untracked_file_preserves_index_and_head(repo):
    # The literal filename also matches a tracked file if treated as a pathspec.
    target = repo / "sample[1].txt"
    target.write_text("first\none\nlast\n", encoding="utf-8")
    before = _git_state(repo)
    assert f"?? {target.name}" in before[0].splitlines()
    assert "+staged\n" in before[1]

    result = GitPatcher().apply(_patch(target.name), repo)

    assert result.success, result.error_message
    assert result.applied_patches == [target.name]
    assert target.read_text(encoding="utf-8") == "first\ntwo\nlast\n"
    assert _git_state(repo) == before


def test_apply_rejects_mismatched_untracked_context(repo):
    target = repo / "sample.txt"
    target.write_text("different\none\nlast\n", encoding="utf-8")
    before = _git_state(repo)

    result = GitPatcher().apply(_patch(target.name), repo)

    assert not result.success
    assert result.failed_patches == [target.name]
    assert target.read_text(encoding="utf-8") == "different\none\nlast\n"
    assert _git_state(repo) == before


def test_tracked_file_still_requires_matching_index(repo):
    target = repo / "sample1.txt"
    target.write_text("first\none\nlast\n", encoding="utf-8")
    before = _git_state(repo)

    # The diff fits the working copy, but a three-way apply requires the index to match.
    result = GitPatcher().apply(_patch(target.name), repo)

    assert not result.success
    assert result.failed_patches == [target.name]
    assert target.read_text(encoding="utf-8") == "first\none\nlast\n"
    assert _git_state(repo) == before


def test_apply_mixed_diff_rejects_unstaged_tracked_edit(repo):
    untracked = repo / "scratch.txt"
    tracked = repo / "sample1.txt"
    original = b"first\none\nlast\n"
    untracked.write_bytes(original)
    tracked.write_bytes(original)
    before = _git_state(repo)
    index = repo / ".git" / "index"
    index_before = index.read_bytes()
    patch = FilePatch(
        untracked.name, "unified_diff", _patch(untracked.name).content + _patch(tracked.name).content
    )

    result = GitPatcher().apply(patch, repo)

    assert not result.success
    assert result.failed_patches == [untracked.name]
    assert untracked.read_bytes() == tracked.read_bytes() == original
    assert index.read_bytes() == index_before
    assert _git_state(repo) == before


def test_apply_mixed_diff_rejects_tracked_submodule_reference_update(tmp_path):
    source = tmp_path / "submodule-source"
    source.mkdir()
    _git(source, "init", "-q")
    sample = source / "child.txt"
    commit = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit")
    sample.write_bytes(b"first\n")
    _git(source, "add", sample.name)
    _git(source, *commit, "--no-gpg-sign", "-qm", "first")
    first = _git(source, "rev-parse", "HEAD").strip()
    sample.write_bytes(b"second\n")
    _git(source, *commit, "--no-gpg-sign", "-qam", "second")
    second = _git(source, "rev-parse", "HEAD").strip()

    superproject = tmp_path / "superproject"
    superproject.mkdir()
    _git(superproject, "init", "-q")
    _git(
        superproject, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(source), "module",
    )
    module = superproject / "module"
    _git(module, "checkout", "-q", "--detach", first)
    _git(superproject, "add", module.name)
    _git(superproject, *commit, "--no-gpg-sign", "-qm", "add submodule")
    assert _git(superproject, "ls-files", "--stage", "--", module.name).startswith(f"160000 {first} ")
    _git(module, "checkout", "-q", "--detach", second)
    module_diff = _git(
        superproject, "diff", "--submodule=short", "--ignore-submodules=none", "--", module.name
    )
    assert f"-Subproject commit {first}\n+Subproject commit {second}\n" in module_diff
    _git(module, "checkout", "-q", "--detach", first)

    untracked = superproject / "scratch.txt"
    original = b"first\none\nlast\n"
    untracked.write_bytes(original)
    before = _git_state(superproject)
    module_before = _git_state(module)
    index = superproject / ".git" / "index"
    index_before = index.read_bytes()
    patch = FilePatch(untracked.name, "unified_diff", _patch(untracked.name).content + module_diff)

    result = GitPatcher().apply(patch, superproject)

    assert not result.success
    assert result.failed_patches == [untracked.name]
    assert untracked.read_bytes() == original
    assert (module / sample.name).read_bytes() == b"first\n"
    assert index.read_bytes() == index_before
    assert _git_state(superproject) == before
    assert _git_state(module) == module_before


def test_apply_diff_to_two_untracked_files_preserves_index_and_head(repo):
    targets = [repo / "scratch.txt", repo / "other[1].txt"]
    for target in targets:
        target.write_bytes(b"first\none\nlast\n")
    before = _git_state(repo)
    index = repo / ".git" / "index"
    index_before = index.read_bytes()
    patch = FilePatch(
        targets[0].name, "unified_diff", "".join(_patch(target.name).content for target in targets)
    )

    result = GitPatcher().apply(patch, repo)

    assert result.success, result.error_message
    assert result.applied_patches == [targets[0].name]
    for target in targets:
        assert target.read_bytes() == b"first\ntwo\nlast\n"
    assert index.read_bytes() == index_before
    assert _git_state(repo) == before


def test_apply_from_subdirectory_rejects_diff_git_would_only_partly_apply(repo):
    scope = repo / "scope"
    scope.mkdir()
    untracked = scope / "free.txt"
    tracked = repo / "sample1.txt"
    original = b"first\none\nlast\n"
    untracked.write_bytes(original)
    tracked.write_bytes(original)
    before = _git_state(repo)
    index = repo / ".git" / "index"
    index_before = index.read_bytes()
    content = "".join(
        f"diff --git a/{name} b/{name}\n" + _patch(name).content
        for name in ("scope/free.txt", tracked.name)
    )

    result = GitPatcher().apply(FilePatch(untracked.name, "unified_diff", content), scope)

    assert not result.success
    assert result.failed_patches == [untracked.name]
    assert untracked.read_bytes() == tracked.read_bytes() == original
    assert index.read_bytes() == index_before
    assert _git_state(repo) == before


@pytest.mark.parametrize("project_is_subdirectory", [False, True], ids=["repo-root", "subdirectory"])
def test_untracked_diff_requires_declared_path_to_match_git_path(repo, project_is_subdirectory):
    if project_is_subdirectory:
        root = repo / "src"
        content = _patch("src/x.py").content
    else:
        root = repo
        content = _patch("src/x.py").content.replace("--- a/", "--- ").replace("+++ b/", "+++ ")
    declared = root / "src/x.py"
    actual = root / "x.py"
    declared.parent.mkdir(parents=True)
    original = b"first\none\nlast\n"
    declared.write_bytes(original)
    actual.write_bytes(original)
    before = _git_state(repo)
    index = repo / ".git" / "index"
    index_before = index.read_bytes()

    result = GitPatcher().apply(FilePatch("src/x.py", "unified_diff", content), root)

    assert not result.success
    assert result.failed_patches == ["src/x.py"]
    assert "does not exist in index" in result.error_message
    assert declared.read_bytes() == actual.read_bytes() == original
    assert index.read_bytes() == index_before
    assert _git_state(repo) == before


def test_untracked_rename_accepts_declared_symlink_source_in_subdirectory(repo):
    root = repo / "scope"
    root.mkdir()
    referent = root / "referent.txt"
    referent.write_bytes(b"contents\n")
    (root / "old-link").symlink_to(referent.name)
    content = (
        "diff --git a/scope/old-link b/scope/new-link\n"
        "similarity index 100%\n"
        "rename from scope/old-link\n"
        "rename to scope/new-link\n"
    )
    index = repo / ".git" / "index"
    index_before = index.read_bytes()
    head_before = _git(repo, "rev-parse", "HEAD")

    result = GitPatcher().apply(FilePatch("old-link", "unified_diff", content), root)

    assert result.success, result.error_message
    assert result.applied_patches == ["old-link"]
    assert not (root / "old-link").is_symlink()
    assert (root / "new-link").readlink() == referent.relative_to(root)
    assert referent.read_bytes() == b"contents\n"
    assert index.read_bytes() == index_before
    assert _git(repo, "rev-parse", "HEAD") == head_before


@pytest.mark.parametrize(
    "fault",
    [
        "ls-files-timeout",
        "toplevel-nul",
        "ls-files-empty-path",
        "ls-files-unrelated-path",
        "ls-files-absolute-path",
        "numstat-invalid-count",
    ],
)
def test_untracked_query_fault_uses_indexed_apply(repo, fault):
    invalid_numstat = fault == "numstat-invalid-count"
    target = repo / ("scratch.txt" if invalid_numstat else "sample1.txt")
    original = b"first\none\nlast\n"
    target.write_bytes(original)
    before = _git_state(repo)
    index = repo / ".git" / "index"
    index_before = index.read_bytes()

    def runner(command, **kwargs):
        if "--show-toplevel" in command and fault == "toplevel-nul":
            output = subprocess.run(command, **kwargs).stdout
            return subprocess.CompletedProcess(command, 0, output[:-1] + b"\0\n", b"")
        if "--numstat" in command and invalid_numstat:
            return subprocess.CompletedProcess(command, 0, b"invalid\t1\tscratch.txt\0", b"")
        if "ls-files" in command:
            if fault == "ls-files-timeout":
                raise subprocess.TimeoutExpired(command, 1)
            malformed_output = {
                "ls-files-empty-path": b"\0",
                "ls-files-unrelated-path": b"another.txt\0",
                "ls-files-absolute-path": b"/unrelated/absolute\0",
            }.get(fault)
            if malformed_output is not None:
                return subprocess.CompletedProcess(command, 0, malformed_output, b"")
        return subprocess.run(command, **kwargs)

    result = GitPatcher(runner=runner).apply(_patch(target.name), repo)

    assert not result.success
    assert result.failed_patches == [target.name]
    reason = "does not exist in index" if invalid_numstat else "does not match index"
    error = f"error: {target.name}: {reason}"
    assert result.error_message == f"{error}\nretry: {error}"
    assert target.read_bytes() == original
    assert index.read_bytes() == index_before
    assert _git_state(repo) == before


@pytest.mark.parametrize("retry_fails", [False, True], ids=["retry-succeeds", "retry-fails"])
def test_plain_apply_to_untracked_file_retries_once(repo, retry_fails):
    target = repo / "new.txt"
    content = (
        "diff --git a/new.txt b/new.txt\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        "+new contents\n"
    )
    index = repo / ".git" / "index"
    index_before = index.read_bytes()
    head_before = _git(repo, "rev-parse", "HEAD")
    attempts = []

    def runner(command, **kwargs):
        if command == ["git", "apply"]:
            attempts.append(command)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(command, 1, "", "first failure")
            if retry_fails:
                return subprocess.CompletedProcess(command, 1, "", "second failure")
        return subprocess.run(command, **kwargs)

    result = GitPatcher(runner=runner).apply(FilePatch(target.name, "unified_diff", content), repo)

    assert len(attempts) == 2
    if retry_fails:
        assert not result.success
        assert result.failed_patches == [target.name]
        assert result.error_message == "first failure\nretry: second failure"
        assert not target.exists()
    else:
        assert result.success, result.error_message
        assert result.applied_patches == [target.name]
        assert target.read_bytes() == b"new contents\n"
    assert index.read_bytes() == index_before
    assert _git(repo, "rev-parse", "HEAD") == head_before
