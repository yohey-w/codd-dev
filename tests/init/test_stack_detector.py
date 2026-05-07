from __future__ import annotations

from pathlib import Path

from codd.init.stack_detector import StackDetector


FIXTURE = Path(__file__).parent / "fixtures" / "sample_react_fastapi_prisma"


def test_detect_sample_manifest_hints() -> None:
    detection = StackDetector().detect(FIXTURE)

    assert detection.detected_signals == ["package.json", "requirements.txt"]
    assert {"react", "fastapi", "prisma"}.issubset(detection.stack_hints)


def test_package_json_reads_dependency_sections(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        """\
{
  "dependencies": {"alpha-lib": "1.0.0"},
  "devDependencies": {"beta-tool": "1.0.0"},
  "peerDependencies": {"gamma-peer": "1.0.0"},
  "optionalDependencies": {"delta-optional": "1.0.0"}
}
""",
        encoding="utf-8",
    )

    detection = StackDetector().detect(tmp_path)

    assert detection.stack_hints == ["alpha-lib", "beta-tool", "gamma-peer", "delta-optional"]


def test_requirements_txt_extracts_package_names(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        """\
# ignored
Alpha_Pkg[extra]>=1.0
-r base.txt
git+https://example.invalid/repo.git#egg=BetaPkg
""",
        encoding="utf-8",
    )

    detection = StackDetector().detect(tmp_path)

    assert detection.stack_hints == ["alpha_pkg", "betapkg"]


def test_pyproject_toml_extracts_standard_dependency_groups(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """\
[project]
dependencies = ["alpha-one>=1.0"]

[project.optional-dependencies]
dev = ["beta-two==2.0"]

[tool.poetry.dependencies]
python = "^3.10"
gamma-three = "^3.0"

[dependency-groups]
test = ["delta-four"]
""",
        encoding="utf-8",
    )

    detection = StackDetector().detect(tmp_path)

    assert detection.stack_hints == ["alpha-one", "beta-two", "gamma-three", "delta-four"]


def test_polyglot_manifests_extract_generic_hints(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text(
        "module example.invalid/app\nrequire example.invalid/alpha v1.0.0\n",
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text(
        "[dependencies]\nbeta = \"1\"\n[dev-dependencies]\ngamma = \"1\"\n",
        encoding="utf-8",
    )
    (tmp_path / "Gemfile").write_text("gem 'delta'\n", encoding="utf-8")
    (tmp_path / "pom.xml").write_text("<artifactId>epsilon</artifactId>\n", encoding="utf-8")
    (tmp_path / "composer.json").write_text('{"require": {"zeta/pkg": "*"}}\n', encoding="utf-8")

    detection = StackDetector().detect(tmp_path)

    assert "example.invalid/alpha" in detection.stack_hints
    assert {"beta", "gamma", "delta", "epsilon", "zeta/pkg"}.issubset(detection.stack_hints)


def test_detection_deduplicates_hints_preserving_first_occurrence(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"Alpha": "1"}, "devDependencies": {"alpha": "1", "Beta": "1"}}',
        encoding="utf-8",
    )

    detection = StackDetector().detect(tmp_path)

    assert detection.stack_hints == ["alpha", "beta"]


def test_generality_gate_has_no_stack_product_literals() -> None:
    forbidden = [
        "react",
        "fastapi",
        "django",
        "flask",
        "express",
        "hono",
        "nestjs",
        "prisma",
        "sqlalchemy",
        "typeorm",
        "mongoose",
        "kubernetes",
        "helm",
    ]
    source = (Path(__file__).parents[2] / "codd" / "init" / "stack_detector.py").read_text(encoding="utf-8").lower()

    assert [term for term in forbidden if term in source] == []

