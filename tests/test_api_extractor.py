"""Tests for API definition extraction backends."""

import textwrap

from codd.extractor import extract_facts


def _seed_project(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("def main():\n    return 'ok'\n")
    return tmp_path


def test_extracts_openapi_specs(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "openapi.yaml").write_text(
        textwrap.dedent(
            """\
            openapi: 3.0.0
            servers:
              - url: https://api.example.com
            paths:
              /users:
                get:
                  operationId: listUsers
                  summary: List users
                  responses:
                    "200":
                      description: ok
                post:
                  operationId: createUser
                  requestBody:
                    content:
                      application/json: {}
                  responses:
                    "201":
                      description: created
            components:
              schemas:
                User:
                  type: object
                  required: [id]
                  properties:
                    id:
                      type: string
                    email:
                      type: string
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    assert "openapi.yaml" in facts.api_specs
    spec = facts.api_specs["openapi.yaml"]
    assert spec.format == "openapi"
    assert {(endpoint["path"], endpoint["method"]) for endpoint in spec.endpoints} == {
        ("/users", "GET"),
        ("/users", "POST"),
    }
    assert spec.schemas[0]["name"] == "User"
    assert spec.services[0]["url"] == "https://api.example.com"


def test_extracts_graphql_specs(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "schema.graphql").write_text(
        textwrap.dedent(
            """\
            type Query {
              user(id: ID!): User
            }

            type Mutation {
              createUser(input: CreateUserInput!): User!
            }

            type User {
              id: ID!
              email: String!
            }

            input CreateUserInput {
              email: String!
            }
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    assert "schema.graphql" in facts.api_specs
    spec = facts.api_specs["schema.graphql"]
    assert spec.format == "graphql"
    assert {(endpoint["operation_type"], endpoint["name"]) for endpoint in spec.endpoints} == {
        ("query", "user"),
        ("mutation", "createUser"),
    }
    assert ("type", "User") in {(schema["kind"], schema["name"]) for schema in spec.schemas}
    assert ("input", "CreateUserInput") in {
        (schema["kind"], schema["name"]) for schema in spec.schemas
    }


def test_extracts_protobuf_specs(tmp_path):
    project_root = _seed_project(tmp_path)
    (project_root / "api.proto").write_text(
        textwrap.dedent(
            """\
            syntax = "proto3";

            message GetUserRequest {
              string user_id = 1;
            }

            message UserReply {
              string user_id = 1;
              string email = 2;
            }

            enum UserStatus {
              USER_STATUS_UNKNOWN = 0;
              USER_STATUS_ACTIVE = 1;
            }

            service UserService {
              rpc GetUser(GetUserRequest) returns (UserReply);
            }
            """
        )
    )

    facts = extract_facts(project_root, "python", ["src"])

    assert "api.proto" in facts.api_specs
    spec = facts.api_specs["api.proto"]
    assert spec.format == "protobuf"
    assert ("message", "GetUserRequest") in {
        (schema["kind"], schema["name"]) for schema in spec.schemas
    }
    assert ("enum", "UserStatus") in {(schema["kind"], schema["name"]) for schema in spec.schemas}
    assert spec.services[0]["name"] == "UserService"
    assert spec.endpoints[0]["name"] == "GetUser"


def test_skips_projects_without_api_specs(tmp_path):
    project_root = _seed_project(tmp_path)

    facts = extract_facts(project_root, "python", ["src"])

    assert facts.api_specs == {}
