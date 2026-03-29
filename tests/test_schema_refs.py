"""Tests for R5.2 — Schema-code dependency detection (schema_refs.py)."""

import textwrap
from pathlib import Path

import pytest

from codd.schema_refs import SchemaRef, detect_schema_refs, build_schema_refs
from codd.extractor import ModuleInfo, ProjectFacts


# ── Helper ──────────────────────────────────────────────────

def make_facts(*modules: ModuleInfo) -> ProjectFacts:
    facts = ProjectFacts(language="python", source_dirs=["src"])
    for mod in modules:
        facts.modules[mod.name] = mod
    return facts


# ── detect_schema_refs: SQLAlchemy ──────────────────────────

def test_detect_sqlalchemy_tablename_single_quotes():
    content = "    __tablename__ = 'users'\n"
    refs = detect_schema_refs(content, "src/models.py")
    assert len(refs) == 1
    assert refs[0].table_or_model == "users"
    assert refs[0].kind == "sqlalchemy"
    assert refs[0].file == "src/models.py"
    assert refs[0].line == 1


def test_detect_sqlalchemy_tablename_double_quotes():
    content = '    __tablename__ = "order_items"\n'
    refs = detect_schema_refs(content, "src/models.py")
    assert len(refs) == 1
    assert refs[0].table_or_model == "order_items"
    assert refs[0].kind == "sqlalchemy"


def test_detect_sqlalchemy_tablename_with_spaces():
    content = "__tablename__  =  'products'\n"
    refs = detect_schema_refs(content, "src/models.py")
    assert any(r.table_or_model == "products" and r.kind == "sqlalchemy" for r in refs)


# ── detect_schema_refs: Django ──────────────────────────────

def test_detect_django_model_standard():
    content = "class User(models.Model):\n    pass\n"
    refs = detect_schema_refs(content, "app/models.py")
    assert len(refs) == 1
    assert refs[0].table_or_model == "User"
    assert refs[0].kind == "django"
    assert refs[0].line == 1


def test_detect_django_model_abstract_user():
    content = "class CustomUser(AbstractUser):\n    pass\n"
    refs = detect_schema_refs(content, "app/models.py")
    assert any(r.table_or_model == "CustomUser" and r.kind == "django" for r in refs)


def test_detect_django_model_abstract_base_user():
    content = "class AdminUser(AbstractBaseUser):\n    pass\n"
    refs = detect_schema_refs(content, "app/models.py")
    assert any(r.table_or_model == "AdminUser" and r.kind == "django" for r in refs)


def test_detect_django_model_line_number():
    content = textwrap.dedent("""\
        # Comment
        import django

        class Post(models.Model):
            pass
    """)
    refs = detect_schema_refs(content, "app/models.py")
    django_refs = [r for r in refs if r.kind == "django"]
    assert len(django_refs) == 1
    assert django_refs[0].line == 4


# ── detect_schema_refs: Prisma ──────────────────────────────

def test_detect_prisma_find_many():
    content = "results = await prisma.user.find_many()\n"
    refs = detect_schema_refs(content, "app/service.py")
    assert len(refs) == 1
    assert refs[0].table_or_model == "user"
    assert refs[0].kind == "prisma"


def test_detect_prisma_create():
    content = "record = prisma.post.create(data={'title': 'hi'})\n"
    refs = detect_schema_refs(content, "app/service.py")
    assert any(r.table_or_model == "post" and r.kind == "prisma" for r in refs)


def test_detect_prisma_multiple_ops():
    content = textwrap.dedent("""\
        user = prisma.user.find_unique(where={'id': 1})
        new_user = prisma.user.create(data={})
    """)
    refs = detect_schema_refs(content, "service.py")
    prisma_refs = [r for r in refs if r.kind == "prisma"]
    assert len(prisma_refs) == 2
    assert all(r.table_or_model == "user" for r in prisma_refs)


# ── detect_schema_refs: Raw SQL ─────────────────────────────

def test_detect_raw_sql_select():
    content = "cursor.execute('SELECT * FROM orders WHERE id = %s', [order_id])\n"
    refs = detect_schema_refs(content, "repo.py")
    raw = [r for r in refs if r.kind == "raw_sql"]
    assert any(r.table_or_model == "orders" for r in raw)


def test_detect_raw_sql_insert_into():
    content = "db.execute('INSERT INTO payments (amount) VALUES (%s)', [100])\n"
    refs = detect_schema_refs(content, "repo.py")
    raw = [r for r in refs if r.kind == "raw_sql"]
    assert any(r.table_or_model == "payments" for r in raw)


def test_detect_raw_sql_keywords_not_captured_as_tables():
    """SQL keywords like SET, FROM, WHERE must not be captured as table names."""
    content = "sql = 'UPDATE users SET name = %s WHERE id = %s'\n"
    refs = detect_schema_refs(content, "repo.py")
    table_names = {r.table_or_model for r in refs}
    assert "SET" not in table_names
    assert "WHERE" not in table_names
    assert "FROM" not in table_names


def test_detect_no_refs_plain_code():
    """Plain Python code without ORM or SQL yields no refs."""
    content = textwrap.dedent("""\
        def add(a, b):
            return a + b

        class Calculator:
            pass
    """)
    refs = detect_schema_refs(content, "calc.py")
    assert refs == []


# ── build_schema_refs integration ───────────────────────────

def test_build_schema_refs_populates_module(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "models.py").write_text(textwrap.dedent("""\
        class Order(models.Model):
            __tablename__ = 'orders'
    """))

    mod = ModuleInfo(name="models")
    mod.files = ["src/models.py"]
    facts = make_facts(mod)

    build_schema_refs(facts, tmp_path)

    assert len(mod.schema_refs) >= 1
    kinds = {r.kind for r in mod.schema_refs}
    assert "django" in kinds or "sqlalchemy" in kinds


def test_build_schema_refs_missing_file_skipped(tmp_path):
    """Files that cannot be read are silently skipped."""
    mod = ModuleInfo(name="ghost")
    mod.files = ["src/nonexistent.py"]
    facts = make_facts(mod)

    build_schema_refs(facts, tmp_path)

    assert mod.schema_refs == []


def test_build_schema_refs_multiple_modules(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "m1.py").write_text("class Foo(models.Model): pass\n")
    (src / "m2.py").write_text("cursor.execute('SELECT * FROM bar')\n")

    mod1 = ModuleInfo(name="m1")
    mod1.files = ["src/m1.py"]
    mod2 = ModuleInfo(name="m2")
    mod2.files = ["src/m2.py"]
    facts = make_facts(mod1, mod2)

    build_schema_refs(facts, tmp_path)

    assert any(r.kind == "django" for r in mod1.schema_refs)
    assert any(r.kind == "raw_sql" for r in mod2.schema_refs)
