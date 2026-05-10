# Post-mortem — Opt-out Loophole (CoDD C8 ci_health)

Author: gunshi (軍師)
Date: 2026-05-10
Source: cmd_464 Phase A
Status: Root cause identified, design doc separate (`queue/reports/gunshi_464_optout_protection_design.yaml`)

---

## TL;DR

CoDD v2.12.0 shipped C7/C8 test-completeness gates (cmd_462), but C8
`ci_health` accepts a one-line config flag — `ci.provider: none` — that turns
the gate into a silent **PASS**. No justification is required, no expiry is
recorded, the result severity is **demoted to `info`**, and `block_deploy`
becomes **False**. A project can therefore opt out of every C8 finding without
review and without ever surfacing the fact in a report.

A weaker but structurally identical loophole exists at the **defaults layer**:
when `codd.yaml` simply omits the `ci:` section, the loader synthesises
`CiConfig(provider="none")`, so a project that *forgets* to configure CI is
indistinguishable from a project that *deliberately* opted out.

The defect is generic. It applies to any future check that introduces a
"provider=none / disabled=true" escape hatch. The fix must therefore live in
the DAG-check core, not in `ci_health.py` alone.

The C7 (`user_journey_coherence`), C6 (`deployment_completeness`) and C9
(`environment_coverage`) checks have natural skip paths ("no actors / no
deployment signal / no axes declared"), which are **not** the same defect:
those skips reflect "subject not present in DAG", not "user actively disabled
the gate". They are addressed separately by cmd_462's "actors_without_journeys"
rule and are out of scope here, but the new policy must distinguish the two
shapes so we do not accidentally re-introduce silent skips elsewhere.

---

## Defect — Silent Opt-out via Configuration

### What the user sees

```yaml
# codd.yaml
ci:
  provider: none
```

```
$ codd dag verify
  PASS  ci_health [info]
0 check(s) FAILED (severity=red)
```

A red-severity, deploy-blocking gate has been entirely disabled with one line,
and the verifier reports the result as `PASS [info]` indistinguishable from a
green project. There is no record of who approved the opt-out, no required
reason, and no expiry.

### Direct evidence (CoDD core)

`codd/dag/checks/ci_health.py:92-99`:

```python
def check(self, project_root: Path, config: CiConfig) -> CiHealthResult:
    project_root = Path(project_root).resolve()
    if config.provider.strip().lower() == "none":
        return CiHealthResult(
            status="skip",
            message="ci.provider=none, C8 SKIP",
            passed=True,
        )
```

Three properties of this result conspire to hide the opt-out:

| Property            | Value when opted out  | Result downstream                                  |
|---------------------|-----------------------|----------------------------------------------------|
| `status`            | `"skip"`              | Counted as "passing" by `CheckResult.__post_init__` |
| `severity`          | `"info"` (default)    | Excluded from amber/red aggregations               |
| `block_deploy`      | `False` (default)     | `dag verify` exit code 0; `deploy` does not block  |
| `passed`            | `True`                | Reported as PASS in CLI text output                |

`codd/dag/checks/__init__.py:24-27` confirms `"skip"` is treated as a passing
state by the generic `CheckResult` factory:

```python
def __post_init__(self) -> None:
    if self.passed is None:
        self.passed = self.status.lower() in {"pass", "passed", "ok", "skip", "skipped"}
```

The `dag verify` CLI in `codd/cli.py:4904-4933` and the deploy gate in
`codd/deployer.py:543-583` both consume `passed` / `severity` / `block_deploy`
directly, so the opt-out is invisible to every consumer.

### Defaults-layer escape hatch

`codd/dag/checks/ci_health.py:30-44`:

```python
@classmethod
def from_mapping(cls, value: Mapping[str, Any] | None) -> "CiConfig":
    if not isinstance(value, Mapping):
        return cls(provider="none")
    ...
```

A project whose `codd.yaml` simply has no `ci:` mapping — including, for
example, the case where the user copied a stripped template — receives
`provider="none"` by default, falling into the same SKIP path as a deliberate
opt-out. The loader cannot distinguish "forgot to configure" from
"intentionally disabled".

### Why this matters for CoDD's North Star

CoDD's promise is **integrity-driven coherence**: the DAG either holds or it
does not. A `red` check that any project can demote to `info` with one line of
config is not a check; it is a suggestion. Once any single gate is treated as
optional-by-flag, the same precedent legitimises future gates (PCI compliance,
schema-evolution rules, security policies) being silenced the same way. The
loophole is therefore not local to C8 — it is precedential.

The damage is greatest precisely where CoDD itself dogfoods: a project that
hosts a CoDD-driven application, opts out of CI verification, and then deploys
to production has bypassed the integrity contract that justified adopting
CoDD.

---

## Inventory — Where else does this pattern hide?

| Check                     | Skip path?                                                    | Shape           | Verdict |
|---------------------------|---------------------------------------------------------------|-----------------|---------|
| C8 `ci_health`            | `ci.provider == "none"` → `passed=True, severity=info`        | **explicit opt-out** | Defect (this post-mortem) |
| C7 `user_journey_coherence` | No actors and no journeys declared → `passed=True`          | subject-absent  | Distinct defect, partially addressed by cmd_462 (`actors_without_journeys` amber) |
| C6 `deployment_completeness` | `_has_deployment_signal()` False → empty `violations`       | subject-absent  | Acceptable for now (no opt-out flag exists), but vulnerable if a flag is ever added |
| C9 `environment_coverage` | No `coverage_axes` declared → `passed=True`                   | subject-absent  | Acceptable for now |
| Generic                   | Any future check copying the C8 pattern                       | **explicit opt-out** | Latent risk |

The two shapes must be policed differently:

- **subject-absent** ("no journeys, no axes, no deployment signal"): the DAG
  genuinely lacks the subject the check would assess. The right pressure is to
  *encourage declaration* (cmd_462's amber `actors_without_journeys`), not to
  refuse to run.
- **explicit opt-out** ("user wrote `provider: none`"): the user actively
  asked the gate to stand down. The right pressure is to *demand a reason and
  an expiry*, and to keep the gate visible in reports.

This post-mortem proposes a fix only for the second shape. The first remains
the responsibility of each check's own declaration-coverage rules.

---

## Abuse scenarios

1. **One-line gate bypass.** A team adds `ci.provider: none` while triaging an
   unrelated CI failure, intending to revert later. The flag persists for
   months because nothing reminds anyone it is set, and no report ever flags
   the project as having a disabled gate.
2. **Stripped-template inheritance.** A new project is bootstrapped from a
   template that omits the `ci:` section. The loader silently synthesises
   `provider="none"`, and the project ships with no CI verification despite
   never having made an explicit decision to opt out.
3. **Adversarial dogfood.** A CoDD-managed project that *publicly* claims
   coherence guarantees can disable a red gate without any audit trail, which
   undermines the credibility of every CoDD-managed project.
4. **Precedent creep.** A future security check adds `security.enabled: false`
   following the same pattern. Each new "opt-out flag" multiplies the surface
   without the cost ever being centralised, and no one notices because each
   addition is local to its own check file.

---

## Proposed fix direction (handed to Phase B)

The fix is **not** to delete the opt-out path. CoDD must support legitimate
disablement (e.g., a project that genuinely has no CI provider yet, a
short-window migration, an environment that intentionally defers a check).
What the fix must change is the **shape** of disablement:

1. **Standardise opt-out as a first-class CoDD concept.** A single, generic
   schema for declaring an opt-out, applicable to *any* check, owned by the
   DAG-check core rather than each individual check.
2. **Require justification + expiry.** A bare opt-out flag (e.g.
   `provider: none`) must be insufficient on its own; an accompanying
   declaration block with a non-empty `reason` and a future-dated `expires_at`
   must be required, and `codd validate` must reject configurations that omit
   them.
3. **Preserve severity.** An opt-out that is otherwise valid must still
   surface in `dag verify` and in the deploy report as a non-green result
   (status `opt_out`, severity preserved from the original check), even if
   `block_deploy` is allowed to be False under a valid declaration. A project
   with an opt-out is **not** a green project.
4. **Surface the inventory.** `dag verify` and `codd report` must aggregate
   active opt-outs and expired opt-outs, so the cost of a long-standing
   opt-out is visible and monotonically increasing in the dashboard.
5. **Distinguish "forgot to configure" from "explicit opt-out".** The
   defaults-layer fallback that turns a missing `ci:` section into
   `provider="none"` must be removed. A missing section should fail
   `codd validate` with a clear error pointing the user to either declare CI
   or declare an opt-out. Silence must not be consent.

The concrete schema, the API surface for checks to participate in the policy,
and the migration plan for existing `provider: none` configurations are
deferred to the design doc
(`queue/reports/gunshi_464_optout_protection_design.yaml`).

---

## Out of scope

- C7/C6/C9 "subject-absent" skip paths. Addressed elsewhere, with their own
  amber-warning rules.
- Specific CI providers, deployment targets, or industry verticals. The fix
  must remain domain-agnostic; this post-mortem deliberately uses no
  project-specific names.
- Runtime CI polling. Already deferred by the C8 implementation note and
  unaffected by this fix.
