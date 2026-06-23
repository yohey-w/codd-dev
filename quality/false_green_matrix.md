# False-green corpus — detection matrix & saturation status

Measurement record for the `/goal` verifier-hardening loop. Re-measure with:

```
python3 -m pytest tests/dag/test_resource_flow_coherence.py tests/test_dag_verify_cli.py -q
```

Last measured: 2026-06-24 → **29 passed** (kill-rate evidence below).
Detection legend: `caught_red` / `caught_amber` / `missed_green` / `n/a`.

| vector | pri | owner_gate | status | detection | 4-fixture | check / fixtures |
|---|---|---|---|---|---|---|
| resource_supply_use_dangling | P0 | no | shipped v3.3.0 | caught_red | ✅ | resource_flow_coherence / test_resource_flow_coherence.py |
| produced_never_consumed | P1 | no | shipped v3.4.0 | caught_amber | ✅ | resource_flow_coherence / test_resource_flow_coherence.py |
| check_selection_drift | P0 | no | covered | caught_amber | ✅ | unselected_check_names / test_dag_verify_cli.py |
| resource_order_explicit_flow | P0 | consult | memo | missed_green | — | decision_memos |
| extractor_silent_noop | P0 | consult | memo | missed_green | — | decision_memos |
| identity_alias_drift | P1 | consult | memo | missed_green | — | decision_memos |
| assertion_abuse | P0 | owner | memo | missed_green | — | decision_memos |
| cross_artifact_partial_coverage | P0 | owner | memo | missed_green | — | decision_memos |
| cardinality_partial | P1 | owner | memo | missed_green | — | decision_memos |
| produced... (semantic_conflict) | P1 | owner | memo | n/a | — | decision_memos |
| negative_space | P1 | owner | memo | n/a | — | decision_memos |
| stale_evidence | P2 | owner | memo | — | — | decision_memos |
| diagnostic_incompleteness | P2 | no | planned | — | — | (low-risk, queued) |

## Saturation condition (per /goal) — current evaluation

Condition: *P0/P1 non-owner-gated に 4-fixture 完備 かつ kill率 P0 100% / P1 95%+ かつ control/legacy 偽red 0 かつ 2連続で新規 missed_green 無し*.

P0/P1 **non-owner-gated** set = { resource_supply_use_dangling (P0), check_selection_drift (P0), produced_never_consumed (P1) }.

| criterion | status |
|---|---|
| 4-fixture complete (non-owner-gated P0/P1) | ✅ 3/3 |
| kill rate P0 100% | ✅ (2/2 P0 candidates caught: red + amber) |
| kill rate P1 ≥95% | ✅ (1/1 P1 candidate caught) |
| control/legacy false-red = 0 | ✅ (all guard/control/legacy fixtures pass) |
| 2 consecutive discovery rounds, no new missed_green | ⏳ pending — needs discovery rounds |

**→ The non-owner-gated subset is materially saturated** (fixtures, kill rate, false-red). The only open sub-criterion is the *exploration* one (2 clean discovery rounds).

## Autonomous ceiling (honest)

**9 of 13 vectors are owner-gated or consult-needed** (see `false_green_decision_memos.md`). Per the /goal's own boundary, those are NOT auto-implemented — they need an owner decision (new meaning/severity) or a GPT-Pro design session (new mechanism). Discovering *new* non-owner-gated vectors is also a creative step best done with GPT/owner.

So the autonomous loop has reached its productive ceiling: every cleanly-derivable non-owner-gated vector is covered and measured. Further hardening = the owner + GPT session, using the decision memos.
