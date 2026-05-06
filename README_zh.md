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
  中文 | <a href="README.md">English</a> | <a href="README_ja.md">日本語</a>
</p>

---

## 只需编写功能需求和约束，CoDD 将全自动完成后续工作。

CoDD 是一个开发引擎，它将 **需求 → 设计 → 实现 → 测试** 作为一张 DAG 处理，对各节点之间的一致性 (coherence) 进行机器验证，并在发现不一致时让 LLM 自动修复。

人类只需要写清楚 **“要做什么” 和 “做到什么程度”**。至于“如何实现”，交给 CoDD 与 LLM。

```bash
pip install codd-dev
```

---

## Quick Start (5 分钟)

### 1. 安装

```bash
pip install codd-dev
codd --version  # 1.34.0 或更高
```

### 2. 在项目中放置 codd.yaml

```yaml
# codd.yaml
codd_required_version: ">=1.34.0"

dag:
  design_docs:
    - "docs/design/**/*.md"
  implementations:
    - "src/**/*.{ts,tsx,py}"
  tests:
    - "tests/**/*.{spec,test}.{ts,tsx,py}"

repair:
  approval_mode: required   # 自动修复需要人工批准
  max_attempts: 10

llm:
  ai_command: "claude"      # 可调用任意 LLM CLI (claude / codex / gemini 等)
```

### 3. 常用命令

```bash
# 一致性验证（检查需求、设计、实现、测试之间的一致性）
codd dag verify

# 带自动修复的验证（发现违规后由 LLM 生成并应用 patch）
codd dag verify --auto-repair --max-attempts 10

# User Journey 的真实浏览器 PASS 验证（通过 CDP 操作浏览器）
codd dag run-journey login_to_dashboard --axis viewport=smartphone_se

# 从设计文档导出实现步骤（实现阶段的输入）
codd implement run --task M1.2 --enable-typecheck-loop
```

### 4. 如何阅读输出

`codd dag verify` 会运行 9 类 coherence check：

| Check | 作用 |
|-------|------|
| `node_completeness` | 设计文档中声明的节点（实现/测试）是否作为物理文件存在 |
| `transitive_closure` | 需求 → 设计 → 实现 → 测试的依赖链是否闭合 |
| `verification_test_runtime` | 针对实现的测试是否可执行并 PASS |
| `deployment_completeness` | 部署链（Dockerfile/compose/k8s）是否完备 |
| `proof_break_authority` | 关键 journey 是否没有被破坏 |
| `screen_flow_edges` | 画面跳转图中是否没有孤立节点 |
| `screen_flow_completeness` | 所有画面是否都映射到需求 |
| `c8` | 检测 uncommitted patch / dirty file |
| `c9` (`environment_coverage`) | viewport / RBAC role / locale 等 **目标环境覆盖率** |

发现 violation 时会阻断 deploy gate；使用 `--auto-repair` 时，会进入 LLM patch 生成 → 应用 → 再验证的循环。

---

## 典型用例

### 用例 1：需求 → 设计 → 实现自动化

在 `docs/requirements/*.md` 中编写“功能需求 + 约束”，再调用 `codd implement run`：

1. LLM 会从需求动态导出 ImplStep 列表 (Layer 1)
2. 补全最佳实践 (Layer 2，例如登录 → 登出/Remember Me/会话超时等)
3. 经过用户批准 (HITL gate) 后，在 `src/**` 中生成实现
4. 生成过程中若 `tsc` 等 type check 失败，则进入自动修复循环

人类可以体验到“只写功能需求 + 约束，其余全自动”的工作流。

### 用例 2：Auto-Repair (codd verify --auto-repair)

在 CI 中运行 `codd dag verify --auto-repair --max-attempts 10`：

1. 执行 9 类 coherence check
2. 通过 Hybrid Classifier (git diff + LLM) 将 violation 分类为 **可修复 (in-task) / 既存问题 (baseline) / 不可修复 (unrepairable)**
3. 从可修复 violation 中选出 DAG 上最上游的一项，由 LLM 生成 patch
4. 经过 dry-run validation 后 apply，并再次验证
5. 在 max_attempts 内全部解决 → `SUCCESS`，部分修复 → `PARTIAL_SUCCESS`，几乎都是不可修复 → `REPAIR_FAILED`

即便结果为 `PARTIAL_SUCCESS`，已修复的 patch 仍会被反映，残留 violation 会列在 report 中以保证透明性。

### 用例 3：User Journey Coherence (codd dag run-journey)

在 `docs/design/auth_design.md` 的 frontmatter 中编写用户旅程：

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

执行 `codd dag run-journey login_to_dashboard --axis viewport=smartphone_se` 后：

- 将 `project_lexicon.yaml` 中声明的 `viewport=smartphone_se` (375x667) 注入到 CDP runtime
- 使用真实浏览器 (Edge / Chrome) 执行 journey
- 失败时，`codd dag verify` 的 C9 environment_coverage 会阻断 deploy gate

这样可以结构性地防止“只在手机端导航消失”等事故。

---

## v1.34.0 主要功能

| 功能 | 作用 |
|------|------|
| **DAG 完整性** (C1〜C8) | 对需求、设计、实现、测试、部署执行 9 类 coherence check |
| **Coverage Axis Layer** (C9) | 使用统一抽象（支持 16+ 轴）验证 viewport / RBAC role / locale 等 **目标环境覆盖率** |
| **LLM Auto-Repair (RepairLoop)** | violation 检测 → LLM patch 生成 → apply → 再验证，在 `max_attempts` 内尝试全部解决 |
| **Hybrid Classifier** | 通过 git diff (Stage 1) + LLM 判断 (Stage 2) 将 violation 分类为 repairable / pre_existing / unrepairable |
| **Primary Picker** | 在多个 violation 中优先修复 DAG 上最上游的一项（root cause 候选） |
| **PARTIAL_SUCCESS policy** | 只要存在 applied_patches OR pre_existing OR unrepairable 即为 PARTIAL_SUCCESS，可避免 CI 成为 release blocker |
| **BestPracticeAugmenter** | 由 LLM 动态补全设计文档未明写的最佳实践（如密码重置等） |
| **ImplStepDeriver (2-layer)** | 从设计文档动态展开 ImplStep 列表，并在 Layer 2 推断 `required_axes` |
| **Typecheck Repair Loop** | 实现阶段若 `tsc --noEmit` 等 type check 失败，则进入自动修复循环 |
| **`codd version --check --strict`** | 检测项目要求版本与已安装 codd 版本之间的差异 |

详情请参阅 [CHANGELOG.md](CHANGELOG.md)。

---

## 实证案例研究 — 真实 LMS 项目

在实际 LMS 项目 (Next.js + Prisma + PostgreSQL) 上执行 `codd verify --auto-repair --max-attempts 10`，结果如下：

```
status:                PARTIAL_SUCCESS
attempts:              4
applied_patches:       4
pre_existing_violations:  1
unrepairable_violations:  2
remaining_violations:     3 (skip + report 已完成)
smoke proof:           6 checks PASS
CoDD core 修改:        0 行
```

被修复的文件：
- `tests/e2e/environment-coverage.spec.ts`
- `tests/e2e/login.spec.ts`

被跳过的 violation（作为 CoDD 责任范围外的问题在 report 中明确记录）：
- pre_existing: deployment_completeness chain
- unrepairable: Dockerfile dry-run patch validation
- unrepairable: Vitest matcher runtime issue

C9 environment_coverage 验证了 viewport (smartphone_se / desktop_1920) 与 RBAC role (central_admin / tenant_admin / learner) 的 axis × variant 全覆盖，并最终 PASS。

---

## 架构 — 4 个 release 的演进

| Release | 达成状态 |
|---------|----------|
| v1.31.0 | 内侧 100%（内部一致性 coherence）— 通过 type check repair loop 消灭“手动 type fix” |
| v1.32.0 | 外侧 100%（目标环境覆盖率 Coverage Axis）— 以统一抽象吸收 viewport/RBAC/locale 等 |
| v1.33.0 | caveats 解决路径实证 — 真实 CDP run-journey + LLM auto-repair attempt PASS |
| **v1.34.0** | **full pipeline 完全实证** — 在真实项目中 auto-repair PARTIAL_SUCCESS 完整跑通 |

各 release 详情请参阅 [CHANGELOG.md](CHANGELOG.md)。

---

## Generality Gate（绝对保持通用性）

CoDD core code 中 **禁止** 以下 hardcode：

- 特定 stack 名称 (Next.js / Django / Rails / FastAPI 等)
- 特定 framework / library 的 literal
- 特定 domain (Web / Mobile / Desktop / CLI / Backend / Embedded)
- 特定 viewport 值 (375 / 1920 等) 或 device 名称 (iPhone / Android 等)

这些内容全部封装在 **`project_lexicon.yaml`（项目固有）** 中。CoDD 只把它们作为 generic violation object 处理。

当 LLM 提出“针对特定 stack 的最佳 patch”时，该判断交由 **LLM 的知识** 完成，CoDD core 不做决定（也就是不过度拟合）。

---

## 许可证

MIT License — 参阅 [LICENSE](LICENSE)。

## 链接

- [CHANGELOG.md](CHANGELOG.md) — 全部 release notes
- [GitHub Sponsors](https://github.com/sponsors/yohey-w) — 支持开发
- [Issues](https://github.com/yohey-w/codd-dev/issues) — Bug 报告 / 功能请求

---

> “当代码发生变化时，CoDD 会追踪影响范围，检测 violation，并为合并判断生成证据。”
