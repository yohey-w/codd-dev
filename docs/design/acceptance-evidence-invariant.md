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
| 1 | (a) demotion | `runtime_evidence_not_executable` — the criterion declares a runtime obligation and `runtime_smoke` is not enabled, so the stage is skipped silently | red |
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

## What stays outside

- `manual` evidence still needs a person. What is general is the machinery
  around them: enumerate the manual criteria, attribute an owner, expire the
  record when the implementation changes, and refuse to let a `critical`
  criterion ride on a manual record alone.
- (c) only bites on criteria that declare parameters. An undeclared literal is
  amber (`undeclared_numeric`) — a nudge to declare, not an accusation.
