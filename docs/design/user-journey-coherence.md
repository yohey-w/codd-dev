# User Journey Coherence Layer (cmd_393)

CoDD の coherence 検査を「サーバ側の連鎖 (cmd_392 C6)」から「ブラウザ側 / UX 連鎖」へ拡張するための設計書。

cmd_392 v1.24.0 release 直後に発生した実ブラウザログイン失敗 (HTTP 環境 × `__Secure-` Cookie 不整合) を構造的に検出する C7 user_journey_coherence check を、**新 node kind / 新 edge kind を 1 つも追加せず**、既存 4 カテゴリ (`design_doc` / `impl_file` / `plan_task` / `expected_value`) の attribute schema 拡張のみで実装する。

## 動機

cmd_392 deploy verification gate (C6) は次のチェーンを担保する。

```
design_doc → deployment_doc → impl_file → runtime_state → verification_test (smoke, curl)
```

これは API / DB レベルでの連続性で、`POST /api/auth/login` の HTTP 200 までを保証する。
しかし、本番 release 直後に以下の事故が発生した。

1. NextAuth が production mode で `__Secure-` prefix Cookie を発行
2. デプロイ先の VPS は HTTP 環境 (HTTPS 設定なし)
3. ブラウザは仕様として HTTP origin での `__Secure-` Cookie を保存拒否
4. middleware が未認証扱い → `/login` redirect ループ
5. `curl` ベースの smoke test は cookie ファイル使用で `Secure` 属性を無視するため、C6 は PASS だった

つまり次の構造的盲点があった。

- **C6 はブラウザを通らない**: `__Secure-` Cookie のような browser-level の制約は curl では検証不能
- **設計書に動作環境制約が宣言されていない**: 「authentication cookie は HTTPS 前提」が design_doc に書かれていなかった
- **User journey が DAG に登っていない**: 「user が login form を submit して dashboard に到達する」という UX レベルの動線は DAG node として表現されていなかった

cmd_393 は次の方針で解決する。

- **既存 4 node kind の attribute を schema 拡張**して、user_journey / runtime constraint / browser requirement / impl evidence を declarative に表現する
- **新 check C7 user_journey_coherence** が design_doc.NFR ↔ impl_file 証跡 ↔ runtime_state capability ↔ expected_value (browser/runtime) の整合を 8 種の violation で検出する
- すべての stack 知識 (NextAuth / Cookie / `__Secure-` / Chromium 等) は **プロジェクト側に declarative に置き**、CoDD core にはハードコードしない (Generality Gate を最強水準に)

## 制約と非制約

### 殿確定制約 (2026-05-05 23:55)

| ID | 制約 |
|----|------|
| C-A | 新 node カテゴリ追加禁止 |
| C-B | 新 edge 種類追加もできるだけ避ける (既存で吸収可能か先に検討) |
| C-C | 既存 node 体系 = `design_doc` / `impl_file` / `plan_task` / `expected_value` の 4 種のみ |

これは初期設計案 (新 node 3 種 + 新 edge 3 種) を「Generality Gate 違反 = overfitting への第一歩」として却下した結果である。本設計書は再設計版に対応する。

### スコープ

- design_doc / lexicon / impl_file / runtime_state の attribute schema 拡張 (passthrough のみ、CoDD core は意味解釈しない)
- C7 user_journey_coherence check (8 violation type)
- C7 deploy gate (7 つ目) 統合 + DriftEvent + ntfy critical
- 新 CLI: `codd dag verify --check user_journey_coherence` / `codd dag journeys`
- osato-lms 実証 (HTTP × `__Secure-` 事故の構造的検出)
- Generality Gate: stack 名のハードコードゼロを test grep で証明

### 非スコープ

- 修復の自動化 (HTTPS 化 wizard / auth_provider 切替 helper) — 検出のみ。修復は別 release
- 既存 cmd_376/377/378/386/388/392 の挙動変更
- E2E test の自動生成 (test 実装は人間 / impl 担当の責務)

## 既存 4 node カテゴリへの mapping

殿の hint 「`runtime_environment` / `user_journey` / `browser_capability` は実は NFR (非機能要件) = 既存 design_doc 内で表現されるべき内容」を採用し、新次元はすべて **既存 node kind の attribute** として表現する。

| 殿モデル | 実装 node kind | 属性化される情報 |
|----------|----------------|------------------|
| design_doc (要件) | `design_doc` (frontmatter) | `runtime_constraints`, `user_journeys` |
| design_doc (設計) | `design_doc` (frontmatter) | 認証 Cookie の Secure 属性 / SameSite 方針も同 frontmatter で宣言 |
| impl_file | `impl_file` (静的スキャン) | `runtime_evidence` (project 宣言の `capability_patterns` 経由) |
| expected_value | `expected` (lexicon) + `runtime_state` + `verification_test` | `journey`, `browser_requirements`, `runtime_requirements`, `capabilities_provided` |
| plan_task | `plan_task` (`expected_outputs`) | `lexicon:` / `design:` 接頭辞対応で journey に紐付け |

cmd_392 で導入された `deployment_doc` / `runtime_state` / `verification_test` は本設計書の文脈では **expected_value カテゴリの実体** として再利用する (既存の v1.24.0 release との互換性維持)。新 node kind は 1 つも増やさない。

## attribute schema 拡張

すべて optional。既存プロジェクトは無宣言で従来通り動作する。

### `design_doc.attributes.runtime_constraints`

design_doc が要求する非機能要件を declarative に列挙する。

```yaml
# docs/design/auth_authorization_design.md frontmatter
---
runtime_constraints:
  - capability: tls_termination
    required: true
    rationale: "Authentication cookie issuer in this stack requires TLS at origin"
  - capability: cookie_security_secure_attribute
    required: true
    rationale: "Session cookie must persist across requests"
---
```

`capability` は単なる文字列。CoDD core はこの値を意味解釈せず、runtime_state の `capabilities_provided` との set 演算で整合判定する。

### `design_doc.attributes.user_journeys`

ユーザの動線を構造化して列挙する。

```yaml
user_journeys:
  - name: login_to_dashboard
    criticality: critical
    steps:
      - { action: navigate, target: /login }
      - { action: form_submit, fields: [email, password] }
      - { action: expect_url, value: /dashboard }
      - { action: expect_browser_state, key: cookie.session, present: true }
    required_capabilities: [tls_termination, cookie_security_secure_attribute]
    expected_outcome_refs: [lexicon:e2e_login_journey]
```

`steps[].action` の語彙 (`navigate` / `form_submit` / `expect_url` / `expect_browser_state` 等) も project 自身の語彙で、CoDD core は意味解釈しない。

### `impl_file.attributes.runtime_evidence`

`codd.yaml [coherence.capability_patterns]` で project が宣言した正規表現で impl_file をスキャンし、何の capability を「証跡として実装している」かを記録する。

```yaml
# codd.yaml
coherence:
  capability_patterns:
    cookie_security_secure_attribute:
      matches:
        - regex: '__Secure-'
          languages: [typescript, javascript]
```

スキャン結果は `impl_file.attributes.runtime_evidence` に格納される:

```json
"runtime_evidence": [
  {
    "capability_kind": "cookie_security_secure_attribute",
    "value": true,
    "line_ref": "src/lib/auth.ts:42",
    "source": "capability_patterns.yaml"
  }
]
```

CoDD core は **regex 1 件も同梱しない**。NextAuth / `__Secure-` / SameSite 等の知識はすべて project の `codd.yaml` に declarative に置く。これが Generality Gate の中核。

### `expected (lexicon).attributes`

`project_lexicon.yaml` の `required_artifacts` 各 entry に optional 属性を追加する。

```yaml
required_artifacts:
  - id: e2e_login_journey
    journey: login_to_dashboard
    path: tests/e2e/login.spec.ts
    browser_requirements:
      - capability: cookie_set
        value: true
        rationale: "Session cookie must persist across requests for authenticated navigation"
    runtime_requirements:
      - capability: tls_termination
        required: true
```

### `runtime_state.attributes.capabilities_provided`

deploy.yaml から推論した、実環境が提供する capability の集合。

```yaml
# codd/deployment/defaults/runtime_capability_inference.yaml (CoDD 同梱の default)
inference_rules:
  - target_type: docker_compose
    capabilities: [container_runtime, server_running]
  - target_type: vercel
    capabilities: [tls_termination, serverless_runtime]
  - healthcheck_url_prefix: "https://"
    capabilities: [tls_termination]
```

project 側は `codd.yaml [coherence.runtime_capability_inference]` で同形式の override を宣言可能。defaults は infrastructure-level の deployment target type 概念に閉じており、framework / auth 知識を含まない。

### `plan_task.expected_outputs` (語彙拡張のみ)

既存属性 `expected_outputs` の値域に `lexicon:<id>` / `design:<journey_name>` 接頭辞を許可する。新属性追加なし。`produces` edge は接頭辞解決後の対象に対して張られる (例: `lexicon:e2e_login_journey` の expected node に `produces` edge)。

## edge 戦略

**新 edge kind は 0**。

| 必要な関係 | 実装 |
|-----------|------|
| design_doc が user_journey を持つ | `design_doc.attributes.user_journeys` に in-line 格納 (node 内属性で表現) |
| design_doc が runtime constraint を要求 | 同上 (`runtime_constraints` 属性) |
| journey が lexicon entry を期待 | 既存 `expects` edge に `attributes={journey: <name>}` を付与して、design_doc → expected の lexicon ref を表現 |
| plan_task が user_journey 用 e2e test を produces | 既存 `produces` edge を `lexicon:` / `design:` 接頭辞解決後に張る |
| journey が verification_test を実行 | 既存 cmd_392 の `verified_by` chain を流用 |

journey 帰属の細粒度情報は `Edge.attributes` に格納する。これにより同一 (from, to, kind) で複数 journey に属する edge を区別できる。

```python
# codd/dag/__init__.py
@dataclass
class Edge:
    from_id: str
    to_id: str
    kind: str
    attributes: dict[str, Any] | None = None  # cmd_393c で None default に変更
```

## C7 user_journey_coherence check

`codd/dag/checks/user_journey_coherence.py`。`@register_dag_check("user_journey_coherence")` で plug。

### 検出ロジック (擬似コード)

```python
for design_doc in dag.nodes(kind="design_doc"):
    for journey in design_doc.attributes.get("user_journeys", []):
        # (1) Coverage chain
        for ref in journey.expected_outcome_refs:
            if ref not in dag.nodes: violation("missing_journey_lexicon", ...)
        plan_tasks = traverse_produces_to(journey)
        if not plan_tasks: violation("no_plan_task_for_journey", ...)
        e2e_tests = traverse_produces_or_verified_by(plan_tasks, e2e=True)
        if not e2e_tests: violation("no_e2e_test_for_journey", ...)
        if not deploy_includes(e2e_tests): violation("e2e_not_in_post_deploy", ...)

        # (2) Runtime capability coherence
        required = set(design_doc.runtime_constraints.required) | set(journey.required_capabilities)
        actual   = set(runtime_state.capabilities_provided)
        for missing in (required - actual):
            violation("unsatisfied_runtime_capability", capability=missing, ...)

        # (3) Implementation evidence consistency
        for evidence in collect_impl_runtime_evidence(design_doc):
            requires = derive_runtime_requirements(evidence.capability_kind)
            if requires and not requires.issubset(actual):
                violation("impl_evidence_runtime_mismatch",
                          evidence=evidence, missing=requires - actual)

        # (4) Browser-level expected coverage
        for cap in collect_browser_requirements(journey):
            if not any_e2e_asserts(cap, e2e_tests):
                violation("browser_expected_not_asserted", capability=cap)

        # (5) Journey hygiene
        if not has_assertion_step(journey):
            violation("journey_step_no_assertion", severity="amber")
```

### Violation taxonomy

| type | severity | 意味 |
|------|----------|------|
| `missing_journey_lexicon` | red | design_doc が宣言する `expected_outcome_refs` の lexicon entry が見当たらない |
| `no_plan_task_for_journey` | red | journey を `expected_outputs` に含む plan_task が無く、実装計画が断絶 |
| `no_e2e_test_for_journey` | red | plan_task → verification_test (E2E) チェーン断絶 |
| `e2e_not_in_post_deploy` | red | E2E test 存在するが `deploy.yaml` の `post_deploy` hook に組み込まれず |
| `unsatisfied_runtime_capability` | red | design_doc.runtime_constraints が要求する capability を runtime_state が提供しない (今夜事故の主検出器) |
| `impl_evidence_runtime_mismatch` | red | impl_file の runtime_evidence (例: `__Secure-` cookie 発行) が runtime capability (例: `tls_termination=false`) と矛盾 (今夜事故の補強検出器) |
| `browser_expected_not_asserted` | red | lexicon の `browser_requirements` が E2E test で assert されない |
| `journey_step_no_assertion` | amber | journey.steps に検証 step (`expect_*`) が 1 件も含まれない |

### 出力例 (osato-lms HTTP × `__Secure-` 事故)

```json
{
  "user_journey": "login_to_dashboard",
  "design_doc": "docs/design/auth_authorization_design.md",
  "violations": [
    {
      "type": "unsatisfied_runtime_capability",
      "required_capability": "tls_termination",
      "required_by": "design_doc.runtime_constraints[0]",
      "rationale_from_design": "Authentication cookie issuer in this stack requires TLS at origin",
      "actual_runtime_state": "vps:protocol=http (capabilities_provided=[server_running])"
    },
    {
      "type": "impl_evidence_runtime_mismatch",
      "capability_kind": "cookie_security_secure_attribute",
      "evidence": "src/lib/auth.ts:42 (matched regex '__Secure-')",
      "missing_runtime_capability": "tls_termination"
    }
  ],
  "remediation_hints": [
    "Provide tls_termination in deployment (declare in deploy.yaml or update runtime_capability_inference)",
    "Relax runtime_constraints in design_doc with explicit dev-only rationale",
    "Demote user_journey criticality with override path"
  ]
}
```

`remediation_hints` は `runtime_constraints[].rationale` を引用する形でのみ生成する。CoDD core は固有名 (NextAuth / `__Secure-`) を出力に出さない。

### Deploy gate / DriftEvent / ntfy 統合

C7 violation 検出時は次が連鎖する。

1. `codd deploy --apply` の前段で `_collect_user_journey_coherence_gate` が 7 つ目の gate として実行される (cmd_392 C6 と同じ pattern)
2. red violation 1 件以上で deploy `INCOMPLETE_JOURNEY` マークされ block
3. `_publish_user_journey_coherence_events` が `DriftEvent(kind="user_journey_coherence", severity=red)` を CoherenceEngine に publish
4. cmd_377 severity classifier 経由で **ntfy critical** が殿に飛ぶ

## Generality Gate

CoDD core 内部に **以下の固有名を 1 つもハードコードしない**:

- `NextAuth` / `NextJS` / `Auth0` / `Clerk` / `Supabase` / `Firebase`
- `Cookie` / `SameSite` / `__Secure-` / `__Host-`
- `Chromium` / `Firefox` / `WebKit` / `Mobile Safari`
- 特定 stack の Cookie 実装パターン

stack 知識の所在を以下に分離する。

| 知識 | 所在 |
|------|------|
| auth provider の Cookie 仕様 | プロジェクトの `design_doc.frontmatter.runtime_constraints[].rationale` (declarative 宣言) |
| `__Secure-` regex | プロジェクトの `codd.yaml [coherence.capability_patterns]` (project 宣言) |
| browser 制約 (e.g. SameSite) | プロジェクトの `project_lexicon.yaml` browser_requirements (project 宣言) |
| deployment target → capability map | `codd/deployment/defaults/runtime_capability_inference.yaml` (CoDD 同梱の defaults。 ただし infrastructure target type のみ。 project は `codd.yaml [coherence.runtime_capability_inference]` で override 可) |

### Generality Gate test

QC 段階で次の grep が必須 (`v1.25.0` release で zero hit を確認済):

```bash
grep -rEi 'nextauth|__secure-|samesite|chromium|vercel|cloudflare' \
  codd/dag/checks/user_journey_coherence.py \
  codd/dag/extractor.py \
  codd/dag/builder.py \
  codd/deployment/extractor.py \
  || echo OK
```

cookbook 同梱もしない。`capability_patterns` の参考 yaml はプロジェクト側で書くもので、CoDD core / packaging に bundle されない。これは cmd_385 (個別 detector 量産路線を停止した) の教訓を継承している: stack 別ハックを CoDD に集めると overfitting が始まる。

## CLI

新規 subcommand 2 つ。専用 namespace は作らない。

| command | 役割 |
|---------|------|
| `codd dag verify --check user_journey_coherence` | C7 を C1-C6 と同列で plug |
| `codd dag journeys` | design_doc 横断で `user_journeys` 一覧 + 各 journey の C7 status を表示 |

`codd journey {verify,list,test}` のような独立 namespace は導入しない (前 cmd_392 の `codd deploy` のような top-level CLI は cross-cutting check には不要)。

## cmd_388 CDAP との連動

`docs/design/**/*.md` の frontmatter (user_journeys / runtime_constraints) や `project_lexicon.yaml` / `codd.yaml [coherence.*]` への変更は、`watch.propagation_pipeline` 経由で次のように動的に伝搬する。

1. `FileChangeEvent` 検出 → 影響 design_doc の DAG node 再ビルド
2. journey 帰属の `expects` / `produces` edge を再推論
3. C7 自動再実行 → red 検出時 DriftEvent publish

`codd test --related` (cmd_388 の機能) は `executes_journey` 帰属 edge を辿って関連 e2e test を抽出する。journey 起点で test を選択できるようになるため、file 起点 (`--related <file>`) と相補。

## 既存 check / cmd との整合性

| 関連項目 | 影響 |
|----------|------|
| C1 node_completeness | 新 node kind 追加 0 のため列挙不変 |
| C2 edge_validity | 新 edge kind 追加 0 のため列挙不変 |
| C3 depends_on_consistency | design_doc 間 depends_on は変更なし |
| C4 task_completion | `plan_task.expected_outputs` の `lexicon:` / `design:` 接頭辞対応の小拡張のみ |
| C5 transitive_closure | 新 kind なしのため列挙不変 |
| C6 deployment_completeness | C6 = サーバ側連鎖、C7 = ブラウザ側 / journey 連鎖。重複なく補完。両 PASS で production ready |
| cmd_376 autonomous mode | C7 violation 自動検知、家老介入なし |
| cmd_377 preflight | C7 critical violation は preflight critical 候補 |
| cmd_378 GLPF | journey 失敗は典型 re-plan trigger、phase 動的注入可 |

## Backwards compatibility

| 状態 | 挙動 |
|------|------|
| `user_journeys` / `runtime_constraints` 未宣言 design_doc | C7 該当 design_doc は SKIP (INFO log) |
| `codd.yaml [coherence.capability_patterns]` 不在 | `runtime_evidence` 空 → `impl_evidence_runtime_mismatch` 検出対象外 |
| `lexicon.browser_requirements` 不在 | `browser_expected_not_asserted` 検出対象外 |
| deploy.yaml 不在 | cmd_392 既存仕様通り、C7 関連検出は SKIP |

既存 v1.24.0 プロジェクト (osato-lms 含む) は cmd_393 release 直後も挙動変化ゼロ。各 attribute を opt-in で追加することで C7 が active 化する。

## osato-lms 実証

cmd_393_lms (実装担当: 足軽 1 号) で次の 3 ファイルのみを編集して実証完了。

1. `osato-lms/docs/design/auth_design.md` frontmatter に `runtime_constraints` + `user_journeys: [login_to_dashboard]` 追加
2. `osato-lms/project_lexicon.yaml` に `e2e_login_journey` entry + `browser_requirements: [{ capability: cookie_set }]` 追加
3. `osato-lms/codd/codd.yaml` に `coherence.capability_patterns.cookie_security_secure_attribute` の regex 1 行追加

実装コードには 1 行も触れていない (検出層追加のみ、修復は別作業)。

### 実証結果 (commit `b004801` on osato-lms)

```
$ codd dag verify --check user_journey_coherence

[FAIL] user_journey_coherence [red]
  design_doc: docs/design/auth_design.md
  user_journey: login_to_dashboard
  violations:
    - no_plan_task_for_journey
    - unsatisfied_runtime_capability (tls_termination)
    - unsatisfied_runtime_capability (cookie_security_secure_attribute)
    - impl_evidence_runtime_mismatch
        evidence: src/generated/m1_2/auth.config.ts:80
        missing_runtime_capability: tls_termination
    - impl_evidence_runtime_mismatch
        evidence: src/generated/m1_2/lib/auth.config.ts:252
        missing_runtime_capability: tls_termination

$ codd dag verify --check deployment_completeness
[PASS] deployment_completeness [red]
```

C7 のみ FAIL、C6 は PASS のまま。**curl smoke test では捕捉できなかった事故**を、attribute schema 拡張のみで構造的に検出できた。修復は cmd_394 で実施済 (本設計書のスコープ外)。

## 設計の経緯

| 段階 | 内容 |
|------|------|
| 初期案 (subtask_393_g0_design) | 新 node 3 種 (`runtime_environment` / `user_journey` / `browser_capability`) + 新 edge 3 種 + 3 plug-in 提案 (1470 LOC / 119 tests) |
| 殿却下 (2026-05-05 23:55) | 「node は要件定義書から下って汎用的に導出される存在。新カテゴリ追加 = Generality Gate 違反 = overfitting への第一歩」 |
| 再設計 (subtask_393_g0_design2) | attribute schema 拡張 + 既存 edge 流用のみで等価機能 (1150 LOC / 108 tests、320 LOC / 11 tests 削減) |
| v1.25.0 release | 1427 tests PASS / SKIP=0、Generality Gate grep zero hit、osato-lms 実証 PASS |

「新次元 = attribute schema 拡張で吸収」の pattern を確立した。これは cmd_385 (個別 detector 量産路線を停止) の問題意識を、後続の cross-cutting check (cmd_392 / cmd_393) でも持続的に適用できる方針として codified された。

## 将来拡張余地

cmd_394 以降での発展 (本設計書のスコープ外、参考):

- **修復自動化**: HTTPS 化 wizard / auth_provider 切替 helper / dev environment override generator
- **journey simulation**: declarative steps を Playwright スクリプトに自動展開
- **stack cookbook**: `docs/cookbook/capability_patterns/{nextauth,auth0,clerk,supabase}.yaml` (CoDD core 同梱せず、コピペ運用)
- **C9 tool dependency freshness** (cmd_395 系): tool release → consumer project 自律伝搬の枠組みで coherence 5 軸目を追加
- **C7 amber 警告の活用**: `journey_step_no_assertion` 等の amber を cmd_377 preflight で「設計甘さ」シグナルとして扱う

## 設計書フッタ

| 項目 | 値 |
|------|-----|
| 関連 cmd | cmd_393 (本体) / cmd_394 (修復、別 release) |
| release tag | v1.25.0 |
| 実装ファイル | `codd/dag/checks/user_journey_coherence.py`, `codd/dag/extractor.py`, `codd/dag/builder.py`, `codd/deployment/extractor.py` |
| 設計骨子 YAML | `multi-agent-shogun:queue/reports/gunshi_393_g0_design.yaml` (再設計版) |
| 実証 report | `multi-agent-shogun:queue/reports/ashigaru1_393_lms_c7_proof_report.yaml` |
