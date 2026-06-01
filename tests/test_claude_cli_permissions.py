from codd.claude_cli import with_default_claude_permission_bypass


def test_claude_command_gets_permission_bypass_by_default(monkeypatch):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)

    command = with_default_claude_permission_bypass(["claude", "--print"])

    assert command == [
        "claude",
        "--print",
        "--model",
        "claude-opus-4-8",
        "--effort",
        "max",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]


def test_claude_command_overrides_existing_permission_mode(monkeypatch):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)

    command = with_default_claude_permission_bypass([
        "claude",
        "--print",
        "--permission-mode",
        "plan",
    ])

    assert command == [
        "claude",
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "--model",
        "claude-opus-4-8",
        "--effort",
        "max",
        "--dangerously-skip-permissions",
    ]


def test_claude_command_respects_existing_model_and_effort(monkeypatch):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)

    command = with_default_claude_permission_bypass([
        "claude",
        "--print",
        "--model",
        "sonnet",
        "--effort=max",
    ])

    assert command == [
        "claude",
        "--print",
        "--model",
        "sonnet",
        "--effort=max",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
    ]


def test_non_claude_command_is_unchanged(monkeypatch):
    monkeypatch.delenv("CODD_CLAUDE_SAFE_PERMISSIONS", raising=False)

    assert with_default_claude_permission_bypass(["codex", "exec"]) == ["codex", "exec"]


def test_safe_permissions_env_opt_out(monkeypatch):
    monkeypatch.setenv("CODD_CLAUDE_SAFE_PERMISSIONS", "1")

    assert with_default_claude_permission_bypass(["claude", "--print"]) == [
        "claude",
        "--print",
        "--model",
        "claude-opus-4-8",
        "--effort",
        "max",
    ]
