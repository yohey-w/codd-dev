from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECIPES_DIR = PROJECT_ROOT / "codd" / "hooks" / "recipes"
README = PROJECT_ROOT / "README.md"


def test_hooks_recipes_dir_exists():
    assert RECIPES_DIR.is_dir()


def test_claude_settings_example_json_valid():
    data = json.loads((RECIPES_DIR / "claude_settings_example.json").read_text(encoding="utf-8"))

    assert "PostToolUse" in data["hooks"]
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Edit|Write|MultiEdit"


def test_codex_hook_sh_exists():
    assert (RECIPES_DIR / "codex_hook.sh").is_file()


def test_git_pre_commit_sh_exists():
    assert (RECIPES_DIR / "git_pre_commit.sh").is_file()


def test_git_post_commit_sh_exists():
    assert (RECIPES_DIR / "git_post_commit.sh").is_file()


def test_readme_hook_section_exists():
    assert "Hook Integration" in README.read_text(encoding="utf-8")


def test_readme_claude_hook_mentioned():
    assert "PostToolUse" in README.read_text(encoding="utf-8")


def test_readme_git_hook_mentioned():
    content = README.read_text(encoding="utf-8")

    assert "pre-commit" in content
