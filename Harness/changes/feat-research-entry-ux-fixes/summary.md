# 变更：feat-research-entry-ux-fixes

- **日期**：2026-10-05
- **作者**：Claude / 启琳
- **Phase**：Phase 6 L5 AI Native：研究型 Agent 入口体验修复
- **状态**：draft
- **关联变更**：[feat-research-entry](../feat-research-entry/summary.md)（predecessor：研究入口本体）
- **迁移版本**：0113_research_session_model（W5，尚未落地）
- **MEMORY**：[qa-system-research-entry-ux-gaps.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-entry-ux-gaps.md)

> **阶段说明**：本记录为**设计阶段 draft**，§1/§2/§3/§4/§5/§9 已按设计完整填写；
> §6（测试）/§7（安全审查）/§8（部署验证）须在实现完成后填齐，填齐并转 `in-review` 是本变更合并的前置条件。
> `draft` 状态**不允许合并**。

---

## 1. 需求

**背景**：研究型 Agent 入口（`feat-research-entry`）合并上线后，用户真机实测反馈 7 项体验 / 功能缺口：

1. 新建研究会话时没有选择 LLM 模型的地方。
2. 提示"检测到语义歧义，如何处理？"但看不到歧义具体是什么。
3. 提示"待决策检查点，验证哪些假设"，但看不到有什么检查点、该验证什么假设。
4. 「确认 / 修改 / 拒绝」三个按钮不知针对哪个事项。
5. 「研究 / 归因 / 对比」三个选项不知如何用、有何区别。
6. 会话列表只有「打开 / 报告」，缺「删除」。
7. 没有数据源（数据库）选择，不知对哪个库做研究。

**验收标准（用户视角、可测量）**：

| # | 验收条件 |
|---|---|
| 1 | 新建时可选模型；所选模型在**后续追问**与**进程重启后 resume** 仍生效；不选 = 自动路由行为与今天完全一致 |
| 2 | 歧义检查点**列出**候选指标（名称 + 置信度）或 wiki 冲突项 |
| 3 | 计划相位列出步骤；假设相位列出候选且**可直接勾选** |
| 4 | 卡片顶部显示"本次针对什么" |
| 5 | 下拉项与报告页均有说明，用户能说清三者差异 |
| 6 | 列表项可删除、有二次确认；删除后会话及子数据不可再访问 |
| 7 | 新建时可选数据源（默认选中默认源）；列表与会话页显示所用库 |

## 2. 设计评审

**探查结论（决定性输入）**：7 条中 **5 条（2/3/4/6/7）是"能力已在、暴露缺失"**，1 条（5）是文案缺失，**只有 1 条（1）是真正新增能力**。逐条根因见设计稿 §2。

四个决策点各有 ≥2 候选，已由用户 2026-10-05 裁定：

### D1 模型选择深度

| 方案 | 取舍 | 决定 |
|---|---|---|
| **A. 会话级落库**（迁移 0113） | 语义完整；代价是一次迁移 | ✅ **采纳** |
| B. 仅请求级传递，不落库 | 省迁移；但研究是**多轮可恢复、会话 state 不落库**的，resume 后失效 → 用户会觉得"选了没用" | ✗ |
| C. 本轮不做 | 无法满足第 1 条诉求 | ✗ |

### D2 三模式处理

| 方案 | 取舍 | 决定 |
|---|---|---|
| **A. 只加说明，不改后端** | 诚实反映现状（三者仅改报告章节）；低风险、立刻可用 | ✅ **采纳** |
| B. 让 mode 真正影响流程 | 要重做 planning 与 prompt 并重新验收，工作量与风险远超本次范围 | ✗ |
| C. 隐藏未跑通模式 | 损失已实现能力（`compare` 报告章节已实现，非空壳） | ✗ |

### D3 检查点渲染深度

| 方案 | 取舍 | 决定 |
|---|---|---|
| **A. 前端结构化渲染 + 后端补文案** | 前后端都补齐：候选可勾选、问句带对象 | ✅ **采纳** |
| B. 仅前端结构化 | 后端问句仍泛，用户仍不知"针对什么" | ✗ |
| C. 仅后端文案插值 | 候选项仍不可勾选，"修改"仍需手打 | ✗ |

### D4 删除语义

| 方案 | 取舍 | 决定 |
|---|---|---|
| **A. 硬删除，与 chat 一致** | 复用现存 `ON DELETE CASCADE`，无需迁移；与 chat 用户预期统一 | ✅ **采纳** |
| B. 软删除 | 需新列 + 所有查询加过滤；与 chat 行为不一致 | ✗ |
| C. 硬删 + 强制输入标题确认 | 摩擦过重，与 chat 不一致 | ✗ |

### 关键设计取舍（非用户决策，属工程判断）

| 取舍 | 原因 |
|---|---|
| **不统一后端 `sub_question` / `subQuestion` 键名**，改前端双键回落 | 历史 checkpoint 的 `options` 已落库，改键名会让新旧数据形态不一致 |
| **W5 独立成批** | `ResearchSessionCreate` 是 `extra="forbid"`，前端加 `modelId` 必须与后端同批上线，否则 422 |
| **W1 补显式空态** | 降级路径下 `candidates` 本就是 `[]`，不提示会让用户误判"没修好" |
| **W3 保持整页列表** | 用户原话是"在现有的 [模式]、打开、报告基础上增加删除"，非改造成 chat 侧栏 |

## 3. 数据模型变更

| 项 | 内容 |
|---|---|
| 表 | `research_session`（既有，0111 建） |
| 新增列 | `model_id INTEGER NULL`（无 FK，与既有 `datasource_id` 同风格） |
| 迁移 | `0113_research_session_model.py`（30 字符，`0113` 已确认未占用） |
| 索引 / CheckConstraint | 无新增 |
| 软删除列 | **不加**（D4 裁定走硬删，复用既有 CASCADE） |
| W3 删除的级联 | 复用既有 `research_models.py:43-48` 的 `ON DELETE CASCADE`（session → turn / checkpoint / finding / report），**无需迁移** |

**迁移文件名实测**：`变更记录强制规范` §校验清单要求 ≤ 32 字符。`0111_research_entry_tables.py` = 29（合规）；既有 `0112_research_session_datasource.py` = **35（已超限）**。本次取 `0113_research_session_model.py` = **30（合规）**，向规范收敛而非与超限文件看齐。

**绝不在本地手工执行 `alembic upgrade head`**（默认指向**生产**元数据库）；迁移由容器启动自动执行。

## 4. 接口契约变更

| 方法 | 路径 | 变更 |
|---|---|---|
| POST | `/api/v1/research/sessions` | 请求新增可选 `modelId: int \| None`；响应新增 `modelId`。`extra="forbid"` 放行该字段 |
| GET | `/api/v1/research/sessions`（及详情） | **响应新增 `datasourceId: int \| None`**（W4）。请求侧 `datasourceId` 自 0112 起已存在且已落库，但 `ResearchSessionRead` 从不回显 ⇒ 无法在列表/会话页显示"用了哪个库"。本次仅补响应字段 + `_sessionRead` 映射，**不改接收侧与执行期** |
| DELETE | `/api/v1/research/sessions/{sessionId}` | **新增**。硬删除；归属不符 → 404；删除 0 行 → 404 |

- **状态机**：无变化（会话 `status` 枚举 `running/awaiting_user/done/failed/aborted` 不变）。
- **SSE**：无事件结构变化；仅 `research.checkpoint` 的 `prompt` **文案**变化（`phase` / `options` 结构保持不变，因落库兼容性依赖结构稳定）。
- **归属校验**：复用既有约定 —— 列表 `research.py:292` 按 `created_by == user.dbUserId` 过滤，详情 `:565` 校验归属 → 404。DELETE 沿用同一口径，不泄露会话存在性。

## 5. 实现要点

**设计稿（完整方案）**：`docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md`

| 包 | 关键文件 |
|---|---|
| W1 检查点可读性 | 后端文案：`services/research_agent_service.py:487`（runtime_dynamic）、`:600`（hypothesis）、`:492`、`:531`；前端渲染：`components/research/CheckpointCard.tsx`；i18n：`src/i18n/zh-CN.ts` / `en-US.ts` |
| W2 三模式说明 | `pages/research/ResearchListPage.tsx:17-21`（`MODE_OPTIONS`）、`pages/research/ResearchReportPage.tsx` |
| W3 历史删除 | 后端：`api/v1/research.py`（新增 DELETE）、`services/research_session_service.py`；前端：`api/research.ts`、`stores/researchStore.ts`、`pages/research/ResearchListPage.tsx`（照 `components/chat/ChatHistoryPanel.tsx:156-174`） |
| W4 数据源选择 | 后端（**仅响应侧**）：`domain/research_schemas.py:92-102`（`ResearchSessionRead` 加 `datasourceId`）、`api/v1/research.py` 的 `_sessionRead` 映射；前端：`pages/research/ResearchListPage.tsx:74-96`（新建表单加 Select，默认选 `is_default` 源）、列表项与会话页的源 Tag；复用 `api/datasource.ts` 的 `listDataSources`；参考 `components/chat/ChatPanel.tsx:174-187` |
| W5 模型选择 | 迁移 `alembic/versions/0113_research_session_model.py`；`domain/research_schemas.py:54-65`（放行 `modelId`）、`:92-102`（回显）；`domain/research_models.py:51-76`（新列）；`api/v1/research.py:244-265`（创建）；`services/research_agent_ports.py:641-684`（执行期直选，参考 `services/chat_service.py:831-845` 的消费模式）；前端复用 `api/modelConfig.ts` 的 `listModels` |

**关键算法 / 依赖注入点**：

- W5 执行期读会话上的 `model_id`：指定 → 直选并跳过 router；未指定 → 走现有 `resolveModelConfig` 自动路由（行为逐字不变）。模型不存在 / 未激活 → 显式报错，**不静默回落**（否则用户以为用了 A 实际用了 B）。
- W1 前端读取器需**双键回落**（先 `camelCase` 再 `snake_case`），因 `options.plan.steps[].sub_question` 为 snake_case（`research_agent_ports.py:492-511`）而 `options.stepResults[].subQuestion` 为 camelCase（`:530-541`）。

## 6. 测试

> **待实现完成后填齐**（本记录为设计阶段 draft）。计划覆盖范围：

| 层 | 计划覆盖 |
|---|---|
| 前端 vitest + RTL | W1 三类内容渲染 + **空态分支** + snake/camel 双键；W2 模式说明文案；W3 Popconfirm 与列表刷新；W4 数据源默认选中；W5 模型默认「自动」 |
| 后端 pytest（真实 PG，`localhost:5434` / `qa_metadata_test`） | W3 DELETE 级联 + 404 语义（含越权 → 404）；W5 `modelId` 落库 + 执行期直选 + 非法模型报错 + `extra=forbid` 放行后行为 |
| 覆盖率 | 改动模块 ≥ 80%（CLAUDE.md 红线） |

**测试环境约束**：串行执行；绝不并行跑两个套件；不跑全量 `app/tests/integration`（无界尾部）；`pytest-timeout` 未安装。

## 7. 安全审查

> **待实现完成后填齐**。触发条件**已成立**：本变更新增 `DELETE` 端点（W3）并引入用户输入字段（W5 `modelId`），按 `Harness/rules/权限与安全规范.md` 必须由 `security-reviewer` 出具结论（CRITICAL / HIGH / MEDIUM / LOW 至少 1 项）。

**已内建的安全设计**（待 reviewer 复核）：

- W3 删除**必须**校验 `created_by == user.dbUserId`，不匹配返回 **404**（而非 403），避免泄露会话存在性 —— 与既有 `research.py:565` 口径一致。
- W5 `modelId` 走显式校验（存在 + 已激活），不静默回落；不引入任意模型名注入。

## 8. 部署验证

> **待实现完成后填齐**。计划内容：`docker compose` 启动冒烟 + `/openapi.json` 端点核对 + 真机跑通「新建（选源+选模型）→ 检查点可读 → 删除」链路；W5 迁移由容器启动自动执行后确认 `research_session.model_id` 存在。

## 9. 关联

- 设计稿：`docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md`
- 前序设计稿：`docs/superpowers/specs/2026-10-04-research-entry-design.md`
- 关联变更：`../feat-research-entry/summary.md`（predecessor）
- Wiki：`Harness/wiki/frontend.md`（前端组件与交互，**待实现后补章节**）
- Rules：`Harness/rules/变更记录强制规范.md`、`Harness/rules/测试规范.md`、`Harness/rules/权限与安全规范.md`
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-entry-ux-gaps.md`
