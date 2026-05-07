from __future__ import annotations

from pathlib import Path

from codd.brownfield.pipeline import BrownfieldPipeline
from codd.elicit.finding import ElicitResult
from codd.extractor import ProjectFacts, synth_docs
from codd.parsing import PrismaSchemaInfo


class _NoopElicitEngine:
    def run(self, project_root: Path, lexicon_config=None) -> ElicitResult:
        return ElicitResult(findings=[])


def test_extract_synth_schema_design_template_renders(tmp_path: Path) -> None:
    facts = ProjectFacts(
        language="typescript",
        source_dirs=[],
        schemas={
            "prisma/schema.prisma": PrismaSchemaInfo(
                file_path="prisma/schema.prisma",
                enums=[{"name": "Status", "values": ["ACTIVE", "INACTIVE"]}],
            )
        },
    )

    output_dir = tmp_path / "docs" / "extracted"
    synth_docs(facts, output_dir)

    schema_doc = next((output_dir / "schemas").glob("*.md")).read_text(encoding="utf-8")
    assert "## Prisma Enums" in schema_doc
    assert "`Status`: ACTIVE, INACTIVE" in schema_doc


def test_brownfield_pipeline_succeeds_on_existing_project(tmp_path: Path) -> None:
    prisma_dir = tmp_path / "prisma"
    prisma_dir.mkdir()
    (prisma_dir / "schema.prisma").write_text(
        """
        enum Color {
          RED
          BLUE
        }

        model Item {
          id Int @id
          color Color
        }
        """,
        encoding="utf-8",
    )

    pipeline = BrownfieldPipeline(elicit_engine_factory=lambda: _NoopElicitEngine())
    result = pipeline.run(tmp_path)

    schema_doc = next((result.extract_output / "schemas").glob("*.md")).read_text(encoding="utf-8")
    assert result.extract_input is not None
    assert result.extract_input.exists()
    assert "`Color`: RED, BLUE" in schema_doc
