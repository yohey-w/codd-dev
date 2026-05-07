<p align="center">
  <strong>CoDD — Coherence-Driven Development（一致性驱动开发）</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="README_ja.md">日本語</a> | <a href="README.md">English</a> | 中文
</p>

---

## 北极星 (Vision)

**「只需写功能需求和约束，代码就能自动生成、修复并验证」**

CoDD 将 **需求 → 设计 → 实现 → 测试** 视为一个 DAG，机器化验证每个节点的一致性 (coherence)，发现不一致时由 LLM 自动修复。人类只需写 **「做什么」和「边界在哪里」**。

## 当前状态 (v2.0.0 — Lexicon-Driven Completeness)

v2.0.0 不仅仅是版本号升级，而是 **定位的转变**。v1.x 完成了 *extract → diagnose → repair* 流水线；v2.0 把 **约束侧 (constraint side)** 作为 plug-in 一等公民引入。

- ✅ 在真实项目 (Next.js + Prisma + TypeScript Web 应用) 上 dogfooding
- ✅ `codd verify --auto-repair` 在真实 LMS 项目上以 `PARTIAL_SUCCESS` 完成 (attempts=4 / applied_patches=4)
- ✅ DAG 完整性的 9 种 coherence check 已运作
- ✅ **31 个 lexicon plug-in** 覆盖 7 个领域 (Methodology / Web / Mobile / Backend-API / Data / Ops / Compliance / Process)
- ✅ **`codd elicit` / `codd diff` / `codd brownfield`** — 同时支持 greenfield 与 brownfield 的覆盖度 / drift 发现
- ✅ **`codd init --suggest-lexicons`** — 自动检测 manifest file → 推荐 lexicon 写入 `project_lexicon.yaml`
- ✅ **`codd lexicon list/install/diff` + `codd coverage report`** — plug-in 管理 CLI + matrix 报告 (JSON / Markdown / HTML)
- ✅ Generality Gate 三层架构 (Layer A core / Layer B templates / Layer C plug-ins) — core code 中零 specific framework / domain literal hardcode

```bash
pip install codd-dev
```

---

## 快速开始 (5 分钟)

### 1. 安装

```bash
pip install codd-dev
codd --version  # 2.0.0 或更高
```

### 2. 在项目中放置 codd.yaml

```yaml
# codd.yaml
codd_required_version: ">=2.0.0"

dag:
  design_docs:
    - "docs/design/**/*.md"
  implementations:
    - "src/**/*.{ts,tsx,py}"
  tests:
    - "tests/**/*.{spec,test}.{ts,tsx,py}"

repair:
  approval_mode: required   # 自动修复需要人工审批
  max_attempts: 10

llm:
  ai_command: "claude"      # 可调用任意 LLM CLI (claude / codex / gemini 等)
```

### 3. 典型命令

```bash
# 一致性验证 (检查需求、设计、实现、测试的一致性)
codd dag verify

# 带自动修复的验证 (发现违规时由 LLM 生成并应用 patch)
codd dag verify --auto-repair --max-attempts 10

# 在真实浏览器中确认 User Journey PASS (通过 CDP 控制浏览器)
codd dag run-journey login_to_dashboard --axis viewport=smartphone_se

# 从设计文档导出实现步骤 (实现阶段的输入)
codd implement run --task M1.2 --enable-typecheck-loop
```

### 4. 输出说明

`codd dag verify` 会运行 9 种 coherence check:

| Check | 作用 |
|-------|------|
| `node_completeness` | 确认设计文档中声明的节点 (实现/测试文件) 在物理上存在 |
| `transitive_closure` | 确认需求 → 设计 → 实现 → 测试的依赖链是闭合的 |
| `verification_test_runtime` | 确认实现对应的测试可执行并通过 |
| `deployment_completeness` | 确认部署链 (Dockerfile/compose/k8s) 完备 |
| `proof_break_authority` | 确认关键 journey 未被破坏 |
| `screen_flow_edges` | 检测画面迁移图中的孤立节点 |
| `screen_flow_completeness` | 确认所有画面都映射到需求 |
| `c8` | 检测 uncommitted patch / dirty file |
| `c9` (`environment_coverage`) | 验证 viewport / RBAC role / locale 等 **目标环境覆盖性** |

发现违规时部署门会被 block，使用 `--auto-repair` 进入 LLM patch 生成 → 应用 → 重新验证的循环。

---

## 典型用例

### 用例 1: 需求 → 设计 → 实现的自动化

在 `docs/requirements/*.md` 中写「功能需求 + 约束」，调用 `codd implement run`:

1. LLM 从需求动态导出 ImplStep 序列 (Layer 1)
2. 补充最佳实践 (Layer 2，例如登录 → 登出 / Remember Me / 会话超时等)
3. 经用户审批 (HITL gate) 在 `src/**` 中生成实现
4. 生成中若 `tsc` 等 type check 失败，进入自动修复循环

### 用例 2: Auto-Repair (`codd verify --auto-repair`)

在 CI 中运行 `codd dag verify --auto-repair --max-attempts 10`:

1. 执行 9 种 coherence check
2. 通过 Hybrid Classifier (git diff + LLM) 将违规分类为 **可修复 (in-task) / pre-existing (baseline) / unrepairable**
3. 在可修复违规中选 DAG 上最上游的，由 LLM 生成 patch
4. 经过 dry-run 验证后应用并重新验证
5. `max_attempts` 内全部解决 → `SUCCESS`，部分修复 → `PARTIAL_SUCCESS`，仅剩 unrepairable → `REPAIR_FAILED`

`PARTIAL_SUCCESS` 时已修复 patch 仍然反映，剩余违规在 report 中透明列出。

### 用例 3: User Journey Coherence (`codd dag run-journey`)

在 `docs/design/auth_design.md` 的 frontmatter 中声明 user journey:

```yaml
user_journeys:
  - name: login_to_dashboard
    criticality: critical
    steps:
      - { action: navigate, target: "/login" }
      - { action: fill, selector: "input[type=email]", value: "user@example.com" }
      - { action: click, selector: "button[type=submit]" }
      - { action: expect_url, value: "/dashboard" }
```

通过 `codd dag run-journey login_to_dashboard --axis viewport=smartphone_se`:

- 从 `project_lexicon.yaml` 声明的 `viewport=smartphone_se` (375x667) 在 runtime 注入到 CDP
- 在真实浏览器 (Edge / Chrome) 中执行 journey
- 失败时 `codd dag verify` 的 C9 environment_coverage 阻塞部署门

从结构上防止「智能手机专用 nav 消失」之类的事故。

---

## v1.34.0 主要功能

| 功能 | 作用 |
|------|------|
| **DAG 完整性** (C1〜C8) | 跨需求、设计、实现、测试、部署的 9 种 coherence check |
| **Coverage Axis Layer** (C9) | 通过统一抽象 (16+ 轴对应) 验证 viewport / RBAC role / locale 等 **目标环境覆盖性** |
| **LLM Auto-Repair (RepairLoop)** | 违规检测 → LLM 生成 patch → 应用 → 重新验证的循环，尝试在 `max_attempts` 内全部解决 |
| **Hybrid Classifier** | 通过 git diff (Stage 1) + LLM 判断 (Stage 2) 将违规分为 repairable / pre_existing / unrepairable |
| **Primary Picker** | 从多个违规中优先修复 DAG 上最上游 (root cause 候选) |
| **PARTIAL_SUCCESS policy** | 存在 applied_patches、pre_existing、unrepairable 时返回 PARTIAL_SUCCESS，从透明的非当前任务问题中将 release blocker 解除 |
| **BestPracticeAugmenter** | 由 LLM 动态补充设计文档未明记的最佳实践 (如密码重置) |
| **ImplStepDeriver (2-layer)** | 设计文档 → ImplStep 序列的动态展开，Layer 2 推断 `required_axes` |
| **Typecheck Repair Loop** | 实现阶段中 `tsc --noEmit` 等 type check 失败时进入自动修复循环 |
| **`codd version --check --strict`** | 检测项目要求的 CoDD 版本与已安装版本的差异 |

详见 [CHANGELOG.md](CHANGELOG.md)。

---

## 实证案例 — 真实 LMS Web 应用 (Next.js + Prisma + PostgreSQL)

在真实项目 (LMS、仅 Web、主要单一 viewport) 上运行 `codd verify --auto-repair --max-attempts 10` 的结果:

```
status:                PARTIAL_SUCCESS
attempts:              4
applied_patches:       4
pre_existing_violations:  1
unrepairable_violations:  2
remaining_violations:     3 (skip + report 完成)
smoke proof:           6 checks PASS
CoDD core 修改:        0 行
```

修复的文件:
- `tests/e2e/environment-coverage.spec.ts`
- `tests/e2e/login.spec.ts`

跳过的违规 (作为 CoDD 责任外明示在 report 中):
- pre_existing: deployment_completeness chain
- unrepairable: Dockerfile dry-run patch validation
- unrepairable: Vitest matcher runtime issue

C9 environment_coverage 验证了 viewport (smartphone_se / desktop_1920) 和 RBAC role (central_admin / tenant_admin / learner) 的 axis × variant 全覆盖，达到 PASS。

**此次实证的覆盖范围**:
- ✅ Next.js + Prisma + TS 栈上 auto-repair 可以 `PARTIAL_SUCCESS` 完成
- ✅ CoDD core 修改 **0 行**即可吸收项目特异需求 (Generality 维持)
- ⚠️ 仅 1 项目 1 栈的 dogfooding，其他领域 (Mobile / Desktop / CLI / 嵌入式 / ML / Game) 尚未验证
- ⚠️ unrepairable=2 残留 = 不是全自动而是 semi-automated

---

## 架构 — 4 release 演进与下期计划

### 已达成 (v1.31.0 〜 v1.34.0)

| Release | 到达点 |
|---------|--------|
| v1.31.0 | 内侧 100% (内部一致性 coherence) — 通过 typecheck repair loop 消除「手动 type fix」 |
| v1.32.0 | 外侧 100% (目标环境覆盖性 Coverage Axis Layer C9) — 用统一抽象吸收 viewport/RBAC/locale 等 |
| v1.33.0 | caveats 解决路径实证 — 真机 CDP run-journey + LLM auto-repair attempt PASS |
| **v1.34.0** | **full pipeline 完整实证** — Next.js Web 1 项目 dogfooding 上 auto-repair PARTIAL_SUCCESS 完成 |

### 下期 (v1.35.0 〜 v2.0.0、Roadmap)

| Release | 计划 |
|---------|------|
| **v1.35.0** | **`codd elicit`** — AI 从需求文档抽出 axis 候选 + 规格漏洞的 Discovery Engine |
| v1.36.0 | BABOK lexicon (`@codd/lexicon/babok`) 包含 + multi-formatter (md/json/PR comment) |
| v1.37.0 | **`codd diff`** — brownfield 用、需求 vs 实现的 drift 检测 |
| v1.38.0 | extract → diff → elicit 流水线化、brownfield 完整流程 |
| v1.39.0 | unrepairable 削减 (RepairLoop 的 repair strategy 通用化) |
| v1.40.0 | 其他领域 dogfooding (Mobile / CLI / embedded 等) |
| (v2.0.0) | elicit 与 verify 双向 loop，最接近北极星「全自动」 |

详见 [CHANGELOG.md](CHANGELOG.md)。

---

## North Star 接续: `codd elicit` (v1.35.0)

北极星「功能需求 + 约束就够全自动」最大的差距是 **「需求需是完整的」这一前提**。需求有漏洞就会形成实现漏洞，并以 demo 前事故的形式出现 (例: 中央管理员在智能手机 viewport 时 nav 消失)。

`codd elicit` 从结构上解决:

```bash
$ codd elicit
[INFO] Reading docs/requirements/requirements.md (483 lines)
[INFO] Loading project_lexicon.yaml + @codd/lexicon/babok ...
[INFO] Generated 27 findings (axis_candidates: 11, spec_holes: 16)
[OK]   findings.md created
```

```markdown
## f-001 [axis_candidate] locale (severity: high)
**details**: variants: ja_JP, en_US / source: persona 描述及需求 3.5
**approved**: yes
**note**: en_US 在 phase2

## f-002 [spec_hole] 视频播放中关闭浏览器，进度会丢失吗? (severity: high)
**approved**: yes
```

```bash
$ codd elicit apply findings.md
[OK] project_lexicon.yaml updated (11 axis sections appended)
[OK] docs/requirements/requirements.md updated (TODO 追加)
$ git add -A && git commit -m "feat: apply elicit findings"
```

人类只做 **审查需求 (extract 结果)** 和 **Yes/No 审批 (elicit findings)**。其余都是 AI 动态发散和收敛。

---

## Generality Gate (绝对维持通用性)

CoDD core code 中以下 hardcode 是 **禁止** 的:

- 特定 stack 名 (Next.js / Django / Rails / FastAPI 等)
- 特定 framework / library 字面量
- 特定 domain (Web / Mobile / Desktop / CLI / Backend / Embedded)
- 特定 viewport 值 (375 / 1920 等) 或 device 名 (iPhone / Android 等)
- 特定 axis 种类 (viewport / locale / a11y) 或 finding kind (axis_candidate / spec_hole) 在 core 中列举

这些都封闭在 **`project_lexicon.yaml` (项目特定)** 或 **lexicon plug-in (`@codd/lexicon/babok` 等)** 中。CoDD 仅作为通用的 violation/finding object 处理。

LLM 提案「stack 特定的最优 patch」时，该判断委托给 **LLM 的知识**，CoDD core 不决定 (= 不 overfitting)。

---

## 贡献者

CoDD 由以下成员塑造:

- **[@yohey-w](https://github.com/yohey-w)** — Maintainer / Architect
- **[@Seika86](https://github.com/Seika86)** — Sprint regex 见解 (PR #11)
- **[@v-kato](https://github.com/v-kato)** — brownfield 复现报告 (Issues #17 / #18 / #19)
- **[@dev-komenzar](https://github.com/dev-komenzar)** — `source_dirs` bug 复现 (Issue #13)

欢迎来自外部的 issue / PR / lexicon 提议 — 详见 [Issues](https://github.com/yohey-w/codd-dev/issues)。

---

## 许可证

MIT License — 详见 [LICENSE](LICENSE)。

## 链接

- [CHANGELOG.md](CHANGELOG.md) — 全部 release notes
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) — 开发支援
- [Issues](https://github.com/yohey-w/codd-dev/issues) — bug 报告 / 功能请求

---

> 「代码改变时，CoDD 追踪影响范围、检测违规、为 merge 判断生成证据。」
