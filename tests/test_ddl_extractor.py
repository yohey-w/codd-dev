"""Schema extraction tests for SQL DDL and Prisma artifacts."""

import textwrap

from codd.extractor import extract_facts


def test_extract_facts_discovers_sql_ddl_schema(tmp_path):
    src = tmp_path / "src"
    db = tmp_path / "db"
    src.mkdir()
    db.mkdir()

    (src / "app.py").write_text("def main():\n    return None\n")
    (db / "schema.sql").write_text(
        textwrap.dedent(
            """\
            CREATE TABLE users (
              id INTEGER PRIMARY KEY,
              role_id INTEGER,
              CONSTRAINT fk_users_role FOREIGN KEY (role_id) REFERENCES roles(id)
            );

            CREATE INDEX idx_users_role ON users(role_id);
            CREATE VIEW active_users AS SELECT * FROM users;
            """
        )
    )

    facts = extract_facts(tmp_path, "python", ["src"])
    schema = facts.schemas["db/schema.sql"]

    assert [table["name"] for table in schema.tables] == ["users"]
    assert schema.tables[0]["columns"][0]["name"] == "id"
    assert schema.tables[0]["columns"][1]["name"] == "role_id"

    assert schema.foreign_keys == [
        {
            "name": "fk_users_role",
            "table": "users",
            "columns": ["role_id"],
            "references_table": "roles",
            "references_columns": ["id"],
        }
    ]
    assert schema.indexes[0]["name"] == "idx_users_role"
    assert schema.indexes[0]["table"] == "users"
    assert schema.indexes[0]["columns"] == ["role_id"]
    assert schema.views[0]["name"] == "active_users"


def test_extract_facts_discovers_prisma_schema(tmp_path):
    src = tmp_path / "src"
    prisma = tmp_path / "prisma"
    src.mkdir()
    prisma.mkdir()

    (src / "app.py").write_text("def main():\n    return None\n")
    (prisma / "schema.prisma").write_text(
        textwrap.dedent(
            """\
            model User {
              id    Int    @id @default(autoincrement())
              name  String
              posts Post[]
            }

            model Post {
              id       Int   @id @default(autoincrement())
              author   User  @relation(fields: [authorId], references: [id])
              authorId Int
            }

            enum Role {
              ADMIN
              USER
            }
            """
        )
    )

    facts = extract_facts(tmp_path, "python", ["src"])
    schema = facts.schemas["prisma/schema.prisma"]
    models = {model["name"]: model for model in schema.models}
    enums = {enum["name"]: enum for enum in schema.enums}

    assert {"User", "Post"} == set(models)
    assert [field["name"] for field in models["User"]["fields"]] == ["id", "name", "posts"]
    assert [field["name"] for field in models["User"]["relations"]] == ["posts"]
    assert [field["name"] for field in models["Post"]["relations"]] == ["author"]
    assert enums["Role"]["values"] == ["ADMIN", "USER"]
