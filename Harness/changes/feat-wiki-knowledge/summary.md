# 变更：feat-wiki-knowledge

- **日期**：2026-09-11
- **作者**：启琳（Claude Code）
- **Phase**：8 — 通用知识管理与自学习扩展
- **状态**：M1 完成（0053 已上生产 + 18/18 集成测试绿 + drift 校验通过）；
  **M2 完成**（0054 已上生产 + 54/54 集成测试绿 + 前端导入向导 14/14 绿 + drift 校验通过）；
  **M3 完成**（0055 已上生产 + 分类调整闭环，32 个新测试绿）；
  **M4 完成**（0056 已上生产 + 关系发现与审核，24 个集成测试绿，真机冒烟通过，
  并自查修掉一处注入隔离失效见 §8.5）；
  **M5 完成**（0057 已上生产 + 机制 3 冲突检测 + 机制 4 结构化建议，25 个新集成测试绿，
  审查修掉 token 计量被回滚见 §9.6）；
  **M6 完成**（0058 已上生产 + 机制 5 渐进结构 + 规则 dry-run + 流程，65 个新集成测试绿 + 真机冒烟通过，
  审查修掉数组取值被丢弃等 7 项见 §10.5）；
  **M7 完成**（0059 覆盖度 + class→domain 映射 + 看板页 + 一级菜单「企业 Wiki」）；
  **M8 完成**（4 个知识 Agent Tool + 策略种子愈合 + 端到端 4 demo 通过，
  集成测试当场打红 3 个真实缺陷见 §10B.4）。
  **M1~M8 全部落地，本 feature 完成**；§13 的效果指标（分类准确率等）待真实知识量上评测。
  **一处刻意偏离方案**：M4 不做 Neo4j 入图，见 §8.3。

## 1. 需求

把 Wiki 做成**通用、可生长**的知识管理模块，**不锁定业务域**，随知识积累自动扩展，
系统化掌握 5 维结构：业务知识 / 管理流程 / 业务定义 / 业务影响 / 业务规则。

**用户原话（关键约束）**：「并不是马上聚集某个业务域，而是 wiki 模块管理知识的时候
能够和想有的功能结合到一起，可以随着知识的积累不断的学习扩展」。

补充需求：知识导入时可**选择使用哪个 LLM 模型**。

**方案 SSOT**：`~/.claude/plans/abundant-swimming-quail.md`（修正版 v2，13 张新表 + 6 机制，19 周）。

## 2. 已拍板的 3 个决策

| 决策 | 结论 | 影响 |
|---|---|---|
| 分类方式 | **先自动解析识别分类，业务专家可调整修改**（建议 + 默认值，非静默决定） | `wiki_page.auto_classification` 存 LLM 建议，`dimension` 是生效值可覆盖 |
| 业务规则 | **独立表 `wiki_rule_executable` + Agent 直读**，同时文档化 + dry-run | **不碰** `feature_rule` |
| 覆盖度域轴 | **新建 `class_domain_mapping` 映射表** | `ontology_class` 无 `domain` 字段，域轴必须外挂 |

## 3. 已验证的代码事实（地基，勿再假设）

| 事实 | 证据 |
|---|---|
| `feature_rule` + `feature_rule_threshold`（1:N），**无表达式 JSONB 列** | `models.py:1591/1647` |
| `ontology_class` **无 `domain` 字段**；域词表只在 `AGENT_DATA_DOMAINS` 常量 | `models.py:205-265`, `agent_vocabulary.py` |
| `term_dictionary` 自然键是 `term`（UNIQUE），**无 `term_code`** | `models.py:853-877` |
| backend 下 `wiki` **零命中，greenfield** | 全目录 grep |
| migration head 本轮前 = **`0052_system_config`** | `0052_system_config_table.py` |
| 按 id 选模特范式 | `chat_service.py:817`, `data_quality_generate.py:96` |
| LLM 调用 = `createClient(config)` → `client.complete(messages, maxTokens=)` | `base_client.py:83` |

## 4. M1 已落地（0053 Wiki 核心 4 表）

**迁移**：`alembic/versions/0053_wiki_core_tables.py`（`down_revision=0052_system_config`，
`CREATE TABLE IF NOT EXISTS` 幂等；**已 upgrade 到生产 `qa_metadata`**，drift 校验通过）。

**新文件**：

- `app/domain/wiki_models.py` — `WikiPage` / `KnowledgeClaim` / `Evidence` / `KnowledgeRelation`
  + 词表常量 `KNOWLEDGE_DIMENSIONS`(8) / `STRUCTURE_STAGES`(3) / `RELATION_TYPES`(7) / `RELATION_TARGET_TYPES`(4)
- `app/domain/wiki_schemas.py` — Create/Update/Read + `WikiPageListRead`(rows+total)
- `app/services/wiki_page_service.py` — CRUD + `generatePageId`(`PAGE-<SLUG>-<8hex>`) + 维度/阶段白名单校验
- `app/api/v1/wiki.py` — `/api/v1/wiki/pages` CRUD + `/claims` + `/relations`
- `app/tests/integration/test_wiki_api.py` — 18 个真实 PG 集成测试

**改动**：`app/domain/models.py`（末尾 re-export 4 个模型）、`app/services/messages_zh.py`（5 个文案常量）、
`app/main.py` + `app/tests/_testapp.py`（注册 wiki router）。

## 5. 关键实现坑

### M1 踩到的

1. **`_UnsetType` 的 serializer 会让 `model_dump()` 输出 None（上游潜在 bug）**
   `app/domain/schemas.py:2617-2623` 的 `plain_serializer_function_ser_schema(lambda _: None)`
   在 `_UnsetType | str` 这类 union 里**总是先命中**（无类型校验），导致
   `model_dump(exclude_unset=True)` 把**已提供**字段也序列化成 `None` →
   PATCH 把 `title` 写空 → `NotNullViolationError`。
   **本 feature 的规避**：`wiki_page_service.updatePage` 改为遍历 `dto.model_fields_set` + `getattr` +
   `isinstance(value, _UnsetType)`，与既有 `agent_tool_config_service` 的逐字段判断同思路。
   **未动的上游**：`AgentToolConfigUpdate` / `FeatureRuleUpdate` 也声明了 `_UnsetType` 字段，
   但它们只做逐字段 isinstance、**不调 `model_dump`**，故暂未被咬；**修复 `_UnsetType` 本体是独立课题**（会改响应序列化行为，需单独回归）。
2. **异步惰性加载 `MissingGreenlet`**：序列化 `KnowledgeClaimRead.evidences` 时触发懒加载报错。
   修复 = `KnowledgeClaim.evidences` 关系加 `lazy="selectin"`（`WikiPage.claims` 同样处理）。
3. **测试 app 绕过 main.py**：`app/tests/_testapp.py` 直接挂载子路由（规避 FastAPI 0.141
   `_IncludedRouter` prefix 双重叠加 bug）。**新增 router 必须同时注册 `main.py` 与 `_testapp.py`**，否则集成测试 404。

### M2 新增踩坑

4. **异常 → 状态码映射曾在生产 app 与测试 app 各写一份，已漂移**
   `main.py` 的 `_statusFor` 有 `LLMUnavailableError → 503`，`_testapp.py` 的 handler **没有**
   → 落到 `else: 400`。后果是「测试断言 503、测试 app 实际 400」——断言与真实行为脱节，
   而生产其实是 503。
   **修复**：抽出 `app/domain/exceptions.py: statusForError(exc) -> int` 作为唯一映射源，
   `main.py` 与 `_testapp.py` 共用。**后续新增领域异常只改这一处**。
5. **配置性错误吞进逐条降级 → 用户看到假成功**
   `createClient` 返回 None（模型没配 key）时，若只在循环内逐条 `_classify` 降级，
   结果是「所有条目导入成功、所有条目未分类」，任务状态 `SUCCEEDED` ——真相被掩盖。
   **修复**：`LearningLLMInvoker.preflight()` 在建任务前验证 primary/fallback 可用性，
   都不可用则立刻 503（且不留半截 task）。
   因 preflight 必须在建 task 之前、而计量要归属 task，`bindImportTask(taskId)` 在建 task 后补挂。
6. **测试里 `dbSession.expire_all()` 在 async 下会炸 `MissingGreenlet`**
   测试会话与请求会话是**不同**的 `AsyncSession` 实例（同一 engine），
   `select()` 已是真实 DB 读，`expire_all()` 纯属多余；而它会让紧随其后的
   `page.dimension` 属性访问触发**同步**懒加载 → asyncpg 下 `MissingGreenlet`。
   **`expire_all()` 在这个测试基座上是遗留反模式，不要再写**；需要刷新用 `await session.refresh(obj)`。
7. **`LlmClientError` 是 `LLMUnavailableError` 的兄弟类，只捕后者会漏**
   `_classify` 原本只 `except LLMUnavailableError`。但真实客户端（`openai_client` /
   `ollama_client`）把网络、超时、解析异常一律包成 **`LlmClientError`**，而它与
   `LLMUnavailableError` **同为 `DomainError` 的直接子类**（不是父子关系）。
   于是：一次 LLM 瞬时抖动 → 异常冒穿 `_classify` → 冒穿循环的
   `except (ConflictError, ValidationError)` → 整批导入中止。
   **修复**：`_classify` 改捕 **`DomainError`**（含 `NotFoundError`——配置被并发删除同理），
   分类降级为「无建议」，不毁掉已入库的进度。
   假客户端也跟着改成抛 `LlmClientError`：抛裸 `RuntimeError` 测的不是生产那条路。
8. **异常中止会留下永远 `RUNNING` 的僵尸任务**
   任务在循环前就 `commit` 成 `RUNNING`，此后若异常冒穿整个循环，任务再也不会被更新
   ——台账里挂一条「进行中」，实际早就没人跑了。
   **修复**：循环包一层 `try/except Exception`，先 `_markFailedBestEffort` 落 `FAILED` 再 `raise`。
   两个易错点：(a) 必须先 `session.rollback()`（诱因常是上一次 commit 失败，session 正卡在
   `PendingRollbackError`）；(b) **属性赋值要在 rollback 之后**——rollback 会 expire 实例属性，
   先赋值再回滚等于白写。已 `commit` 的页数一并写进台账，别让「失败」看起来像一条都没进。
9. **`parseDrafts` 静默丢弃首个标题前的引言**
   切分按 `source[start:end]` 取，`source[0:start]`（文档摘要/适用范围）直接人间蒸发，
   与 docstring 承诺的「标题前的引言归入第一篇」不符。
   **修复**：把 `intro` 前置拼进第一篇 body（只拼第一篇，不污染后续条目）。

### 任务状态语义（M2 定稿）

| 状态 | 含义 |
|---|---|
| `SUCCEEDED` | 全部草稿入库成功，**且**（若开了自动分类）全部完成分类 |
| `PARTIAL` | 部分入库失败，**或**开了自动分类但有条目未分类成功 |
| `FAILED` | 一条都没入库成功，或导入被异常中止（`error_message` 记原因） |

「开了分类却没分类成」记 `PARTIAL` 而非 `SUCCEEDED`：用户显式选了模型要求分类，
拿到「成功」却零分类，等于把「模型没跑成」谎报成「模型认为这些条目无维度」。
缺口条数写进 `error_message`。（`invoker is None` 即没请求分类时，缺口恒为 0。）

## 6. M2 已落地（0054 导入任务 + LLM 调用层 + 选模）

**迁移**：`alembic/versions/0054_wiki_import_task.py`（`down_revision=0053_wiki_core_tables`）。
建 `wiki_import_task` + `wiki_token_usage`，并给 `wiki_page` 加两列
`imported_via_task_id`（FK→wiki_import_task.id, ON DELETE SET NULL）、
`processing_model_id`（FK→llm_config.id）。
幂等写法：`CREATE TABLE IF NOT EXISTS` + `DO $$ ... IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname=...)`
（PG 无 `ADD CONSTRAINT IF NOT EXISTS`；FK 名 `fk_wiki_page_import_task` / `fk_wiki_page_processing_model`）。
**已 upgrade 到生产 `qa_metadata`**，drift 校验 0 问题。

**新文件**：

- `app/domain/wiki_learning_models.py` — `WikiImportTask` / `WikiTokenUsage`
  + 词表 `IMPORT_TASK_TYPES` / `IMPORT_TASK_STATUSES` / `LEARNING_MECHANISMS`(CLASSIFY/RELATE/CONFLICT/STRUCTURE)
- `app/services/learning/llm_invoker.py` — `LearningLLMInvoker`（选模 + fallback + 计量 + `preflight()` + `completeJson()`）
- `app/services/learning/auto_classifier.py` — 机制 1 分类器（`classify()` → 建议 + 计量结果）
- `app/services/learning/prompts/classify_v1.txt` — 8 维分类 few-shot 提示词
- `app/services/wiki_token_usage_service.py` — `wiki_token_usage` 写入（独立于 `session_token_usage`，**只 flush 不 commit**）
- `app/services/wiki_import_service.py` — `parseDrafts` 切分 + `execute` 编排 + `listUsableModels` + `listTasks`
- `app/api/v1/wiki_import.py` — `/api/v1/wiki/import/{models,preview,execute,tasks}`
- `app/tests/integration/test_wiki_import_api.py` — 54 个真实 PG 集成测试（假 LLM client，保留真实调用链）

**改动**：`app/domain/models.py`（re-export 2 模型）、`app/domain/wiki_schemas.py`（M2 DTO）、
`app/services/messages_zh.py`（7 个文案常量）、`app/main.py` + `app/tests/_testapp.py`（注册 router）、
`app/domain/exceptions.py`（新增 `statusForError`，见坑 4）。

### 设计要点（刻意的取舍）

- **按页提交**：每条草稿成功后 commit 一次。部分成功的进度必须落库，避免
  「跑完 99 条挂在第 100 条 → 全部回滚」。
- **SAVEPOINT 隔离单条**：`async with session.begin_nested()` 包住单条导入，
  失败只回滚这一条（裸 `session.rollback()` 会把 task 一起卷掉）。
- **先查 pageId 冲突再调模型**：撞号是纯本地就能判定的错误，不该白烧一次 LLM 调用。
- **分类失败不阻断入库**：正文才是价值，分类是增强。分类挂掉 → `dimension=None`、任务记成功，
  M3 的批量重分类补。但**计量不吞**：token 真花了，`wiki_token_usage` 行照留。
- **成本以台账为准**：`task.total_cost_usd` 用 `SUM(wiki_token_usage.cost)` 回算，不在 Python 里累加
  ——「调用成功但输出解析失败」的路径也会留计量行，靠累加会漏账。
- **配置性错误必须前置炸掉**：`preflight()` 在建任务**之前**验证 primary/fallback 能造出 client。
  若等循环里逐条降级，用户会拿到「全部导入成功、全部未分类」的假象。见坑 5。

### M2 前端（导入向导）

**新文件**：

- `frontend/src/types/wikiImport.ts` — 与 `wiki_schemas.py` 的 CamelModel 逐字段对齐
- `frontend/src/api/wikiImport.ts` — `listImportModels` / `previewImport` / `executeImport` / `listImportTasks`
- `frontend/src/pages/AdminWikiImportPage.tsx` — 四步向导（`STEP_SOURCE`→`STEP_MODEL`→`STEP_PREVIEW`→`STEP_CONFIRM`）
- `frontend/src/tests/AdminWikiImportPage.test.tsx` — 21 个 Vitest 用例
  （页面覆盖率 100% stmts/lines/funcs、93.8% branches；覆盖 4 步主链路、503 回退、非可行动错误留在原步、
  模型列表失败的降级 + 重试、台账拉取失败降级、超限拦截、上一步导航、重新切分、批量移除）
  —— 其中「超限」用例要渲染 201 个草稿编辑器，单测约 24s，是全套最慢的一条

**改动**：`App.tsx`（`/admin/wiki-import` 路由）、`i18n/zh-CN.ts` + `en-US.ts`（`menu.item.adminWikiImport` + `wikiImport` 顶层块）、
`scripts/seed_menu_config.py`（31 项，`sort_order=590`，icon `import`）、
`tests/integration/test_seed_menu_config.py` + `test_menu_config_icon_alignment.py`（断言随之更新）。

**设计要点**：

- **先预览再入库**：切分是确定性的，但「一段文字该算几条知识」只有用户知道。草稿在落库前
  必须可编辑标题/正文、可删除，否则切错了只能删了重来。
- **不重复 toast**：`httpClient` 的响应拦截器已经 `message.error` 过并 reject 出带 `status` 的
  Error，页面只对**可行动**的状态额外处理——`503`（模型不可用）时 `message.warning` +
  退回 `STEP_MODEL`，其余用页面级 `Alert` 常驻展示（可关闭，不打断重试）。
- **不可用模型保留在下拉里但 `disabled`**：直接过滤掉会让用户以为「模型压根没配」，
  保留并标注「不可用」才能把用户导向「去模型配置补凭据」。
- **关掉自动分类则模型可省**：`canLeaveModelStep = !autoClassify || modelId !== null`，
  后端 `modelId` 也随之为 `null`——「不分类」和「分类但没选模型」是两回事。
- **暂不抽 `ModelSelect` 组件**（plan 里列了）：当前只有本页一个消费者，`ChatPanel` /
  `DataQualityRuleGeneratePage` 各自内联的 Select 语义也不同（Chat 要按会话切、DQ 要持久化）。
  等第三个真实消费者出现再抽，避免先造一个谁都不完全合身的抽象。

**code-reviewer 复审后的 5 处修正**（0 CRITICAL / 0 HIGH，2 MEDIUM + 3 LOW，全部已修）：

| # | 问题 | 修法 |
|---|---|---|
| M1 | `fetchModels` 失败时吞掉异常且**没有重试入口**：自动分类开着 + 模型列表为空 → 「下一步」永久禁用，用户被困死在选模步（原注释还谎称「重进本步会再拉一次」，根本没有这个 effect） | 失败落 `modelsError` 状态 → 选模步出可重试的 warning Alert；注释改成实话 |
| M2 | `MAX_DRAFTS=200` 只写在预览文案里，**从未校验**；超限要打到后端才被 Pydantic 拒，而 `client.ts` 只透传 **string** 形态的 `detail`，数组形态被丢弃 → 用户只看到「请求失败 (HTTP 422)」 | 发请求前拦：超限则禁用「确认入库」并给出「请删除多余草稿或分批导入」的 Alert |
| L1 | `totalCostUsd: string` 与全仓 Decimal 字段约定（`number \| string`，见 `types/modelConfig.ts`）不一致 | 改 `number \| string` |
| L2 | 503 分支三重提示：拦截器已 toast 后端原文 → 页面又 `message.warning` → 再 `setErrorMsg(err.message)` | 去掉多余 toast，`errorMsg` 改成本地化的可操作文案（后端原文由拦截器 toast 已覆盖） |
| L3 | 422 用例 mock 的是「文案恰好等于后端原文」的假形状，页面错误面全坏了它也照样过 | mock 改成拦截器真实产出的形状（通用兜底 message + `detail`），断言页面实际渲染的内容 |

**未修（记录待办）**：`client.ts` 对**数组形态**的 `detail`（Pydantic 校验错误）直接丢弃，
导致所有页面拿到的 422 都只剩「请求失败 (HTTP 422)」。这是跨页面的公共行为，
改动会波及每个页面的错误展示，**不在本 feature 范围内**，故只修了本页能自保的部分（M2）。
建议单独排一个 `feat-client-error-detail`。

**前端测试要点**（踩到的两个坑）：

- 草稿标题/正文是**受控 `Input` 的 value**，不是文本节点：断言要用
  `findByDisplayValue(...)`，`getByText(...)` 永远找不到。
- 移除草稿后 React 会重建列表，**旧按钮节点已脱离文档**——`for (const b of getAllByRole(...))`
  循环点第二次是点空节点。每次点击前**重新查询**。

## 7. M3 已落地（0055 学习反馈 + 机制 1 分类可调整）

**迁移**：`alembic/versions/0055_learning_feedback.py`（`down_revision=0054_wiki_import_task`）。
建 `learning_feedback`（6 类机制共用的反馈表）+ 两个索引
（`ix_learning_feedback_entity` (mechanism, entity_type, entity_id)、
`ix_learning_feedback_time` (feedback_at DESC)）。幂等 `CREATE TABLE/INDEX IF NOT EXISTS`。
**已 upgrade 到生产 `qa_metadata`**，drift 校验通过。

**新文件**：

- `app/services/learning/feedback_loop.py` — `FeedbackLoop.record()` / `recordClassification()`
  + `decideClassificationAction()`
- `app/tests/unit/test_feedback_loop.py` — 10 个决策函数单测（9 参数化 + 1 优先级）
- `app/tests/integration/test_wiki_reclassify_api.py` — 22 个真实 PG 集成测试

**改动**：`app/domain/wiki_learning_models.py`（`LearningFeedback` ORM + `LEARNING_ENTITY_TYPES` /
`LEARNING_USER_ACTIONS` 词表）、`app/domain/models.py`（re-export）、
`app/services/wiki_page_service.py`（`updatePage` 接反馈 + 新增 `reclassify`）、
`app/domain/wiki_schemas.py`（`WikiReclassifyRequest` / `WikiReclassifyRead`）、
`app/api/v1/wiki.py`（PATCH 取当前用户 + `POST /pages/{pageId}/reclassify`）、
`app/services/messages_zh.py`（5 个文案常量）。

### 分类反馈是一个**完全划分**

`decideClassificationAction(autoClassification, newDimension)` 对「用户对系统建议做了什么」
给出穷尽的四分类，不存在第五种情况：

| 条件 | action | 含义 |
|---|---|---|
| 从未分类过（`auto_classification` 为空） | `None` | **不记录** |
| `newDimension` 传了 `null` | `REJECT` | 打回这条分类建议 |
| `newDimension == 建议的 primary` | `CONFIRM` | 认可系统建议 |
| 其它 | `MODIFY` | 改成别的维度（`user_modification` 记新值） |

两个刻意的选择：

- **比对基准是「建议的 primary」而不是「上一次的 dimension」**。用旧值当基准的话，
  用户把 `RULE` 改成 `PROCESS`（记 MODIFY），明天又改回 `RULE`——那只是回到自己
  之前的判断，却会被记成「CONFIRM 了系统」。系统没有说对，凭什么记它一分。
- **没有建议就完全不写反馈**。`auto_classification` 为空时，改维度只是一次普通编辑；
  记成反馈会让训练集里混进「系统什么都没说，用户自己填了个值」的噪声。

### 事务边界是正确性要求，不是风格

`FeedbackLoop.record()` **只 `flush` 不 `commit`**，`updatePage` 末尾统一 commit。
理由：反馈行与 `dimension` 改写必须同生共死。若 `record` 自己 commit，
后面 `_assertDimension` 之外的失败（如并发删除）就会留下「反馈说用户改了维度、
但维度没改」的假记录——学习闭环的输入脏了，后面所有基于它的分析都不成立。
已写测试钉住：非法维度 422 时**既无反馈行、dimension 也保持原值**。

### `reclassify` 与 `PATCH` 的关系

两者落库路径**完全共用**（`reclassify` 内部就是调 `updatePage`），差别只在语义：
`reclassify` 是「针对分类建议的显式处置」这一动作的专用入口（UI 上是一个按钮），
返回体多带一个 `action` 字段，调用方据此区分文案——`action=None` 时不能说「已记录修正」。

`WikiReclassifyRequest.dimension` 是**必填键**（无 default）：漏传是调用方写错了，
显式传 `null` 是「打回这个分类」，两者语义相反。给个 `default=None` 就会把它们混成一个。

## 8. M4 已落地（0056 关系发现 + 候选审核）

**迁移**：`alembic/versions/0056_relation_rejected_at.py`（`down_revision=0055_learning_feedback`）。
给 `knowledge_relation` 加 `rejected_at TIMESTAMPTZ`。**已 upgrade 到生产 `qa_metadata`**，
改前已 `pg_dump -t knowledge_relation` 备份，drift 校验通过。

**新文件**：

- `app/services/learning/relation_discovery.py` — `RelationDiscovery.discoverForPage()`
- `app/services/learning/prompts/extract_relation_v1.txt` — 实体抽取提示词
- `app/services/learning/prompt_fence.py` — 提示词围栏隔离（**抽出的共用实现**，见 §8.5）
- `app/services/wiki_relation_service.py` — `WikiRelationService.review()`
- `app/tests/integration/test_wiki_relation_api.py` — 24 个真实 PG 集成测试

**改动**：`app/domain/wiki_models.py`（`KnowledgeRelation.rejected_at` + `JsonColumn` 导出）、
`app/domain/wiki_schemas.py`（`WikiRelationDiscoverRequest/Read`、`KnowledgeRelationRead.rejected_at`）、
`app/api/v1/wiki.py`（3 个新端点）、`app/services/messages_zh.py`（3 个文案常量）、
`app/services/learning/auto_classifier.py`（改用共用的 `neutralizeFence`，见 §8.5）。

### 8.1 三态用「两字段」表达，而不是两个布尔

`(confirmed, rejected_at)`：待审 `(false, null)` / 已确认 `(true, null)` / 已打回 `(false, 有值)`。

- **打回不删行**：删了下次发现会重新算出同一条候选，用户得反复打回同一件事——
  一个会自己复活的「拒绝」不算拒绝。行留下 + 唯一约束 = 天然幂等。
- **不用第二个布尔**：两个布尔能表达 4 种组合，其中「既确认又打回」是非法态，
  得再造一个约束去挡。用「时间戳有值 = 打回过」把非法态从类型上消掉，
  顺带保住「什么时候打回的」这个信息。
- `confirmed` 的 M1 语义不变（`confirmedOnly` 过滤照旧只认 `true`），已写测试钉住。

### 8.2 审核的幂等语义

409 **只在重复同一个动作**时抛（已确认再 CONFIRM、已打回再 REJECT）：
挡住 UI 双击把反馈刷成噪声，也避免调用方把「早就确认过了」误读成「刚刚确认成功」。

**反向动作不抛 409**——`REJECT` 一个已确认的关系是**撤回确认**（`confirmed` 置回 `false`），
`CONFIRM` 一个已打回的关系是**反悔**。两者都是合法的新意图，都要留反馈。
（真机冒烟时「已确认再 reject 返回 200」第一眼像 bug，已补测试
`test_reject_after_confirm_revokes_it` 把语义钉住。）

反馈快照必须在**改写之前**取：改完再读，系统当时输出的置信度就没了。
`inputSnapshot` 留空是**如实**而非偷懒——反馈回答的是「用户如何处置系统的输出」，
系统的输入（哪篇正文产出的）由 `upstreamPageId` 指回去，在反馈表里再抄一份只会造成两份真相。

### 8.3 刻意不做：Neo4j 入图（**本 feature 唯一偏离方案处**）

方案 §机制 2 写「低置信度关系不直接写 Neo4j，用户确认后才同步图」。
**M4 实现里确认动作只改 PG 状态，不写图**，原因：

1. Neo4j 侧**没有 wiki 条目的节点类型**——`neo4j_client._ALLOWED_LABELS` 只有
   `{Class, Property, Metric}`，wiki 条目没有对应的节点 label。
2. 更要命的是 `linkClassRelation` 用的是 `MATCH` 而非 `MERGE`：端点节点不存在时
   它**静默返回，既报错也不建边**。于是今天写图只能是「看起来同步了其实一条没写」，
   而那比不同步更糟——排查时你看不到任何异常。
3. 关系两端还大量指向 `PAGE`（wiki 条目），本来也没有对应的图节点。

入图是独立特性（要新建 wiki 条目节点类型 + 改 `linkClassRelation` 为 MERGE 或
在同步路径上先建节点），排在 M8 之后。**在此之前「已确认的关系」的唯一真相源是 PG 的
`knowledge_relation`，任何消费方（含 Agent Tool）必须读 PG，不要读图。**
该决定已写进 `relation_discovery.py` 的模块 docstring。

### 8.4 关系发现的两条路径

| 路径 | 类型 | 置信度 | 成本 |
|---|---|---|---|
| 引用检测（其它条目标题在本文正文里逐字出现） | `REFERENCES` → `PAGE` | 0.900 | 零 |
| LLM 抽实体 → 本地匹配 `ontology_class` | `DESCRIBES` → `ONTOLOGY_CLASS` | 0.700 | 一次调用 |

**刻意不做的两路**（阶段性取舍，非遗漏）：

- **嵌入相似度**：要给每条知识建/查 Milvus 向量，成本与运维面都不小，且与引用检测的
  召回高度重叠，留到有真实语料后再评估收益。
- **数据流分析**：要扫 `entity_mapping` 35 万行反查知识条目影响面，属于「影响分析」
  的下游能力，放 M8 Agent Tool 时按需懒查询（方案 §风险 已定）。

设计要点：

- **LLM 路径失败不阻断确定性路径**：抽不出实体只意味着少一路候选，不该让已经算出来的
  引用关系一起丢掉。状态用 `SKIPPED`（没请求）／`SUCCEEDED`／`FAILED`（请求了没成）
  三分，避免「模型静默失败」被读成「这篇文章确实没提到任何本体类」。
- **不给 `modelId` 就不调模型**：确定性路径零 token 是它的卖点，已写测试断言 `fake.calls == 0`。
- **匹配在本地做**（不把 67 个类名塞进 prompt）：类目录变动不必同步改 prompt，
  模型只负责抽原文里真实出现的名词——那是它擅长的。
- **`valid_to IS NULL` 过滤软删除的类**：给已下线的类挂新关系等于往坟头上贴标签。
- **`ON CONFLICT DO NOTHING ... RETURNING`** 而非「先查后插」：并发两次发现时先查后插
  会双双通过检查、第二条撞唯一约束冒 500；`RETURNING` 天然只回吐真正插入的行，
  正好是「本次新增」的定义。
- **短标题不参与引用检测**（`MIN_REFERENCE_TITLE_CHARS=4`）：否则「制度」「流程」
  会在正文里命中一大片，把有价值的关系淹掉。

### 8.5 自查发现并修掉的注入隔离失效（**HIGH，真实缺陷**）

M2 的 `auto_classifier` 里有一段提示词围栏隔离：把正文里的 `<user_content>`
打断成一个插了零宽空格（U+200B）的变体，防止恶意正文提前闭合围栏、把后续文字
变成「围栏之外的指令」。

M4 复制这段逻辑时，**替换值两端的不可见字符丢了**，写成了：

```python
safeContent = page.content.replace("</user_content>", "</user_content>")  # ← 空操作
```

`str.replace(x, x)` 是个 **no-op**：围栏隔离完全失效（攻击者可在正文里写
`</user_content>` + 指令，诱导模型吐出指定实体名；实体名只要命中 `ontology_class`
就被写成真实的 `knowledge_relation` 行），**而全部 21 个测试照样通过** ——
原来的测试只断言「LLM 被调用了、结果被落库了」，从没断言「模型实际收到了什么」。

**修法**：抽出 `app/services/learning/prompt_fence.py`（`neutralizeFence()`），
`auto_classifier` 与 `relation_discovery` 共用一份；替换值由 `_ZERO_WIDTH_SPACE`
**派生**（`"\\u200b"` 显式转义，不再是不可见字面量），保证「替换值恒不等于被替换值」。

**回归保护**：`test_neutralize_fence_breaks_fence_tags` 断言的就是
`neutralizeFence(x) != x` 这个不变量本身（与用什么字符无关）；
`test_discover_neutralizes_fence_in_page_content` 造一条正文里带伪闭合标签的条目，
断言**模型实际收到的 user prompt 里 `<user_content>` / `</user_content>` 各只出现 1 次**。

**教训（值得记住的通用模式）**：一段「肉眼看起来一样」的代码是天然的静默失效点；
用不可见字符做字符串变换时，必须有一条**显式断言替换确实发生**的测试，
否则防御可以在没有任何测试变红的情况下消失。

**已知规模上限**：引用检测是 O(条目数 × 正文长度) 的全量扫描，知识条目上万后会变慢。
届时应改用 PG `pg_trgm` 倒排或 Milvus 召回候选集，而不是继续加大这个循环。
现在只有百量级条目，先不引入该复杂度（已写进方法 docstring）。

### 8.6 M3/M4 代码审查结论与修复

`code-reviewer` + `security-reviewer` 双审（项目规则要求「每次写完代码立即审查」）。
两条 HIGH → MEDIUM 级问题已修，均**带回归测试**：

| 级别 | 问题 | 修法 | 回归测试 |
|---|---|---|---|
| HIGH | **零候选时 token 计量被回滚**：`_persistCandidates` 在没有候选时直接 `return []`，从未 commit。而 `WikiTokenUsageService.record` 只 flush 不 commit（事务边界归调用方），请求会话在 `getDb` 退出时关闭 → 计量行被回滚。「模型调用成功但没抽出任何能命中本体类的实体」这条路径恰恰最该被计量观察，却把真金白银记成 0 —— 与核心约束「每次 LLM 调用必须记录 Token 消耗与成本」直接冲突 | `_persistCandidates` 改为**无条件单点提交**（有候选则先 INSERT，无候选则直接 commit），候选与计量行同生共死 | `test_discover_records_token_usage_even_with_zero_candidates` |
| MEDIUM | **反馈输入快照取到改写后的值**：`updatePage` 先 `setattr` 完所有字段再调 `recordClassification`，届时 `page.title`/`page.content` 已是新值，而 `system_output` 里那条建议是基于**旧**正文算出来的 —— 反馈表里于是出现一组现实中从未同时存在的「输入 → 输出」配对 | 新增 `snapshotPageInput(page)`，在 `updatePage` 的 `setattr` 循环**之前**取快照，作为**必填参数**传给 `recordClassification`（不再由后者自己读 `page`） | `test_patch_snapshot_uses_pre_edit_title` |
| LOW | `_loadClassCatalog` 用 `setdefault` 实现「先到先得」，但没有 `ORDER BY` —— PG 行序不保证，同一实体可能在两次请求里匹配到不同的 `class_name`，候选集不可复现 | 加 `.order_by(OntologyClass.id)` | 已由既有别名匹配测试间接覆盖 |
| LOW | `WikiRelationService._getRelation` 是 check-then-write（先读状态判定重复、再改写），并发两个 confirm 会双双通过检查、各写一条 CONFIRM 反馈，污染「用户确认率」统计 | `SELECT ... FOR UPDATE` 加行锁；第二个事务阻塞后重新读到已更新行，正确地抛 409 | `test_review_reads_relation_with_row_lock`（去掉 `with_for_update` 即失败，已变异验证） |
| MEDIUM | `POST /pages/{pageId}/relations/discover` 会触发 LLM 调用却**没有显式限流**（同组唯一另一个会花钱的端点 `/wiki/import/execute` 有） | 补 `@limiter.limit(rateLimitValue)` + `request: Request` 形参 | 由 slowapi 中间件覆盖 |

**未修（刻意接受，非遗漏）**：

- `authority_level` 未做白名单校验 —— M1 的既有问题，与 M4 无关，归属到策展/权限里程碑统一收口。
- 引用检测的 O(条目数 × 正文长度) 扫描 —— 见上「已知规模上限」，属阶段性取舍。

**方法论要点**：本轮 4 条带新测试的修复都做了**变异验证**（把修复改回去 → 对应测试必须变红 → 再还原：
`with_for_update` 去掉、`prompt_fence` 还原成空操作、`_persistCandidates` 恢复「无候选即早退」、
快照改回 `setattr` 之后再取 —— 四次都如期变红），避免「加了测试但测试不绑定实现」这一最常见的假安全感。
唯一例外是 `ORDER BY id` 那条：它只在「两个类归一化后同名」时才改变行为，现有夹具没有这种数据，
**没有专门测试绑定它**，属已知的覆盖缺口（LOW，行为本身是确定性修复，不是猜测）。
事务边界类缺陷（HIGH 那条）单靠读代码不易发现：它的表现是「少了一条数据」而非报错，
**静默丢数据比抛异常危险得多**。

## 9. M5 已落地（0057 机制 3 冲突 + 机制 4 结构化建议）

一个迁移（0057）承载两条机制，因为它们共享同一件事：**判定「这条知识有什么问题」，
产出待人工处置的记录**。两张表也是这个关系的直接映射。

### 9.1 机制 3：四类冲突，只有一类需要模型

| 冲突类型 | 检测手段 | 判定依据 |
|---|---|---|
| `STALENESS` | 确定性 | 引用的目标条目已失效（`status=EXPIRED` 或 `valid_to` 已过） |
| `GAP` | 确定性 | 关系指向的目标在库里不存在（悬空引用） |
| `OVERLAP` | 确定性 | 两条条目标题归一化后相同 |
| `CONTRADICTION` | LLM | 两条自然语言陈述是否实质矛盾 |

前三类本可以「顺手都交给模型」，但那样做有两处坏处：给每次检测加一笔固定成本，
以及**把本来确定的事实变成概率输出** —— 一条指向不存在本体的知识，模型完全可能看走眼
说「没问题」。确定的事就用确定的方法判。

LLM 那一路也做了成本收敛：只在**已有关系连接的条目对**之间判定（取前 5 条，按
`downstream_id` 排序 ⇒ 候选集可复现），而不是全库两两比较。关系本身就是「这两条知识
有关」的机器判断（M4 产出），拿它当候选集比让模型扫全库便宜几个数量级；召回损失也有限
—— 真正互相矛盾的两条知识通常本就该有引用关系，没有的话那是机制 2 的召回问题，
该在机制 2 修，不该在这里用全库比对兜底。

**模型回传的 `pageId` 必须落在候选集内**，越界的直接丢弃并 `logger.warning`。不校验的
后果不是「多一行脏数据」那么轻：`page_ids` 是无外键的多态数组，DB 拦不住，冲突表里会多出
一条指向空气的记录，看板上永远清不掉（没人能处置一条不存在的知识），而它还会参与冲突数
统计。校验成本是一次 set 查找。

**`SUPERSEDES` 关系排除在失效检测之外**：指向旧版本正是这条关系的用途。把「目标已失效」
一律当冲突，会让每一次正常的版本演进都刷出一条冲突，看板很快变成噪声，用户开始整片忽略
—— 那比不检测更糟。

### 9.2 幂等靠一个**部分**唯一索引，代价是必须排序

```sql
CREATE UNIQUE INDEX uq_knowledge_conflict_open
  ON knowledge_conflict (conflict_type, page_ids) WHERE resolved_at IS NULL;
```

`page_ids` 是数组列，`['A','B']` 与 `['B','A']` 在数组意义上**不相等**。不排序则这个唯一
索引形同虚设，同一对冲突每次检测都新增一行。排序在 `_conflictRow` —— 唯一的构造点 —— 里做，
比指望每个调用方都记得调 `sorted()` 可靠。已在 `qa_metadata_test` 上实测：重复插 `('OVERLAP', ARRAY['A','B'])`
撞唯一约束，插 `('OVERLAP', ARRAY['B','A'])` **放行**。

写入用 `ON CONFLICT DO NOTHING` + `RETURNING`（同 M4）：先查后插在并发下会双双通过检查，
第二条撞唯一约束冒 500；`RETURNING` 只回吐真正插入的行，正好是「本次新增」的定义。

**部分**索引用 `ON CONFLICT` 时必须同时给 `index_where=saText("resolved_at IS NULL")`，
否则 PG 报 `no unique or exclusion constraint matching the ON CONFLICT specification` ——
PG 要靠它把冲突目标推断到那个部分索引上。ORM 侧用 `Index(..., postgresql_where=...)` 声明。

### 9.3 机制 4：两段式，预筛是成本闸门

1. `detectStructureTrigger(content)` 用正则判断这条知识**像不像**有结构可抽
   （阈值 → RULE / ≥3 步编号 → PROCESS / 等号+比值 → METRIC / 定义句 → CONCEPT）。
2. 只有命中触发器**且**调用方给了 `modelId`，才真正调模型。

必须筛的理由：机制 1/2/3 的调用次数由「新建条目数」「有关系的条目对数」决定，而机制 4 若对
每条知识都跑，导入 200 条就是 200 次调用，且**绝大多数条目根本没结构可抽**（一份 FAQ、一段
说明文字）。代价是漏掉措辞不典型的条目 —— 这个取舍是对的：漏掉的可以手工触发，白花的 token
收不回来。

判定顺序固定 RULE → PROCESS → METRIC → CONCEPT，取第一个命中的。顺序有实际影响：一条
「准时交付率 ≥ 95% 不达标则扣款」同时像 RULE 和 METRIC，归到 RULE 更贴近它的用途（可执行
判定，不是口径定义）。已用 `test_trigger_priority_is_fixed` 钉住。

**维度由触发器判定，不由模型决定**：模型只填 `extracted_structure`，`suggested_dimension`
始终取触发器命中的值。让模型同时决定「是什么维度」和「填什么内容」，等于把两个可能各错一半
的判断绑在一起，事后无法归因是分类错了还是抽取错了。

**`seq` 由下标重建**，不信模型给的号（模型偶尔跳号或从 0 起编，而 seq 是流程渲染的唯一序键）。

**形状校验在写库前做**（`validateStructure`，返回归一化后的新字典而非原始 `data`）：模型常
附带解释性字段，原样落库会让 `extracted_structure` 的形状随模型心情漂移。先落库再等人工发现
它没法用，比不落更差 —— 建议列表里混进空壳，审核的人得逐条点开才知道是废的。

### 9.4 建议的终态**不可逆**（与 M4 关系审核刻意不同）

M4 允许「撤回确认」，因为那只是翻一个状态位。而接受一条结构化建议意味着这条知识要升到新结构
阶段、并（M6 起）物化出一条可执行规则或流程草稿 —— 「反悔」得连带撤掉那个产物，是另一个动作。
此处静默翻转状态会让建议与产物脱节。故重复处置**与反向处置**一律 409。

### 9.5 提取状态是四态而非两态

`SKIPPED`（没要求跑 / 没命中触发器）/ `SUCCEEDED` / `FAILED`（模型调用本身失败 —— 修模型接入）
/ `INVALID`（模型答了但填不进任何结构 —— 修 prompt，或这条知识确实抽不出结构）。
四种情况的 `suggestions` 都是空列表，只看列表长度无法区分，用户也就无从知道该不该给个模型
再试一次。`FAILED` 与 `INVALID` 分开是因为两者的修复方向不同，不该共用一个状态码。

### 9.6 M5 代码审查结论与修复

| 级别 | 问题 | 修法 | 回归测试 |
|---|---|---|---|
| HIGH（**M4 复发**） | 机制 4 的 `_persist` 若在「没有建议可写」时提前 `return`，`WikiTokenUsageService.record` 的 flush 会随请求会话回滚 → 形状不合法（`INVALID`）这条**失败最多、调用最频繁**的路径把真金白银记成 0 | `_persist` 无条件单点提交，建议与计量行同生共死 | `test_invalid_structure_still_records_token_usage` |
| MEDIUM | 重复抽取时虽有 `ON CONFLICT` 保证库里只有一条建议，但**用户每点一次按钮仍白付一次调用费** | `_hasPending` 预检：已有待处置的同维度建议则直接跳过，不调模型 | `test_generate_is_idempotent_and_skips_second_call`（断言第二次 `calls == 0`） |
| MEDIUM | 提示词围栏隔离是 M4 那条 HIGH 的同源风险，机制 4 是**新增**一处插值点 | 经共享 SSOT `promptFence.neutralizeFence`，并用**计数**断言（围栏包装本身 1 组标签，多出来的每组都是一次成功注入） | `test_prompt_neutralizes_fence_in_title_and_content` |

**方法论要点（本轮的教训比结论更重要）**：

- 25 个测试**首次运行即全绿**，意味着没有观察到 RED。为补偿，对 5 条承重不变量做了变异验证
  （去掉 operator 校验 / `_nonEmptyStr` 只判类型 / 去掉 `_hasPending` 短路 / 去掉终态检查 /
  去掉围栏中和），**5 条全部如期变红**。
- 其中**第 1 条第一次没被打挂**：原测试样本写成 `{"conditions": [{"value": "1000"}]}`，同时缺
  `field` 与 `operator`，`field` 那道校验把 `operator` 分支挡住了 —— 去掉 operator 校验后测试
  照样绿。这与 M4「`replace(x,x)` 空操作」是**同一类缺陷**：测试写得像在覆盖，实际只覆盖了
  相邻分支。修法是把样本参数化成三个（缺 field / 缺 operator / conditions 不是数组），
  再跑变异，这次**只有 missing-operator 一条变红** —— 命中范围精确，说明测试真的绑定了那条分支。
- 教训：**变异测试的价值不在于「证明测试有效」，而在于它能把「看起来在覆盖」的样本打回原形**。
  凡测试样本「同时缺多个必备字段」的，都要怀疑它只覆盖了第一个检查。

## 10. M6 已落地（0058 机制 5 渐进结构 + 规则 dry-run）

新增两张表（纯新增，不改任何既有表）：

| 表 | 用途 | 唯一键 |
|---|---|---|
| `wiki_rule_executable` | 可执行业务规则（Agent 直读） | `page_id`（**不是**版本流） |
| `process_workflow` | 结构化流程（PROCESS 维度产物） | `page_id` |

`page_id` 做唯一键是刻意的：**一条知识一份产物**。想要版本历史是另一个特性（产物表的
`version` 列先留着），现在按「多版本」建表会让「当前生效的规则是哪条」变成一个每次都要
回答的查询。

**刻意不 sync 到 `feature_rule`**（用户在 plan 阶段拍板）：`feature_rule` 是「特征名 + 算子
+ 阈值」的三元组集合，没有表达式列，装不下 `{conditions: [...], action: {...}}` 这种 DNF
形态；强行摊平会造出一批没有 `feature_name` 语义的伪特征，污染特征注册表。两者是**不同
抽象层的两件事**：`feature_rule` 是给计算引擎用的，`wiki_rule_executable` 是给人和 Agent
读的判决依据。

### 10.1 三态求值：判不了 ≠ 不匹配

`wiki_rule_engine.ConditionResult.matched` 是 `True` / `False` / `None`：

- `True` —— 记录里有这个字段且可比，条件成立
- `False` —— 有字段可比，条件不成立
- `None` —— **判不了**（字段在记录里缺失，或值不可比，如 `"待定" >= 1000`）

`RuleEvaluation.matched = all(c.matched is True for c in conditions)`，所以 `None`
**永远不会**被算成命中。`undecidable` 是派生属性（`any(c.matched is None)`），不是存下来的
字段 —— 存下来就有机会和 `conditions` 不一致。

为什么不用两态：把「判不了」压成 `False`，运维看到的是一条**默默从不触发**的规则，
排查方向会从「数据缺字段」歪到「规则写错了」；压成 `True` 更糟 —— 一条本该拦人的准入
规则会把没人填的字段判成合规。三态把「数据不全」这件事**显式暴露**成第三种结果，
dry-run 报告里的 `undecidable` 计数就是给这条用的。

**算子归一化在写入时做**（`_normalizedConditions`），落库的都是规范形态（`>=` 而不是
`不低于`）。引擎里的中文别名表（`_WORD_ALIASES` / `_SYMBOL_ALIASES`）是给**早于本版本
写入的行**兜底的兼容层，不是新数据的常态。认不出的算子**当场 422**，不做「反正匹配不上
就当 False」的静默降级。

### 10.2 阶段是「派生」的，不是「递增」的

`computeStructureStage(session, pageId)` 每次从**库里的既成事实**算：

```
有产物行（wiki_rule_executable / process_workflow）      → FULLY_STRUCTURED
有半结构化信号（claim / 已确认且未打回的关系 / 已接受建议）→ SEMI_STRUCTURED
否则                                                      → MARKDOWN
```

递增计数器只增不减：产物删了、关系撤回了，计数器还停在原地，看板长期高报且没人发现。
派生让它**自愈** —— `test_stage_drops_back_when_artifact_row_is_removed` 删掉产物行
再重算，阶段从 FULLY 掉回 SEMI。代价是每次多几张走索引的 `LIMIT 1` 存在性查询，
相比「进度长期失真」很划算。

`_hasSemiStructuredSignal` 刻意**不含**待处置（`PENDING`）与已打回（`REJECTED`）的记录：
那些还没被任何人认下来，把它们算成「已结构化」等于让机器单方面宣布结构升级。

### 10.3 事务边界：物化与阶段同步只 flush，不 commit

`applyStructureStage` / `materializeArtifact` **只 flush**，`commit` 一律留给调用方
（`wiki_suggestion_service._resolve` / `wiki_relation_service.review`）。

这是 **M4 与 M5 都栽过的同一个坑的第三次预防**（M5 §9.6 表里那条 HIGH）：辅助方法内部
commit 会让同事务里更早写入的计量/反馈行被一并提交或回滚，而调用方以为自己控制着边界。
把边界统一收在服务方法里，让「谁负责提交」永远只有一个答案。

### 10.4 接受建议时物化走 UPSERT

`materializeArtifact` 对 `page_id` 走 `on_conflict_do_update`。理由：`page_id` 上有唯一
约束，而「改完正文重新抽一条建议、再接受」是**合法的日常路径** —— 直接 INSERT 会撞唯一键，
把一次正常的审核动作变成 500。`test_repeat_materialization_upserts_single_row` 绑定了这个
性质（两次物化后表里仍只有 1 行）。

只有 `RULE` / `PROCESS` 两个维度物化（`_MATERIALIZING_DIMENSIONS`）。`METRIC`（公式）与
`CONCEPT`（定义）不是可执行判决，硬写成规则会让 Agent 拿到一条**没有条件的规则**，
也污染规则准确率统计。`materializeArtifact` 对这两类返回 `None`（不物化，但不是错误）。

`authority_chain` 取自条目自身的 `authority_level`：产物**不该比它的来源更权威**。
条目未定级时留 `NULL` —— 猜一个 L5 会让一条普通文档产出的规则看起来像最高效力。

### 10.5 代码审查结论与修复

| 级别 | 问题 | 修法 | 回归测试 |
|---|---|---|---|
| **HIGH** | 机制 4 抽取时 `_stringOrNone` 把 `IN` / `NOT IN` 的**数组取值压成 `None`**（该函数只放行字符串与数值）。压掉的规则**仍能通过物化的全部校验**（算子合法、条件非空），却因 `_compareMembership` 要求数组而永远判不了 —— 审核人在 dry-run 里看到的是「没命中」，不是「规则坏了」 | 新增 `_conditionValue(value, operator)`：集合算子保留数组形状（逐项归一化）。**抽取阶段仍对形状宽松**，严格裁决留给物化 | `test_membership_rule_survives_extraction_and_can_match`（断言落库是数组 **且** 两个样例都 PASSED、`undecidable == 0` —— 只断言前者会被「存了数组但求值仍判不了」的实现骗过） |
| **HIGH** | 同上问题的**物化侧缺口**：`_normalizedConditions` 对取值只做透传，任何形状都能进库 | 新增 `_normalizedValue(operator, value)`：集合算子取值必须是**非空数组**，否则 422（空数组也拒 —— `IN []` 恒假、`NOT IN []` 恒真，都是「没有条件」的伪装）。层叠防御：抽取阶段出问题时，这里会把「静默不命中」变成「明确的 422」 | `test_materialization_rejects_membership_operator_without_list_value`（并断言建议仍为 PENDING、产物表为空） |
| MEDIUM | `target_entity`（列宽 `VARCHAR(100)`）**无长度校验**：SQLAlchemy 不校验长度，超长值到 Postgres 抛 `DataError` → 无领域异常映射 → 用户可见的 500；而接受建议是终态不可逆的，用户既看不懂也没有重试入口 | 新增 `_boundedText(value, maxLength)`：截到列宽并**记 warning**。截断而非 422 —— 完整取值仍原样保留在 `rule_expression.action`（读模型一并返回），截的只是索引列；422 会让用户卡在一条他无从修改的建议上 | `test_materialization_truncates_overlong_target_entity`（断言列值 100 字符 **且** JSONB 里仍是完整值） |
| LOW | dry-run 的 `examples` **无条数上限**：求值是 `O(样例数 × 条件数)` 的纯 Python 循环，响应还把 `input` 与每条条件明细原样回显 —— 单请求放大，全局 30 req/min 兜不住请求内部的放大 | schema 加 `Field(max_length=100)` | `test_dry_run_rejects_excessive_example_count` |
| LOW | dry-run 的路由注释写「无限流」**是错的**：全局默认限额（30 req/min/IP）一直生效，它才是真正的兜底 | 注释改为「不挂**每路由**限流」并点明全局限额 | （注释，无测试） |
| LOW | `_upsertRule` 里 `rule_kind` / `rule_expression` / `target_entity` / `authority_chain` 在 `values()` 与 `set_` 里**各算一遍**（`deriveRuleKind` 被调两次）。当前结果一致只因这几个函数恰好纯、输入恰好没被改写 | 先算一次再被两处共用 | 由既有 217 条全绿覆盖 |
| LOW（**冒烟发现**） | `dry-run` 把 `dto` 声明成必填，于是**完全不传 body** 会先撞 422 请求校验 —— 而 `examples` 本身是可选的，「不传 body」与「body 里没有 examples」语义相同（都用存的样例）。文档写的三态在入口就少了一态 | 改 `dto: WikiRuleDryRunRequest \| None = Body(default=None)` | `test_dry_run_with_no_request_body_uses_stored_examples` |

**真机冒烟（生产 `qa_metadata`，0058 已 upgrade）**：健康检查 → 建条目（阶段 MARKDOWN）→ `recompute` 幂等 →
三个读端点 404 → **直接注入 PENDING 建议 → 走真实 `accept`** 物化 → 验证 `rule_kind=SET_MEMBERSHIP`、
`target_entity=SUPPLIER`、**`value` 仍是 `["制造业","批发业"]` 数组**（这正是新修的 HIGH）、
`authority_chain=NULL`（条目不猜定级）→ dry-run 两条样例 **`passed=2, undecidable=0`**（规则真能判）→
标量取值的 `IN` 接受 **422 且建议仍 PENDING、产物表 0 行** → **删产物行后 `recompute` 把阶段从
FULLY 掉回 SEMI**（派生自愈在真实数据上成立）→ 删条目级联清干净。

> 冒烟刻意**绕过 LLM**：机制 4 的抽取要真调模型（生产无 key 会 503），故用 SQL 注入 PENDING 建议，
> 再走真实 `accept` 链路。这样验的是 M6 自己的代码（物化、守卫、阶段、dry-run），而不是模型的可用性
> —— 但**覆盖不到**「机制 4 抽取 → 建议 → 接受」这一段（那段由 217 条集成测试里的假 client 覆盖）。
> 注：`test_dry_run_with_no_request_body_uses_stored_examples` 这条**就是冒烟发现的** ——
> 集成测试一直写 `json={}`，把「不传 body」这个入口悄悄绕过去了。

**方法论要点 —— 又一次「看起来在覆盖」的样本**：

- 65 个测试（60 个新机制 + 5 个审查/冒烟修复）**首次运行观察到了 RED**（`ModuleNotFoundError: app.services.learning.wiki_rule_engine`
  —— 模块还没建），这一点比 M5 好（M5 是首次即全绿）。为对冲「一次就绿」的风险，
  照旧对 21 条承重不变量跑了变异验证（审查修复的 4 条另见下文）。
- 这 21 条里 **19 条第一批就被打红**，2 条泄漏，两条都不是「测试白写」这么简单：
  - **泄漏 1（真缺陷，已修测试）**：`test_pending_suggestion_does_not_move_stage` 在
    「去掉阶段联动」的变异下照样绿。根因是**产生建议根本不触发 `applyStructureStage`**，
    所以那条页面的阶段压根没被计算过，断言 `== "MARKDOWN"` 是因为**字段的默认值**恰好
    也是 MARKDOWN —— 它测的是「什么都没发生」，不是「算了，算出来是 MARKDOWN」。
    修法：在断言前显式 `POST /structure/recompute`，让断言绑到**计算的结果**上。这是一条
    通用教训：**断言一个「没变」的值时，必须先证明它被算过**，否则默认值会冒充计算结果。
  - **泄漏 2（测试没问题，是代码冗余）**：`test_rejected_relation_does_not_move_stage`
    在去掉 `rejected_at.is_(None)` 过滤后仍绿。追查发现 `review()` 的 REJECT 路径**同时**
    把 `confirmed` 置 `False` 且写 `rejected_at`，CONFIRM 路径反过来 —— 两个过滤条件
    **互为冗余**，单独去掉任一个都改不了结果，只有**同时**去掉才会翻。用双重变异验证后
    该测试如期变红，确认它确实绑定了性质。结论：过滤器冗余是**刻意的纵深防御**，不改代码。
- 审查修复的 4 处**各自补了变异验证**（把修复逐个反转，看对应测试是否变红）：抽取丢数组、物化不校验形状、
  `target_entity` 不截断、`examples` 去上限 —— **4 条全部如期变红**。其中「`examples` 去上限」第一次
  用 `MUTANT_UNBOUNDED` 替换，报的是 **collection error 而非断言失败**（模块 import 就炸了）——
  这不算数，它只证明「改坏了代码会挂」，不证明「测试绑定了那条边界」。改用**合法但错误**的
  `max_length=1000` 重跑，才是真的断言失败。**变异必须落在「能跑但语义错」上**，落在「语法/名字不存在」
  上等于没测。
- 抽取侧修复还暴露出 A/B 两处修复是**分层防御而非二选一**：单独反转抽取修复时，物化侧的 422
  兜住了那条静默不命中的规则 —— 报错信息明确（`算子 IN 的取值必须是非空数组`），而不是让它进库。
- 顺带把 prompt 的算子契约补全了（`suggest_structure_v1.txt` 原先只列了 `>= <= > < =`，既没有
  `!=`/`IN`/`NOT IN`，也没说集合算子要数组）。**代码是权威、prompt 是对齐**：prompt 写得含糊
  只会让模型多产出几种形状，而形状裁决永远在 `_normalizedValue` 这一处。
  刻意**没有**加「逗号分隔字符串自动拆数组」—— 那是投机的一般化（正文里的公司名可能真带逗号），
  且会把一次明确的抽取缺陷变成一次静默的猜测。
- 与 M4（`replace(x,x)` 空操作）、M5（样本同时缺 field 与 operator）合成一条规律：
  **变异测试的价值在于把「看起来在覆盖」的样本打回原形**，而不是证明测试有效。
  本轮新增的怀疑信号是「**断言一个未变的值**」—— 与「样本同时缺多个必备字段」并列。

## 10A. M7 已落地（0059 机制 6 覆盖度 + class→domain 映射）

两张表：`coverage_cell`（矩阵格，`dimension × ontology_class_id × domain` 唯一）与
`class_domain_mapping`（类 → 业务域）。`ontology_class` **没有 `domain` 字段**，所以域轴
只能靠这张映射表带出来 —— 这是 plan 阶段就验证过的事实（§3），不是实现时才发现的。

### 10A.1 覆盖度是**派生快照**，不是累积状态

`POST /coverage/refresh` 是全量重算：先算出现在应有的格子，再**删掉这次没算出来的旧格**。
反复刷不会「越刷越高」，类被软删或域标注被摘掉后旧格会自己消失。这条性质是刻意的 ——
覆盖度是「按当前 ontology_class × 当前映射算出来的投影」，任何一处源头变了投影就该跟着变；
把它做成增量累加，删掉一个类之后看板上就永远留着一格指向不存在的类。

`GET /coverage` 读的是**上一次刷新的快照**，不实时算。「打开看板」不该是一个写操作。

### 10A.2 `UNASSIGNED` 与「缺口」是两个不同的东西

未标业务域的格子（`domain = 'UNASSIGNED'`）默认**不算缺口**，能通过
`includeUnassigned=true` 收进来。理由：一个类还没人认领业务域，是**元数据没填**，
不是**知识没写** —— 混进缺口清单会让「去补哪篇文档」这个问题被「去填哪个域」淹没。

真正隐藏在这条线之外的是**未挂载条目**（`unlinked`）：正文写了、维度也有了，但没挂到
任何已确认的业务对象上。这类条目**在矩阵里根本不可见**（矩阵只有 class 轴），所以
`/coverage/overview` 把它单列一块返回，并在 Agent 的回答里点出来 ——
「矩阵看不见这部分」比「覆盖度是 0」更接近事实。

### 10A.3 一次取齐

`GET /coverage/overview` 把 summary + gaps + unlinked 合成一个响应。三块数据是**同时**
渲染的，分三个请求不仅多两次往返，更会让三块来自不同的刷新时刻 —— 看板上会出现
「汇总说 8 格、缺口列表按 7 格算」这种自相矛盾。

### M7 前端与菜单

4 个页面挂在**新的一级目录「企业 Wiki」**下（用户明确要求：一级 = 企业 Wiki，
二级 = 具体功能）。菜单是 `menu_config` DB 驱动，新增页面必须同步
`scripts/seed_menu_config.py`：

```
section.enterpriseWiki（一级，sort 240）
  ├ item.wikiPages        250  /admin/wiki-pages        AdminWikiPagesPage
  ├ item.wikiImport       260  /admin/wiki-import       AdminWikiImportPage
  ├ item.wikiConflicts    270  /admin/wiki-conflicts    AdminWikiConflictsPage
  ├ item.wikiSuggestions  280  /admin/wiki-suggestions  AdminWikiSuggestionsPage
  └ item.wikiCoverage     290  /admin/wiki-coverage     AdminWikiCoveragePage
```

240 是本段唯一空闲的百位区间；5 个二级项各占 10 的步长，给以后插页留缝。
`label_key` 一律走 **`menu.item.*`** 命名空间（不是 `appLayout.menu.*`）。

## 10B. M8 已落地（Agent Tool + 端到端 demo）

### 10B.1 一个 Agent 只能绑一个工具 ⇒ 4 个知识工具 = 4 个 Agent

`agent_definition.tool_name` 是**单值可空列**（feat-agent-tool-config-db 定的），
所以「一个知识 Agent 挂 4 个工具」在当前模型下表达不了。要么改列成数组，要么
一个工具配一个 Agent。选了后者：改列会让 `AgentBindingCache`、`tool_name_updated_at`
的失效语义、以及所有按 code 查绑定的调用点全部跟着动，而知识工具的调用方本来就是
「知道要问哪种问题」的（检索 / 读全文 / 试跑规则 / 看覆盖度），不是一个模糊的入口。
分裂成 4 个 Agent 反而让 ACL 与调用意图都更清楚。

```
WIKI_SEARCH_AGENT    → wiki_search      data_object=WIKI_PAGE      layers=[]
WIKI_READ_AGENT      → wiki_read        data_object=WIKI_PAGE      layers=[]
WIKI_RULE_AGENT      → rule_evaluate    data_object=WIKI_RULE      layers=[]
WIKI_COVERAGE_AGENT  → coverage_status  data_object=WIKI_COVERAGE  layers=[]
```

`domains` / `layers` 全空是刻意的：知识库是**跨域**资产（条目归业务对象 KO，不归数据域），
给它钉一个采购域等于造了一个假边界。

### 10B.2 四个工具全是**零 token** 的只读工具

没有 `modelId` 参数，不调 LLM，所以 `tokensUsed=0` / `cost=0`。这是设计的直接结果：
它们**读的是已经有结论的东西**（已物化的规则、已确认的关系、已刷新的覆盖度快照），
不做推理。要推理的是机制 1~4，那是写路径。

`wiki_search` / `wiki_read` / `rule_evaluate` 用 `wiki_text` 抽取器（正文即条目指称），
`coverage_status` 用 `wiki_no_args`（输入不携带参数）。

### 10B.3 条目指称解析：精确 ID 命中即终局

`findPageCandidates` 分档匹配：**page_id 精确命中 → 直接返回，不再往下捞**。

这一条是被集成测试逼出来的。原实现把 page_id 精确命中与 ILIKE 命中**合并**排序，
于是「用户抄了 pageId」会被降级成「请从 N 条里挑一条」—— 别的条目正文里提到过这个 ID，
就会被 ILIKE 捞上来当候选，`wiki_read` 于是返回 `resolved: false` + 一堆候选。而
`page_id` 是唯一键，这一档最多命中一条，命中就是决定性答案。

### 10B.4 M8 集成测试抓到的三个**真实产品缺陷**

这是本 feature 里集成测试（真 PG + 完整 API 链路）价值最高的一次 —— 21 条测试当场
打红了三个 unit 与 273 条 wiki 测试都没碰到的缺陷：

| 级别 | 缺陷 | 表现 | 根因 | 修法 |
|---|---|---|---|---|
| **HIGH** | `agent_tools_wiki` 漏 import `MSG_WIKI_AGENT_PAGE_REF_NOT_FOUND` | 任何没匹配上的条目指称 → **500** 而不是 404 | 新增的 4 个 handler 用了消息常量却没进 import 列表；单元测试直接调 handler 不走异常映射层，看不到 | 补 import |
| **HIGH** | `AgentAccessPolicyCreate` 缺 `data_object` 归一化 | 管理员填 `"  wiki_page  "` → 存储原样 → 运行时 `p.data_object == tool.data_object` 精确比对**永不命中** → 403。**写入毫无报错**，授权静默失效 | `data_layer` 在同一个类里有 `_normalizeDataLayer`，`data_object` 却没有；而 `AgentToolConfigCreate` 两个都有 —— 一个**不对称**的遗漏 | 补 `_check_data_object` |
| MEDIUM | `seed_agents.py` 独立执行必炸 `AgentToolConfigRegistry 未 warmUp（lifespan bug）` | `python -m scripts.seed_agents` 直接 RuntimeError | 该脚本从不走 lifespan，而 `_policiesFor` 依赖已 warmUp 的 registry。**报错信息把人引向 lifespan，真因是脚本自己没装 registry** | 加 `_ensureToolRegistry`（seed tool config → invalidate → warmUp），并把 tool_name 绑定一并补上（原来是 lifespan 干的活，脚本独立跑会「seed 报成功但 Agent 全 409 未绑定工具」） |

三者有共同形状：**单测看得见的层（handler / schema / service）与产品真正跑的那条路
（异常映射 / 边界归一化 / 脚本独立执行）之间有一条缝**。补法也一样 —— 让测试走真链路。

### 10B.5 策略种子必须**跟着工具走**

`_policiesFor` 原先对无绑定 Agent 无差别回退 `_DEFAULT_POLICIES`（一份写死 SUPPLIER 的
通配策略）。对 SUPPLIER 工具恰好等价，对知识工具就是**静默 fail-closed**：运行时按
精确对象比对，一条 `WIKI_PAGE` 策略都没有 → 全 403。

改法：**层无关工具按对象粒度授权**（`data_layer=None`），对象取该工具自己声明的
`data_object`。元数据 Agent（无绑定 / 工具被禁用）才回退通配。授权要跟着工具走，
不能跟着「默认」走。最终矩阵：

```
GRAPH_REASONING_AGENT | SUPPLIER      | DWD        | read
SUPPLIER_360_AGENT    | SUPPLIER      | FEATURE    | read
WIKI_COVERAGE_AGENT   | WIKI_COVERAGE |            | read
WIKI_READ_AGENT       | WIKI_PAGE     |            | read
WIKI_RULE_AGENT       | WIKI_RULE     |            | read
WIKI_SEARCH_AGENT     | WIKI_PAGE     |            | read
```

#### 10B.5.1 第四个缺陷：**已存在的 Agent 永远补不回策略**（demo 复跑时抓到）

`_policiesFor` 修好之后，上面那张矩阵里四个知识 Agent 仍然是 403。上面那条修法
只覆盖了 **新建** Agent 的路径，而：

- `seedAgents` 对已存在的 `agent_code` 一律 `continue` —— `_policiesFor` 不会再跑；
- 既有的愈合函数 `_reconcileLayeredPolicies` 开头就 `if not tool.data_layers: return 0, 0`
  —— 层无关工具被**整类跳过**。

两条路都关上，结果是 **Agent 行在、策略没了 → 永久 403，而 seed 输出一切正常**。
触发条件很日常：测试库被 TRUNCATE 后重 seed（autouse fixture 干的）、旧部署新增知识
工具、任何「Agent 先于工具存在」的库。修法是把愈合拆成两支，层无关工具走
`_reconcileObjectGrainPolicy`（**只增不删** —— 这类工具的正确策略本身就是
`data_layer=None`，套用有层分支的删除逻辑会把正确策略删掉）。

回归测试：`test_seed_agents.py::test_existing_layerless_agent_is_healed_by_reseed`
（删策略 → 重跑 seed → 断言补回）与 `::test_heal_is_idempotent_and_leaves_layered_agents_alone`。
**变异验证**：把愈合分支改回 `return 0, 0`，前者立刻打红 —— 这条测试有承重。

**写这条测试时踩到的 identity map 陷阱**：`AgentRegistryService.listPolicies` 读的是
`agent.policies` **关系集合**（identity map 缓存），测试里用批量 `delete()` 绕过 ORM
关系删策略后，愈合会看到「幽灵策略」而不触发。真实故障里删策略的是**另一个进程**，
会话本就干净 —— 所以修法是测试里 `expire_all()`（忠实模拟），不是给代码打补丁。

#### 10B.5.2 复跑验证（三条都要真的看到）

| 动作 | 期望 | 实际 |
|---|---|---|
| 手工删掉 4 条 `WIKI_*` 策略 → 跑 demo | seed 打印 4 行「策略愈合 删0 条 / 增1 条」，4 demo 全过 | ✅ 与期望一致，DB 里 notes 为愈合分支文案 |
| 再跑一次 demo | **不**打印愈合行（幂等），4 demo 仍全过 | ✅ |
| 复跑时清理上一轮数据 | 三类 feedback 全清（`DEMO-` 字符串 + 两个数字 id） | ✅ `3 条目 / 2 关系 / 1 冲突 / 3 反馈`；修前只清得掉 1 条 |

`learning_feedback.entity_id` 是**无 FK 的多态业务键**：CLASSIFY → `page.page_id`
（`"DEMO-RULE-0001"`）、RELATE → `str(relation.id)`、CONFLICT → `str(conflict.id)`。
所以 `LIKE 'DEMO-%'` 只匹配得上第一类，另两类会静默泄漏成脏数据（复跑次数越多越脏）。
正解是先取 id 再用 `entity_type + entity_id` 精确匹配 —— 这也解释了为什么清理计数
必须把四类都打印出来：只报一个数就看不见漏。

### 10B.6 端到端 4 demo（`scripts/demo_wiki_e2e.py`，可重复执行）

4 个 demo 全部走**真实 app 的完整 API 链路**（`createApp()` + 真 lifespan +
httpx ASGI 传输），不是直接调 service —— 绕过路由层的演示证明不了「用户点得到的那条路」
是通的。每个 demo 都带会抛异常的断言，「演示通过」这句话因此有代价。

```
DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test' \
    .venv/bin/python -m scripts.demo_wiki_e2e
```

| demo | 主张 | 关键断言 |
|---|---|---|
| 1 写规则→分类→dry-run→Agent 直读 | 规则闭环 + 学习闭环 | `action == "MODIFY"`（基准是**建议的**维度）→ feedback 行 +1；dry-run 三态 (1,1,1)；**Agent 报的三态与 HTTP dry-run 逐字段相等**；`tokensUsed == 0` |
| 2 跨条目关系 | 候选 ≠ 事实 | 管理面 `/relations` 看得到 `confirmed=false` 的候选；**Agent 看不到**；`confirm` 之后同一进程内立刻看得到 |
| 3 矛盾检测 | 不给模型也能检出确定性那部分 | `llmStatus == "SKIPPED"`（不是 FAILED）；`STALENESS` 命中；处置后 `resolvedAt` 非空 |
| 4 覆盖度 | 看板与 Agent 是**同一份快照** | Agent 的 `summary` 与 `/coverage/overview` **逐字段相等**，`unlinked.pageCount` 也相等 |

demo 4 那条断言是 M7+M8 的接缝：工具若现场重算，两个界面就会各说各话 ——
而「同一份数字被反复引用」正是看板的全部价值。

**确定性优先**：所有需要 LLM 的步骤（机制 1 抽取 / 机制 2 关系发现 / 机制 4 结构化）
都走脚本直接落库，并在输出里**就地标注**「生产上这一步由 X 产出」。这样 demo 不依赖
任何模型 key，也不会因为模型抖动而时绿时红。被标注的三处：

- `wiki_page.auto_classification`（机制 1 产出）→ 没有它 `reclassify` 的 `action` 会是 `None`，学习闭环那一环根本没被触发
- `wiki_rule_executable`（机制 4「建议 → 接受」产出）→ 要真调模型
- `knowledge_relation(confirmed=false)`（机制 2 发现产出）→ 带 modelId 才调模型

**写库闸**：脚本会写数据，默认只接受库名含 `test` 的目标，否则 `SystemExit`。要打别的库
得显式加 `--allow-non-test-db`。这条闸是给 `qa_metadata` 那次整库被清的事故立的规矩
（见 memory `qa-system-pg-wipe-incident`）：演示脚本不该有「手滑打到生产库」这个选项。

**压 SQL echo 只能用 `logging.disable`**：非生产环境 `create_engine(echo=True)` 会让
每条 SQL 打两遍，把 4 个 demo 的结论淹掉。但 `logger.setLevel(WARNING)` **压不住** ——
SQLAlchemy 的 `InstanceLogger._log`（`sqlalchemy/log.py`）先算
`selected_level = _echo_map[echo]`，`echo=True` 时它**直接是 INFO**，根本不回退去读
logger 自己的级别（只有 `echo=None` 才 `getEffectiveLevel()`）。`logging.disable(INFO)`
走的是另一条路（`_log` 开头就查 `logger.manager.disable >= level`），是唯一能挡下 echo 的开关。

## 11. 接口面

### M1

```
GET    /api/v1/wiki/pages                    分页（dimension/status 过滤 + limit/offset）
POST   /api/v1/wiki/pages                    创建 201（pageId 可省，重名 409，非法维度 422）
GET    /api/v1/wiki/pages/{pageId}           详情（无则 404）
PATCH  /api/v1/wiki/pages/{pageId}           PATCH 语义（未传字段不动）
DELETE /api/v1/wiki/pages/{pageId}           204（DB 级联清 claim/evidence/relation）
GET    /api/v1/wiki/pages/{pageId}/claims    事实原子 + 证据
GET    /api/v1/wiki/pages/{pageId}/relations 传出关系（confirmedOnly 可选）
```

### M2

```
GET    /api/v1/wiki/import/models            可选模型 + usable 标注（is_active 且 createClient 能造出 client）
POST   /api/v1/wiki/import/preview           Markdown → 草稿（不落库、不调模型）
POST   /api/v1/wiki/import/preview-file      上传文件 → 纯文本 + 来源类型（不落库、不调模型）
                                             格式 PDF/.docx/.md/.txt；>10MB → 413；格式不支持 → 422（换格式）；
                                             解析失败 → 422（换文件，底层异常只进日志不回显）
POST   /api/v1/wiki/import/execute           201 建 Page（可选分类）+ 任务台账 + 计量
GET    /api/v1/wiki/import/tasks             导入作业台账（倒序 + total）
```

### M3 / M4

```
POST   /api/v1/wiki/pages/{pageId}/reclassify        机制 1：调整分类结论（回 action + 写反馈）
POST   /api/v1/wiki/pages/{pageId}/relations/discover 机制 2：发现候选（modelId 可选，返回新增候选）
POST   /api/v1/wiki/relations/{relationId}/confirm    审核通过（重复确认 409）
POST   /api/v1/wiki/relations/{relationId}/reject     打回（保留行盖章；对已确认的是撤回）
```

`PATCH /pages/{pageId}` 同时获得了反馈语义：显式提供 `dimension` 即视为对机制 1 建议的处置，
actor 取自当前用户（**不信任请求体**，已写测试：body 里塞 `feedbackUserId: 99999` 会被忽略）。

### M5

```
POST   /api/v1/wiki/pages/{pageId}/conflicts/detect    机制 3：检测冲突（modelId 可选，返回**新增**的）
GET    /api/v1/wiki/pages/{pageId}/conflicts           某条目的冲突（未处置在前）
GET    /api/v1/wiki/conflicts                          冲突看板列表（status/severity/conflictType + limit/offset）
POST   /api/v1/wiki/conflicts/{conflictId}/resolve     处置（RESOLVED/IGNORED/MERGED，写反馈；重复处置 409）
POST   /api/v1/wiki/pages/{pageId}/suggestions         机制 4：产出结构化建议（modelId 可选，返回 triggeredKind）
GET    /api/v1/wiki/pages/{pageId}/suggestions         某条目的建议（status 过滤）
POST   /api/v1/wiki/suggestions/{suggestionId}/accept  接受（写反馈 CONFIRM；**终态**，重复/反向 409）
POST   /api/v1/wiki/suggestions/{suggestionId}/reject  拒绝（写反馈 REJECT；**终态**，重复/反向 409）
```

`GET /wiki/conflicts` 的 `status` 形参在 Python 侧叫 `status_`（`alias="status"`）：模块顶部的
`from fastapi import status`（HTTP 状态码常量）会被同名的形参遮蔽，而同文件其他路由都在用它。

两个检测端点（`conflicts/detect`、`suggestions`）都挂了 `@limiter.limit(rateLimitValue)`，
理由是它们是「用户输入 × LLM 调用」的放大入口 —— 调用方既能选 `modelId` 又能决定正文内容，
重复调用就是重复付费。全局默认限额已兜底，显式声明是为了把「这是花钱的接口」写在代码上。

**`IGNORED` 与 `RESOLVED` 必须分开**：前者是「用户判定这不是冲突」（系统误报），后者是
「确实是冲突且已处理」。合并成一个「已关闭」，机制 3 的误报率就永远算不出来。动作到反馈的
映射：`RESOLVED`/`MERGED` → `CONFIRM`，`IGNORED` → `REJECT`。

**授权现状**：两个 router 都在 router 级挂 `dependencies=[Depends(getCurrentUser)]`，
整组要求已认证。角色细分（KNOWLEDGE_OWNER/REVIEWER）留到策展里程碑。

**Markdown 切分规则**（`parseDrafts`，确定性）：取**最浅**标题层级作为切分点；
最浅层出现 ≥2 次 → 按它切分多篇（标题前的引言归入第一篇）；否则整篇一条草稿。
这样 `# 标题` + `## 小节` 的文档不会被切成碎片。

### M6

```
POST   /api/v1/wiki/pages/{pageId}/structure/recompute   重算并落库结构阶段（幂等）
GET    /api/v1/wiki/pages/{pageId}/rule                   取可执行规则（无则 404）
GET    /api/v1/wiki/pages/{pageId}/workflow               取结构化流程（无则 404）
POST   /api/v1/wiki/pages/{pageId}/rules/dry-run          在样例上试跑（不调 LLM）
```

`dry-run` **刻意不挂限流**（与 M5 的两个 detect 端点相反）：它是纯读 + 纯函数，不调 LLM，
重放不花钱，也就没有「用户输入 × 付费调用」的放大效应。把它限流只会挡住正当的调试行为。

`dry-run` 的 body `examples` 是**三态**语义：不传 → 用规则里存的样例；传 `[]` → 明确地
「这次不带样例」（得到 `total=0` 的报告）；传非空数组 → 用这次的。**「用存的」与「用空的」
不能混**，否则想跑一次空样例的人会莫名其妙拿到一堆结果。

### M7（机制 6 覆盖度）

```
GET    /api/v1/wiki/coverage              覆盖度矩阵（domain/dimension/coverageStatus 过滤；读上一次快照）
GET    /api/v1/wiki/coverage/overview     一次取齐：summary + gaps + unlinked
POST   /api/v1/wiki/coverage/refresh      全量重算（幂等；返回 cellCount/classCount/removedCount）
GET    /api/v1/wiki/coverage/domains      已用过的业务域词表（从数据里去重，不是常量）
GET    /api/v1/wiki/coverage/mappings     类→域映射列表
```

路由顺序：`/coverage/overview` 这类**静态段必须排在动态段前**（同 §M5 的
`/rules/options` 陷阱）。

`/coverage/overview` 的 `includeUnassigned` 默认 **false**、`gapLimit` 默认
`DEFAULT_GAP_LIMIT`（上限 `MAX_GAP_LIMIT`）：缺口清单是「去补哪篇文档」的行动项，
不该被「还没人认领业务域」的元数据待办挤满。

### M8（Agent Tool）

四个工具都是 `agent_tool_config` DB 行 + 代码里 `WIKI_HANDLERS` 的 handler 引擎
（`AgentToolAssembly.assemble` 合并，见 feat-agent-tool-config-db）。**没有新 HTTP 路由**
—— 调用面是既有的 `POST /api/v1/agents/{agentCode}/run`：

```
POST   /api/v1/agents/WIKI_SEARCH_AGENT/run      input = 检索词        → wiki_search
POST   /api/v1/agents/WIKI_READ_AGENT/run        input = 条目指称      → wiki_read
POST   /api/v1/agents/WIKI_RULE_AGENT/run        input = 条目指称      → rule_evaluate
POST   /api/v1/agents/WIKI_COVERAGE_AGENT/run    input = 任意非空串    → coverage_status
```

四个工具**全部只读、零 token、零成本**（`tokensUsed=0` / `cost=0`），
`AgentRunRequest.input` 是 `min_length=1`，所以空格串（`"   "`）能进到抽取器、
真空串在 schema 层就 422 —— 想验「空输入也返回结果」必须用空格串。

**ACL 是 deny-by-default 且按对象精确比对**：`AgentRuntimeService._enforcePolicies`
比 `p.data_object == tool.data_object`（精确，非前缀非通配），层无关工具再比
`data_layer`。策略对象不匹配 → 403，零策略 → 403。故 `AgentAccessPolicyCreate`
的 `data_object` 必须与 `data_layer` 一样在**边界归一化**（strip + upper），
否则管理员填 `"  wiki_page  "` 会存成原样、策略永不命中、授权静默失效（见 §10B.4）。

## 12. 里程碑总览（全部完成）

| 里程碑 | 内容 | 迁移 | 状态 |
|---|---|---|---|
| M1 | Wiki 核心 4 表 + Page CRUD | 0053 | ✅ |
| M2 | 导入任务 + 模型选择 + `LearningLLMInvoker` + 导入向导页 | 0054 | ✅ |
| M3 | 机制 1 分类可调整 + `learning_feedback` 学习闭环 | 0055 | ✅ |
| M4 | 机制 2 关系发现 + 候选确认/打回 | 0056 | ✅（**不入图**，见 §8.3） |
| M5 | 机制 3 冲突（1 路 LLM + 3 路确定性）+ 机制 4 结构化建议（预筛 + 精抽） | 0057 | ✅ |
| M6 | 机制 5 渐进结构 + 规则 dry-run + 流程 | 0058 | ✅ |
| M7 | 机制 6 覆盖度 + class→domain 映射 + 看板页 + 一级菜单「企业 Wiki」 | 0059 | ✅ |
| M8 | 4 个知识 Agent Tool + 策略种子愈合 + 端到端 4 demo | — | ✅ |

**M1~M8 全部落地，本 feature 完成。** 剩余的是 §13 的**效果指标**（分类 Top-1 ≥ 85% 等），
那需要在真实知识量（100 条后）与人工标注 ground truth 上评测，不是代码交付项。

> **迁移编号已两度顺延，以本表为准**。plan 原排 `0054_wiki_learning_tables` /
> `0055_wiki_coverage_tables` / `0056_wiki_structure_tables` / `0057_wiki_import_task`；
> 实际实现按里程碑先后重排：导入任务提前到 **0054**（M2 先行，`wiki_page` 的两个新字段
> 也由它带来），M3 的 `learning_feedback` 落在 **0055**，M4 的 `rejected_at` 落在 **0056**。
> 连带的后果是：后面 M5/M6/M7 各再顺延两位（0057/0058/0059），**表内容与 plan 一致**，
> 只是落库顺序不同。另注意 M3/M4 的迁移**不是** plan 里的那两张建表迁移
> ——`structure_suggestion` / `knowledge_conflict` / `coverage_cell` 等表仍属 M5-M7。

## 13. 验收指标

- 机制1 分类 Top-1 ≥ 85%（100 条后） / 机制2 关系召回 ≥ 70% / 机制3 冲突准确率 ≥ 80% / 机制4 建议接受率 ≥ 40%
  — **待评测**：需要 100 条以上真实条目 + 人工标注 ground truth，属效果指标而非交付项。
- 端到端 4 demo：写规则→分类→dry-run→Agent 直读；跨 Page 关系；矛盾检测；覆盖度缺口
  — ✅ **已通过**，`scripts/demo_wiki_e2e.py` 可重复执行，见 §10B.6。
