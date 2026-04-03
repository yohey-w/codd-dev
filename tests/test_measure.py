"""Tests for codd measure — project metrics collection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from codd.measure import (
    CoverageMetrics,
    GraphMetrics,
    MeasureResult,
    QualityMetrics,
    format_measure_json,
    format_measure_text,
    run_measure,
)


class TestGraphMetrics:
    def test_connectivity_empty(self):
        g = GraphMetrics(total_nodes=0, total_edges=0)
        assert g.connectivity == 0.0

    def test_connectivity_single_node(self):
        g = GraphMetrics(total_nodes=1, total_edges=0)
        assert g.connectivity == 0.0

    def test_connectivity_partial(self):
        g = GraphMetrics(total_nodes=3, total_edges=3)
        # max_edges = 3 * 2 = 6, connectivity = 3/6 = 0.5
        assert g.connectivity == 0.5


class TestCoverageMetrics:
    def test_coverage_ratio_no_files(self):
        c = CoverageMetrics(tracked_files=0, source_files=0)
        assert c.coverage_ratio == 0.0

    def test_coverage_ratio_partial(self):
        c = CoverageMetrics(tracked_files=3, source_files=10)
        assert c.coverage_ratio == 0.3

    def test_coverage_ratio_full(self):
        c = CoverageMetrics(tracked_files=10, source_files=10)
        assert c.coverage_ratio == 1.0


class TestHealthScore:
    def test_perfect_health(self):
        result = MeasureResult()
        assert result.health_score == 100

    def test_validation_errors_reduce_score(self):
        result = MeasureResult(quality=QualityMetrics(validation_errors=2))
        assert result.health_score == 80  # -10 * 2

    def test_policy_critical_reduce_score(self):
        result = MeasureResult(quality=QualityMetrics(policy_critical=1))
        assert result.health_score == 85  # -15 * 1

    def test_combined_deductions(self):
        result = MeasureResult(
            quality=QualityMetrics(validation_errors=1, policy_warnings=2),
        )
        # 100 - 10 - 6 = 84
        assert result.health_score == 84

    def test_score_floor_zero(self):
        result = MeasureResult(
            quality=QualityMetrics(validation_errors=10, policy_critical=5),
        )
        assert result.health_score == 0

    def test_coverage_bonus(self):
        result = MeasureResult(
            coverage=CoverageMetrics(tracked_files=9, source_files=10),
        )
        # 100 + 5 (bonus for >80% coverage) = 100 (capped)
        assert result.health_score == 100

    def test_orphan_deduction(self):
        result = MeasureResult(
            graph=GraphMetrics(total_nodes=10, orphan_nodes=5),
        )
        # orphan_ratio = 0.5, deduction = int(0.5 * 20) = 10
        assert result.health_score == 90


class TestRunMeasure:
    def test_basic_project(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        src = project / "src"
        src.mkdir()
        (src / "main.py").write_text("import os\n", encoding="utf-8")
        (src / "util.py").write_text("x = 1\n", encoding="utf-8")

        codd_dir = project / "codd"
        codd_dir.mkdir()
        config = {
            "scan": {
                "source_dirs": ["src/"],
                "test_dirs": [],
                "doc_dirs": [],
                "config_files": [],
                "exclude": [],
            },
            "policies": [],
        }
        (codd_dir / "codd.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

        result = run_measure(project)
        assert result.coverage.source_files == 2
        assert result.quality.validation_errors == 0
        assert result.health_score > 0

    def test_no_codd_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run_measure(tmp_path)


class TestFormatText:
    def test_contains_health_score(self):
        result = MeasureResult()
        text = format_measure_text(result)
        assert "Health Score: 100/100" in text

    def test_contains_sections(self):
        result = MeasureResult(
            graph=GraphMetrics(total_nodes=5, total_edges=8, orphan_nodes=1, max_depth=3, avg_out_degree=1.6),
            coverage=CoverageMetrics(tracked_files=3, source_files=10, design_documents=5),
            quality=QualityMetrics(
                validation_errors=0, validation_warnings=1,
                policy_critical=0, policy_warnings=2,
                documents_checked=5, files_policy_checked=10, rules_applied=3,
            ),
        )
        text = format_measure_text(result)
        assert "Graph:" in text
        assert "5 nodes" in text
        assert "Coverage:" in text
        assert "3/10" in text
        assert "Quality:" in text
        assert "2 warnings" in text


class TestFormatJson:
    def test_valid_json(self):
        result = MeasureResult(
            graph=GraphMetrics(total_nodes=5, total_edges=8),
            coverage=CoverageMetrics(tracked_files=3, source_files=10, design_documents=5),
            quality=QualityMetrics(validation_errors=1, policy_warnings=2),
        )
        raw = format_measure_json(result)
        data = json.loads(raw)
        assert "health_score" in data
        assert data["graph"]["total_nodes"] == 5
        assert data["coverage"]["coverage_ratio"] == 0.3
        assert data["quality"]["validation_errors"] == 1
