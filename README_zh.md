<p align="center">
  <strong>CoDD — 一致性驱动开发（Coherence-Driven Development）</strong><br>
  <em>AI 辅助开发中变更管理的证据引擎。</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/v/codd-dev?style=flat-square&color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/codd-dev/"><img src="https://img.shields.io/pypi/pyversions/codd-dev?style=flat-square" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License"></a>
  <a href="https://github.com/yohey-w/codd-dev/stargazers"><img src="https://img.shields.io/github/stars/yohey-w/codd-dev?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README_ja.md">日本語</a> | 中文
</p>

---

> *当代码发生变更时，CoDD 追踪受影响的部分，检查违反的约束，并为你的合并决策提供完整的证据链。*

```
pip install codd-dev
```

## 🆕 v1.16.0-alpha — Coherence Engine（一致性驱动的中央枢纽）

**通过 `DriftEvent` 统一格式连接 drift / validate / propagate / fix 的中央枢纽。**

| 组件 | 作用 |
|------|------|
| `DriftEvent` | 包含 source/target/change_type/payload/severity/fix_strategy/kind 的统一事件类型 |
| `EventBus` | 进程内 pub/sub。Detector publish、Orchestrator subscribe |
| `Orchestrator` | severity 路由: `red` → auto-fix / `amber` → pending HITL / `green` → log |
| `coherence_adapters` | drift / validate / design-token violation 输出 → DriftEvent 转换 |
| `codd propagate --coherence` | 注入 lexicon + DESIGN.md 到 AI prompt（防止术语漂移、色值不一致） |
| Fixer Coherence-Mode | `run_fix(coherence_event=...)` 时允许修改设计文档（test 失败修复流程保持分离） |

auto-fix 失败时自动降级为 amber，记录到 `docs/coherence/pending_hitl.md` 作为待人工审查的条目。ntfy 通知带速率限制（默认 60 秒）防止通知爆炸。**现有 CLI（`codd drift` / `validate` / `propagate` / `fix`）100% 向后兼容**，不传 `--coherence` 时维持原有行为。

⚠️ **alpha 版本**: Phase 4+（Detector ↔ Applier 直接配管 / `codd fixup-drift` 子命令）将在下一版本实现。本次发布为中央枢纽的架构确立阶段。

---

## 🆕 v1.14.0 — `codd implement` 的 Batch guard

`codd implement` 支持 `--max-tasks N`（默认 30）和 `--wave WAVE_ID`，可安全分批执行大型 implementation plan。**preflight task count guard** 防止 AI 失控 fan-out，并返回包含 `--wave` / `--max-tasks` / `--task` 替代方案的 actionable error message。

```bash
codd implement --max-tasks 30           # 超过 30 个则 abort
codd implement --wave wave_2_1          # 仅执行 wave_2_1 的 tasks
```

> v1.13.1 是 `DesignTokenDriftLinker` 的 `project_root` Path 转换 bug 修复补丁。

---

## 🆕 v1.13.0 — DESIGN.md 集成（Google Stitch OSS, W3C Design Tokens）

**实现从 UI 设计到代码生成的完整可追溯性。**

| 功能 | 说明 |
|------|------|
| `DesignMdExtractor` | 自动解析 DESIGN.md（W3C Design Tokens spec） |
| `KnowledgeFetcher` UI 检测 | 自动识别 React/Vue/Svelte/Flutter 等并建议采用 DESIGN.md |
| `codd implement` 注入 | 生成 UI 文件时自动将 DESIGN.md tokens 加入 AI prompt |
| `codd validate --design-tokens` | 检测硬编码的 #hex/px 值并推荐参照 DESIGN.md |
| `codd drift` design_token | 比对 UI 实现的 token 引用与 DESIGN.md 定义集合 |
| `codd verify --design-md` | 集成 `npx @google/design.md lint` 到 CoDD 报告 |

```yaml
# DESIGN.md 示例（放在项目根目录）
---
version: "1.0"
name: "My App"
colors:
  Primary: "#1A73E8"
components:
  Button.primary:
    background: "{colors.Primary}"
---
```

规格说明: [google-labs-code/design.md](https://github.com/google-labs-code/design.md)

---

## 🆕 v1.12.0 — 元设计上下文层（project_lexicon）

CoDD 增加 **元设计上下文层**。在 `project_lexicon.yaml` 中一次性声明项目的术语表、命名规约、设计原则后，所有 AI 命令（require / plan / generate / implement）都会自动使用。

- 📖 `ProjectLexicon` — 声明节点术语、命名规约、设计原则、failure modes
- 🌐 `KnowledgeFetcher` — Web Search 优先的知识层，30 天缓存。CoDD 核心零硬编码框架知识
- 🔍 `codd validate --lexicon` — 检测 lexicon 内的命名规约违反
- 🔌 Extractor registry — 通过 Python module path 声明 extractor 类。`FileSystemRouteExtractor` 是首个注册示例
- 🧙 Lexicon wizard — `codd plan` 在 lexicon 不存在时自动生成 draft `project_lexicon.yaml`
- 📋 `CoverageAuditor` — 需求 gap 检测，AUTO_ACCEPT / ASK / AUTO_REJECT 三分类规则
- 🏷️ Provenance tracking — 每个 lexicon 条目带 `provenance` / `confidence` / `fetched_at`

---

## 🆕 v1.11.0 — 文件系统路由感知的 Drift 检测

CoDD 现在理解文件系统路由框架（Next.js, SvelteKit, Nuxt, Astro, Remix），可检测设计文档与实现之间的 URL drift。

- 📐 `FileSystemRouteExtractor` — 从目录结构提取 endpoint 节点
- 🔗 `DocumentUrlLinker` — 自动将设计文档的 URL 链接到 endpoint
- 🔍 `codd drift` — 检测设计与实现的 URL gap
- 🎨 `codd extract --layer routes` — 反向生成 screen-flow 图

详细信息请参阅英文版 README 的 [Filesystem Routing Adapter Recipes](README.md#filesystem-routing-adapter-recipes)。

---

**v1.9.0** — `codd implement` 支持**多 AI 引擎**（Claude stdout + Codex 文件写入）和**阶段内自动并行执行**（通过 git worktree 隔离）。支持阶段里程碑格式（`#### M1.1`）。重度推理模型的超时时间延长至 1 小时。SWE-bench Verified: **73/73 = 100%** 已解决。

---

## 为什么选择 CoDD？

AI 能生成规格说明书。但 **上游变更时会发生什么？**

所有规格优先工具都止步于创建阶段。CoDD 从那里开始。当需求变更、代码更新或设计假设偏移时，CoDD **自动将变更向下游传播** — 更新受影响的设计文档，标记过时的制品，并生成完整的证据链。

```
需求变更 → codd impact 识别出 6 个受影响的文档
代码变更 → codd propagate 更新下游设计
设计变更 → CEG 图追踪所有依赖制品
```

没有其他工具能做到这一点。spec-kit、Kiro 和 cc-sdd 只负责创建文档。**CoDD 确保它们始终保持一致。**

## 工作原理

```
需求（人工编写）  →  设计文档（AI 生成）  →  代码和测试（AI 生成）
       ↕                    ↕                      ↕
   codd impact        codd propagate          codd extract
  （什么变了？）     （更新下游）          （逆向工程）
```

### 三层架构

```
Harness（CLAUDE.md、Hooks、Skills）   ← 规则、护栏、工作流
  └─ CoDD（方法论）                    ← 跨变更一致性
       └─ 设计文档（docs/*.md）        ← CoDD 管理的制品
```

CoDD **与 harness 无关** — 可与 Claude Code、Copilot、Cursor 或任何 Agent 框架配合使用。

## 核心原则：推导，而非配置

| 架构 | 推导出的测试策略 | 需要配置？ |
|---|---|---|
| Next.js + Supabase | vitest + Playwright | 无需 |
| FastAPI + Python | pytest + httpx | 无需 |
| Go CLI 工具 | go test | 无需 |

**上游决定下游。** 你定义需求和约束，AI 推导出其余一切。

## 快速开始

### 全新项目（Greenfield）

```bash
pip install codd-dev
mkdir my-project && cd my-project && git init

# 初始化 — 传入需求文件，支持任意格式
codd init --project-name "my-project" --language "typescript" \
  --requirements spec.txt

# AI 设计文档依赖图
codd plan --init

# 按波次生成设计文档
waves=$(codd plan --waves)
for wave in $(seq 1 $waves); do
  codd generate --wave $wave
done

# 质量门禁 — 捕获 AI 偷懒（TODO、占位符）
codd validate

# 从设计文档生成代码
codd implement

# 组装代码片段为可构建的项目
codd assemble
```

### 已有项目（Brownfield）

```bash
codd extract              # 从代码逆向工程设计文档
codd require              # 从代码推断需求（构建了什么以及为什么）
codd plan --init          # 从提取的文档生成 wave_config
codd scan                 # 构建依赖图
codd impact               # 变更影响分析
codd audit --skip-review  # 完整变更审查：验证 + 影响 + 策略
codd measure              # 项目健康评分（0-100）
```

## 演示

### 可复现的端到端演示 — 3 种传播模式

以下演示固定在提交 [`d7d9f45`](https://github.com/yohey-w/codd-dev/commit/d7d9f45)，你可以在本地完整复现。

**设置：**
```bash
pip install codd-dev>=1.6.0
mkdir demo && cd demo && git init
cat > spec.txt << 'EOF'
TaskFlow — 需求
- 用户认证（邮箱 + Google OAuth）
- 工作区管理（团队、角色、邀请）
- 任务 CRUD（负责人、标签、截止日期）
- 实时更新（WebSocket）
- 文件附件（S3）
- 通知系统（应用内 + 邮件）
EOF
codd init --project-name "taskflow" --language "typescript" --requirements spec.txt
```

**模式 1 — 源 → 文档**（规格 → 设计文档）：
```bash
codd plan --init
for wave in $(seq 1 $(codd plan --waves)); do codd generate --wave $wave; done
codd validate        # 预期：PASS，0 个错误
codd scan            # 预期：17 个节点，30+ 条边
```

**模式 2 — 文档 → 文档**（需求变更 → 下游更新）：
```bash
# 编辑需求：为认证模块添加 "SSO (SAML 2.0)"
codd impact          # 预期：7 个设计文档中有 6 个在 Green/Amber 区间

# 重新生成受影响的波次（propagate 仅用于代码→文档）
codd generate --wave 1 --force   # 从更新的需求重新推导验收标准
codd generate --wave 2 --force   # 从更新的 Wave 1 重新推导系统设计
# 按依赖顺序对每个受影响的波次重复操作
```

**模式 3 — 文档 → 文档（通过 CEG）**（代码变更 → 设计更新）：
```bash
# 修改认证模块的源代码
codd propagate       # 预期：识别出 auth-design、system-design 受影响
codd propagate --update  # AI 根据代码差异更新受影响的设计文档
```

**预期输出**：20 行规格 → 17 个设计制品（5,100+ 行）→ 下游传播在变更后保持所有文档一致。模式 3（基于 CEG 的传播）是创新性的 — 没有其他工具能通过依赖图追踪代码变更回溯更新设计文档。

### Greenfield — 从规格到可运行应用

37 行规格 → 6 个设计文档（1,353 行）→ 102 个代码文件（6,445 行）→ TypeScript strict 构建通过。无需交互式 AI 对话 — 整个工作流就是一个 shell 脚本。

完整教程：[Harness as Code — CoDD 指南 #1](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide?locale=en)

### Brownfield — 变更影响分析

需求中修改 2 行 → `codd impact` 识别出 7 个设计文档中有 6 个受影响。Green 区间：AI 自动更新。Amber 区间：人工审查。在任何东西出问题之前，你就知道需要修复什么。

深度解析：[CoDD 深度解读](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence?locale=en)

## 波次生成

设计文档按依赖顺序生成 — 每个波次依赖于前一个：

```
Wave 1  验收标准 + ADR             ← 仅需求
Wave 2  系统设计                   ← 需求 + Wave 1
Wave 3  数据库设计 + API 设计       ← 需求 + Wave 1-2
Wave 4  UI/UX 设计                 ← 需求 + Wave 1-3
Wave 5  实施计划                   ← 以上全部
```

验证自底向上进行（V-Model）：

```
单元测试          ← 验证详细设计
集成测试          ← 验证系统设计
端到端/系统测试   ← 验证需求 + 验收标准
```

## Frontmatter = 唯一事实来源

依赖关系在 Markdown 的 frontmatter 中声明。无需单独的配置文件。

```yaml
---
codd:
  node_id: "design:api-design"
  modules: ["api", "auth"]        # ← 链接到源代码模块
  depends_on:
    - id: "design:system-design"
      relation: derives_from
    - id: "req:my-project-requirements"
      relation: implements
---
```

`modules` 字段实现反向可追溯性：当源代码变更时，`codd extract` 识别受影响的模块，`modules` 字段将这些模块映射回需要更新的设计文档。

`codd/scan/` 是缓存 — 每次 `codd scan` 时重新生成。

## 自定义节点前缀

默认情况下，`node_id` 值必须使用内置前缀之一（`design:`、`req:`、`doc:`、`module:` 等）。要将 CoDD 用于非软件领域（知识库、审查文档、提示词管理），在 `codd.yaml` 中添加自定义前缀：

```yaml
# codd.yaml
prefixes:
  - knowledge
  - schema
  - review
  - prompt
```

自定义前缀**与内置默认值合并** — 无需重新列出 `design`、`req` 等。前缀名称仅允许小写字母和下划线（`[a-z_]+`）。

## AI 模型配置

CoDD 调用外部 AI CLI 进行文档生成。默认使用 Claude Opus：

```yaml
# codd.yaml
ai_command: "claude --print --model claude-opus-4-6"
```

### 按命令覆盖

不同命令可以使用不同模型。例如，使用 Opus 生成设计文档，使用 Codex 实现代码：

```yaml
ai_command: "claude --print --model claude-opus-4-6"   # 全局默认
ai_commands:
  generate: "claude --print --model claude-opus-4-6"    # 设计文档生成
  restore: "claude --print --model claude-opus-4-6"     # brownfield 重建
  review: "claude --print --model claude-opus-4-6"      # 质量评审
  plan_init: "claude --print --model claude-sonnet-4-6" # wave_config 规划
  implement: "codex --print"                             # 代码生成
```

**优先级**：CLI `--ai-cmd` 参数 > `ai_commands.{command}` > `ai_command` > 内置默认值（Opus）。

## 命令

| 命令 | 状态 | 描述 |
|---------|--------|-------------|
| `codd init` | **稳定** | 在任何项目中初始化 CoDD |
| `codd scan` | **稳定** | 从 frontmatter 构建依赖图 |
| `codd impact` | **稳定** | 变更影响分析（Green / Amber / Gray） |
| `codd validate` | **Alpha** | Frontmatter 完整性和图一致性检查 |
| `codd generate` | 实验性 | 按波次顺序生成设计文档（greenfield） |
| `codd restore` | 实验性 | 从提取的事实重建设计文档（brownfield） |
| `codd plan` | 实验性 | 波次执行状态（`--init` 支持 brownfield 回退） |
| `codd verify` | 实验性（Pro） | V-Model 验证 |
| `codd implement` | 实验性 | 设计到代码生成 |
| `codd propagate` | **Alpha** | 将代码/文档变更向下游传播到受影响的设计文档 |
| `codd review` | 实验性（Pro） | AI 驱动的制品质量评估（LLM-as-Judge） |
| `codd extract` | **Alpha** | 从现有代码逆向工程设计文档 |
| `codd require` | **Alpha** | 从现有代码库推断需求（brownfield） |
| `codd audit` | **Alpha**（Pro） | 综合变更审查包（验证 + 影响 + 策略 + 评审） |
| `codd policy` | **Alpha** | 企业策略检查器（源代码中的禁止/必需模式） |
| `codd measure` | **Alpha** | 项目健康度量（图、覆盖率、质量、健康评分 0-100） |
| `codd mcp-server` | **Alpha** | 用于 AI 工具集成的 MCP 服务器（stdio，零依赖） |

## OSS / Pro 划分

CoDD v1.6.0 通过桥接模式引入了清晰的 OSS/Pro 边界。

**OSS（MIT，免费）** — 保持文档一致性所需的一切：

`init` · `scan` · `impact` · `generate` · `restore` · `propagate` · `extract` · `require` · `plan` · `validate` · `measure` · `policy` · `mcp-server`

**Pro（私有，付费）** — 企业级审查和验证：

`review` · `verify` · `audit` · `risk`

```bash
# 仅 OSS
pip install codd-dev

# 添加 Pro 扩展
pip install "codd-pro @ git+ssh://git@github.com/yohey-w/codd-pro.git"
```

当 `codd-pro` 已安装时，Pro 实现通过 entry-points 插件发现自动覆盖 OSS 回退。未安装时，Pro 命令显示迁移提示并正常退出。无需额外配置。

## CI 集成（GitHub Action）

在每个 Pull Request 上运行 CoDD 审计。该 Action 会发布评论，包含裁定（APPROVE / CONDITIONAL / REJECT）、验证结果、策略违规和影响分析。

### 快速设置

在你的项目中添加 `.github/workflows/codd.yml`：

```yaml
name: CoDD Audit
on:
  pull_request:
    branches: [main]

permissions:
  contents: read
  pull-requests: write

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: yohey-w/codd-dev@main
        with:
          diff-target: origin/${{ github.base_ref }}
          skip-review: "true"  # 设置为 "false" 启用 AI 审查
```

## 与其他规格驱动工具的区别

所有主流规格驱动工具都专注于**创建**设计文档。没有一个解决文档**变更后**会发生什么的问题。CoDD 通过依赖图、影响分析和分区更新协议填补了这个空白。

| | **spec-kit**（GitHub） | **Kiro**（AWS） | **cc-sdd**（gotalab） | **CoDD** |
|--|---|---|---|---|
| 重点 | 规格创建 | 集成 SDD 的 IDE | Claude Code 的 Kiro 风格 SDD | **创建后的一致性维护** |
| 变更传播 | 否 | 否 | 否 | **`codd impact` + 依赖图** |
| 影响分析 | 否 | 否 | 否 | **Green / Amber / Gray 分区** |
| Harness 锁定 | GitHub Copilot | Kiro IDE | Claude Code | **任何 Agent / IDE** |

简而言之：spec-kit、Kiro 和 cc-sdd 回答的是*"如何创建规格？"* CoDD 回答的是*"当上游发生变更时，如何自动更新所有下游？"*

## SWE-bench 验证

CoDD 在 SWE-bench Lite（真实开源项目的 bug 修复基准）上进行了验证。在 brownfield 场景下——给定一个现有代码库和需要修复的 bug——CoDD 的设计文档提取和自主修复循环实现了显著的改善：

| 阶段 | 方法 | 解决率 |
|---|---|---|
| 单次尝试（Phase 1） | CoDD extract + Claude Opus 4.6 | 57.5% |
| 单次尝试（Phase 1） | CoDD extract + GPT-5.4 | 60.3% |
| 自主循环（Phase 2） | Phase 1 失败 → DIVERGENT 策略重试 | *进行中* |
| 30 题试点 | 自主循环 v2 | 90.0% |

Phase 2 使用 DIVERGENT 策略：当同一类型连续失败 2 次后，强制切换假设方向。这实现了从 60% → 90% 的跳跃——不是靠对基准测试过拟合，而是靠系统性地探索不同的修复假设。

## 实际使用

在生产 Web 应用上经过实战检验 — 18 个设计文档通过依赖图连接。所有文档、代码和测试都由 AI 按照 CoDD 生成。当项目中期需求变更时，`codd impact` 识别了受影响的制品，AI 自动修复了它们。

### CoDD 管理自身的开发

CoDD 使用自身进行开发（吃自己的狗粮）。`.codd/` 目录包含 CoDD 自己的配置，`codd extract` 从自己的源代码逆向工程设计文档：

```bash
codd init --config-dir .codd --project-name "codd-dev" --language "python"
codd extract          # 15 个模块 → 带依赖 frontmatter 的设计文档
codd scan             # 49 个节点，83 条边
codd verify           # mypy + pytest（434 个测试通过）
```

如果 CoDD 管理不了自己，它也不应该管理你的项目。

## 文章

- [dev.to: Harness as Code — 像基础设施一样对待 AI 工作流](https://dev.to/yohey-w/harness-as-code-treating-ai-workflows-like-infrastructure-27ni)
- [dev.to: "规格优先"之后会发生什么](https://dev.to/yohey-w/codd-coherence-driven-development-what-happens-after-spec-first-514f)
- [Zenn: Harness as Code — CoDD 指南 #1 规格 → 设计 → 代码](https://zenn.dev/shio_shoppaize/articles/codd-greenfield-guide?locale=en)
- [Zenn: Harness as Code — CoDD 指南 #2 Brownfield](https://zenn.dev/shio_shoppaize/articles/shogun-codd-brownfield?locale=en)
- [Zenn: Harness as Code — CoDD 指南 #3 使用 CoDD extract 修复 Bug（SWE-bench）](https://zenn.dev/shio_shoppaize/articles/codd-swebench-pilot?locale=en)
- [Zenn: CoDD 深度解读](https://zenn.dev/shio_shoppaize/articles/shogun-codd-coherence?locale=en)

## 赞助

<a href="https://github.com/sponsors/yohey-w">
  <img src="https://img.shields.io/badge/Sponsor-%E2%9D%A4-ea4aaa?style=for-the-badge&logo=github-sponsors" alt="Sponsor">
</a>

您的赞助让 CoDD 持续免费并推动开发。查看[赞助等级](https://github.com/sponsors/yohey-w)。

## 许可证

MIT
