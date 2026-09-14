# The acceptance-evidence invariant

## Why

A requirement shipped with a passing test suite, and the feature was still
wrong. Reconstructed: the requirement said "15 per sheet", the acceptance
document said 20, the code said 20; one test existed and it exercised a
command-line script, while the thing users actually pressed was a button on an
admin page that no test touched; the runtime verification stage was disabled, so
it was skipped without printing anything; and a manual "looks right" check from
two weeks earlier still read as evidence for an implementation that had since
been rewritten.

Four separate holes, one shape: **the acceptance criterion the customer signed
was never connected to anything a machine runs.** CoDD reconciled design against
implementation and test documents against test markers, but never the
acceptance criterion itself — and its coverage gate treated "this project
declares no verifiable behaviors" as a pass.

## The invariant

> For every requirement R and every acceptance criterion AC of R there is
> evidence E such that
>
> - **(a)** E is machine-executed — or, when it genuinely cannot be, an
>   owner-attributed `manual` record;
> - **(b)** E runs through the **shipped path** of R: the code reachable from
>   the entry point a user actually touches;
> - **(c)** E's checked values reference AC's **named parameters**, not a
>   re-typed literal;
> - **(d)** E is bound to the **content** of the implementation it verified, so
>   a changed implementation invalidates stale evidence.
>
> If any of the four is missing, verification is red. "Nothing is declared, so
> print a notice and pass" does not exist.

## Where it lives

| Piece | File |
|---|---|
| Parser + resolver (acceptance criteria, evidence refs, params) | `codd/acceptance_evidence.py` |
| Gate | `codd/dag/checks/acceptance_evidence.py` (registered check, runs in `codd verify`) |
| Manual-verdict ledger (invariant (d)) | `<codd-dir>/acceptance_ledger.json`, written by `codd acceptance record` |
| Runtime **execution** ledger (invariant (a)) | `<codd-dir>/runtime_ledger.json`, written by the runtime smoke runner |
| Configuration | `acceptance_evidence:` in `codd/defaults.yaml` |

## How a criterion is found (and why no project has to restructure)

Acceptance criteria are read from the project's **existing** requirement tables:
the same documents and the same in-scope rule `requirement_reconciliation`
already uses (a table that references an `operation_flow.<id>`, or a table under
a configured section heading). A table contributes criteria only when one of its
**columns** is an acceptance column — built-in English and Japanese header
vocabulary, extended (never replaced) by `acceptance_evidence.acceptance_columns`.

A table that lists requirements without ever stating how they are accepted
declares no criteria and is silent. That is deliberate: inventing an obligation
for every table row would flood a brownfield project and train its owners to
ignore the check.

The machine-readable declarations are **extra columns of that same table**:

```
| ID  | Requirement | Acceptance         | verified_by            | params        | critical |
| R-1 | QR sheet    | 15 names per sheet | test:qrSheet           | per_sheet=15  | true     |
```

`verified_by` is `test:<name>` / `runtime:<case>` / `manual:<owner>`, whitespace-
or comma-separated. `params` is `name=value` pairs. Both are optional: a project
that has not adopted them is still audited through the anchors it already has
(`operation_flow.<id>` references, requirement-id tokens in source, VB coverage
markers), and adopting a column makes its declaration authoritative for that row.

## Stages

| Stage | Invariant | Finding | Severity |
|---|---|---|---|
| 1a | (a) demotion | `runtime_evidence_not_executable` — the criterion declares a runtime obligation and `runtime_smoke` is not enabled, so the stage is skipped silently | red |
| 1b | (a) execution | `runtime_evidence_not_executed` — the stage is enabled but no run discharged the obligation: no ledger (`no_record`), a ledger written against a different runtime configuration (`config_changed`) or against a different target (`target_changed`), a ledger older than the configured window (`stale`), a run that failed (`run_failed`), or — for an explicitly declared `runtime:<case>` — a ledger in which no check of that name passed (`target_not_executed`) | amber |
| 2 | (a) wiring, (d) freshness | `vb_registry_missing`, `unbound_acceptance`, `unresolved_evidence`, `manual_evidence_missing`, `stale_manual_evidence` | red |
| 3 | (c) parameters | `param_not_referenced` (red), `undeclared_numeric` (amber) | red / amber |
| 4 | (b) shipped path | `off_shipped_path`, `multiple_implementers`, `reachability_unknown` | amber |

Stage 4 asks its question about **evidence**, not about every file that mentions
a requirement id. A requirement legitimately spans layers — a page, a library, a
migration — and a migration being unreachable from a page is architecture, not a
defect. What is a defect is proof that lives off the path that ships. Concretely,
the evidence for a criterion is the tests bound to it plus the tests of any file
that claims its id; if none of them, nor anything they exercise, appears in the
import closure of the requirement's entry point, that is `off_shipped_path`.

Stage 4 is amber on purpose. Reachability depends on per-language import
extraction, and dynamic imports / DI / reflection cannot always be followed; an
unfollowable edge is reported as `reachability_unknown` rather than guessed at
in either direction. It is never silent.

Entry points come from the requirement's own `operation_flow.<id>` reference:
the operation's `route:` resolved through the project's declared
`filesystem_routes`, or an explicit `entry_file:` on the operation, which always
wins over inference.

## A switch is a declaration, not evidence

Stage 1a asked whether the runtime stage *could* run. That left the mirror-image
hole: a project turns `runtime_smoke.enabled: true` on, never runs it, and the
criterion reads as bound to evidence that has never executed once. Measured on a
real project: flipping that one boolean, with no dev server and no
`codd verify --runtime`, moved `runtime_evidence_not_executable` 56 → 0 and
`unbound_acceptance` 57 → 19. The cheapest way to clear the gate was to declare,
not to verify — which is the incentive exactly backwards.

So **only an EXECUTED runtime obligation binds**, and the execution has to leave
a record a machine can read:

- the runtime smoke runner writes `<codd-dir>/runtime_ledger.json` when the
  stage is enabled **and at least one check actually ran** (an all-skipped run,
  or a disabled stage, writes nothing — a record of nothing is not a record);
- the ledger holds `recorded_at` (UTC), `passed`, the per-check
  `name` / `category` / `passed` / `skipped`, and `config_digest`, a hash of the
  project's own `runtime_smoke` + `runtime` configuration as it stood at run
  time. Change what the stage targets and the old run stops certifying it;
- a missing, malformed or unversioned ledger reads as **no evidence**, never as
  evidence — the same rule the manual ledger already follows, and so does a
  ledger recording no executed check at all, which a hand-written file could
  otherwise use to certify everything;
- the ledger also records the **target** the run went against (the effective
  dev-server URL, which `--runtime-base-url` may have overridden). A run aimed
  at a different deployment is not evidence about this one;
- a run that FAILED does not discharge anything. It is still *executed* — the
  finding says `run_failed`, not "never ran" — but a failing run recorded on
  Monday must not read as green evidence on Tuesday. Within the run itself Step
  8 reports the failure in red; the ledger is read by every LATER verification,
  and that is where "it ran, and it was broken" would otherwise pass for proof.
  A failure in one check does not sink another: an explicitly named
  `runtime:<case>` is discharged when the check answering to that name PASSED,
  whatever else in the same run did not.

Target matching follows the asymmetry the obligation itself is built on: an
**explicit** `verified_by: runtime:<case>` names a case, so a ledger with no
passing check of that `name` or `category` has not discharged it; an
**inferred** obligation (an `operation_flow.<id>` reference in a project that
never adopted the `verified_by` column) declares no such mapping, so a passing
run of the project's own stage satisfies it. CoDD does not invent a
correspondence the project never wrote down.

The digest is taken over the project's **own** `runtime_smoke` (minus its
`report:` block, which decides where output is written and not what is
exercised) and `runtime` sections, read from `codd.yaml` itself rather than from
the defaults-merged view — so upgrading CoDD, or moving a report file, does not
expire a run that still covers what the project targets.

Like the manual ledger, this file is a project artifact meant to be committed
and read in diffs: it is the standing answer to "when did the declared runtime
evidence last actually run, and against what configuration?". Unlike the manual
ledger it is written automatically — and that is consistent, not a contradiction:
a manual ledger records a *human verdict*, which nothing but a human may refresh,
while this one records *the fact that a machine ran*, which only the machine that
ran can honestly write.

## What stays outside

- `manual` evidence still needs a person. What is general is the machinery
  around them: enumerate the manual criteria, attribute an owner, expire the
  record when the implementation changes, and refuse to let a `critical`
  criterion ride on a manual record alone.
- (c) only bites on criteria that declare parameters. An undeclared literal is
  amber (`undeclared_numeric`) — a nudge to declare, not an accusation.
- Runtime execution evidence is **not yet bound to implementation content**, the
  way a manual record is. A recorded run expires when the runtime configuration
  changes (`config_digest`) or when `runtime_max_age_hours` says so, but a
  rewritten implementation under an unchanged configuration does not invalidate
  it. Closing that would need a content digest both the runner and the gate can
  compute, and the runner has no dependency graph; until then the honest
  statement is the narrow one: the ledger proves the stage RAN, not that it ran
  against today's code.
- `runtime_evidence_not_executed` is amber even under `mode: strict`
  (`runtime_execution_severity`). A project that never persisted the ledger is
  not thereby proven wrong — it is unproven — and upgrading CoDD must not turn
  an existing green build red on its own. The `unbound_acceptance` that
  accompanies it takes the SAME severity, for one criterion whose only declared
  binding is that unexecuted runtime obligation: the unbound-ness is *caused by*
  the not-executed-ness, one defect with one remedy ("run it"), and pricing it
  at `unbound_severity` would strand a strict project — red before Step 8 runs,
  and Step 8 is what writes the record that would clear it. When the stage is
  OFF the ordinary `unbound_severity` applies, because such a project has no
  runtime path to run at all.
- A ledger the runner could not write (a read-only checkout, a full disk) is a
  lost artifact, not a false green: the next verification honestly reports
  `no_record`. Step 8 says so loudly on stderr rather than failing the run.
