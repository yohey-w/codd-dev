"""Tests for codd extract — brownfield design document extraction."""

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from codd.extractor import (
    ExtractResult,
    ModuleInfo,
    ProjectFacts,
    Symbol,
    extract_facts,
    run_extract,
    synth_docs,
)


@pytest.fixture
def python_project(tmp_path):
    """Create a minimal Python project for testing."""
    # Source code
    src = tmp_path / "src"
    (src / "auth").mkdir(parents=True)
    (src / "auth" / "__init__.py").write_text("")
    (src / "auth" / "service.py").write_text(textwrap.dedent("""\
        from db.models import User
        from utils.crypto import hash_password
        import jwt

        class AuthService:
            def authenticate(self, username: str, password: str) -> bool:
                user = User.query.filter_by(username=username).first()
                return hash_password(password) == user.password_hash

            def create_token(self, user_id: int) -> str:
                return jwt.encode({"sub": user_id}, "secret")
    """))
    (src / "auth" / "middleware.py").write_text(textwrap.dedent("""\
        from auth.service import AuthService

        class AuthMiddleware:
            def __init__(self):
                self.service = AuthService()
    """))

    (src / "db").mkdir(parents=True)
    (src / "db" / "__init__.py").write_text("")
    (src / "db" / "models.py").write_text(textwrap.dedent("""\
        from sqlalchemy import Column, Integer, String
        from sqlalchemy.ext.declarative import declarative_base

        Base = declarative_base()

        class User(Base):
            __tablename__ = 'users'
            id = Column(Integer, primary_key=True)
            username = Column(String)
            password_hash = Column(String)

        class Session(Base):
            __tablename__ = 'sessions'
            id = Column(Integer, primary_key=True)
            user_id = Column(Integer)
            token = Column(String)
    """))

    (src / "utils").mkdir(parents=True)
    (src / "utils" / "__init__.py").write_text("")
    (src / "utils" / "crypto.py").write_text(textwrap.dedent("""\
        import hashlib

        def hash_password(password: str) -> str:
            return hashlib.sha256(password.encode()).hexdigest()

        def verify_hash(password: str, hashed: str) -> bool:
            return hash_password(password) == hashed
    """))

    (src / "api").mkdir(parents=True)
    (src / "api" / "__init__.py").write_text("")
    (src / "api" / "routes.py").write_text(textwrap.dedent("""\
        from fastapi import FastAPI
        from auth.service import AuthService
        from db.models import User

        app = FastAPI()

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.post("/login")
        def login(username: str, password: str):
            svc = AuthService()
            return svc.authenticate(username, password)
    """))

    # Tests
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("")
    (tests / "test_auth.py").write_text(textwrap.dedent("""\
        from auth.service import AuthService

        def test_authenticate():
            svc = AuthService()
            assert svc is not None
    """))
    (tests / "test_db.py").write_text(textwrap.dedent("""\
        from db.models import User

        def test_user_model():
            assert User.__tablename__ == 'users'
    """))

    # pyproject.toml
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "test-project"
        dependencies = ["fastapi", "sqlalchemy", "pyjwt"]

        [project.optional-dependencies]
        test = ["pytest"]
    """))

    return tmp_path


@pytest.fixture
def ts_project(tmp_path):
    """Create a minimal TypeScript project for testing."""
    src = tmp_path / "src"
    (src / "auth").mkdir(parents=True)
    (src / "auth" / "index.ts").write_text(textwrap.dedent("""\
        import { User } from '../db/models'
        import jwt from 'jsonwebtoken'

        export class AuthService {
            authenticate(username: string, password: string): boolean {
                return true
            }
        }

        export function createToken(userId: number): string {
            return jwt.sign({ sub: userId }, 'secret')
        }
    """))

    (src / "db").mkdir(parents=True)
    (src / "db" / "models.ts").write_text(textwrap.dedent("""\
        export class User {
            id: number
            username: string
        }

        export class Session {
            id: number
            token: string
        }
    """))

    (tmp_path / "package.json").write_text('{"dependencies": {"express": "^4.0", "prisma": "^5.0"}, "devDependencies": {"jest": "^29.0"}}')

    return tmp_path


class TestExtractFacts:
    def test_python_module_discovery(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        assert "auth" in facts.modules
        assert "db" in facts.modules
        assert "utils" in facts.modules
        assert "api" in facts.modules

    def test_python_symbol_extraction(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        auth = facts.modules["auth"]
        class_names = [s.name for s in auth.symbols if s.kind == "class"]
        assert "AuthService" in class_names
        assert "AuthMiddleware" in class_names

        func_names = [s.name for s in auth.symbols if s.kind == "function"]
        assert "authenticate" in func_names
        assert "create_token" in func_names

    def test_python_import_graph(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        auth = facts.modules["auth"]
        # auth imports db and utils
        assert "db" in auth.internal_imports
        assert "utils" in auth.internal_imports

        api = facts.modules["api"]
        # api imports auth and db
        assert "auth" in api.internal_imports
        assert "db" in api.internal_imports

    def test_python_external_imports(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        auth = facts.modules["auth"]
        assert "jwt" in auth.external_imports

        db = facts.modules["db"]
        assert "sqlalchemy" in db.external_imports

    def test_python_test_mapping(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        auth = facts.modules["auth"]
        assert any("test_auth" in t for t in auth.test_files)

        db = facts.modules["db"]
        assert any("test_db" in t for t in db.test_files)

    def test_framework_detection(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        assert "FastAPI" in facts.detected_frameworks
        assert facts.detected_test_framework == "pytest"

    def test_line_counting(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])

        assert facts.total_files > 0
        assert facts.total_lines > 0
        for mod in facts.modules.values():
            assert mod.line_count > 0

    def test_language_autodetect(self, python_project):
        facts = extract_facts(python_project, language=None, source_dirs=["src"])
        assert facts.language == "python"

    def test_ts_module_discovery(self, ts_project):
        facts = extract_facts(ts_project, "typescript", ["src"])

        assert "auth" in facts.modules
        assert "db" in facts.modules

    def test_ts_symbol_extraction(self, ts_project):
        facts = extract_facts(ts_project, "typescript", ["src"])

        auth = facts.modules["auth"]
        class_names = [s.name for s in auth.symbols if s.kind == "class"]
        assert "AuthService" in class_names

        func_names = [s.name for s in auth.symbols if s.kind == "function"]
        assert "createToken" in func_names

    def test_ts_framework_detection(self, ts_project):
        facts = extract_facts(ts_project, "typescript", ["src"])
        assert "Express" in facts.detected_frameworks


class TestSynthDocs:
    def test_generates_system_context(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])
        output_dir = python_project / "codd" / "extracted"
        generated = synth_docs(facts, output_dir)

        system_ctx = output_dir / "system-context.md"
        assert system_ctx.exists()
        assert system_ctx in generated

        content = system_ctx.read_text()
        assert "design:extract:system-context" in content
        assert "source: extracted" in content
        assert "Module Map" in content

    def test_generates_module_docs(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])
        output_dir = python_project / "codd" / "extracted"
        generated = synth_docs(facts, output_dir)

        modules_dir = output_dir / "modules"
        assert modules_dir.is_dir()

        auth_doc = modules_dir / "auth.md"
        assert auth_doc.exists()
        content = auth_doc.read_text()
        assert "design:extract:auth" in content
        assert "AuthService" in content
        assert "source: extracted" in content

    def test_frontmatter_has_depends_on(self, python_project):
        facts = extract_facts(python_project, "python", ["src"])
        output_dir = python_project / "codd" / "extracted"
        synth_docs(facts, output_dir)

        auth_doc = (output_dir / "modules" / "auth.md").read_text()
        assert "depends_on:" in auth_doc
        assert "design:extract:db" in auth_doc

    def test_confidence_below_green(self, python_project):
        """Extracted docs should never reach green band — human review needed."""
        facts = extract_facts(python_project, "python", ["src"])
        output_dir = python_project / "codd" / "extracted"
        synth_docs(facts, output_dir)

        for f in (output_dir / "modules").iterdir():
            content = f.read_text()
            # confidence should be < 0.90 (green threshold)
            import re
            m = re.search(r'confidence:\s*([\d.]+)', content)
            assert m, f"No confidence in {f}"
            assert float(m.group(1)) < 0.90, f"Confidence too high in {f}"


class TestRunExtract:
    def test_full_pipeline(self, python_project):
        result = run_extract(python_project, "python", ["src"])

        assert result.module_count == 4  # auth, db, utils, api
        assert result.total_files > 0
        assert result.total_lines > 0
        assert len(result.generated_files) >= 5  # system-context + 4 modules
        assert result.output_dir.exists()

    def test_works_without_codd_init(self, python_project):
        """Extract should work even without codd init (brownfield bootstrap)."""
        # No codd/ directory exists
        assert not (python_project / "codd" / "codd.yaml").exists()

        result = run_extract(python_project, "python", ["src"])
        assert result.module_count > 0
        assert result.output_dir.exists()

    def test_custom_output_dir(self, python_project):
        custom_out = python_project / "my-docs"
        result = run_extract(python_project, "python", ["src"], output=str(custom_out))

        assert result.output_dir == custom_out
        assert custom_out.exists()
        assert (custom_out / "system-context.md").exists()


class TestCLI:
    def test_extract_command_exists(self):
        from codd.cli import main
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main, ["extract", "--help"])
        assert result.exit_code == 0
        assert "brownfield" in result.output.lower() or "Extract" in result.output

    def test_extract_on_project(self, python_project):
        from codd.cli import main
        from click.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main, [
            "extract",
            "--path", str(python_project),
            "--language", "python",
            "--source-dirs", "src",
        ])
        assert result.exit_code == 0
        assert "Extracted:" in result.output
        assert "modules" in result.output
        config = yaml.safe_load((python_project / "codd" / "codd.yaml").read_text(encoding="utf-8"))
        assert config["project"]["name"] == python_project.name
        assert config["project"]["language"] == "python"
        assert config["scan"]["source_dirs"] == ["src"]
        assert config["graph"]["path"] == "codd/scan"
        assert "Generated: codd/codd.yaml" in result.output

    def test_extract_uses_hidden_config_when_codd_dir_is_source_tree(self, tmp_path):
        from codd.cli import main
        from click.testing import CliRunner

        project = tmp_path / "project"
        project.mkdir()
        (project / "src").mkdir()
        (project / "src" / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
        (project / "codd").mkdir()
        (project / "codd" / "__init__.py").write_text("", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(main, [
            "extract",
            "--path", str(project),
            "--language", "python",
            "--source-dirs", "src",
        ])

        assert result.exit_code == 0
        hidden_config = project / ".codd" / "codd.yaml"
        assert hidden_config.exists()
        assert (project / ".codd" / "extracted" / "system-context.md").exists()
        assert not (project / "codd" / "codd.yaml").exists()

        config = yaml.safe_load(hidden_config.read_text(encoding="utf-8"))
        assert config["project"]["name"] == "project"
        assert config["project"]["language"] == "python"
        assert config["scan"]["source_dirs"] == ["src"]
        assert config["graph"]["path"] == ".codd/scan"
        assert "Generated: .codd/codd.yaml" in result.output
