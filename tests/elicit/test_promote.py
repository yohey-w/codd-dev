"""Stage-2 Axis-P Phase C1: promote CONFIRMED coverage decisions -> contracts.

These tests pin the owner-free rails:
  * red only via owner-CONFIRMED (ASK / RECOMMENDED_PROCEEDING / OVERRIDDEN
    never promote);
  * unknown gap kind -> left as amber residue (not promoted);
  * routing override repoints promotion;
  * idempotent (re-run never duplicates a contract);
  * end-to-end: a CONFIRMED missing_producer promotes a resource_contracts
    consumer that makes resource_flow_coherence go red — and the SAME design,
    before promotion, is NOT red (the gap was amber only).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.dag.builder import build_dag
from codd.dag.checks.resource_flow_coherence import ResourceFlowCoherenceCheck
from codd.elicit.promote import promote_confirmed_to_contract
from codd.frontmatter import parse_frontmatter
from codd.lexicon import AskItem


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_lexicon(root: Path, decisions: list[AskItem]) -> Path:
    from codd.lexicon import ask_item_to_dict

    payload = {
        "node_vocabulary": [],
        "naming_conventions": [],
        "design_principles": [],
        "coverage_decisions": [ask_item_to_dict(d) for d in decisions],
    }
    path = root / "codd" / "project_lexicon.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _confirmed_missing_producer(
    *,
    resource: str = "order_total",
    capability: str = "checkout",
    decision_id: str | None = None,
) -> AskItem:
    """A CONFIRMED missing_producer decision carrying a required-consumer payload.

    The ``gap_context`` is the data-driven contract shape the finding emitter
    would supply: it declares the required consumer that makes the missing
    producer a genuine red once promoted.
    """
    return AskItem(
        id=decision_id or f"axis_p.missing_producer.{resource}",
        question=f"Must {resource} be produced?",
        status="CONFIRMED",
        answer="yes_produce",
        gap_kind="missing_producer",
        gap_subject=resource,
        gap_context={
            "consumers": [
                {"capability": capability, "required": True, "on_missing": "fail"}
            ]
        },
    )


_CRITICAL_JOURNEY = {
    "name": "checkout_flow",
    "criticality": "critical",
    "required_capabilities": ["checkout"],
}


def _design_doc_with_optional_consumer(resource: str, capability: str) -> str:
    """A design doc that is NOT red before promotion.

    The capability consumes ``resource`` but as an OPTIONAL consumer
    (``required: false``), which resource_flow_coherence does not gate — so the
    missing producer is amber-only until an owner confirms it.
    """
    codd_block = {
        "node_id": "checkout_design",
        "user_journeys": [_CRITICAL_JOURNEY],
        "capability_contracts": [
            {
                "capability": capability,
                "consumes": [
                    {"resource": resource, "required": False, "on_missing": "skip"}
                ],
            }
        ],
    }
    frontmatter = yaml.safe_dump({"codd": codd_block}, sort_keys=False)
    return f"---\n{frontmatter}---\n# Checkout Design\n"


def _settings():
    return {
        "design_doc_patterns": ["docs/design/*.md"],
        "impl_file_patterns": ["src/**/*.py"],
        "lexicon_file": "project_lexicon.yaml",
    }


def _resource_flow(root: Path):
    dag = build_dag(root, _settings())
    return ResourceFlowCoherenceCheck(dag).run()


def _read_codd(doc_path: Path) -> dict:
    parsed = parse_frontmatter(doc_path.read_text(encoding="utf-8"))
    return parsed.mapping.get("codd", {})


# ---------------------------------------------------------------------------
# end-to-end: CONFIRMED missing_producer -> resource_contracts -> red
# ---------------------------------------------------------------------------

def test_before_promotion_missing_producer_is_not_red(tmp_path: Path) -> None:
    """The gap is amber-only before an owner confirms it: NOT red."""
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    assert doc.exists()
    result = _resource_flow(tmp_path)
    assert result.passed is True
    assert result.severity != "red"


def test_confirmed_missing_producer_promotes_and_goes_red(tmp_path: Path) -> None:
    """CONFIRMED missing_producer -> resource_contracts consumer -> RED."""
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    _write_lexicon(tmp_path, [_confirmed_missing_producer()])

    # sanity: not red before promotion
    assert _resource_flow(tmp_path).severity != "red"

    result = promote_confirmed_to_contract(tmp_path)
    assert result.changed is True
    assert any(p["contract_key"] == "resource_contracts" for p in result.promoted)

    # the promoted required consumer with no producer -> dangling -> red
    flow = _resource_flow(tmp_path)
    assert flow.passed is False
    assert flow.severity == "red"
    assert any(v["type"] == "dangling_required_consumer" for v in flow.violations)

    # the promoted entry is tagged for traceability
    codd = _read_codd(doc)
    promoted = codd.get("resource_contracts", [])
    assert any(e.get("source") == "axis_p_confirmed" for e in promoted)
    assert any(e.get("resource") == "order_total" for e in promoted)


# ---------------------------------------------------------------------------
# rails: only CONFIRMED promotes (anti-false-red)
# ---------------------------------------------------------------------------

def test_ask_status_is_not_promoted(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    decision = _confirmed_missing_producer()
    decision.status = "ASK"
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert result.promoted == []
    assert result.changed is False
    assert _resource_flow(tmp_path).severity != "red"


def test_recommended_proceeding_is_not_promoted(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    decision = _confirmed_missing_producer()
    decision.status = "RECOMMENDED_PROCEEDING"
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert result.promoted == []
    assert result.changed is False
    assert _resource_flow(tmp_path).severity != "red"


def test_overridden_is_not_promoted(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    decision = _confirmed_missing_producer()
    decision.status = "OVERRIDDEN"
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert result.promoted == []
    assert result.changed is False


# ---------------------------------------------------------------------------
# rails: unknown gap kind -> amber residue (not promoted)
# ---------------------------------------------------------------------------

def test_unknown_kind_is_not_promoted_and_left_amber(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    decision = AskItem(
        id="axis_p.some_unknown_kind.thing",
        question="?",
        status="CONFIRMED",
        gap_kind="some_unknown_kind",
        gap_subject="thing",
    )
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert result.promoted == []
    assert result.changed is False
    assert [s["id"] for s in result.skipped_unknown_kind] == [
        "axis_p.some_unknown_kind.thing"
    ]


# ---------------------------------------------------------------------------
# routing override -> promotes into a different key
# ---------------------------------------------------------------------------

def test_routing_override_promotes_to_different_key(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    _write_lexicon(tmp_path, [_confirmed_missing_producer()])

    config = {"axis_p": {"gap_routing": {"missing_producer*": ["runtime_constraints"]}}}
    result = promote_confirmed_to_contract(tmp_path, codd_config=config)

    assert any(p["contract_key"] == "runtime_constraints" for p in result.promoted)
    assert all(p["contract_key"] != "resource_contracts" for p in result.promoted)

    codd = _read_codd(doc)
    assert "runtime_constraints" in codd
    assert any(
        e.get("source") == "axis_p_confirmed" for e in codd["runtime_constraints"]
    )
    # not promoted into resource_contracts -> resource_flow stays non-red
    assert _resource_flow(tmp_path).severity != "red"


# ---------------------------------------------------------------------------
# idempotency: re-run does not duplicate contracts
# ---------------------------------------------------------------------------

def test_promotion_is_idempotent(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    _write_lexicon(tmp_path, [_confirmed_missing_producer()])

    first = promote_confirmed_to_contract(tmp_path)
    assert first.changed is True
    codd_after_first = _read_codd(doc)
    count_first = len(codd_after_first.get("resource_contracts", []))

    second = promote_confirmed_to_contract(tmp_path)
    assert second.changed is False  # nothing new to write
    codd_after_second = _read_codd(doc)
    count_second = len(codd_after_second.get("resource_contracts", []))

    assert count_first == count_second  # no duplicate entry


# ---------------------------------------------------------------------------
# backward compatibility: a decision without gap_kind/gap_subject fields
# (persisted before Phase C) still routes via the id encoding.
# ---------------------------------------------------------------------------

def test_legacy_decision_without_structured_fields_routes_via_id(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        _design_doc_with_optional_consumer("order_total", "checkout"),
    )
    # No gap_kind / gap_subject / gap_context — only the id carries identity.
    legacy = AskItem(
        id="axis_p.missing_producer.order_total",
        question="?",
        status="CONFIRMED",
    )
    _write_lexicon(tmp_path, [legacy])

    result = promote_confirmed_to_contract(tmp_path)
    assert any(p["contract_key"] == "resource_contracts" for p in result.promoted)
    codd = _read_codd(doc)
    assert any(
        e.get("resource") == "order_total"
        for e in codd.get("resource_contracts", [])
    )


# ---------------------------------------------------------------------------
# preservation: existing sibling contracts are not clobbered
# ---------------------------------------------------------------------------

def test_forbidden_kind_promotes_into_negative_space_mapping(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        "---\ncodd:\n  node_id: d\n---\n# C\n",
    )
    decision = AskItem(
        id="axis_p.forbidden_pattern.secret",
        question="?",
        status="CONFIRMED",
        gap_kind="forbidden_pattern",
        gap_subject="secret",
        gap_context={
            "scope": {"paths": ["logs/**"]},
            "patterns": [{"name": "s", "regex": "SECRET"}],
            "on_violation": "fail",
        },
    )
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert any(
        p["contract_key"] == "negative_space.forbidden_evidence"
        for p in result.promoted
    )
    codd = _read_codd(doc)
    forbidden = codd["negative_space"]["forbidden_evidence"]
    assert any(e.get("source") == "axis_p_confirmed" for e in forbidden)
    assert any(e.get("on_violation") == "fail" for e in forbidden)


def test_acceptance_signal_promotes_into_user_journeys_expected_outcomes(
    tmp_path: Path,
) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        "---\ncodd:\n  node_id: d\n---\n# C\n",
    )
    decision = AskItem(
        id="axis_p.acceptance_signal.login_ok",
        question="?",
        status="CONFIRMED",
        gap_kind="acceptance_signal",
        gap_subject="login_ok",
    )
    _write_lexicon(tmp_path, [decision])

    result = promote_confirmed_to_contract(tmp_path)
    assert any(
        p["contract_key"] == "user_journeys.expected_outcomes"
        for p in result.promoted
    )
    codd = _read_codd(doc)
    journeys = codd["user_journeys"]
    anchor = next(j for j in journeys if j.get("source") == "axis_p_confirmed")
    assert any(
        o.get("source") == "axis_p_confirmed" for o in anchor["expected_outcomes"]
    )


def test_non_codd_frontmatter_and_body_preserved(tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        "---\ntitle: My Title\ncodd:\n  node_id: d\n---\n# Heading\n\nBody text.\n",
    )
    _write_lexicon(tmp_path, [_confirmed_missing_producer()])

    promote_confirmed_to_contract(tmp_path)

    parsed = parse_frontmatter(doc.read_text(encoding="utf-8"))
    assert parsed.mapping.get("title") == "My Title"
    assert "Body text." in parsed.body


def test_existing_contracts_preserved(tmp_path: Path) -> None:
    codd_block = {
        "node_id": "checkout_design",
        "user_journeys": [_CRITICAL_JOURNEY],
        "resource_contracts": [
            {"resource": "pre_existing", "producers": [{"obligation": "seed"}]}
        ],
        "capability_contracts": [
            {
                "capability": "checkout",
                "consumes": [
                    {"resource": "order_total", "required": False, "on_missing": "skip"}
                ],
            }
        ],
    }
    doc = _write(
        tmp_path / "docs" / "design" / "checkout.md",
        f"---\n{yaml.safe_dump({'codd': codd_block}, sort_keys=False)}---\n# Checkout\n",
    )
    _write_lexicon(tmp_path, [_confirmed_missing_producer()])

    promote_confirmed_to_contract(tmp_path)

    codd = _read_codd(doc)
    resources = {e.get("resource") for e in codd.get("resource_contracts", [])}
    assert "pre_existing" in resources  # untouched
    assert "order_total" in resources  # promoted
