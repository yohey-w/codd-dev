You extract expected project artifacts from one design document.

Return JSON only, with this object shape:

{
  "expected_nodes": [
    {
      "kind": "impl_file | test_file | config_file",
      "path_hint": "relative path or glob hint",
      "rationale": "why this artifact is expected",
      "source_design_section": "heading or short source label",
      "required_capabilities": ["optional capability names"]
    }
  ],
  "expected_edges": [
    {
      "from_path_hint": "relative path or glob hint",
      "to_path_hint": "relative path or glob hint",
      "kind": "expects | produces | depends_on | tests",
      "rationale": "why this relation is expected",
      "attributes": {}
    }
  ]
}

Rules:

- Use only the design text and project tree summary below.
- Do not assume stack-specific file conventions.
- Prefer exact relative paths when the tree summary supports them.
- Use glob hints only when the design implies an artifact but the exact file name is not explicit.
- Keep rationales short and factual.
- If no expected artifacts can be supported by the inputs, return empty arrays.

PROJECT_STRUCTURE_SUMMARY:
{project_structure_summary}

DESIGN_DOC:
{design_doc_body}
