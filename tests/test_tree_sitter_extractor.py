"""Tree-sitter backed extractor tests for Python and TypeScript."""

import textwrap

from codd.extractor import extract_facts
from codd.parsing import RegexExtractor, TreeSitterExtractor, get_extractor


def test_python_tree_sitter_extracts_multiline_signature_and_decorators(tmp_path):
    src = tmp_path / "src"
    (src / "api").mkdir(parents=True)
    (src / "services").mkdir(parents=True)
    (src / "services" / "__init__.py").write_text("", encoding="utf-8")
    (src / "services" / "auth.py").write_text("class AuthService:\n    pass\n", encoding="utf-8")
    (src / "api" / "__init__.py").write_text("", encoding="utf-8")
    (src / "api" / "routes.py").write_text(
        textwrap.dedent(
            """\
            from services.auth import (
                AuthService,
            )

            class Outer(Base):
                class Inner(Model):
                    pass

            @router.get("/health")
            async def health(
                user_id: int,
                include_meta: bool = False,
            ) -> dict[str, str]:
                return {"status": "ok"}
            """
        )
    )

    facts = extract_facts(tmp_path, "python", ["src"])
    api = facts.modules["api"]
    symbols = {symbol.name: symbol for symbol in api.symbols}

    assert "Outer" in symbols
    assert symbols["Outer"].bases == ["Base"]
    assert "Inner" in symbols
    assert symbols["Inner"].bases == ["Model"]

    health = symbols["health"]
    assert "user_id: int" in health.params
    assert "include_meta: bool = False" in health.params
    assert health.return_type == "dict[str, str]"
    assert health.decorators == ['router.get("/health")']
    assert health.is_async is True

    assert "services" in api.internal_imports
    assert "/health" in api.patterns["api_routes"]
    assert "Outer" in api.patterns["db_models"]


def test_typescript_tree_sitter_extracts_interfaces_aliases_and_reexports(tmp_path):
    src = tmp_path / "src"
    (src / "auth").mkdir(parents=True)
    (src / "shared").mkdir(parents=True)
    (src / "shared" / "mod.ts").write_text("export const marker = true\n")
    (src / "shared" / "reexports.ts").write_text("export const other = true\n")
    (src / "auth" / "index.ts").write_text(
        textwrap.dedent(
            """\
            import Foo, { Bar as Baz, type Quux } from "../shared/mod"
            export { X as Y } from "../shared/reexports"

            export interface Reader extends BaseReader {
              value: string
            }

            export type Result<T> = Promise<T>

            export enum Status {
              Ready = "ready",
            }

            export const run = async (value: string): Promise<void> => {}

            export class Repo extends BaseEntity implements Reader, Writer {}
            """
        )
    )

    facts = extract_facts(tmp_path, "typescript", ["src"])
    auth = facts.modules["auth"]
    symbols = {symbol.name: symbol for symbol in auth.symbols}

    assert symbols["Reader"].kind == "interface"
    assert symbols["Reader"].bases == ["BaseReader"]
    assert symbols["Result"].kind == "type_alias"
    assert symbols["Status"].kind == "enum"
    assert symbols["run"].kind == "function"
    assert symbols["run"].is_async is True
    assert symbols["run"].return_type == "Promise<void>"
    assert symbols["Repo"].bases == ["BaseEntity"]
    assert symbols["Repo"].implements == ["Reader", "Writer"]

    assert "shared" in auth.internal_imports
    assert "Repo" in auth.patterns["db_models"]


def test_typescript_tree_sitter_extracts_const_objects(tmp_path):
    src = tmp_path / "src"
    (src / "config").mkdir(parents=True)
    (src / "config" / "index.ts").write_text(
        textwrap.dedent(
            """\
            export type ProviderCaps = {
              family: string;
              hints: string[];
            };

            const DEFAULT_CAPS: ProviderCaps = {
              family: "default",
              hints: [],
            };

            const PROVIDER_FALLBACKS: Record<string, Partial<ProviderCaps>> = {
              anthropic: {
                family: "anthropic",
                hints: ["claude"],
              },
              mistral: {
                hints: ["mistral", "mixtral"],
              },
              moonshot: {
                family: "moonshot",
              },
            };

            export const TIMEOUT_MS = 5000;

            export const ALLOWED_MODELS = [
              "gpt-4",
              "claude-3",
              "gemini-pro",
            ];

            export const STATUS_MAP = {
              ready: 1,
              pending: 2,
              error: 3,
            } as const;

            export function resolve(provider: string): ProviderCaps {
              return { ...DEFAULT_CAPS, ...PROVIDER_FALLBACKS[provider] };
            }
            """
        )
    )

    facts = extract_facts(tmp_path, "typescript", ["src"])
    config = facts.modules["config"]
    symbols = {symbol.name: symbol for symbol in config.symbols}

    # Const objects captured
    assert symbols["DEFAULT_CAPS"].kind == "const_object"
    assert symbols["DEFAULT_CAPS"].return_type == "ProviderCaps"
    assert "family" in symbols["DEFAULT_CAPS"].params
    assert "hints" in symbols["DEFAULT_CAPS"].params

    assert symbols["PROVIDER_FALLBACKS"].kind == "const_object"
    assert "Record<string, Partial<ProviderCaps>>" in symbols["PROVIDER_FALLBACKS"].return_type
    assert "anthropic" in symbols["PROVIDER_FALLBACKS"].params
    assert "mistral" in symbols["PROVIDER_FALLBACKS"].params
    assert "moonshot" in symbols["PROVIDER_FALLBACKS"].params

    # Array captured
    assert symbols["ALLOWED_MODELS"].kind == "const_object"
    assert "3" in symbols["ALLOWED_MODELS"].params  # [3] elements

    # as const unwrapped
    assert symbols["STATUS_MAP"].kind == "const_object"
    assert "ready" in symbols["STATUS_MAP"].params

    # Regular function still works
    assert symbols["resolve"].kind == "function"

    # Scalar const (TIMEOUT_MS = 5000) is NOT captured — no object/array value
    assert "TIMEOUT_MS" not in symbols


def test_extract_facts_falls_back_to_regex_when_tree_sitter_is_unavailable(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "simple.py").write_text(
        textwrap.dedent(
            """\
            class SimpleService:
                pass

            def work(value: str) -> str:
                return value
            """
        )
    )

    monkeypatch.setattr(
        TreeSitterExtractor,
        "is_available",
        classmethod(lambda cls, language=None: False),
    )

    facts = extract_facts(tmp_path, "python", ["src"])
    module = facts.modules["simple"]

    assert isinstance(get_extractor("python"), RegexExtractor)
    assert {symbol.name for symbol in module.symbols} == {"SimpleService", "work"}
