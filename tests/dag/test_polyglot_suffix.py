import logging
from pathlib import Path

import pytest
import yaml

import codd.dag.builder as builder
from codd.dag.builder import build_dag, load_dag_settings


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_codd_yaml(project: Path, payload: dict | None = None) -> None:
    config = {
        "scan": {
            "source_dirs": ["src/"],
            "test_dirs": ["tests/"],
            "doc_dirs": [],
        }
    }
    if payload:
        config.update(payload)
    _write(project / "codd" / "codd.yaml", yaml.safe_dump(config, sort_keys=False))


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("Cargo.toml", "rust"),
        ("Gemfile", "ruby"),
        ("package.json", "web"),
        ("go.mod", "go"),
        ("demo.csproj", "csharp"),
        ("demo.sln", "csharp"),
        ("CMakeLists.txt", "cpp_embedded"),
        ("build.gradle", "kotlin"),
        ("mix.exs", "elixir"),
        ("build.sbt", "scala"),
        ("Sources/App.swift", "swift"),
    ],
)
def test_detect_project_type_from_markers(tmp_path, marker, expected):
    _write(tmp_path / marker, "")

    assert builder._detect_project_type(tmp_path) == expected


def test_detect_project_type_makefile_with_c_file_returns_cpp_embedded(tmp_path):
    _write(tmp_path / "Makefile", "all:\n\tcc src/main.c\n")
    _write(tmp_path / "src" / "main.c", "int main(void) { return 0; }\n")

    assert builder._detect_project_type(tmp_path) == "cpp_embedded"


def test_detect_project_type_without_markers_returns_generic(tmp_path):
    assert builder._detect_project_type(tmp_path) == "generic"


def test_detect_project_type_uses_first_priority_match(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'demo'\n")
    _write(tmp_path / "package.json", "{}\n")

    assert builder._detect_project_type(tmp_path) == "rust"


def test_load_suffix_config_project_override_wins(tmp_path):
    suffixes = builder._load_suffix_config(
        tmp_path,
        {"implementation_suffixes": [".rs", ".py"], "test_suffixes": [".rs"]},
    )

    assert suffixes == ((".rs", ".py"), (".rs",))


def test_load_suffix_config_dag_section_override_wins(tmp_path):
    suffixes = builder._load_suffix_config(
        tmp_path,
        {"dag": {"implementation_suffixes": ["rb"], "test_suffixes": ["rb"]}},
    )

    assert suffixes == ((".rb",), (".rb",))


def test_load_suffix_config_detects_rust_defaults_without_codd_yaml(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'demo'\n")

    assert builder._load_suffix_config(tmp_path, {}) == ((".rs",), (".rs",))


def test_load_suffix_config_unknown_project_uses_generic_defaults(tmp_path):
    implementation_suffixes, test_suffixes = builder._load_suffix_config(tmp_path, {})

    assert ".rs" in implementation_suffixes
    assert ".cpp" in implementation_suffixes
    assert ".scala" in test_suffixes


def test_load_suffix_config_detects_web_defaults(tmp_path):
    _write(tmp_path / "package.json", "{}\n")

    implementation_suffixes, test_suffixes = builder._load_suffix_config(tmp_path, {})

    assert implementation_suffixes == (".ts", ".tsx", ".js", ".jsx")
    assert ".test.ts" in test_suffixes


def test_load_dag_settings_reads_web_yaml_suffix_section(tmp_path):
    _write(tmp_path / "package.json", "{}\n")

    settings = load_dag_settings(tmp_path)

    assert settings["project_type"] == "web"
    assert settings["implementation_suffixes"] == (".ts", ".tsx", ".js", ".jsx")


@pytest.mark.parametrize(
    "project_type",
    [
        "rust",
        "ruby",
        "csharp",
        "cpp_embedded",
        "kotlin",
        "elixir",
        "scala",
        "swift",
        "generic",
    ],
)
def test_polyglot_default_yaml_files_have_suffix_schema(project_type):
    path = builder.DEFAULTS_DIR / f"{project_type}.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert isinstance(payload["implementation_suffixes"], list)
    assert isinstance(payload["test_suffixes"], list)
    assert all(str(item).startswith(".") for item in payload["implementation_suffixes"])
    assert all(str(item).startswith(".") for item in payload["test_suffixes"])


@pytest.mark.parametrize(
    ("marker", "source", "node_id", "language"),
    [
        ("Cargo.toml", "src/lib.rs", "src/lib.rs", "rust"),
        ("Gemfile", "src/app.rb", "src/app.rb", "ruby"),
        ("demo.csproj", "src/Program.cs", "src/Program.cs", "csharp"),
    ],
)
def test_build_dag_extracts_polyglot_impl_files(tmp_path, marker, source, node_id, language):
    _write(tmp_path / marker, "")
    _write_codd_yaml(tmp_path)
    _write(tmp_path / source, "/* implementation */\n")

    dag = build_dag(tmp_path)

    assert dag.nodes[node_id].kind == "impl_file"
    assert dag.nodes[node_id].attributes["language"] == language


@pytest.mark.parametrize(
    ("marker", "source", "node_id", "language"),
    [
        ("package.json", "src/index.ts", "src/index.ts", "typescript"),
        ("go.mod", "src/main.go", "src/main.go", "go"),
        ("pyproject.toml", "src/app.py", "src/app.py", "python"),
    ],
)
def test_existing_typescript_go_python_suffixes_still_build(tmp_path, marker, source, node_id, language):
    _write(tmp_path / marker, "")
    _write_codd_yaml(tmp_path)
    _write(tmp_path / source, "print('ok')\n")

    dag = build_dag(tmp_path)

    assert dag.nodes[node_id].kind == "impl_file"
    assert dag.nodes[node_id].attributes["language"] == language


def test_codd_yaml_mixed_suffix_override_builds_multiple_languages(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'demo'\n")
    _write_codd_yaml(
        tmp_path,
        {
            "dag": {
                "implementation_suffixes": [".rs", ".py"],
                "test_suffixes": [".py"],
            }
        },
    )
    _write(tmp_path / "src" / "lib.rs", "pub fn ok() {}\n")
    _write(tmp_path / "src" / "tool.py", "def ok(): pass\n")

    dag = build_dag(tmp_path)

    assert "src/lib.rs" in dag.nodes
    assert "src/tool.py" in dag.nodes


def test_missing_default_yaml_falls_back_to_legacy_suffixes(monkeypatch, tmp_path, caplog):
    defaults_dir = tmp_path / "defaults"
    defaults_dir.mkdir()
    monkeypatch.setattr(builder, "DEFAULTS_DIR", defaults_dir)

    with caplog.at_level(logging.WARNING):
        suffixes = builder._load_suffix_config(tmp_path, {"project_type": "missing"})

    assert suffixes == (builder.LEGACY_IMPLEMENTATION_SUFFIXES, builder.LEGACY_TEST_SUFFIXES)
    assert "using legacy fallback" in caplog.text


def test_invalid_default_yaml_missing_implementation_suffixes_falls_back(monkeypatch, tmp_path, caplog):
    defaults_dir = tmp_path / "defaults"
    defaults_dir.mkdir()
    _write(defaults_dir / "broken.yaml", "test_suffixes: ['.broken']\n")
    monkeypatch.setattr(builder, "DEFAULTS_DIR", defaults_dir)

    with caplog.at_level(logging.WARNING):
        implementation_suffixes, test_suffixes = builder._load_suffix_config(tmp_path, {"project_type": "broken"})

    assert implementation_suffixes == builder.LEGACY_IMPLEMENTATION_SUFFIXES
    assert test_suffixes == (".broken",)
    assert "missing implementation_suffixes" in caplog.text


def test_scan_patterns_use_dynamic_suffixes_for_rust_project(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'demo'\n")
    _write_codd_yaml(tmp_path)

    settings = load_dag_settings(tmp_path)

    assert "src/**/*.rs" in settings["impl_file_patterns"]
    assert "tests/**/*.rs" in settings["test_file_patterns"]


def test_rust_cfg_test_file_is_not_classified_as_both_impl_and_test(tmp_path):
    _write(tmp_path / "Cargo.toml", "[package]\nname = 'demo'\n")
    _write_codd_yaml(tmp_path, {"scan": {"source_dirs": ["src/"], "test_dirs": ["src/"], "doc_dirs": []}})
    _write(
        tmp_path / "src" / "lib.rs",
        "pub fn add(a: i32, b: i32) -> i32 { a + b }\n"
        "#[cfg(test)]\n"
        "mod tests { }\n",
    )

    dag = build_dag(tmp_path)

    assert dag.nodes["src/lib.rs"].kind == "impl_file"
    assert [node for node in dag.nodes.values() if node.path == "src/lib.rs"] == [dag.nodes["src/lib.rs"]]


def test_package_json_web_project_keeps_existing_typescript_behavior(tmp_path):
    _write(tmp_path / "package.json", "{}\n")
    _write_codd_yaml(tmp_path)
    _write(tmp_path / "src" / "app" / "page.tsx", "export default function Page() { return null; }\n")

    dag = build_dag(tmp_path)

    assert dag.nodes["src/app/page.tsx"].kind == "impl_file"
    assert dag.nodes["src/app/page.tsx"].attributes["language"] == "typescript"
