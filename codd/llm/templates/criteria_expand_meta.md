You expand task criteria from declared artifacts and expected relationships.

Return strict JSON only, with this shape:

{
  "dynamic_items": [
    {
      "id": "snake_case_id",
      "text": "A verifiable criterion derived from the supplied artifacts.",
      "source": "expected_node | expected_edge | user_journey | v_model | coverage_axis",
      "source_ref": "source identifier",
      "severity": "critical | high | medium | info",
      "axis_type": "required only when source is coverage_axis",
      "variant_id": "required only when source is coverage_axis"
    }
  ],
  "coverage_summary": {
    "expected_node_count": 0,
    "expected_edge_count": 0,
    "user_journey_count": 0,
    "v_model_count": 0
  }
}

Rules:
- Keep the original static criteria out of dynamic_items.
- Derive only from the inputs below.
- Each expected artifact and each declared relationship should have at least one criterion when it represents required behavior.
- Each declared coverage axis variant should have at least one criterion when it represents required behavior.
- For coverage_axis items, set source_ref to "<axis_type>:<variant_id>" and fill axis_type and variant_id.
- Criteria must be concrete enough for an independent reviewer to check.
- Do not add stack-specific assumptions.

TASK ID:
{task_id}

STATIC CRITERIA:
{static_criteria_json}

DESIGN DOCUMENTS:
{design_doc_bundle}

EXPECTED EXTRACTIONS:
{expected_extraction_json}

COVERAGE AXES:
{coverage_axes_hint}

DECLARED OPERATIONS:
{operation_flow_hint}

PROJECT CONTEXT:
{project_context_json}
