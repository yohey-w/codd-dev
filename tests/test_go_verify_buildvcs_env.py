"""Go verify build-vcs robustness: verify runs Go commands with -buildvcs=false so
a greenfield Go project (often not a clean git repo) builds during verify instead
of failing 'error obtaining VCS status: exit 128'. Surfaced by the C-Go dogfood,
where SUT-generated `go build` tests failed on VCS stamping. -buildvcs=false is the
fix the failure_report literally names ("Use -buildvcs=false").
"""
from __future__ import annotations

from codd.repair.verify_runner import _go_aware_env


def test_non_go_project_returns_none(tmp_path):
    # No go.mod → inherit ambient env unchanged (no Go-specific flags).
    assert _go_aware_env(tmp_path) is None


def test_go_project_sets_buildvcs_false_and_mod_readonly(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    monkeypatch.delenv("GOFLAGS", raising=False)
    env = _go_aware_env(tmp_path)
    assert env is not None
    flags = env["GOFLAGS"].split()
    assert "-buildvcs=false" in flags
    assert "-mod=readonly" in flags


def test_preserves_ambient_goflags(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    monkeypatch.setenv("GOFLAGS", "-tags=integration")
    flags = _go_aware_env(tmp_path)["GOFLAGS"].split()
    assert "-tags=integration" in flags  # ambient preserved
    assert "-buildvcs=false" in flags and "-mod=readonly" in flags


def test_no_duplicate_when_already_present(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module x\n")
    monkeypatch.setenv("GOFLAGS", "-buildvcs=false -mod=readonly")
    flags = _go_aware_env(tmp_path)["GOFLAGS"].split()
    assert flags.count("-buildvcs=false") == 1
    assert flags.count("-mod=readonly") == 1


def test_env_is_full_environ_copy_not_just_goflags(tmp_path, monkeypatch):
    # The returned env must carry the rest of the environment (PATH etc.), else
    # `go` wouldn't be found when passed as the subprocess env.
    (tmp_path / "go.mod").write_text("module x\n")
    monkeypatch.setenv("CODD_TEST_SENTINEL", "1")
    env = _go_aware_env(tmp_path)
    assert env.get("CODD_TEST_SENTINEL") == "1"
    assert "PATH" in env
