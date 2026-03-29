# CoDD Verify Report

Generated: 2026-03-30 00:56:58
Language: python

## Result: FAIL

## Typecheck
- Status: FAIL
- Errors: 33

## Tests
- Status: PASS
- Total: 127 | Passed: 127 | Failed: 0 | Skipped: 0

### Typecheck Errors
- `codd/validator.py:10:0` import-untyped: Library stubs not installed for "yaml"
- `codd/validator.py:224:0` assignment: Incompatible types in assignment (expression has type "DocumentRecord | None", variable has type "DocumentRecord")
- `codd/graph.py:78:0` assignment: Incompatible default for argument "path" (default has type "None", argument has type "str")
- `codd/graph.py:79:0` assignment: Incompatible default for argument "name" (default has type "None", argument has type "str")
- `codd/graph.py:79:0` assignment: Incompatible default for argument "module" (default has type "None", argument has type "str")
- `codd/graph.py:114:0` assignment: Incompatible default for argument "condition" (default has type "None", argument has type "str")
- `codd/graph.py:161:0` assignment: Incompatible default for argument "detail" (default has type "None", argument has type "str")
- `codd/config.py:10:0` import-untyped: Library stubs not installed for "yaml"
- `codd/verifier.py:129:0` misc: Unrecognized option: file.py:line: error: message  [code] = True
- `codd/synth.py:10:0` import-untyped: Library stubs not installed for "yaml"
- `codd/synth.py:528:0` var-annotated: Need type annotation for "layers"
- `codd/synth.py:620:0` var-annotated: Need type annotation for "concerns"
- `codd/parsing.py:15:0` import-untyped: Library stubs not installed for "yaml"
- `codd/parsing.py:21:0` import-not-found: Cannot find implementation or library stub for module named "tomli"
- `codd/parsing.py:28:0` assignment: Incompatible types in assignment (expression has type "None", variable has type Module)
- `codd/extractor.py:18:0` import-untyped: Library stubs not installed for "yaml"
- `codd/extractor.py:179:0` arg-type: Argument "key" to "max" has incompatible type overloaded function; expected "Callable[[str], SupportsDunderLT[Any] | SupportsDunderGT[Any]]"
- `codd/scanner.py:14:0` import-untyped: Library stubs not installed for "yaml"
- `codd/generator.py:13:0` import-untyped: Library stubs not installed for "yaml"
- `codd/propagate.py:7:0` import-untyped: Library stubs not installed for "yaml"
- `codd/propagate.py:14:0` assignment: Incompatible default for argument "output_path" (default has type "None", argument has type "str")
- `codd/propagate.py:45:0` var-annotated: Need type annotation for "all_impacts" (hint: "all_impacts: dict[<type>, <type>] = ...")
- `codd/planner.py:11:0` import-untyped: Library stubs not installed for "yaml"
- `codd/planner.py:549:0` var-annotated: Need type annotation for "adjacency"
- `codd/hooks.py:8:0` import-untyped: Library stubs not installed for "yaml"
- `codd/cli.py:156:0` assignment: Incompatible types in assignment (expression has type "GenerationResult", variable has type "PlanInitResult")
- `codd/cli.py:157:0` attr-defined: "PlanInitResult" has no attribute "path"
- `codd/cli.py:158:0` attr-defined: "PlanInitResult" has no attribute "status"
- `codd/cli.py:158:0` attr-defined: "PlanInitResult" has no attribute "node_id"
- `codd/cli.py:159:0` attr-defined: "PlanInitResult" has no attribute "status"
- `codd/cli.py:339:0` assignment: Incompatible types in assignment (expression has type "PlanResult", variable has type "PlanInitResult")
- `codd/cli.py:345:0` arg-type: Argument 1 to "plan_to_dict" has incompatible type "PlanInitResult"; expected "PlanResult"
- `codd/cli.py:348:0` arg-type: Argument 1 to "render_plan_text" has incompatible type "PlanInitResult"; expected "PlanResult"

## Warnings
- No @generated-from header in /home/tono/codd-dev/codd/validator.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/graph.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/config.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/verifier.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/synth.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/parsing.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/extractor.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/scanner.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/generator.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/propagate.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/planner.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/hooks.py — manual review required
- No @generated-from header in /home/tono/codd-dev/codd/cli.py — manual review required
