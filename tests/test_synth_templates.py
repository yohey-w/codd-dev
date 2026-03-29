"""Tests for Jinja2-backed extracted document synthesis."""

import textwrap
from pathlib import Path

from codd.extractor import ModuleInfo, ProjectFacts, extract_facts, synth_docs
from codd.synth import synth_architecture


def _seed_synth_project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "api").mkdir(parents=True)
    (src / "services").mkdir(parents=True)
    (src / "db").mkdir(parents=True)
    (src / "utils").mkdir(parents=True)

    (src / "api" / "__init__.py").write_text("")
    (src / "api" / "routes.py").write_text(
        textwrap.dedent(
            """\
            from fastapi import FastAPI
            from services.auth import AuthService

            app = FastAPI()

            @app.get("/health")
            async def health() -> dict[str, str]:
                service = AuthService()
                return {"status": service.status()}
            """
        )
    )

    (src / "services" / "__init__.py").write_text("")
    (src / "services" / "auth.py").write_text(
        textwrap.dedent(
            """\
            from db.models import User
            from utils.crypto import hash_password

            class AuthService:
                def status(self) -> str:
                    return "ok"

                def authenticate(self, username: str, password: str) -> bool:
                    return hash_password(password) == username and User is not None
            """
        )
    )

    (src / "db" / "__init__.py").write_text("")
    (src / "db" / "models.py").write_text(
        textwrap.dedent(
            """\
            from sqlalchemy import Column, Integer, String
            from sqlalchemy.orm import declarative_base

            Base = declarative_base()

            class User(Base):
                __tablename__ = "users"
                id = Column(Integer, primary_key=True)
                role_id = Column(Integer)
                username = Column(String)
            """
        )
    )

    (src / "utils" / "__init__.py").write_text("")
    (src / "utils" / "crypto.py").write_text(
        textwrap.dedent(
            """\
            import hashlib

            def hash_password(value: str) -> str:
                return hashlib.sha256(value.encode()).hexdigest()
            """
        )
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_auth.py").write_text(
        textwrap.dedent(
            """\
            from services.auth import AuthService

            def test_status():
                assert AuthService().status() == "ok"
            """
        )
    )

    db_dir = tmp_path / "db"
    db_dir.mkdir()
    (db_dir / "schema.sql").write_text(
        textwrap.dedent(
            """\
            CREATE TABLE roles (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL
            );

            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              role_id INTEGER,
              username TEXT NOT NULL,
              CONSTRAINT fk_users_role FOREIGN KEY (role_id) REFERENCES roles(id)
            );

            CREATE INDEX idx_users_role ON users(role_id);
            """
        )
    )

    (tmp_path / "openapi.yaml").write_text(
        textwrap.dedent(
            """\
            openapi: 3.0.0
            paths:
              /health:
                get:
                  summary: Health check
                  responses:
                    "200":
                      description: ok
            components:
              schemas:
                HealthResponse:
                  type: object
                  properties:
                    status:
                      type: string
            """
        )
    )

    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "synth-demo"
            dependencies = ["fastapi", "sqlalchemy"]

            [project.optional-dependencies]
            dev = ["pytest"]
            """
        )
    )

    return tmp_path


def test_synth_docs_renders_system_context_and_architecture(tmp_path):
    project_root = _seed_synth_project(tmp_path)
    facts = extract_facts(project_root, "python", ["src"])

    output_dir = project_root / "docs" / "extracted"
    generated = synth_docs(facts, output_dir)

    system_context = output_dir / "system-context.md"
    architecture = output_dir / "architecture-overview.md"

    assert system_context in generated
    assert architecture in generated

    system_content = system_context.read_text()
    architecture_content = architecture.read_text()

    assert "Module Map" in system_content
    assert "Schema Artifacts" in system_content
    assert "API Specifications" in system_content
    assert "Build & Dependency Metadata" in system_content
    assert "Architectural Layers" in architecture_content
    assert "design:extract:architecture-overview" in architecture_content


def test_module_detail_includes_api_routes_and_async_functions(tmp_path):
    project_root = _seed_synth_project(tmp_path)
    facts = extract_facts(project_root, "python", ["src"])

    output_dir = project_root / "docs" / "extracted"
    synth_docs(facts, output_dir)

    api_doc = (output_dir / "modules" / "api.md").read_text()

    assert "## API Routes" in api_doc
    assert "`/health`" in api_doc
    assert "## Async Functions" in api_doc
    assert "health()" in api_doc


def test_schema_design_renders_foreign_keys_and_indexes(tmp_path):
    project_root = _seed_synth_project(tmp_path)
    facts = extract_facts(project_root, "python", ["src"])

    output_dir = project_root / "docs" / "extracted"
    synth_docs(facts, output_dir)

    schema_doc = next((output_dir / "schemas").glob("*.md")).read_text()

    assert "## Foreign Keys" in schema_doc
    assert "users(role_id) -> roles(id)" in schema_doc
    assert "## Indexes" in schema_doc
    assert "idx_users_role" in schema_doc


def test_api_contract_renders_openapi_endpoints(tmp_path):
    project_root = _seed_synth_project(tmp_path)
    facts = extract_facts(project_root, "python", ["src"])

    output_dir = project_root / "docs" / "extracted"
    synth_docs(facts, output_dir)

    api_doc = next((output_dir / "api").glob("*.md")).read_text()

    assert "## OpenAPI Endpoints" in api_doc
    assert "`/health`" in api_doc
    assert "HealthResponse" in api_doc


def test_synth_architecture_classifies_layers_and_flags_violations(tmp_path):
    facts = ProjectFacts(
        language="python",
        source_dirs=["src"],
        modules={
            "api": ModuleInfo(
                name="api",
                files=["src/api/routes.py"],
                patterns={"api_routes": "HTTP route handlers: /health"},
                internal_imports={"service": ["from service import run"]},
            ),
            "service": ModuleInfo(
                name="service",
                files=["src/service.py"],
                internal_imports={"db": ["from db import User"]},
            ),
            "db": ModuleInfo(
                name="db",
                files=["src/db/models.py"],
                patterns={"db_models": "ORM models: User"},
                internal_imports={"api": ["from api import router"]},
            ),
            "utils": ModuleInfo(
                name="utils",
                files=["src/utils/crypto.py"],
            ),
        },
    )

    output_dir = tmp_path / "docs" / "extracted"
    output_dir.mkdir(parents=True)

    architecture_path = synth_architecture(facts, output_dir)
    content = architecture_path.read_text()

    assert "### Presentation" in content
    assert "### Application" in content
    assert "### Domain" in content
    assert "### Shared" in content
    assert "`db` (Domain) imports `api` (Presentation)" in content
