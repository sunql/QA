# 研究型 Agent 入口 — 体验与功能缺口修复 设计文档

> **本设计是本次修复的 SSOT 实现契约。** 落 SSOT 摘要于 `Harness/changes/feat-research-entry-ux-fixes/summary.md`。
>
> 状态：待用户审阅
> 日期：2026-10-05
>
> **脑暴来源：** 2026-10-05 用户实测反馈（7 条）。
> **前序设计：** `docs/superpowers/specs/2026-10-04-research-entry-design.md`（研究入口本体，已合并）。

## 1. 目标

修复研究型 Agent 入口上线后用户实测暴露的 7 项体验 / 功能缺口。

**核心边界：全部为增量修复，不改动已合并的研究流水线语义** —— 意图分类 / ESL / 规划 / 执行 / 假设 / 验证的行为零变化。唯一触碰共享层的是第 1 条（模型选择）注入一处"会话指定模型优先"的分支，且为新增可选路径、默认关闭（不选 = 现有自动路由行为逐字不变）。

**验收标准（用户视角、可测量）：**

| # | 用户诉求 | 可测量的验收条件 |
|---|---|---|
| 1 | 模型选择 | 新建会话时可选模型；所选模型在**后续追问**与**进程重启后 resume** 仍生效；不选 = 自动路由行为与今天一致 |
| 2 | 语义歧义可见 | 歧义检查点卡片**列出**候选指标（含名称与置信度）或 wiki 冲突项，而非仅一句问句 |
| 3 | 检查点内容可见 | 计划相位列出步骤；假设相位列出候选，且候选**可直接勾选**，无需手打 |
| 4 | 三按钮指向明确 | 卡片顶部显示"本次针对什么"（相位 → 人类可读目标），按钮与事项有明确归属 |
| 5 | 三模式说明 | 下拉项与报告页均有说明，用户能说出三者差异（差异在报告章节结构） |
| 6 | 历史删除 | 列表项可删除，有二次确认；删除后列表刷新且会话及其子数据不可再访问 |
| 7 | 数据源可见 | 新建时可选数据源（默认选中默认源）；列表与会话页显示本次研究用的库 |

## 2. 现状诊断（本轮探查结论）

每条给出根因与证据。**这是本设计的主要价值：多数诉求不是"缺功能"，而是"缺最后一跳"。**

| # | 现象 | 根因 | 证据 |
|---|---|---|---|
| 1 | 没有模型选择入口 | **三层皆无**：请求 schema 无 `modelId`、DB 无列、服务无参数。且 `ResearchSessionCreate` 是 `extra="forbid"`，前端擅自传 `modelId` 会被 **422** 拒绝 | `research_schemas.py:54-65`；`research_models.py:51-76`；`research_agent_ports.py:641-684` |
| 2 | 只看到"检测到语义歧义，如何处理？" | **前端渲染缺失**。歧义清单 `options.conflicts`（含 `detail` + 候选项 `kpiCode/displayName/confidence`）已随 SSE 下发并被 store 完整接收，但 `CheckpointCard` 只读 `options.arms.metrics`，**从不读 `conflicts`**。辅因：该问句是后端**无插值的死字面量**。触发相位是 `runtime_dynamic`，不是 `intent` | 产出 `enterprise_semantic_layer.py:119-135`；打包 `research_agent_service.py:475-488`；下发 `:431-436`；store 保留 `researchStore.ts:63`；未渲染 `CheckpointCard.tsx:22-32,48-57` |
| 3 | 只看到"验证哪些假设？"却无假设 | **同上是前端渲染缺失**：`options.candidates` 已到达 store，`CheckpointCard` 不读。planning 相位的 `options.plan.steps` 同样未渲染 | `research_agent_service.py:580-601`、`:527-533`；`researchStore.ts:63` |
| 4 | 三按钮不知针对什么 | 卡片标题恒为 i18n `research.checkpoint.title`（"待决策检查点"），卡片内**无 phase / 目标说明** | `CheckpointCard.tsx:49`；`zh-CN.ts:2910` |
| 5 | 三模式不知区别 | 后端**只在报告章节结构上有差异**，检索/计划/执行/假设/验证零差异；前端**零说明**（无 tooltip、无描述） | `report_planner.py:58-62`；`zh-CN.ts:2961-2963` |
| 6 | 列表无删除 | **三层皆缺**：无 `DELETE` endpoint、无软删除列、无 store action、无按钮。物理删会 CASCADE 清掉 turn/checkpoint/finding/report | `research.py` 全路由无 `@router.delete`；`research_models.py:43-48` 有 CASCADE |
| 7 | 没有数据库选择 | **后端已全就绪，前端一行没接线**：schema 有 `datasourceId`、迁移 0112 有列、执行期按会话取源、缺省回落默认源 | `research_schemas.py:59-64`；`0112_research_session_datasource.py`；`research_agent_execution.py:129-153` |

**结论**：7 条中 5 条（2/3/4/6/7）是"能力已在、暴露缺失"；1 条（5）是"文案缺失"；**只有 1 条（1）是真正的新增能力**。

## 3. 关键决策（用户已裁定 2026-10-05）

| # | 决策点 | 裁定 | 取舍理由 |
|---|---|---|---|
| D1 | 模型选择做到什么程度 | **会话级落库**（新增迁移 0113） | 研究是**多轮可恢复、会话 state 不落库**的，只在请求上一次性传递会在 resume 后失效，语义半残 |
| D2 | 三模式怎么处理 | **只加说明，不改后端** | 现状三者只改报告章节；假装它们是不同研究方法是误导。低风险、立刻可用 |
| D3 | 检查点渲染深度 | **结构化渲染 + 后端补文案** | 前后端都补齐：候选项可勾选（治"修改要手打"），问句带对象（治"不知针对什么"） |
| D4 | 删除语义 | **硬删除，与 chat 一致** | chat 已是硬删（`session_history_service.py:267`），复用现存 CASCADE，无需迁移，用户预期统一 |

## 4. 工作包

### W1 检查点可读性（第 2/3/4 条）

**后端（文案补对象）** —— 四处 `prompt` 字面量改为带对象表述：

| 位置 | 现状 | 改为 |
|---|---|---|
| `research_agent_service.py:487`（`runtime_dynamic`） | `"检测到语义歧义，如何处理？"` | 含冲突种类与候选数量的表述 |
| `:600`（`hypothesis`） | `"验证哪些假设？"` | 含候选数量的表述 |
| `:492`（`intent`） | `"三臂是否齐全？"` | **保持不变**（已是具体问题，无需改） |
| `:531`（`planning`） | `"计划是否确认？"` | **保持不变**（同上） |

**约束**：文案改动**不得**改变 checkpoint 的 `phase` / `options` 结构（落库的 `research_checkpoint.options` 兼容性依赖结构稳定）。

**前端（结构化渲染）** —— `CheckpointCard.tsx` 新增三类内容渲染：

```
┌─ 检查点 · 需要你决策 ─────────────────┐
│ 本次针对：指标歧义（候选分差过小）       │  ← 新增「目标行」：phase → 人类可读目标
│                                        │
│ 检测到 2 个指标候选分数接近，请确认用哪个：│  ← 后端文案改为带对象
│   ○ 供货量 (KPI-A)          置信 0.71   │  ← 新增：options.conflicts[].candidates
│   ○ 收货量 (KPI-B)          置信 0.66   │
│                                        │
│  [确认]  [修改]  [拒绝]                 │
└────────────────────────────────────────┘
```

- **`conflicts` 渲染**：逐条渲染 `kind`（指标歧义 / wiki 分歧）、`detail`、`candidates`。
- **`plan.steps` 渲染**（planning 相位）：列出步骤描述与维度。
- **`candidates` 渲染**（hypothesis 相位）：渲染为**可勾选列表**，勾选结果通过 `answerCheckpoint` 的 `choice` 字段提交（`api/research.ts:62-71` 已支持 `choice?: Record<string, unknown>`）。
- **目标行**：新增 phase → i18n 文案映射（`intent`/`planning`/`hypothesis`/`runtime_dynamic`/`low_confidence_step`）。

**两个必须处理的坑（否则修完仍是坏的）：**

1. **键名不一致（真实 landmine）**：`options.plan.steps[].sub_question` 是 **snake_case**（`ports.py:492-511`），而 `options.stepResults[].subQuestion` 是 **camelCase**（`ports.py:530-541`）。且**历史 checkpoint 已落库**，不能靠改后端键名来统一（会让新旧数据形态不一致）。
   → **方案：前端读取器双键回落**（先 camelCase 再 snake_case），并在类型定义处写明原因注释。**不新增后端契约变更。**
2. **降级空态**：走无 LLM / 解析失败路径时 `candidates` 与 `conflicts` 本来就是 `[]`（`research_agent_service.py:698-713`）。
   → **方案：显式空态文案**（如"本次未生成候选（模型不可用或解析失败），可直接点「修改」补充"）。不渲染空列表，否则用户看到空白会误判为未修复。

**i18n**：新增词条 zh/en 双语（`research.checkpoint.*` 命名空间）。

### W2 三模式说明（第 5 条）

- **下拉项加描述**：`ResearchListPage.tsx:17-21` 的 `MODE_OPTIONS` 的 `label` 改为**两行渲染**（主标题 + 副描述），利用 antd Select 的 `label` 接受 ReactNode 的能力。**不用 Tooltip** —— 悬停才可见对"用户不知道为什么选"这个痛点帮助有限，选项需要一眼能读懂。
- **报告页显示模式 Tag**：`ResearchReportPage.tsx` 显示本次报告的模式。
- **文案内容**（诚实表述）：

| 模式 | 说明 |
|---|---|
| 研究 | 通用数据研究：执行摘要 → 数据 → 引用知识 → 方法学 |
| 归因 | 侧重假设：结论 → **假设验证表** → 数据 → 备选假设 |
| 对比 | 侧重横向比较：**对比维度表** → 数据 → 差异分析 |

- **必须包含的一句免责说明**：三者共享同一条研究流水线，**只改变报告的章节组织**，不改变分析行为。
- i18n zh/en 双语。

### W3 历史删除（第 6 条）

**范围澄清**：研究页当前是**整页纵向列表**，**没有**右侧可折叠历史面板（那是 chat 的形态）。按用户"在现有的 [模式]、打开、报告 基础上增加删除"的表述，**只加删除按钮，不改造成侧栏**。

**后端：**

- 新增 `DELETE /api/v1/research/sessions/{sessionId}`。
- **硬删除**：删 `research_session` 主行，靠既有 `ON DELETE CASCADE`（`research_models.py:43-48`）级联清 turn / checkpoint / finding / report。**无需迁移。**
- **归属校验**：复用现有约定 —— `research.py:565` 的 `row.created_by != user.dbUserId` 判定，不匹配 → **404**（与列表 `:292` 的过滤口径一致，不泄露会话存在性）。
- **404 语义**：删除 0 行 → 404（与 chat `session_history_service.py:267` 的 `RETURNING` 计数约定一致）。

**前端：**

- `api/research.ts` 新增 `deleteResearchSession(sessionId)`。
- `stores/researchStore.ts` 新增 delete action（成功后从本地列表移除）。
- `ResearchListPage.tsx` 列表项加删除按钮 + `Popconfirm` 二次确认（照 `components/chat/ChatHistoryPanel.tsx:156-174` 的写法）。

### W4 数据源选择（第 7 条）

**后端：零改动**（schema / 迁移 0112 / 执行期读取三层已通）。

**前端：**

- 新建表单（`ResearchListPage.tsx:74-96`）加数据源 Select，选项来自 `listDataSources`（`api/datasource.ts`），**默认选中 `is_default` 的源**。
- 复用 chat 侧现成参考实现：`components/chat/ChatPanel.tsx:174-187`。
- 列表项与会话页显示本次研究的数据源 Tag（治"不知道对哪个库研究"）。

### W5 模型选择（第 1 条）

**数据模型** —— 新增迁移 **0113**（文件名 ≤ 32 字符）：`research_session.model_id INTEGER NULL`。照 0112 的写法与注释风格（说明"研究是多轮可恢复，故必须持久化在会话上"）。

**接口契约：**

- `ResearchSessionCreate` 增加 `modelId: int | None = None`（放行 `extra="forbid"`）。
- `ResearchSessionRead` 回显 `modelId`。

**服务层：**

- 创建时落 `model_id`（`research_session_service.py:75` 附近）。
- 执行期读会话上的 `model_id`：**指定则直选（跳过 router）、不指定则走现有自动路由**。照 chat 的消费模式（`chat_service.py:831-845`）。
- 模型不存在 / 未激活 → **显式报错**（照 chat 的 `MSG_MODEL_CONFIG_UNAVAILABLE` 口径），不静默回落。

**前端：**

- 新建表单加模型 Select，选项来自 `listModels`（`api/modelConfig.ts`），默认「自动」（`null`）。

**耦合警告**：因 `extra="forbid"`，**W5 的前端改动必须与后端同批上线**，否则请求体带 `modelId` 会被 422 拒绝。这是 W5 必须独立成批的原因。

## 5. 数据模型变更

| 项 | 内容 |
|---|---|
| 表 | `research_session` |
| 列 | 新增 `model_id INTEGER NULL`（无 FK，与 `datasource_id` 同风格） |
| 迁移 | `0113_research_session_model.py`（30 字符，`0113` 经确认未被占用） |
| 索引 / 约束 | 无新增 |
| 删除 | 无（W3 复用既有 CASCADE，不加软删除列） |

**迁移文件名实录**：`变更记录强制规范` §校验清单要求迁移文件名 **≤ 32 字符**。实测 `0111_research_entry_tables.py` = 29 字符（合规），但既有的 `0112_research_session_datasource.py` = **35 字符（已超限）** —— 说明该约束在实际提交中并未被严格施行。本次选 `0113_research_session_model.py`（**30 字符，合规**），不与既有超限文件保持一致，以向规范收敛。`0113` 经 `ls alembic/versions` 确认未被占用。

**绝不在本地手工执行 `alembic upgrade head`** —— 该命令默认指向**生产**元数据库；迁移由容器启动时自动执行（`docker/Dockerfile.backend:79` 的 CMD）。

## 6. 接口契约变更

| 方法 | 路径 | 变更 |
|---|---|---|
| POST | `/api/v1/research/sessions` | 请求新增可选 `modelId`；响应新增 `modelId` |
| DELETE | `/api/v1/research/sessions/{sessionId}` | **新增**。硬删，归属不符 → 404，删除 0 行 → 404 |

无状态机变更（会话 `status` 枚举不变）。无 SSE 事件变更（checkpoint payload 结构不变，仅 `prompt` 文案变化）。

## 7. 错误处理与边界

| 场景 | 处理 |
|---|---|
| W1：`candidates` / `conflicts` 为空（降级路径） | 显式空态文案，不渲染空列表 |
| W1：后端键名 snake/camel 混用 | 前端双键回落读取；不改后端契约 |
| W1：未知 phase | 目标行回落到通用文案，不抛错 |
| W3：会话不存在 / 非本人 | 404（不泄露存在性） |
| W3：删除失败 | 显式错误提示 + 列表不刷新（不做乐观移除） |
| W5：`modelId` 不存在 / 未激活 | 创建时显式校验报错；执行期读到的模型失效则显式报错，不静默回落自动路由 |
| W5：选定模型后模型被停用 | 执行期报错并提示，**不静默换模型**（否则用户以为用了 A 实际用了 B） |

## 8. 测试策略

| 层 | 范围 | 重点 |
|---|---|---|
| 前端 vitest + RTL | W1/W2/W3/W4/W5 前端 | checkpoint 三类内容渲染 + 空态；模式说明文案；删除 Popconfirm 与刷新；数据源默认选中；模型默认「自动」 |
| 后端 pytest（真实 PG，`localhost:5434` / `qa_metadata_test`） | W3、W5 | DELETE 级联与 404 语义（含越权 404）；`modelId` 落库与执行期直选；`extra=forbid` 放行后非法模型报错 |
| 覆盖率 | 全部改动模块 | ≥ 80% |

**约束**：串行执行，绝不并行跑两个套件；不跑全量 `app/tests/integration`（无界尾部）；`pytest-timeout` 未安装。

## 9. 交付批次与风险

| 批 | 内容 | 理由 |
|---|---|---|
| **批一** | W1 + W2 + W3 + W4 | 全部"后端已就绪或纯前端"，可立刻真机验收；出问题不污染批二 |
| **批二** | W5 | 唯一动 DB 的（新迁移 + `extra=forbid` 放行）；前后端必须同批上线 |

| 风险 | 缓解 |
|---|---|
| W1 修完仍"看不到"（因降级空 `candidates`） | 显式空态文案 + 测试覆盖空态分支 |
| W1 读 `plan.steps` 取不到值（snake/camel 混用） | 双键回落 + 单测覆盖两种键形态 |
| W3 误删导致报告不可恢复 | Popconfirm 二次确认 + 归属校验；语义与 chat 一致，用户预期统一 |
| W5 迁移在生产未跑 | 迁移由容器启动自动执行；**不手工 upgrade**；合并前确认 CI 链路 |
| W5 前后端版本错配 → 422 | 同批上线；前端传 `modelId` 前先确认后端已放行 |

## 10. 明确不做（YAGNI）

- **不**把研究列表改造成 chat 那种右侧可折叠历史面板（用户表述为"在现有基础上加删除"）。
- **不**让 `mode` 影响检索 / 计划 / 执行 / 假设（D2 裁定：只加说明）。
- **不**统一后端 `sub_question` / `subQuestion` 键名（会让已落库的历史 checkpoint 形态不一致）。
- **不**引入软删除（D4 裁定：与 chat 一致走硬删）。
- **不**做多用户协同 / ACL（沿用前序设计的预留）。
- **不**做 ESL 后置 LLM 精化层（前序设计的独立优化钩子）。

## 11. 关联

- 前序设计：`docs/superpowers/specs/2026-10-04-research-entry-design.md`
- SSOT 摘要：`Harness/changes/feat-research-entry-ux-fixes/summary.md`
- 前序 SSOT：`Harness/changes/feat-research-entry/summary.md`
