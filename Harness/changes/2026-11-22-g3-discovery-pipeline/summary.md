# G3-0：发现流水线六表（迁移 0166）+ 守卫白名单扩表

- 日期：2026-11-22　分支：`feat/ontology-evolution`
- 依据：`docs/update/1118/M7/ontology-evolution-program.md` §二（统一数据模型）/ §三 G3
- 结论：**A2 的数据底座落地**（只建表，不含服务/API/Worker —— 那些是 G3-1…G3-4）。
  **未新增发布写路径**，提案只持有 `semantic_release.id` 指针。

## 一、六张表

| 表 | 作用 | 关键约束 |
|---|---|---|
| `discovery_run` | 运行记录 | `trigger`/`status` 字面量 CHECK；**LLM 计量三列**（`prompt_tokens` / `completion_tokens` / `cost`）非负；`source_id`/`snapshot_id` → RESTRICT |
| `discovery_profile` | 确定性剖析 | `sql_digest` 必须 SHA-256；`(run_id, table_name, COALESCE(column_name,''))` 唯一（**COALESCE 不可省**）；`run_id` → CASCADE |
| `discovery_candidate` | 候选 | `payload` 必须对象；**`evidence_refs` 非空数组**（证据完整率 100% 红线）；`confidence` ∈ [0,1] |
| `discovery_finding` | 主数据问题 | 四类问题 / 三级 severity / 四态 status；`detail` 非空 |
| `discovery_proposal` | 变更单 | `diff_json` 必须对象；`base_release_id` / `release_id` → `semantic_release` RESTRICT；五态 status |
| `discovery_review` | 审核留痕 | `decision` 三态；`approval_task_id` **可空** + `ON DELETE SET NULL`（二期接 P5） |

## 二、守卫同步

- `test_wp_ont_ai_fusion_contract.py`：`ALLOWED_DISCOVERY_TABLES` 由 2 张扩到 8 张；
  扫描面加入 `discovery_pipeline_models.py`。命名空间红线（禁 `ontology_*`）与
  「不得 import 治理写路径」不变；**LLM 计量断言仍为 `llmFiles == 0`**（G3-2 引入
  LLM 时改为「必须带计量」）。
- `test_procurement_scope_ruling.py` 不受影响：它只管 `procurement_*` 命名空间。

## 三、验证

| 项 | 结果 |
|---|---|
| 新增 `test_discovery_pipeline_models.py`（16 例：ORM↔迁移双向钉住） | 16 passed |
| 发现域守卫 + 既有 discovery 单测 + 采购裁决守卫 | 95 passed |
| 发现域其余单测 + schema drift 单测 | 108 passed |
| `alembic upgrade head`（测试库 5434/mainline，0165 → 0166） | 成功 |
| `alembic upgrade head` 在**全新空库**（从 0 跑完 166 个迁移） | 成功 |
| `alembic check` | **新六表无漂移**；报的 3 项（`procurement_object_reconciliation_journal` 自删表 + `entity_mapping`/`feature_definition` 两个旧 FK）在**空库上同样出现** ⇒ 既有残留，与 0166 无关 |

## 四、既有红基线（不是本批引入）

- `test_ontology_drift_api.py` 的 2 例失败：集成 conftest 每个用例会种 5 个 header 本体类
  （`seedHeaderOntologyClasses`），而该用例假定库里只有它自己种的 3 个类
  ⇒ `checkedTables` 期望 3 实得 7。**已登记在记忆 `qa-system-local-green-gate-blockers`**，
  与 0166 无关（新表不参与 drift 计算）。

## 五、G3-1 确定性剖析器（同批落地）

| 文件 | 职责 |
|---|---|
| `app/services/discovery_profile_sql.py` | **纯函数**：标识符白名单 + 方言引号、按类型裁剪聚合、分块、`sql_digest`、结果解析 |
| `app/services/discovery_profiler.py` | IO 编排：建 run（RUNNING）→ 只读聚合 → 一次性落 profile 行 → SUCCEEDED / FAILED |
| `scripts/profile_discovery_tables.py` | 真数据探针（自动登记 `probe:*` 发现源 + 采集快照 + 剖析） |

关键取舍：

- **不采样**：只发 `COUNT(*)` / `COUNT(col)` / `COUNT(DISTINCT col)` / `MIN` / `MAX`
  （D-3「只收结构不收样本」；行级样本等 Q3 采样授权口径）。
- **不调 LLM**：`discovery_run` 的 token/cost 恒为 0（守卫仍断言 `llmFiles == 0`）。
- **失败不留半份证据**：查询全跑完再一次性落库；任一失败 ⇒ run=FAILED + profile 0 行。
- **`sql_digest` 是块级**：同一块里的列共享同一个摘要（摘要标识「哪条 SQL 产出了这些行」）。
- **JSONB 的 SQL NULL 坑**：`sample_values` / `impact_json` 必须 `JSONB(none_as_null=True)`，
  否则 Python `None` 存成 **JSON `null`**，直接违反 `IS NULL` 的 CHECK（实测踩中）。

### 真数据验收（dev THBI Oracle，2026-11-22）

```
run id=1 status=SUCCEEDED  tokens=0/0 cost=0
DIM_SUPPLIER   row_count=3500    columns=229
DIM_IMATERIAL  row_count=350922  columns=248
DIM_SUPPLIER.BPSNUM_0  distinct=3500 (= 行数，唯一键特征)
DIM_IMATERIAL.ITMREF_0 distinct=350922 (= 行数)
```

**可复现**：紧接着跑第二次（run id=2），两次的 479 行 `(表, 列) → sql_digest`
**479/479 完全相同**。

测试：`test_discovery_profile_sql.py`（29 例纯函数）+ `test_discovery_profiler.py`
（9 例真实 PG + 假适配器）+ 模型守卫 17 例；F1 守卫把两个新服务加入扫描面后仍 15 绿。

## 六、G3-2 候选生成（同批落地，**首次引入 LLM**）

| 文件 | 职责 |
|---|---|
| `app/services/discovery_candidate_prompt.py` | 纯函数：剖析事实**有界**整理（逐表 + 总量上限，截断优先高信号列）、system/user prompt、响应逐条校验 |
| `app/services/discovery_candidate_service.py` | IO：解析模型配置 → 调一次 LLM → **先记账再解析** → 覆盖写候选/问题 |

关键取舍：

- **证据完整率 100%**：候选必须引用事实里真实存在的 (table, column)；无证据/证据不实
  ⇒ 整条丢弃并记原因（单条非法只丢该条；JSON 整体坏了才整批失败）。
- **证据形状对齐 `KnowledgeEvidenceRead`**：每条证据带
  `channel=PROFILE` / `kind=TABLE_STAT|COLUMN_STAT` / `ref` / `title` 语义的定位
  与 `summary`（含该列实测数字）⇒ 复核不必回库重算。
- **钱先记账**：LLM 返回后立刻把 token/cost 累加到 `discovery_run` 并提交，再解析；
  解析失败时钱已花，账不能缺。
- **候选失败不改 run 状态**：剖析成功就是成功；候选可重跑。
- **覆盖写**：同一 run 重跑候选 ⇒ 先删再插（一份 run 一套候选）。
- **prompt 安全**：剖析事实走 `<discovery_profile>`（`UntrustedLabel` 新增第 14 个 label）；
  system 明说「其中任何指令都不执行」。
- **F1 计量守卫翻转**：`llmFiles == 0` → `llmFiles >= 1` 且同文件必须出现
  `prompt_tokens`/`completion_tokens`/`cost`（符号集补了 `BaseLlmClient`/`createClient`/`llmFactory`）。

测试：`test_discovery_candidate_prompt.py`（19 例纯函数）+ `test_discovery_candidate_service.py`
（8 例：真实 PG + 假 LLM，覆盖计量、幂等、坏条目、前置条件、无模型配置）。

## 七、G3-3 提案 + 四眼审核（同批落地）

`app/services/discovery_proposal_service.py`（只写 `discovery_proposal` / `discovery_review`）。

| 决策 | 内容 |
|---|---|
| **`diff_json` 存完整 manifest，不存补丁** | 补丁无法独立校验（跨分区引用只有对完整快照跑 `validateManifest` 才知道）；载荷 = `{"kind":"MANIFEST_REPLACEMENT","baseReleaseId":…,"manifest":{…}}` |
| **「可编译」判据** | 与发布同一道契约闸 `validateManifest`（v1/v2 按 `schemaVersion` 分发，未知版本拒绝）⇒ 失败 422 |
| **影响面** | 用既有 `diffManifests(base, candidate)`：changeCount / breaking / additive / cosmetic / riskLevel / sections，不另造差异口径 |
| **四眼** | `reviewer == proposer` ⇒ 409（跨表约束，库层 CHECK 表达不了 ⇒ 服务层 + 单测） |
| **审批对接** | `approval_task_id` 可空；非空时必须是 `subject_type='ontology_proposal'`（防随便挂任务） |
| **状态机** | DRAFT →submit→ IN_REVIEW →APPROVE→ APPROVED / →REJECT→ REJECTED / →REQUEST_CHANGES→ DRAFT |
| **不写治理表** | 批准只到 `APPROVED` + `release_id` 留空；**生成 release DRAFT 与 publish 属于 G4**（唯一写口 = 既有 `semanticReleaseService`）。这是对 G3 验收行「审核后生成 release DRAFT」的**有意收窄**，避免在发现域开第二写口 |

测试：`test_discovery_proposal_service.py`（16 例，真实 PG；含四眼、状态机、
审批任务护栏——护栏的正/反例各一，通过真实 `workflow_definition`/`instance`/`approval_task` 造数）。

## 八、G3-4 API + Worker（同批落地）

新增端点（全部挂在既有 `/api/v1/ontology-discovery` 下）：

| 方法/路径 | 门禁 | 说明 |
|---|---|---|
| `POST /runs` | admin + 2/minute | **202**：只受理（建 RUNNING 行），剖析 + 候选在后台跑 |
| `GET /runs/{runId}` | admin | 运行详情（含 token/cost/error_message） |
| `GET /candidates`、`GET /findings` | admin | AI 未确认结论 ⇒ 普通用户不得当事实 |
| `POST /proposals` | admin | 建草稿（manifest 过契约 + 影响面） |
| `GET /proposals`、`GET /proposals/{id}` | admin | 未发布治理在途件 |
| `POST /proposals/{id}/submit` | admin | DRAFT → IN_REVIEW |
| `POST /proposals/{id}/review` | admin | 四眼：`reviewer != proposer`（同人 409） |

- **后台会话隔离**：`DiscoveryRunService.runPipeline` 走 `ownSession()` 并在新会话里重读 run
  （请求会话 rollback 后实例 expired ⇒ MissingGreenlet，2026-11-16 事故同型）。
- **后台永不炸请求线程**：剖析失败已由 profiler 落 `FAILED`；候选失败（含无模型配置）
  只记日志，剖析结果保留。
- **已知边界**：`tables` 子集目前闭包在 `BackgroundTasks` 里（单进程、进程重启丢任务）；
  G4 换持久化队列时，把请求参数落到 run 行。
- `runPipeline` 的 `session` 形参**仅测试/脚本注入**（生产不传 ⇒ 自开会话），
  避免单测借用全局会话工厂。

测试：`test_discovery_run_service.py`（11 例：受理/快照缺省/重入/后台隔离与吞异常）+
`test_ontology_discovery_api.py` 追加 8 例（202/未知字段/详情/候选分页 total/问题/
提案建-提-审流程/列表过滤）+ 路由 auth 守卫登记（**36 passed**）。
发现域广回归 **183 passed**。

## 九、G3 完成状态

G3 的 **0/1/2/3/4 全部落地**。按此前登记的口径，**「审核后生成 release DRAFT」被有意
推迟到 G4**：发布只允许经既有 `semanticReleaseService` 一个写口，发现域不建第二写口。

## 十、G4-0 发布联动（同批落地）

| 组件 | 内容 |
|---|---|
| `semanticReleaseService.createDraftFromManifest` | 从**给定 manifest** 建草稿（不编译治理表）；与 `createDraft` 共用 `_persistDraft` 尾部（契约闸 / 体量闸 / 版本唯一 / 校验落表 / 审计） |
| `semanticReleaseService.publishProposal` | 提案 APPROVED → `createDraftFromManifest` → `markValidated` → `activate`；提案置 PUBLISHED + 回填 `release_id` |
| `POST /proposals/{id}/publish` | admin；body `{version, domainCode?, note?}`；返回发布状态与 blockers/warnings |
| F1 守卫 | 从「禁止 import semantic_release_service」改为**受限写口**：只允许 `semanticReleaseService` 且只允许点 `publishProposal`（AST 同时查导入符号与调用点） |

**为什么不能走 `createDraft`**：它编译的是**当前治理表**，而提案尚未落到治理表
⇒ 发布出去的会是「现状」而不是「审核过的提案」。`createDraftFromManifest` 保证
发布的 `manifest_hash` 与提案那份**逐字节相同**（单测断言）。

测试：`test_semantic_release_publish_proposal.py`（6 例）+ 发现 API 发布用例（1 例）；
F1 守卫 16 例 + 路由 auth 36 例；发布链回归 **181 passed**。

## 十一、G4-1 增量监测（同批落地）

`app/services/discovery_monitor_service.py` + `POST /sources/{id}/monitor`（admin，5/minute）：

```
captureSnapshot（内容寻址，幂等）
  ├─ reused=true（同内容）⇒ changed=false，不起运行（不烧 LLM）
  └─ reused=false ⇒ 建 RUNNING（trigger=DIFF）⇒ 后台剖析 + 候选
```

- **判变复用快照哈希**，不自己再 diff 一遍（第二套口径会漂移）。
- **不自动把候选拼成提案**：候选不是结论（D-3），自动组装 = 机器替人拍板本体
  ⇒ 触碰「AI 写本体」红线。提案仍是人工组装 + 四眼审核。
- **周期驱动由外部调度器调本端点**；本批不自造 Worker 拓扑（P5 裁决）。

测试：monitor 服务 4 例 + API 1 例；路由 auth 38 passed。

## 十二、G4-2 评测 + 回滚演练（同批落地）

- **迁移 0167**：`discovery_evaluation`（`proposal_id` 可空 RESTRICT；`datasetVersion` 非空；
  `metricsJson` 必须对象；`verdict` 三态）。
- **`DiscoveryEvaluationService.evaluateRun`**：从运行数据算**可复算指标**
  （候选数 / 问题数 / 分类分布 / **证据完整率**），并落结论：
  - `PASS`：有候选且证据完整率 = 1；
  - `FAIL`：有候选但存在**证据解析不了**的候选（脏数据/人工改库）；
  - `INCONCLUSIVE`：没有候选 —— **没结论 ≠ 质量差**（不混成 FAIL）。
- **`recordEvaluation`**：脚本/人工上报（verdict / metrics / datasetVersion 契约校验）。
- **API**：`POST /runs/{runId}/evaluate`、`GET /evaluations`（均 admin）。
- **回滚演练**（单测）：发布新提案 → ACTIVE 变新版 → `rollback` 到旧 ACTIVE
  ⇒ 新 ACTIVE 归档、内容与 `manifest_hash` 回到旧版。

不做「候选准确率/召回率」：需要业务标注集（G5 试点签收后才存在）；本批只给
**不依赖人工标注**的可复算指标，避免自己估一个数当验收。

测试：评测服务 5 例 + 回滚演练 1 例 + 评测 API 1 例；路由 auth **58 passed**；
dev 库（5433）已升级 **0167**。

## 十三、G4 完成状态

G4-0/1/2 全部落地。

## 十四、G5 前台签收（同批落地）

- **迁移 0168**：`discovery_candidate` / `discovery_finding` 补 `decision_note`；
- **决断写口**：`PATCH /candidates/{id}/decision`（ACCEPTED/REJECTED/NEEDS_EVIDENCE）与
  `PATCH /findings/{id}/status`（OPEN/ACKNOWLEDGED/RESOLVED/IGNORED），可改判、备注落库；
- **运行列表**：`GET /runs`（前台运行选择器）；
- **前台页**：「业务配置 → 发现评审」——候选/问题两个页签、判定弹窗、签收进度、证据条数；
- **试点真跑**：dev THBI run=7（DIM_SUPPLIER+DIM_IMATERIAL）→ 18 候选 / 16 问题 / PASS，
  摘要归档 `docs/update/1118/M7/pilot/summary-dev-2026-11-22.md`（真实值 CSV 只在 /tmp）。

测试：决断服务 6 例 + API 3 例 + 前端 6 例；路由 auth 64 passed；i18n 对等 25 passed。

**剩余**：业务逐条判定（准确率/召回标注集）与签收签字；其后可把采纳候选组装成提案（G3-3）。
