# 变更：feat-research-evidence-contract

- **日期**：2026-10-13
- **作者**：Claude / 启琳
- **Phase**：**环节一** 研究会话证据契约（Task 1–9）
- **状态**：in-review（32 个提交已在分支 `feat/multi-step-persist`，HEAD `17cd1f0`；**未合并 main**）
- **关联变更**：
  - successor（计划中，**尚未启动**）：环节二 `feat-research-platform`（Tasks 10–16，平台级增强）
  - 邻近变更（同分支、不同域）：[feat-multi-step-persist](../feat-multi-step-persist/summary.md)（chat 侧多步落库/续跑）
- **迁移版本**：`b1ea6cb32588_run_state`, `0118_research_active_run`, `0119_research_step_result`（三个均 ≤ 32 字符）
- **MEMORY**：[qa-system-schema-drift-head-parse-trap.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-drift-head-parse-trap.md)

> **交付边界**：本变更 = 计划 `docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`
> 的**环节一全部 9 个任务**。环节二（持久化任务队列 / event outbox / 三 mode 策略 /
> 证据回放 / 预算治理）**未做**，按用户指令「先完成 B（环节一），再根据实际情况开展剩余」。
> 计划是**建议**不是契约 —— Task 7/8/9 的伪代码经侦察证明系统性虚构，均按真实代码重塑，
> 重塑依据逐条记录在 `.superpowers/sdd/2026-10-12-research-session-evidence-and-platform/`。
> **已登记的遗留项见 §7/§9，记录不代表已修**。

---

## 1. 需求

**背景**：研究会话（`/research/*`）跑完即丢，用户拿不到可核对的证据。评审建议文档
（`docs/ 1012研究会话--代码分析与优化建议.md`）列出三类缺陷，本环节全部覆盖：

| # | 缺陷（用户可见现象） | 验收标准（可测量） |
|---|---|---|
| 1 | **追问后刷新，问题变回最初那句** —— 恢复态从 `input_seed` 还原，续跑改写的问题不回写 | 续跑新版本落**新** `research_run_state` 行，`current_question` 是改写后的；恢复读到的是它 |
| 2 | **同一会话能被并发触发两次**，两个状态机同时写同一份 state | 第二次触发必须被拒（`409`），且**原子认领**（无条件 UPDATE，非先查后写） |
| 3 | **报告标注「已验证」但数据不支持** —— 判据是「没报错」不是「数据支持」 | 报告 `findingsRef[].verified` 唯一来源 = `verdict == "supported"`；零支持场景必须**不**标 |

**做完是什么样（用户视角）**：研究跑到一半断线/刷新，能继续；每一步**真实执行结果**
（SQL、状态、成败、证据摘要）在报告里可查、可核对；报告不说假话。

**来源**：用户提供建议文档 → 我分析是否需要照做 → 出方案 A/B/C → 用户裁定「按方案 C 编写完整计划，
但把 B 作为单独环节一先行」→ 本变更是环节一。

## 2. 设计评审

### 2.1 关键取舍（每个都曾有 ≥2 个候选）

| 议题 | 候选 A | 候选 B（**采纳**） | 定案理由 |
|---|---|---|---|
| **问题范围如何不丢** | 续跑**原地改** `research_run_state` 行（1 turn 恒 1 行，最省） | **版本化**：续跑**新增**一行（新 UUID，`parent_run_id` 串链，`turn_id` 复制） | 原地改会**销毁历史**：报告要能说清「这一版是拿哪个问题跑的」，证据也要挂在**当时**的版本下。代价是读侧必须按 `turn_id` 跨版本聚合（正是 Task 6→8 的接缝） |
| **并发互斥怎么实现** | 先 `SELECT` 判空再 `UPDATE`（两次往返） | **原子条件 UPDATE** 认领 + 幂等键（一次往返） | A 有 TOCTOU 窗口，两个请求可同时判空。B 由 DB 保证唯一性；释放判定按**会话终态**（不是 run-id —— 版本化会换行，按 run-id 判会漏） |
| **报告里放多少证据** | 内嵌**全量原始行**，前端自己汇总 | 只放 **7 个元数据字段** + `summary` 摘要 | 全量行体积无上界（`research_report.payload` 是 JSONB）、且是 DB 已有数据的副本。取舍见 §7 MEDIUM-1 的已知例外 |
| **Task 7 的 `ExecutionContext`** | 按计划**接线**（把 metrics 灌进 SQL 生成） | **只删死参数**（α）；(β)=跳过整任务 (γ)=接线 | 侦察证明计划的 `ExecutionContext` **虚构**（读的 `business_objects`/`rules` 键不存在、签名不存在）+ 与既有载体大量重复。用户裁定 **α**：`StepQueryPlanner.plan` 的 `classes` 参数**收下从不使用**（`_plan_by_llm` 无该形参，prompt 无 schema）—— 删掉它让接口与设计一致；接线会偏离经过调优的无 schema 设计，回归风险大 |
| **Task 8/9 的范围** | 照计划伪代码实现 | **重塑**：计划里的 `ReportPlanner.render(...)`、`research_session_factory`、`scope=state["executionContext"]` 均不存在 | 侦察逐条证伪（`task-8-brief.md` / `task-9-brief.md`）。重塑后的承重价值：Task 9 的真正缺口 = 报告 `evidence` 此前**只**在 `ReportPlanner.compose` 层被断言，**从未**经 `GET /report` 端点走一遍 |

### 2.2 多视角意见

每个任务都走「独立实现 → 独立评审（`code-reviewer`，只读、钉 revision、跑前后 `git status`）→
修复轮 → 关闭」。评审**独立复现**控制器的承重断言（不用报告自证），并**变异测试**确认断言有牙：
Task 9 的评审逐一变异（加字段/删 summary/读侧 ±1 行/数值转 float/回落分支）确认每条守卫都会红。
Task 5 走了 4 轮修复（评审每轮发现新洞并独立复现）。收敛历史见 §9。

### 2.3 定案

按上述 B/α 落地；`review` 结论一律 APPROVE 或 APPROVE-WITH-FOLLOWUPS，**零 must-fix 遗留**
（Task 9 的 2 条 LOW 当场修掉 1 条、登记 1 条）。

## 3. 数据模型变更

新增 2 表 + `research_session` 加 1 列。完整字段见 [wiki/data-model.md](../../wiki/data-model.md#phase-环节一-增量研究会话证据契约表)。

| 迁移文件（**≤32 字符**已校） | 字符数 | 内容 |
|---|---|---|
| `b1ea6cb32588_run_state.py` | 25 | `research_run_state` 建表（版本化快照，自引用 `parent_run_id`） |
| `0118_research_active_run.py` | 27 | `research_session.active_run_id` + FK(`ON DELETE SET NULL`) + 索引 |
| `0119_research_step_result.py` | 28 | `research_step_result` 建表（挂 `run_state_id`，**无** `turn_id`） |

- **`research_run_state`**：`id` UUID PK、`session_id`/`turn_id` FK(CASCADE)、`parent_run_id` FK 自引用(SET NULL)、
  `current_question`、`mode`、`scope_version`、`datasource_id`、`schema_version`、`model_id`、
  `state_payload` JSONB、时间戳。索引 `session_id` / `turn_id`。
- **`research_step_result`**：`id` UUID PK、`run_state_id` FK(CASCADE)、`step_index`、`step_type`、
  `sql_text`、`raw_result_ref` JSONB（全量行 + `schemaVersion`）、`evidence_summary` JSONB、
  `status`（success/empty/failed）、`error`、`created_at`。索引 `run_state_id`。
- **`research_session` 增列**：`active_run_id` UUID FK(SET NULL)、`idempotency_key`。
- **无 CheckConstraint**；无数据回填（`active_run_id` 可空 ⇒ 存量行免迁移）。
- **文件名约束**：`变更记录强制规范` 要求迁移文件名 ≤ 32 字符（含 `.py`）。本批 `0118`/`b1ea6cb32588`
  初版 35/38 字符**超限**，2026-10-13 改短（文件内 `revision` 未动 ⇒ 对已应用的两库无影响，同 0115 先例）。
- **生产库 head 实测 `0119`**（见 §8）。

## 4. 接口契约变更

| 方法 + 路径 | 变更 |
|---|---|
| `POST /api/v1/research/sessions/{id}/abort` | **新增**。只允许 `awaiting_user` 阶段打断；其他阶段 `409`（堵住「打断正在跑的状态机」）；先 `_ownedSession` 再动 |
| `POST /api/v1/research/sessions/{id}/turns` | 语义强化：断言执行权；被占用 ⇒ `409`（幂等键命中则复用同一次运行）；终态会话不可续跑 |
| `GET /api/v1/research/sessions/{id}/report` | 响应 `payload` **新增** `evidence.stepResults[]`，元素**顶层恰 7 字段** `{index, type, sql, status, error, summary, schemaVersion}`。**不带** `scope`（计划里的虚构字段） |
| SSE `/api/v1/research/stream` | 帧内标量口径统一为 JSON 安全标量（`Decimal`→**str**、`date`/`datetime`→ISO、`UUID`→str）；非有限 float 与白名单外类型**抛错**而非静默 |
| 报告中 `findingsRef[].verified` | 判定源改为 `verdict == "supported"`（缺 `verdict` 的历史行回落旧的 `error is None`） |
| `checkpoint.options` | 新增 `stepResults[].evidence`（同一份摘要，供检查点回显） |

**状态机变化**：`active_run_id` 引入「会话级执行权」——`running` ⇒ 必持 claim；release 发生在
**会话终态**（不是 run 终态）。已知边界（遗留）：`running ⇒ 持 claim` 只在 `createTurn` 成立，
`resumeTurn` 从不 claim（见 §7/§9）。

## 5. 实现要点

**新增模块**（均为小函数拆分，单函数 < 50 行）：

| 文件 | 行数 | 职责 |
|---|---|---|
| `app/services/research_run_state.py` | 89 | 无状态构造器：建版本行、复制 `turn_id`、串 `parent_run_id` |
| `app/services/research_json_encoder.py` | 80 | SSE 唯一 JSON 生产者：`Decimal`→str、非有限 float 抛错、白名单外抛错 |
| `app/services/research_evidence_summary.py` | 688 | 结构化证据摘要。28 个小函数（列类型推断 / 时间轴 vs 度量判列 / 聚合 / 样例） |
| `app/services/research_hypothesis_test.py` | 306 | 假设判定 → `verdict`（否定式方向词门：否定字 + 情态词含 `是` · 距方向词 ≤ 4 字） |
| `app/services/research_step_result.py` | 252 | 步结果落库 + 跨版本读 + 逐行 JSON 容错 |

**改动模块**：`domain/research_models.py`（3 个模型/列）、`api/v1/research.py`（abort 端点、`_sseFrame`、
409 文案）、`research_session_service.py`（原子认领/释放）、`report_planner.py`（`_loadStepResults` seam +
`_stepEvidence`）、`research_agent_{service,stages,phases,execution}.py`（落库接线、`rebuildState` 读版本行）、
`step_query_planner.py`（删死参数 `classes`）、`infrastructure/schema_drift.py`（启动漂移门）。

**关键算法/接缝**：
- **跨版本聚合**：`loadStepResultsByTurn(session, turnId)` = `research_step_result ⋈ research_run_state
  ON run_state_id WHERE research_run_state.turn_id = :t`，排序 `(created_at, id, step_index)`。
  失败降级 `[]` + 警告（`begin_nested()` savepoint 隔离，不杀 turn）。
- **原子认领**：`UPDATE ... WHERE active_run_id IS NULL`（条件 UPDATE 一次往返，无 TOCTOU）。
- **启动漂移门**：`schema_drift._parseRevisionFiles` 用 **AST** 取模块级 `revision`/`down_revision`
  赋值（正则只在文件解析不了时回落），校验「单一 head」+ ORM↔DB 一致，不过**拒绝启动**。

**依赖注入点**：所有 research 路由 `dependencies=[Depends(getCurrentUser)]` + 每个 handler
`Depends(_requireResearchUser)` + `_ownedSession` 属主校验（非属主返 404，不泄漏存在性）。
`ReportPlanner` 可注入 `sessionService`（自加载步结果）与 `llmClientFactory`（缺省 None ⇒ 摘要降级不联网）。

## 6. 测试

TDD（先 RED 再 GREEN），真实 PostgreSQL（5434 `qa_metadata_test`）+ 完整 API 链路；
**无 sqlite 内存库、无直调 service 替代 HTTP**。

| 测试文件 | 覆盖 |
|---|---|
| `integration/research/test_run_state_recovery.py`（313） | 追问后恢复不回落（Task 1） |
| `integration/research/test_run_concurrency.py`（1036） | 并发拒绝 / 幂等键 / 认领与释放（Task 2） |
| `integration/research/test_sse_serialization.py`（211） | Decimal/date/UUID 序列化 + 非有限 float（Task 3） |
| `integration/research/test_hypothesis_verdict.py`（349） | verdict 判定 + 报告「已验证」改判（Task 4/5） |
| `integration/research/test_step_result_snapshot.py`（375） | 步结果落库（Task 6） |
| `integration/research/test_step_result_by_turn.py`（218） | **跨版本**聚合（2 版本 / 6 行，Task 8） |
| `integration/research/test_evidence_contract_e2e.py`（261） | **HTTP 端到端**：`GET /report` → `evidence.stepResults`，与**独立直查** DB 交叉校验（Task 9） |
| `unit/services/test_research_evidence_summary.py`（1184） | 摘要/判列（TP+FP 矩阵） |
| `unit/services/test_research_hypothesis_test.py`（501） | 否定门 17 TP + 30 FP + 残差标注 |
| `unit/services/test_research_json_encoder.py`（145） | 编码口径 |
| `unit/test_research_run_state.py`（95）/ `unit/test_step_result_by_turn.py`（29） | 构造器 / 降级分支 |
| `frontend/src/tests/EvidencePanel.test.tsx`（126） | 证据面板（**6 passed**） |

**覆盖率**（`--cov` 六个环节一模块，真实 PG）：**86.33%**（要求 ≥80%）。分模块：
`research_json_encoder` 100% / `research_run_state` 100% / `research_hypothesis_test` 99% /
`research_evidence_summary` 92% / `report_planner` 87% / `research_step_result` 42%
（低者因其写路径在集成用例中覆盖，单测只覆盖读/降级；整体达标）。
单次运行：`236 passed`（unit 集合）。

## 7. 安全审查

**触发**：是（改动了认证路由、SQL 执行路径、序列化边界、启动门）。

**结果**：`security-reviewer`（只读，钉 `4d6ded3`，跑前后 `git status` 一致）
→ **APPROVE-WITH-FOLLOWUPS**，**0 CRITICAL / 0 HIGH / 2 MEDIUM / 2 LOW**。

| 级别 | 位置 | 处置 |
|---|---|---|
| MEDIUM | `schema_drift.py` 正则贪心首匹配可抓 docstring 里的 `down_revision` 例子 ⇒ 对合法仓库**拒绝启动** | **本次已修**（改 AST 解析，见 §5；3 条回归测试：docstring 举例 / 行首注释 / `revision` 同险） |
| MEDIUM | `report_planner._stepEvidence` docstring 称「不含原始行」**不准确** —— `summary.sampleRows` 带 ≤5 行真实业务行 | **本次已修文档**（4 处口径统一）。**是否收紧 payload 样例行 = 产品/合规裁定，登记遗留，未擅自改行为** |
| LOW | `_sseFrame` 无守卫 ⇒ 一帧坏值（如 `bytes` 行）会截断整条 SSE 流 | **登记遗留**（落库路径已逐行容错；SSE 路径没有。建议加 per-frame `try/except` 发 `research.error`） |
| LOW | 若干新增 `except Exception` 吞异常（`_jsonSafeRowsPerRow` 静默丢行，仅 WARNING） | **登记遗留**（各点均在「不得打断 turn/report」路径上，有文档；`_jsonSafeRowsPerRow` 计数不静默） |

**复查独立验证**（非取信于报告）：认证面（router 级 + 10 个 handler + `_ownedSession`，非属主 404 不泄漏存在性）、
注入面（两条 SQL 执行路径 + 漂移门只发固定 `information_schema`/`pg_*` SELECT + 幂等键走参数绑定）、
**在真实仓上重放正则**（119 文件 → `HEADS=['0119']`，无误报）、XSS（`dangerouslySetInnerHTML` 零命中，
面板全走 JSX 文本子节点）、密钥（全 diff 扫描仅命中测试夹具的 `encrypt_api_key("test-password")`）。
未独立复现项（已知）：真实 NaN/LOB 是否真能经适配器进 SSE；报告 payload 的导出/PDF 路径是否只属主可见。

> **复查过程注记**：该子代理报告自己的读前/读后 `git status` 由 11 → 13 项，**非它所为** ——
> 是我在并行写入 wiki 文档。读只读纪律本身完好（它只用 `git show`/`/tmp` 脚本）。

## 8. 部署验证

**冒烟命令与真实输出**：`docker/docker-compose.yml` 全栈；产物
[`deploy-smoke.txt`](deploy-smoke.txt)（钉 `git HEAD 17cd1f0`，tracked 树干净）：

```
--- 1) 容器状态 ---
qa-backend	Up 14 seconds	qa-system-backend
qa-frontend	Up 19 minutes	qa-system-frontend
--- 2) 后端 openapi 可达性 ---
HTTP 200  http://localhost:8000/openapi.json
--- 3) research 路由（openapi paths） ---
/api/v1/research/sessions                    /api/v1/research/sessions/{sessionId}/abort
/api/v1/research/sessions/{sessionId}        /api/v1/research/sessions/{sessionId}/report
/api/v1/research/sessions/{sessionId}/reports/api/v1/research/sessions/{sessionId}/turns
/api/v1/research/stream                      /api/v1/research/checkpoints/{checkpointId}/answer
--- 4) 前端可达性 --- HTTP 200  http://localhost:5173/
--- 5) 生产库 alembic head --- 0119
--- 7) 启动 drift 报错 --- (空 —— drift 门通过)
```

**真实数据验证脚本**：`backend/scripts/research_evidence_realdata.py`（写库闸：只允许
`qa_metadata_test`，显式拒绝生产端口 5433；幂等：按 `input_seed` 前缀清理自己的行）。
输出 `REALDATA RESULT: PASS`（**14/14 断言**，退出码 0，stderr 空），产物
[`realdata-run.txt`](realdata-run.txt)。三条承重路径：跨版本聚合 6 行/两版本/序 `[0,1,2,0,1,2]`、
报告 `evidence` 与**独立直查** DB 逐行对齐、`summary` 数值标量端到端是字符串（`Decimal`→str）。

## 9. 关联

- 设计稿 / 计划：`docs/superpowers/plans/2026-10-12-research-session-evidence-and-platform.md`（环节一）
- 建议来源：`docs/ 1012研究会话--代码分析与优化建议.md`
- Wiki：[research_evidence_contract.md](../../wiki/research_evidence_contract.md)（**新建**）、
  [data-model.md](../../wiki/data-model.md#phase-环节一-增量研究会话证据契约表)（新增表结构）
- Rules：[变更记录强制规范.md](../../rules/变更记录强制规范.md)、[开发流程规范.md](../../rules/开发流程规范.md)、
  [测试规范.md](../../rules/测试规范.md)、[数据存储防护.md](../../rules/数据存储防护.md)
- Memory：[qa-system-schema-drift-head-parse-trap.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-schema-drift-head-parse-trap.md)（**新增**；MEMORY.md 索引已加）
- 过程证据（SDD 工作区）：`.superpowers/sdd/2026-10-12-research-session-evidence-and-platform/`（brief/recon/report/评审 + progress.md）
- 遗留项（**登记 ≠ 已修**，非阻塞）：
  1. **MEDIUM-1 收尾**：报告 payload 的 `summary.sampleRows` 是否收窄（产品/合规裁定）；
  2. **LOW**：SSE `_sseFrame` 无守卫（建议 per-frame `try/except` 发非终止 `research.error`）；
  3. **Task 8 MEDIUM-1**：面板未按版本分组，多版本时 `步骤 N` 会重复渲染且无「哪版是当前」标识；
  4. **Task 8 LOW-1**：`evidence.stepResults` 无上限（O(续跑次数×步数)）；
  5. **Task 5 Important-1**：程度副词桥接（部分修复，FP 安全信号已验证）；
  6. **Task 4 F4/F5/F6**、**Task 6 Minor-1/2**、**Task 9 LOW-2**（跨版本牙齿缺口，已在 docstring 注明）；
  7. `extract_global_filters.classes` 接线候选；`resumeTurn` 不 claim 执行权（同会话两状态机的路径）；
  8. 已知既有红：`TestColumnAndIndexDriftSubprocess::test_healthy_db_has_no_column_or_index_drift`
     —— **测试库**（5434）有 `knowledge_claim.confidence_level` 列而 ORM 未声明。经 HEAD 基线 worktree 复现
     确认**与本变更无关**（生产库不受影响，生产 drift 门通过）。

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（5 个议题，每议题 ≥2 候选 + 定案理由）
- [x] 第 3 段迁移文件名 ≤ 32 字符（35/38 超限的两个已改短至 27/25）
- [x] 第 7 段触发了 security-reviewer，CRITICAL/HIGH/MEDIUM/LOW 已列（0/0/2/2）+ 逐条处置
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出（`deploy-smoke.txt`，钉 HEAD）
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Wiki ×2 / Rules ×4 / Memory / 计划 / 工作区）
- [x] 相关 wiki 文档已更新（`wiki/research_evidence_contract.md` **新建** + `wiki/data-model.md` 新章节）
- [x] 至少 1 条 MEMORY 索引已在 MEMORY.md 添加（`qa-system-schema-drift-head-parse-trap`）
- [x] 涉及真实 SQL/DB 改动 ⇒ `scripts/research_evidence_realdata.py` 已跑通（14/14 PASS）
