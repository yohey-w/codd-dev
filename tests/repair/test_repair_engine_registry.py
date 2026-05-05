from __future__ import annotations

import pytest

from codd.dag import DAG
from codd.repair import engine
from codd.repair.engine import RepairEngine, get_registry, get_repair_engine, list_repair_engines, register_repair_engine
from codd.repair.schema import ApplyResult, RepairProposal, RootCauseAnalysis, VerificationFailureReport


class DummyRepairEngine(RepairEngine):
    def analyze(self, failure: VerificationFailureReport, dag: DAG) -> RootCauseAnalysis:
        return RootCauseAnalysis("cause", [], "unified_diff", 0.8, "2026-05-06T00:00:00Z")

    def propose_fix(self, rca: RootCauseAnalysis, file_contents: dict[str, str]) -> RepairProposal:
        return RepairProposal([], "rationale", 0.8, "2026-05-06T00:00:01Z", rca.analysis_timestamp)

    def apply(self, proposal: RepairProposal, *, dry_run: bool = False) -> ApplyResult:
        return ApplyResult(True, [], [], None)


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(engine, "_REPAIR_ENGINES", {})


def test_register_repair_engine_decorator_registers_class():
    registered = register_repair_engine("dummy")(DummyRepairEngine)

    assert registered is DummyRepairEngine
    assert get_repair_engine("dummy") is DummyRepairEngine
    assert DummyRepairEngine.engine_name == "dummy"


def test_register_repair_engine_rejects_duplicate_name():
    register_repair_engine("duplicate")(DummyRepairEngine)

    with pytest.raises(KeyError, match="already registered"):
        register_repair_engine("duplicate")(DummyRepairEngine)


def test_get_repair_engine_unknown_name_raises_clear_key_error():
    with pytest.raises(KeyError, match="unknown repair engine: missing"):
        get_repair_engine("missing")


def test_registry_accessors_are_deterministic_and_copy_registry():
    register_repair_engine("zeta")(DummyRepairEngine)
    register_repair_engine("alpha")(type("OtherRepairEngine", (DummyRepairEngine,), {}))

    registry = get_registry()
    registry["mutated"] = DummyRepairEngine

    assert list_repair_engines() == ["alpha", "zeta"]
    assert "mutated" not in get_registry()


def test_register_repair_engine_rejects_empty_name():
    with pytest.raises(ValueError, match="non-empty"):
        register_repair_engine("   ")
