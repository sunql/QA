# 变更：feat-research-entry-ux-fixes

- **日期**：2026-10-05
- **作者**：Claude / 启琳
- **Phase**：Phase 6 L5 AI Native：研究型 Agent 入口体验修复
- **状态**：in-review
- **关联变更**：[feat-research-entry](../feat-research-entry/summary.md)（predecessor：研究入口本体）
- **迁移版本**：0113_research_session_model（W5，**已部署上线**，prod `alembic_version=0113`）
- **MEMORY**：[qa-system-research-entry-ux-gaps.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-entry-ux-gaps.md)

> **阶段说明**：§1–§9 已全部按实现实况填齐（§6 测试 / §7 安全审查 / §8 部署验证均为**实测数字与逐字结论**，非计划值）。
> 状态 `in-review` 表示**等待人工真机验收与代码评审**。

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
| W1 检查点可读性 | 后端文案：`services/research_agent_stages.py` 的 `ambiguityPrompt`（runtime_dynamic）/ `hypothesisPrompt`（hypothesis），+ `conflictKindLabel` 与两条兜底 `_AMBIGUITY_PROMPT_FALLBACK` / `_EMPTY_CANDIDATES_PROMPT`；前端渲染：`components/research/CheckpointCard.tsx`；i18n：`src/i18n/zh-CN.ts` / `en-US.ts` |
| W2 三模式说明 | `pages/research/ResearchListPage.tsx` 的 `MODE_OPTIONS`、`pages/research/ResearchReportPage.tsx` |
| W3 历史删除 | 后端：`api/v1/research.py` 的 `deleteSession` 路由（归属闸门 `_ownedSession`）、`services/research_session_service.py` 的 `deleteSession`；前端：`api/research.ts`、`stores/researchStore.ts`、`pages/research/ResearchListPage.tsx` 的 `SessionRow`（照 `components/chat/ChatHistoryPanel.tsx` 的 `Popconfirm`） |
| W4 数据源选择 | 后端（**仅响应侧**）：`domain/research_schemas.py` 的 `ResearchSessionRead.datasourceId`、`api/v1/research.py` 的 `_sessionRead` 映射；前端：`pages/research/ResearchListPage.tsx` 新建表单的源 `Select`（默认选 `is_default` 源）、列表项与会话页的源 Tag；复用 `api/datasource.ts` 的 `listDataSources`；参考 `components/chat/ChatPanel.tsx` 的同名调用 |
| W5 模型选择 | 迁移 `alembic/versions/0113_research_session_model.py`；`domain/research_schemas.py` 的 `ResearchSessionCreate.modelId`（放行）与 `ResearchSessionRead.modelId`（回显）；`domain/research_models.py` 的 `ResearchSession.model_id`（新列）；`api/v1/research.py` 的 `createSession` + `_resolveModelId`（创建期校验）；`services/research_agent_ports.py` 的 `resolveModelConfig` → `_readConfigs` / `_resolvePreferredConfig`（执行期直选，参考 `services/chat_service.py` 的 `_buildPipelineContext` 直选分支）；前端复用 `api/modelConfig.ts` 的 `listModels` |

**关键算法 / 依赖注入点**：

- W5 执行期读会话上的 `model_id`：指定 → 直选并跳过 router；未指定 → 走现有 `resolveModelConfig` 自动路由（行为逐字不变）。模型不存在 / 未激活 → 显式报错，**不静默回落**（否则用户以为用了 A 实际用了 B）。
- W1 前端读取器需**双键回落**（先 `camelCase` 再 `snake_case`），因 `options.plan.steps[].sub_question` 为 snake_case（写侧 `research_agent_phases.singleStepPlan` / 读侧 `research_agent_execution`）而 `options.stepResults[].subQuestion` 为 camelCase（`research_agent_ports.stepResult` 里的键名）。

## 6. 测试

**执行口径**（2026-10-05，冻结树 `7798419`）：全部**串行**，绝不并行跑两个套件；后端集成测试带全 `TEST_DATABASE_URL`（`localhost:5434` / `qa_metadata_test`）**与** `TEST_NEO4J_URI`（`localhost:7688`）；**不跑**全量 `app/tests/integration`（无界尾部）；`pytest-timeout` 未安装。

### 后端（真实 PostgreSQL，非 sqlite）

| 套件 | 实测 |
|---|---|
| research 集成（**6 个文件** —— 计划只点了 api/stream 两个，按「把存在的都纳入」全跑） | **104 passed**（33.06s） |
| research 单测（`-q -k research`） | **69 passed / 3668 deselected**（12.79s） |
| 改动模块覆盖率 | 见下表，TOTAL **93.35%**（仓库 `fail_under=80` 达标） |

覆盖率模块清单按**改动实况**扩到 6 个（计划漏了 Step 0 新建的 `research_agent_phases.py` 与改过的 `research_agent_service.py`）：

| 模块 | 覆盖率 |
|---|---|
| `app/api/v1/research.py` | **96%** |
| `app/services/research_agent_phases.py` | **97%** |
| `app/services/research_agent_ports.py` | **88%** |
| `app/services/research_agent_service.py` | **94%** |
| `app/services/research_agent_stages.py` | **90%** |
| `app/services/research_session_service.py` | **100%** |

> 陷阱记录：`--cov` 必须给**点号**模块路径（`--cov=app.services.research_agent_ports`）。给**斜杠**路径（`--cov=app/services/...`）时 pytest-cov **静默不打印覆盖率表**，事后读 `.coverage` 会得到 "No data to report" —— 「看起来跑过了、其实没有数字」。

### 前端（vitest + RTL）

命令：`npx vitest run --coverage --coverage.reportOnFailure --testTimeout=30000`（**全量**）。

- **172 文件 / 1529 用例 / 2 failed | 1527 passed**
- 两红均为**既有失败**、且在本分支改动面之外：
  - `src/tests/chatStore.test.ts` —— 「流式 plan 事件回填 ReAct 查询计划（Phase E）」（40 中用 1 红）
  - `src/tests/EntityMappingPage.test.tsx` —— 「点击新建并提交调用 createMapping（camelCase payload）」（6 中用 1 红）
- **基线对照（`git worktree add /tmp/qa-cov-base 1760a00` + 软链 node_modules）**：分叉点为 172 文件 / 1527 用例 / 2 failed ⇒ **零新增红**；用例总数 1527→1529 正是本变更新增的 2 条
- 覆盖率：Stmts **84.64** / Branch **87.73** / Funcs **70.22** / Lines **84.64**

**functions 门槛（70.22% < 80%）是既有红灯，非本变更引入**：基线 `1760a00` 同为 functions 一项红、值 **70.06%** —— 本变更后**微升** 0.16pp。拖累项全在**未触碰**的文件（`ResearchSessionPage.tsx` funcs 14.28%、大量 admin 页 0%）与**本不该进分母**的配置/脚本/产物文件（`vitest.config.ts` 未设 `coverage.include`，v8 把 `vite.config.ts` / `tailwind.config.ts` / `dump-dq.cjs` / `*.mjs` / 构建产物 `dist/assets/*.js` 一并统计）。**本变更触碰的前端文件全部达标**：`pages/research/ResearchListPage.tsx` funcs **89.47%**（唯一未覆盖行 = 新增的 `catch` 降级分支）、`api/research.ts` / `api/modelConfig.ts` / `i18n/**` 均 100%。

**裁定**：**不调低门槛、不在本变更内补无关文件的覆盖率**（项目红线明令「不要调低门槛」）。分母缺陷属**既有配置问题**，另开变更修 `vitest.config.ts` 的 `coverage.include`，不混入本变更。

**其它计划缺陷（执行中修正，记录备查）**：计划 Step 1 只列了 2 个 research 集成文件（实为 6 个）；Step 2 的模块清单漏了 2 个；Step 2 的前端命令「3 个测试文件 + `--coverage`」**注定失败**（全局阈值 + v8 全文件统计 ⇒ 单文件跑给出 All files 10.82% 与四条 ERROR），正确口径是全量。

## 7. 安全审查

**触发条件**：本变更新增 `DELETE` 端点（W3）并引入用户输入字段（W5 `modelId`），按 `Harness/rules/权限与安全规范.md` 由 `security-reviewer` 出具结论（审查范围 = Task 10 之后**不再变**的后端三处：`research.py` 的 `deleteSession` / `_resolveModelId` / `createSession` 与归属校验、`research_session_service.deleteSession`、`ports` 直选分支）。

**结论：无 CRITICAL、无 HIGH。**

| 级别 | 项 | 处置 |
|---|---|---|
| **MEDIUM ×1** | `research_session_service.deleteSession` 的注释宣称「归属在 SQL 层再兜一次…多一道闸门」，而 `where` 子句里**只有** `ResearchSession.id == sessionId`、**没有** `created_by` 谓词；该注释还自相矛盾（下一句又说「不需要 createdBy 参数，由 router 前置校验」）。当前**不可利用**（唯一调用点 `research.py` 的 delete 路由先过 `_ownedSession`），危害是**注释广告了一道并不存在的纵深防御**，未来新增调用方会误信。 | **已修**：改注释使与代码一致，并**显式**写出「唯一闸门在 router，新增调用方必须自行校验归属」（commit `7798419`）。**未**扩大方法签名 —— 该 MEDIUM 是**文档缺陷**，不是缺失校验。 |
| **LOW ×1** | `_resolveModelId` 可用 404 与否枚举「存在且启用的模型 id」。仅数字、不泄露详情，且这正是设计要求的 fail-explicit 行为。 | 按设计**接受**，不改。 |

**5 项必查全部 PASS**：

1. **越权删除必 404 且不泄露存在性** —— `_ownedSession` 对「不存在」与「非本人」返回**同一个** `NotFoundError`；并发分支 `removed == 0` 同样 404 ⇒ 响应不可区分。
2. **硬删 + 子表级联** —— 四张子表 `ON DELETE CASCADE`（ORM `research_models.py` 与迁移 `0111_research_entry_tables.py` **双向**核实）；`model_id` 刻意**无 FK**。
3. **`modelId` 无注入面** —— 全程只做 int 比较/回显，绝无字符串拼接（`int | None` + `extra="forbid"`）。
4. **无枚举 oracle 于错误文案** —— 全量清单查找只回显数字 id（失败文案 `messages_zh.py` 只有 `{id}`），且「不存在」与「已禁用」**同一条分支** ⇒ 连「存在但停用」的 oracle 都没有。
5. **错误与日志无密钥/端点/内部路径。**

**已内建的安全设计（复核确认）**：W3 删除归属不符返回 **404**（而非 403），与既有 `research.py` 口径一致；W5 `modelId` 显式校验（存在 + 已激活）、不静默回落，不引入任意模型名注入。

## 8. 部署验证

**前置：生产备份**（本变更会触发后端容器启动时的 `alembic upgrade head`，故先备份再部署）：

```bash
docker exec qa-postgres pg_dump -U qa_user -d qa_metadata -Fc > ~/backups/qa_metadata_2026-10-05_pre0113.dump
```
→ 13 MB；`pg_restore -l` 实证含 `TABLE public research_session` / `TABLE DATA public research_session` / `alembic_version` / `research_session_pkey`（有效归档且**带数据**）。

**部署前状态**：`research_session=2`、`research_turn=18`、`research_checkpoint=8`、`alembic_version=0112`、`model_id` 列**不存在**（0）。

**构建**：`docker compose -f docker/docker-compose.yml build --no-cache backend frontend` → 退出码 **0**（两镜像 Built）。批二同时改了后端（迁移 0113 + 执行期直选）与前端 ⇒ **两个服务都必须重建**；只重建前端会让后端整批静默不生效（当日实测即前端的 DELETE 打旧后端报 **405**）。

**迁移**：由容器启动自动执行 ⇒ `alembic current` = **`0113 (head)`**；`alembic_version` = `0113`；`information_schema` 实证 `research_session.model_id integer / is_nullable=YES`；**pre-state 的 2 行会话完好**。**未手工执行 `alembic upgrade head`**（该命令默认指向生产库）。

**端点核对**：`/openapi.json` 的 research 路径含 `/api/v1/research/sessions/{sessionId} ['delete', 'get']` ⇒ **405 已消除**。

**真机链路（4 项，全部 PASS）**：

| # | 操作 | 实测证据 |
|---|---|---|
| 1 | 新建：不选模型 / 选 `MiniMax-M3`(id=9) | 均 **201**；响应 `modelId` 分别为 `null` / `9` |
| 2 | 追问轮用所选模型 | `session_token_usage` 新增 `MiniMax-M3 ｜ research_plan ｜ 308 tok` |
| 3 | **重启 backend** 后同会话再跑仍用所选模型 | 重启（`StartedAt=2026-10-05T04:24:40Z`）后新增 `MiniMax-M3 ｜ research_plan ｜ 308 tok` ⇒ 模型来自 **DB 列**而非进程内存 |
| 4 | 停用所选模型后继续 | `PreferredModelUnavailableError: 指定的模型配置 9 不存在或已禁用` → 会话终态 `failed`，且 `session_token_usage` **零新增**（未走到 LLM）⇒ **显式终态错误、无静默换模型**（正是第 1 条诉求要修的「以为用了 A 实际用了 B」） |

> 第 4 项会**短暂改动生产配置**（`llm_config.id=9.is_active`）。实测后**立即恢复**为 `true` 并复核（`9|MiniMax-M3|t`）。

**批一功能的真机顺带验证**：

- **W1 文案**：runtime_dynamic 检查点的 `options.prompt` 实测 = `检测到 1 处语义歧义（知识冲突），请确认采用哪一项？`（此前是不带对象的死字面量）—— 用户反馈第 2 条的直接证据。
- **W3 删除**：DELETE 有子行的会话 → **204**；无子行的会话 → **204**；随机 UUID → **404**；重复删除 → **404**（文案「研究会话不存在」，与「非本人」同响应 ⇒ **不泄露存在性**）。级联实证：删除后 `research_turn=0 / research_checkpoint=0`，`research_session` 计数回到 **2 = pre-state**。
- **W4 回显**：`GET /research/sessions` 两条既有会话均 `datasourceId=1`、`modelId=null`（批二之前建的会话没有 `model_id`，合乎预期）。

**遗留观察（非本次缺陷，交最终评审裁量）**：硬删研究会话**不**清理 `session_token_usage` —— 该表是 chat / 研究**共用**的计量台账、以 `session_id` 字符串为键、**无 FK**，故无级联（实测删除后仍残留 2 行）。倾向**保留**（成本台账不应随业务对象消失），但需明确裁定。

**执行相位失败与本次变更无关（如实记录）**：验收中执行步两次报 `Nl2SqlError: 无法生成有效的查询 SQL，请换一种问法或补充本体元数据` → `RuntimeError: 研究计划全部步失败（无一成功），本 turn 终止`（N1' 急停守卫**按设计**生效）。根因是**验收问题过于宽泛 + 该问法的本体元数据不足**，属数据/环境条件；W5 只决定"选哪个模型"，不改 NL2SQL 生成。

## 9. 关联

- 设计稿：`docs/superpowers/specs/2026-10-05-research-entry-ux-fixes-design.md`
- 前序设计稿：`docs/superpowers/specs/2026-10-04-research-entry-design.md`
- 关联变更：`../feat-research-entry/summary.md`（predecessor）
- Wiki：`Harness/wiki/frontend.md`（前端组件与交互，**已补「研究型 Agent 入口（research）」章节**）
- Rules：`Harness/rules/变更记录强制规范.md`、`Harness/rules/测试规范.md`、`Harness/rules/权限与安全规范.md`
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-research-entry-ux-gaps.md`
