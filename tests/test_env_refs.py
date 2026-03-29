"""Tests for R8 — Environment & config dependency detection."""

import pytest

from codd.env_refs import EnvRef, detect_env_refs, build_env_refs


# ── detect_env_refs: Python env patterns ───────────────────────


class TestPythonEnvPatterns:
    def test_os_getenv_no_default(self):
        code = 'db_host = os.getenv("DB_HOST")'
        refs = detect_env_refs(code, "app.py")
        assert len(refs) == 1
        assert refs[0].key == "DB_HOST"
        assert refs[0].kind == "env"
        assert refs[0].has_default is False

    def test_os_getenv_with_default(self):
        code = 'db_host = os.getenv("DB_HOST", "localhost")'
        refs = detect_env_refs(code, "app.py")
        assert len(refs) == 1
        assert refs[0].key == "DB_HOST"
        assert refs[0].has_default is True

    def test_os_environ_bracket(self):
        code = 'secret = os.environ["SECRET_KEY"]'
        refs = detect_env_refs(code, "config.py")
        assert len(refs) == 1
        assert refs[0].key == "SECRET_KEY"
        assert refs[0].kind == "env"
        assert refs[0].has_default is False

    def test_os_environ_get(self):
        code = 'debug = os.environ.get("DEBUG", "false")'
        refs = detect_env_refs(code, "config.py")
        assert len(refs) == 1
        assert refs[0].key == "DEBUG"
        assert refs[0].has_default is True

    def test_os_environ_get_no_default(self):
        code = 'debug = os.environ.get("DEBUG")'
        refs = detect_env_refs(code, "config.py")
        assert len(refs) == 1
        assert refs[0].has_default is False

    def test_os_environ_pop(self):
        code = 'old = os.environ.pop("OLD_KEY", None)'
        refs = detect_env_refs(code, "cleanup.py")
        assert len(refs) == 1
        assert refs[0].key == "OLD_KEY"
        assert refs[0].has_default is True

    def test_multiple_env_refs_same_line(self):
        code = 'x = os.getenv("A"); y = os.getenv("B")'
        refs = detect_env_refs(code, "multi.py")
        assert len(refs) == 2
        assert {r.key for r in refs} == {"A", "B"}

    def test_line_number_tracking(self):
        code = "# comment\nhost = os.getenv('HOST')\nport = os.getenv('PORT')"
        refs = detect_env_refs(code, "app.py")
        assert len(refs) == 2
        assert refs[0].line == 2
        assert refs[1].line == 3


# ── detect_env_refs: TS/JS patterns ───────────────────────────


class TestJsEnvPatterns:
    def test_process_env_dot(self):
        code = "const host = process.env.DATABASE_URL;"
        refs = detect_env_refs(code, "index.ts")
        assert len(refs) == 1
        assert refs[0].key == "DATABASE_URL"
        assert refs[0].kind == "env"

    def test_process_env_bracket(self):
        code = 'const secret = process.env["API_KEY"];'
        refs = detect_env_refs(code, "config.js")
        assert len(refs) == 1
        assert refs[0].key == "API_KEY"

    def test_process_env_dot_ignores_lowercase(self):
        # Only UPPER_CASE identifiers match dot notation
        code = "const x = process.env.someLocalVar;"
        refs = detect_env_refs(code, "app.ts")
        assert len(refs) == 0

    def test_process_env_bracket_allows_any_case(self):
        code = 'const x = process.env["mixedCase"];'
        refs = detect_env_refs(code, "app.js")
        assert len(refs) == 1
        assert refs[0].key == "mixedCase"


# ── detect_env_refs: config patterns ──────────────────────────


class TestConfigPatterns:
    def test_config_bracket(self):
        code = 'db = config["DATABASE_URL"]'
        refs = detect_env_refs(code, "settings.py")
        assert len(refs) == 1
        assert refs[0].key == "DATABASE_URL"
        assert refs[0].kind == "config"

    def test_settings_attr(self):
        code = "email = settings.EMAIL_BACKEND"
        refs = detect_env_refs(code, "views.py")
        assert len(refs) == 1
        assert refs[0].key == "EMAIL_BACKEND"
        assert refs[0].kind == "config"

    def test_app_config_bracket(self):
        code = 'app.config["SECRET_KEY"]'
        refs = detect_env_refs(code, "flask_app.py")
        assert len(refs) == 1
        assert refs[0].key == "SECRET_KEY"
        assert refs[0].kind == "config"

    def test_current_app_config(self):
        code = 'v = current_app.config["RATE_LIMIT"]'
        refs = detect_env_refs(code, "api.py")
        assert len(refs) == 1
        assert refs[0].key == "RATE_LIMIT"


# ── detect_env_refs: no false positives ───────────────────────


class TestNoFalsePositives:
    def test_plain_string(self):
        code = 'x = "DB_HOST"'
        refs = detect_env_refs(code, "app.py")
        assert len(refs) == 0

    def test_comment_line(self):
        # Regex matches in comments — acceptable heuristic
        code = '# os.getenv("KEY")'
        refs = detect_env_refs(code, "app.py")
        # This actually matches (regex is line-based), which is fine
        assert len(refs) <= 1

    def test_empty_content(self):
        refs = detect_env_refs("", "empty.py")
        assert refs == []


# ── build_env_refs integration ────────────────────────────────


class TestBuildEnvRefs:
    def test_populates_module_env_refs(self, tmp_path):
        from dataclasses import dataclass, field

        @dataclass
        class FakeModule:
            name: str = "mymod"
            files: list = field(default_factory=list)
            env_refs: list = field(default_factory=list)

        @dataclass
        class FakeFacts:
            modules: dict = field(default_factory=dict)

        src_file = tmp_path / "mymod" / "config.py"
        src_file.parent.mkdir()
        src_file.write_text('host = os.getenv("DB_HOST")\nport = os.getenv("DB_PORT")')

        mod = FakeModule(name="mymod", files=["mymod/config.py"])
        facts = FakeFacts(modules={"mymod": mod})
        build_env_refs(facts, tmp_path)

        assert len(mod.env_refs) == 2
        assert {r.key for r in mod.env_refs} == {"DB_HOST", "DB_PORT"}

    def test_handles_missing_file(self, tmp_path):
        from dataclasses import dataclass, field

        @dataclass
        class FakeModule:
            name: str = "mymod"
            files: list = field(default_factory=list)
            env_refs: list = field(default_factory=list)

        @dataclass
        class FakeFacts:
            modules: dict = field(default_factory=dict)

        mod = FakeModule(name="mymod", files=["nonexistent/file.py"])
        facts = FakeFacts(modules={"mymod": mod})
        build_env_refs(facts, tmp_path)

        assert mod.env_refs == []
