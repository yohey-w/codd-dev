# CDP-Browser E2E Verification Template (cmd_397)

cmd_393 で構造化した `design_doc.user_journeys` の declarative steps を、**実ブラウザで自動実行する verification engine** を CoDD に組み込むための設計書。

cmd_392d (Phase d) で確立した `register_verification_template` plug-in 基盤に、CDP (Chrome DevTools Protocol) ベースの新 template `cdp_browser` を追加する。**新 node kind / 新 edge kind は 1 つも追加しない**。stack 多様性 (browser / launcher / form interaction) は 3 軸の plug-in で吸収し、CoDD core には PowerShell / Edge / React 等の固有名を一切ハードコードしない。

## 動機

cmd_393 で次が達成された。

- `design_doc.frontmatter.user_journeys[].steps` に `navigate / form_submit / expect_url / expect_browser_state` という declarative な動線を書ける
- C7 user_journey_coherence が「設計の整合性」を静的に検証する

しかし、cmd_393 v1.25.0 release 後、osato-lms の HTTP × `__Secure-` Cookie 事故修復確認は **将軍が手動で CDP-Edge を 9224 で起動して E2E を回す** という属人的フローで行われた。

```
1. PowerShell launcher (~/.claude/skills/shogun-edge-cdp/scripts/launch_edge_debug.ps1 -Port 9224)
2. WSL2 → host CDP 接続 (mirrored networking)
3. navigate http://144.91.125.163:3000/login
4. React state setter で email/password 入力 → form 送信
5. location.href が /learner/dashboard に遷移したことを確認
6. Cookie 'next-auth.session-token' (Secure 属性なし、HTTP 環境保存可) を検証
```

これらすべての操作は cmd_393 の journey.steps に **すでに declarative に書ける形**になっているにも関わらず、それを実ブラウザで自動実行する engine が CoDD には欠けていた。

`cmd_397` はこの欠落を埋める。cmd_393 で declare されたステップを、`@register_verification_template("cdp_browser")` で登録した CdpBrowser template が CDP プロトコル経由で実ブラウザに送り込む。

## 制約

### 殿確定制約

| ID | 制約 |
|----|------|
| C-A | 新 node カテゴリ追加禁止 (既存 4 種のみ) |
| C-B | 新 edge できる限り避ける |
| C-C | CoDD core にハードコード禁止: LMS / NextAuth / Cookie / PowerShell / Edge / Windows / React native setter 等 |
| C-D | 3 軸 plug-in (Browser / Launcher / Form interaction strategy) で多様性吸収 |
| C-E | `project_lexicon.yaml` で `login_journey` や `browser_capability` を declare できる前提を維持 (cmd_393 で既存) |

### スコープ

- 新 verification template `cdp_browser` (cmd_392d の `VerificationTemplate` ABC を実装)
- 4 plug-in interface: `BrowserEngine` / `CdpLauncher` / `FormInteractionStrategy` / `AssertionHandler`
- `_verification_template_ref()` の拡張 (codd.yaml + design_doc.user_journeys から `cdp_browser` を選択)
- AssertionHandler 標準 3 種 (`expect_url` / `expect_browser_state` / `expect_dom_visible`) を CoDD core に bundle
- cookbook (`docs/cookbook/cdp_browser/`) でサンプル plug-in (PowerShell launcher / React state strategy / Edge engine) — **release tarball には同梱しない**
- osato-lms 実証 (今夜の手動フローを CdpBrowser で再現)

### 非スコープ

- 既存 `playwright` / `curl` template の挙動変更
- 既存 cmd_376/377/378/386/388/392/393 の改修
- Browser engine 自体の同梱 (Chromium binary 等は cookbook の指示で project 側が用意)
- screenshot 取得 / video 録画 (将来 cmd_398+ で別 release)

## アーキテクチャ概要

新 verification template は 1 つ (`cdp_browser`) で、その内部が 3 軸 plug-in に分解されている。

```
                ┌──────────────────────────────────────┐
                │  CdpBrowser (VerificationTemplate)    │
                │                                      │
                │  generate_test_command()              │
                │   └─ design_doc.user_journeys[i] →    │
                │       JSON journey plan を生成        │
                │                                      │
                │  execute(plan)                        │
                │   ├─ CdpLauncher.launch_command()    │ ← plug-in axis 1
                │   ├─ CDP wire (websocket + JSON-RPC) │
                │   ├─ for step in journey.steps:      │
                │   │    ├─ navigate / click           │
                │   │    ├─ FormInteractionStrategy.fill_input_js / submit_form_js │ ← plug-in axis 2
                │   │    └─ AssertionHandler[action].assert_() │ ← bundle 3 + plug-in axis 3
                │   └─ CdpLauncher.teardown_command()  │
                └──────────────────────────────────────┘
                          ▲
                          │
        BrowserEngine (axis 0): cdp_endpoint / capabilities ← plug-in axis 0
```

### 1 つの新 verification template

```python
# codd/deployment/providers/verification/cdp_browser.py
@register_verification_template("cdp_browser")
class CdpBrowserTemplate(VerificationTemplate):
    def generate_test_command(self, runtime_state, test_kind: str) -> str:
        # design_doc.user_journeys[i] を JSON で encode して journey plan を返す
        ...

    def execute(self, command: str) -> VerificationResult:
        # plan を decode → launcher → CDP → step dispatch → assertion
        ...
```

cmd_392d の既存パターン (`register_verification_template` decorator + ABC `generate_test_command` / `execute`) をそのまま踏襲。CoDD core には他に変更なし。

### 4 つの plug-in 軸

CoDD core はそれぞれ ABC + register decorator のみを提供し、**実装は同梱しない**。

| 軸 | ABC | decorator | 役割 | core 同梱実装 |
|----|------|-----------|------|---------------|
| BrowserEngine | `cdp_endpoint(config) -> str`, `normalized_capabilities() -> set[str]` | `@register_browser_engine("<name>")` | Edge / Chrome / Chromium / Firefox 等 engine 固有差を吸収 | なし (cookbook に edge / chromium / firefox 例) |
| CdpLauncher | `launch_command(config) -> list[str]`, `teardown_command() -> list[str]`, `is_alive(config) -> bool` | `@register_cdp_launcher("<name>")` | PowerShell launcher / shell script / WebDriver / external_running | なし (cookbook に powershell_script / shell_script / external_running 例) |
| FormInteractionStrategy | `fill_input_js(selector, value) -> str`, `click_js(selector) -> str`, `submit_form_js(selector) -> str` | `@register_form_strategy("<name>")` | React state setter / 標準 input event / Vue / Angular の差を吸収 | なし (cookbook に react_native_setter / standard_input_event 例) |
| AssertionHandler | `assert_(cdp_session, step) -> AssertionResult`, `action_name: ClassVar[str]` | `@register_assertion_handler("<action>")` | journey.steps の `expect_*` action を実行 | **3 種 bundle** (`expect_url` / `expect_browser_state` / `expect_dom_visible`) |

最後の AssertionHandler のみ標準 3 種を core に bundle する。理由は、これらが journey.steps の **標準語彙** (cmd_393 で確立済) であり、stack 中立だから。それ以外の特殊 assertion は project が `@register_assertion_handler` を呼んで追加できる。

### CDP プロトコル client (hybrid 方式)

CdpBrowser 内部の CDP wire は最小限の websocket + JSON-RPC エンコーディングのみ実装する。`Page.navigate` / `Runtime.evaluate` などの method 名は launcher / strategy plug-in が指定し、core は generic な request/response router として動作する。

これにより:
- core は CDP ベンダー (Chrome / Edge / Chromium) 中立
- 依存追加は `websocket-client` (~50KB) のみ
- 外部 tool 必須化を回避 (UX 改善)

完全 subprocess (CDP を外部 tool に投げるだけ) や bundle CDP client (`pycdp` 等を依存に追加) は不採用。理由は critical-ASK ASK-1 で詳述。

## 既存 4 node + cmd_392 既存 node への mapping

**新 node kind / 新 edge kind は 0**。すべて既存スキーマに乗る。

### user_journey の入力源 (cmd_393 既存)

cmd_393 で確立した `design_doc.attributes.user_journeys` をそのまま使う。

```yaml
# osato-lms/docs/design/auth_design.md frontmatter
user_journeys:
  - name: login_to_dashboard
    criticality: critical
    steps:
      - { action: navigate, target: /login }
      - { action: click, selector: 'button:has-text("メールアドレスでログイン")' }
      - { action: fill, selector: 'input[type=email]', value: '${TEST_EMAIL}' }
      - { action: fill, selector: 'input[type=password]', value: '${TEST_PASSWORD}' }
      - { action: form_submit, selector: 'form#login' }
      - { action: expect_url, value: /learner/dashboard, mode: contains }
      - { action: expect_browser_state, kind: cookie, key: next-auth.session-token, present: true }
```

CdpBrowser は `generate_test_command(runtime_state, "e2e")` でこの steps 配列を JSON 化して journey plan を返す。

### verification_test node 属性拡張のみ

cmd_392 の `verification_test` (kind 既存) の attributes に optional な `cdp_browser_config` を追加する (passthrough のみ、CoDD core は意味解釈しない)。新 node kind 追加なし。

```python
# codd/deployment/extractor.py の _verification_template_ref を拡張
def _verification_template_ref(path: Path, *, has_cdp_browser_config: bool = False) -> str:
    if has_cdp_browser_config:
        return "cdp_browser"
    if path.suffix in {".ts", ".tsx", ".js", ".jsx"}:
        return "playwright"
    if path.suffix == ".sh":
        return "curl"
    return "document"
```

### edges 0 new

| 必要な関係 | 実装 |
|-----------|------|
| design_doc が user_journey を持つ | cmd_393 既存 (in-line attributes) |
| journey が verification_test に紐付く | 既存 `verified_by` edge を流用 (`edge.attributes.journey` で帰属) |
| runtime_state が journey の前提条件を提供 | 既存 cmd_392 chain |

## CdpBrowser template 内部フロー

### `generate_test_command(runtime_state, test_kind="e2e")`

`design_doc.user_journeys[i]` を JSON で encode し、`execute()` がそれを decode して step ループを回せる形にする。playwright/curl と異なり、文字列コマンドというより **JSON journey plan** を生成する。

### `execute(command)` のシーケンス

1. `command` を JSON decode → journey plan を取得
2. `codd.yaml [verification.templates.cdp_browser]` を読み browser / launcher / form_strategy 名を取得
3. `BROWSER_ENGINES[name]` / `CDP_LAUNCHERS[name]` / `FORM_STRATEGIES[name]` から plug-in を resolve
4. `CdpLauncher.launch_command()` を `subprocess.run` → browser を debug mode で起動
5. `BrowserEngine.cdp_endpoint(config)` で得た URL に websocket 接続
6. journey.steps を loop し action 種別に応じて dispatch:
   - `navigate` → `Page.navigate` メソッド (CDP wire)
   - `click` → `Runtime.evaluate(form_strategy.click_js(selector))`
   - `fill` → `Runtime.evaluate(form_strategy.fill_input_js(selector, value))`
   - `form_submit` → `Runtime.evaluate(form_strategy.submit_form_js(selector))`
   - `expect_*` → `AssertionHandler[action].assert_(cdp_session, step)`
7. すべて成功なら `VerificationResult(passed=True, ...)`
8. 任意 step 失敗で early return (output に該当 step + 観測値)
9. `CdpLauncher.teardown_command()` で browser 終了 (attach mode の場合 noop)

### Failure modes

| 状態 | 結果 |
|------|------|
| browser 起動失敗 | red, output に launcher stderr |
| CDP 接続 timeout | red, output に endpoint URL + 試行回数 |
| step 失敗 (assertion fail / JS exception) | red, output に該当 step JSON + 観測値 + DOM/cookie snapshot |
| teardown 失敗 | amber WARN (verification 結果は確定済) |

## codd.yaml 設定例

project 側で次のように declare する。CoDD core にこれらの値はハードコードされない。

```yaml
# osato-lms/codd.yaml
verification:
  templates:
    cdp_browser:
      browser:
        engine: edge
        debug_port: 9224
      launcher:
        kind: powershell_script
        script_path: "~/.claude/skills/shogun-edge-cdp/scripts/launch_edge_debug.ps1"
        args: ["-Port", "9224"]
      form_strategy:
        kind: react_native_setter
      assertion_handlers:
        expect_url: standard
        expect_browser_state: standard
      timeout_seconds: 60
```

`engine: edge` / `kind: powershell_script` / `kind: react_native_setter` の 3 値が `BROWSER_ENGINES` / `CDP_LAUNCHERS` / `FORM_STRATEGIES` registry の key に一致しなければ起動時に red (output に「register required plugin: ...」)。

## cookbook (CoDD release 同梱せず)

cmd_393 で確立した方針 (capability_patterns を core に bundle しない) を継承する。`docs/cookbook/cdp_browser/` にサンプル plug-in 一式を配置するが、`pip install codd-dev` で配布される tarball には含めない。

```
docs/cookbook/cdp_browser/
├── README.md                                  # コピペ運用の手順
├── launchers/
│   ├── powershell_script.py                   # 今夜の launch_edge_debug.ps1 ラッパー
│   ├── shell_script.py                        # Linux/Mac の同等
│   └── external_running.py                    # 既起動 browser に attach
├── engines/
│   ├── edge.py
│   ├── chromium.py
│   └── firefox.py
└── strategies/
    ├── react_native_setter.py                 # 今夜の React state setter + input event
    └── standard_input_event.py                # 純 DOM input.value = ... + dispatchEvent
```

ユーザは必要な plug-in を自 project の `codd_plugins/` ディレクトリにコピーし、`codd.yaml` で名前を declare する。これにより:

- CoDD release が「PowerShell / Edge を推奨している」誤解を招かない
- Generality Gate を維持
- 拡張は project の自由 (任意の launcher / engine / strategy を追加できる)

## Generality Gate

CoDD core 内部に **以下の固有名を 1 つもハードコードしない**:

| 禁止 | 所在 |
|------|------|
| `PowerShell` / `pwsh` / `.ps1` | cookbook (`launchers/powershell_script.py`) |
| `Edge` / `msedge` / `msedge.exe` | cookbook (`engines/edge.py`) |
| `Windows` / `WSL` / `mirrored networking` | cookbook README + project の codd.yaml |
| `NextAuth` / `Cookie` / `__Secure-` | project の design_doc + lexicon (cmd_393 で declarative 化済) |
| `React` / `native-setter` / state setter | cookbook (`strategies/react_native_setter.py`) |
| `osato-lms` / 他 project 名 | project の codd.yaml + journey 宣言 |

許される core 知識:
- `VerificationTemplate` ABC + decorator (cmd_392d 既存)
- 4 plug-in ABC 定義
- CDP wire 最小実装 (websocket + JSON-RPC encoding) — stack 中立
- 標準 step action 語彙 (`navigate` / `click` / `fill` / `form_submit` / `expect_*`)
- AssertionHandler 標準 3 種 (`expect_url` / `expect_browser_state` / `expect_dom_visible`)

QC 段階で次の grep が必須:

```bash
grep -rEi 'powershell|msedge|nextauth|__secure-|react|native[_-]setter|osato-lms|wsl|windows' \
  codd/deployment/providers/verification/cdp_browser.py \
  codd/deployment/providers/verification/cdp_engines.py \
  codd/deployment/providers/verification/cdp_launchers.py \
  codd/deployment/providers/verification/form_strategies.py \
  codd/deployment/providers/verification/assertion_handlers.py \
  || echo OK
```

期待: `OK` (zero hit)。

cmd_385 個別 detector 量産路線の轍を踏まない。CdpBrowser は generic engine 1 つ、stack 多様性は 3 軸 plug-in で吸収。新しい browser engine が出ても plug-in 追加で対応可、core 改修不要。

## CLI

新規 subcommand は最大 1 件 (ASK-2 確定で実装)。専用 namespace は作らない。

| command | 役割 |
|---------|------|
| `codd dag verify --check user_journey_coherence` | 既存 (cmd_393)、変更なし。verification_test の template_ref が `cdp_browser` なら CdpBrowser が動く |
| `codd dag run-journey <journey_name>` | (ASK-2=YES の場合) journey 単発実行 shortcut。CI / 手動デバッグ用。exit code = `VerificationResult.passed` |

## 既存 cmd / check との整合性

| 関連 | 影響 |
|------|------|
| cmd_392 C6 deployment_completeness | C6 既存挙動不変。verification_test の template_ref が `cdp_browser` でも `verified_by` chain は traverse 可 |
| cmd_393 C7 user_journey_coherence | C7 (静的整合) と CdpBrowser (動的検証) は強い相補。C7 が PASS で初めて CdpBrowser が意味を持つ |
| cmd_388 CDAP | verification_test 変更 → C7 自動再実行 (既存)。CdpBrowser 経由でも同様 |
| cmd_376 autonomous mode | CdpBrowser 失敗で DriftEvent 自動発火 (kind=`verification_failed`) |
| cmd_377 preflight | CdpBrowser red は preflight critical 候補 |

## Backwards compatibility

| 状態 | 挙動 |
|------|------|
| `codd.yaml [verification.templates.cdp_browser]` 不在 | 既存挙動 (playwright / curl) のみ |
| `design_doc.user_journeys` 不在 | CdpBrowser を選択する path が無く、選ばれない |
| cookbook plug-in が `codd_plugins/` に未コピー | template 起動時に red (output に「register required plugin: <name>」明示) |
| 既存 v1.25.0 ユーザ | 影響なし (cmd_397 関連は全 opt-in) |

## osato-lms 実証

cmd_397_lms (実装担当: 足軽 1 名) で次を行う:

1. `osato-lms/codd.yaml` に `verification.templates.cdp_browser` 設定を追加
2. cookbook から `powershell_script` launcher / `edge` engine / `react_native_setter` strategy を `osato-lms/codd_plugins/cdp_browser/` にコピー
3. 今夜の手動フロー (Edge 9224 / login → /learner/dashboard) を CdpBrowser で自動実行
4. Cookie `next-auth.session-token` が browser に保存されることを `expect_browser_state` で検証

acceptance:
- `codd dag verify --check user_journey_coherence` で CdpBrowser が走り PASS
- 今夜の手動 flow と同じ結果 (login 成功 + cookie 確認)

## 設計の経緯

| 段階 | 内容 |
|------|------|
| cmd_393 (v1.25.0) | declarative user_journeys を design_doc.frontmatter に書ける + C7 で静的整合検証 |
| 2026-05-06 早朝 | osato-lms 修復確認は将軍が手動 CDP-Edge 9224 で実行 — 自動化されていなかった |
| cmd_397 (v1.26.0、本設計書) | cmd_393 declarative steps を実ブラウザで自動実行する engine を追加 |

「declarative journey が実ブラウザで自動実行される」 = 設計書が動く文書になる。cmd_393 (検出) + cmd_397 (実行) で coherence の UX 軸が 1 周する。

## 将来拡張余地

cmd_398+ で別 release で対応する候補:

- **screenshot / video 録画**: assertion 失敗時の DOM snapshot を超えて、ピクセル単位の証跡保存
- **journey replay**: CDP セッションを記録 → 再生で異なる環境で同じ動線をたどる
- **visual regression**: pixel-level diff の AssertionHandler 標準化
- **mobile emulation**: WebKit / Safari mobile / iOS シミュレータ対応の plug-in
- **performance budget**: Page.lifecycleEvent / Runtime.metrics で perf budget 超過を red 検出

## 設計書フッタ

| 項目 | 値 |
|------|-----|
| 関連 cmd | cmd_393 (declarative 入力) / cmd_392d (verification template plug-in 基盤) / cmd_397 (本体) |
| target release | v1.26.0 |
| 実装ファイル (予定) | `codd/deployment/providers/verification/cdp_browser.py` ほか 4 plug-in module |
| 設計骨子 YAML | `multi-agent-shogun:queue/reports/gunshi_397_g0_design.yaml` |
| critical-ASK | 5 件 (ASK-1: CDP client 戦略 / ASK-2: run-journey CLI / ASK-3: cookbook bundle / ASK-4: assertion bundle / ASK-5: sec model) |
