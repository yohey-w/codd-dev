# Brownfield discovery-layer hardening — GENERALITY-FIRST

## Owner directive (2026-06-25, non-negotiable)
**CoDD never converges unless every fix is generality-first.** Per-OSS / per-language
patches chase infinite layouts and never converge. Fix the CLASS via a generic root
abstraction; add surfacing so unknown cases become visible residue (convergence guarantee).
ALL 6-language stress-dogfood bugs are ONE class: the DISCOVERY layer lacks (a) completeness
accounting and (b) robust generic source/import discovery. Do NOT add `language==` core
branches or per-OSS special-cases.

## GENERIC FIX 1 — completeness accounting (convergence safety-net)
- After dag build: if on-disk source files (of detected language[s]) > source nodes →
  WARN (count + a few example missing files).
- During resolution: an internal-looking specifier (relative / first-party prefix) that
  fails to resolve → record explicit "unresolved residue" + WARN with count.
- Surfacing only (autonomous, = anti-false-green for discovery). A FAIL-gate = new gating =
  owner-gated; do NOT add it. The point: every未知 gap becomes visible, not silent → convergence.

## GENERIC FIX 2 — robust source discovery (ONE mechanism, all languages & layouts)
Fix `codd/extractor.py` `_detect_source_dirs` + `codd/dag/builder.py` source-root synthesis
to generically handle, with no per-layout special-case:
- root-level source files alongside subpackages (BUG A — 5 languages: NestJS/Fastify/Newtonsoft/SQLAlchemy);
- packages whose source lives only in a nested subdir (Java BUG 2: util/→util/concurrent/);
- being scoped INSIDE a package tree (Java BUG 1: `com/google/common` → double-package-prefix
  → silent 0 edges; derive the source root by stripping FQN/namespace segments that match the
  root's trailing path);
- under-scoping (C++ `["include"]`-only — include impl roots db/util/table/…).
Principle: recursively find ALL detected-language source under the root; infer source roots
tolerant of arbitrary scoping. ONE generic algorithm.

## GENERIC FIX 3 — unify import resolution (kill the drift)
C++ scan resolver (`regex_strategies.py:~853 _resolve_cpp_include_path`) drifted from the
builder resolver (`builder.py:~1217 _cpp_header_root_dirs`) → 59% scan edges lost on LevelDB.
Extract ONE shared resolution path used by builder + scan + CEG so they can't diverge (fixes
BUG D and forecloses the "3 JS import regexes" class). Generic, not C++-only in spirit.

## GENERIC FIX 4 — first-line / encoding robustness (all languages)
Strip BOM + leading whitespace before ANY first-line declaration match (fixes C# BOM-orphan,
BUG B, generically — applies to any language's first-line parsing).

## PRECISION (generic refinements)
- Java package-line implicit edges fan out to ALL siblings (imprecise) → emit only real
  import edges, or mark package-implicit edges distinctly. Dedup per-member static imports.
- Label import edges by ACTUAL kind (not always `static_import`, regex_strategies.py:~802).
- Gate the web-only `runtime:db_seed:users` heuristic by project-type (don't synth into C++/Java libs).
- Rename the "vb declarations" label (= verification-boundary, not Visual Basic).

## RIGOR
red-before-green each (unit tests, no --ai); full suite green; **re-verify post-fix on the
exhibiting OSS**: SQLAlchemy (Py BUG A), Newtonsoft (C# BUG A+BOM), LevelDB (C++ BUG D),
Guava (Java BUG 1 scoped-inside + BUG 2 nested), spot Fastify/NestJS — confirm previously
dropped edges now appear AND completeness warnings fire where expected. Generality:
greenfield + the simple first-round OSS (Flask/Express/Zod/Gson/fmt/Dapper) unaffected, no
regression, no `language==` core branch. Claude only; no codex/gpt. Commit to main; no push,
no version bump (batched release 3.7.6).
