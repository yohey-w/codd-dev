# CoDD Diff Discovery (Generic)

与えられた extracted.md と requirements.md を比較し、
3 カテゴリの drift findings を抽出してください。

## 3 カテゴリ

1. implementation_only: 実装にあって要件未記載
2. requirement_only: 要件にあって実装未対応
3. drift: 両方にあるが齟齬

## 入力

extracted_md: {{extracted_content}}
requirements_md: {{requirements_content}}
project_lexicon: {{project_lexicon}}
existing_findings: {{ignored_findings}}

## 出力 (JSON array)

[{
  "id": "DIFF-YYYY-MM-DD-NNN",
  "kind": "<LLM 動的>",
  "severity": "<critical|high|medium|info>",
  "name": "...",
  "question": "...",
  "details": {
    "category": "...",
    "evidence_extracted": "...",
    "evidence_requirements": "...",
    "discrepancy": "..."
  },
  "related_requirement_ids": [...],
  "rationale": "..."
}, ...]

Return only the JSON array, with no prose before or after it.
