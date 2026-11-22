# 整合方案：AI 驱动本体发现与持续演进（含采购域首期实践）

> 2026-11-22 ｜ 整合两份设计的**剩余工作**：
> ① `AI驱动本体发现与持续演进_功能设计_V1.0.md`（WP-ONT-AI，**A2/A3 未做**）
> ② `采购本体模型与语义编译器详细设计_V0.1.docx`（**约 80%，缺发布管理页 + 6 个校验码**）
>
> 结论：**能整合，而且必须整合**——两者的缺口恰好互为对方的前提（见 §二）。

## 一、为什么不是"拼接"，而是"互补"

| 编译器设计的缺口 | 恰好是发现引擎的哪一环 |
|---|---|
| 缺 6 个校验码（CLS/REL/AGT/GRF/MET-002） | 候选**提案要能被确定性校验**才能进审核 → 校验码是提案的守门员 |
| 缺发布管理前端页 | 提案**审核通过的出口**就是发布页（激活/回滚） |
| 缺 §2.2 核心类逐条核对 | 候选**产出的目标清单**就是那份"首期核心类" |

反过来：发现引擎的 A2/A3 若单独做，会**重复实现**校验与发布（第三套审批、第二套发布）——
这正是 D1「不复制造」要防的。

```
采集(A1✅) ──► 剖析/候选(A2) ──► 提案(A2) ──► 校验(补6码) ──► 审核(轻量) ──► 发布(现有链+前端页) ──► 演进监测(A3)
                                    ▲                                                        │
                                    └──────────────── 上下游回归/评测 ◄────────────────────────┘
```

## 二、统一数据模型（一张表说清"建什么、不建什么"）

| 表 | 状态 | 说明 |
|---|---|---|
| `discovery_source` | ✅ 已建(0146) | 授权数据源目录，引用既有 `data_source.id`，**不新造连接** |
| `metadata_snapshot` | ✅ 已建(0146/0147，不可变) | schema 结构快照，内容寻址 |
| `discovery_profile` | 🆕 | 剖析结果（唯一性/空值/值域/关联覆盖率），**可复现 SQL 摘要** |
| `discovery_candidate` | 🆕 | 候选（对象/属性/关系/分类/指标），`evidence_refs` 复用 **KnowledgeEvidence** 契约，带 `model_version` / `prompt_version` |
| `discovery_finding` | 🆕 | 主数据问题（重复/冲突/缺失/映射），`severity` + `status` |
| `discovery_proposal` | 🆕 | 变更单：`base_release_id`（钉住基线）、`diff_json`（**必须可编译成 manifest v2**）、`impact_json` |
| `discovery_review` | 🆕 | 审核留痕（四眼），**可选 `approval_task_id`**（见 §四决策） |
| `discovery_run` | 🆕 | 运行记录：`trigger`、`snapshot_ids`、`prompt_version`、**`prompt_tokens`/`completion_tokens`/`cost`** |
| `discovery_evaluation` | 🆕 | 评测结果：`dataset_version` + `metrics_json` + `verdict` |
| ~~`procurement_ontology_release`~~ | ⤳ 不建 | 复用 `semantic_release` |
| ~~`procurement_key_binding`~~ | ⤳ 不建(D2) | 统一键走 `entity_mapping` |
| ~~`procurement_agent_run_context`~~ | ⤳ 不建(D2) | 复用 run 三列 |

命名铁律（**已有守卫**）：一律 `discovery_*`，**禁止 `ontology_*` 前缀**（= "AI 写本体"）。

## 三、分阶段 WBS（5 个阶段，合计约 26–34 人日）

| 阶段 | 内容 | 人日 | 验收判据 |
|---|---|---|---|
| **G1 发布管理前端页** ✅ | 发布列表 / 版本详情 / 校验结果（severity×result）/ 激活 / 回滚；绑定页已有可复用组件 | **2–3** | ✅ `7a421a3f`：管理员可在浏览器完成「发布→激活→回滚」全流程，无需脚本 |
| **G2 补 6 个校验码** ✅ | CLS-001（类名唯一/无环）、CLS-002（business_object 头类存在 + 主域启用）、REL-001（关系两端类存在 + 白名单）、AGT-001（Agent 域/层/工具一致）、GRF-001（图 label 白名单 + 主键可定位）、MET-002（KPI 绑定有效数据源）；**全部读现表即可判**，不需新表 | **3–4** | ✅ 每个码有单测（正/反例，`test_semantic_release_design_checks.py`）；集成用例证明真实发布上产出对应 check 行；`MAX_CHECKS_PER_RELEASE` 同步从契约重推导 |
| **G3 A2：剖析 + 候选 + 提案 + 审核** | `discovery_profile/candidate/finding/proposal/review` 五表 + `discovery_run`；`POST /runs`（异步 Worker）+ `/candidates` + `/findings` + `/proposals` + `/review`；候选证据复用 Evidence 契约；**提案 diff 必须能过 `compileManifest`** | **10–12** | 采购域两张表（供应商、物料）跑通：剖析→候选→提案→审核→生成 release DRAFT |
| ↳ **G3-0 数据底座** ✅ | 迁移 **0166** 六表 + 守卫白名单扩到 8 张（`2026-11-22-g3-discovery-pipeline`） | 1 | `alembic upgrade head`（含空库从 0 全跑）成功；`alembic check` 新六表无漂移；ORM↔迁移 16 例双向钉住 |
| ↳ **G3-1 剖析器** ✅ | 确定性 SQL 剖析（唯一性/空值/值域）→ `discovery_profile`，**不调 LLM** | 2–3 | ✅ dev THBI：`DIM_SUPPLIER` 3,500 行/229 列、`DIM_IMATERIAL` 350,922 行/248 列；两次 run 的 **479/479** 个 `sql_digest` 完全一致 |
| ↳ **G3-2 候选生成** ✅ | **首次引入 LLM**：剖析事实 → 候选 / 主数据问题；证据完整率 100%；token/cost 写回 `discovery_run`；F1 计量守卫翻转为「必须带计量」 | 3–4 | 单测 19（prompt 构造与解析）+ 服务 8（真实 PG + 假 LLM）；真数据跑等 G5（需可用模型配置） |
| ↳ **G3-3 提案 + 审核** ✅ | `diff_json` = 「完整 manifest + 基线」（补丁无法独立校验）；入库前过 `validateManifest`；影响面用既有 `diffManifests`；四眼（reviewer≠proposer ⇒ 409）；`approval_task_id` 非空须指向 `ontology_proposal` | 3 | 单测 16（真实 PG）；**批准只到 APPROVED，不建 release**（发布留 G4 唯一写口） |
| ↳ **G3-4 API + Worker** ✅ | `POST /runs`（202 + BackgroundTasks，2/minute）/ `GET /runs/{id}` / `/candidates` / `/findings` / `/proposals`（建/列/详情/提交/审核）；新读面一律 admin（AI 未确认结论 + 在途治理件） | 2–3 | 路由 auth 守卫登记 9 条新路由（36 passed）；API 层 8 例；后台用 `ownSession()` 自开会话 |
| **G4 A3：增量监测 + 发布联动 + 评测** | `/diff` 周期驱动提案；`POST /proposals/{id}/publish` 调**既有** `activate`；`discovery_evaluation` + 上下游回归（Graph/RAG/NL2SQL/Agent 各 1 条冒烟）；回滚演练 | **7–9** | 端到端演练通过：发现→提案→审核→发布→**回滚**；误合并率 0 |
| ↳ **G4-0 发布联动** ✅ | `semanticReleaseService.publishProposal`（唯一写口：`createDraftFromManifest` → `markValidated` → `activate`）；提案置 PUBLISHED + 回填 `release_id`；`POST /proposals/{id}/publish` admin | 2 | 单测 6（发布链/状态门/防重/篡改/缺 manifest）+ API 1；F1 守卫改为「只允许 `semanticReleaseService.publishProposal`」 |
| ↳ **G4-1 增量监测** ✅ | `POST /sources/{id}/monitor`（admin，5/minute）：采集快照，内容没变**不起运行**；变了起 `trigger=DIFF` 的运行（剖析+候选）；**不自动拼提案**（候选不是结论，AI 不替人拍板本体） | 2 | 单测 4 + API 1；路由 auth 38 passed |
| ↳ **G4-2 评测 + 回滚演练** ✅ | 迁移 **0167** `discovery_evaluation`；`evaluateRun` 可复算指标（证据完整率 → PASS/FAIL/**INCONCLUSIVE** 三态）；`POST /runs/{id}/evaluate` + `GET /evaluations`；**发布→回滚演练**（发布新提案后回滚到旧 ACTIVE，内容与哈希回到旧版） | 2–4 | 评测服务 5 例 + 回滚演练 1 例；路由 auth 58 passed；dev 库已升 0167 |
| **G5 采购域首期实践** | 供应商 + 物料两对象：真实数据剖析 → 产出候选与治理建议；业务确认与签收；评测基线（准确率/召回/人工耗时） | **4–6** | 业务签收的候选清单 + 误报复核记录 + 评测报告 |

**顺序理由**：G1/G2 是"出口与守门员"，先做完，G3 产出的提案才有地方去、才拦得住；
G5 放在最后是因为它需要前面全部就绪（这也是 WP §8 的 A→B→C 顺序）。

## 四、两个必须先拍板的决策

### 决策 1：审核机制（G3 前必须定）

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 复用 P5 `approval_task` | 与 I4 审批统一 | **强依赖 `instance_id`/`step_id`**（必须挂在 workflow 实例上），而仓里只有 **1 个** workflow definition ⇒ 每个提案都要建实例，成本高 | ❌ 首期不用 |
| 轻量 `discovery_review` + 可选 `approval_task_id` | 轻、可控四眼、后续可接 | 多一张表 | ✅ **建议** |

护栏：`discovery_review` 的 `approval_task_id` 外键**允许为空**，二期接入 P5 时回填即可；
非空时必须指向 `approval_task` 中 `subject_type='ontology_proposal'` 的行（守卫测试）。

### 决策 2：`access_policy` 的语义（G3 前必须定）

现在 `discovery_source.access_policy` 只是**文本**，未强制。必须落到既有 ACL：

- 取值限定为：`datasource_id` 的可见范围（谁能读这个源）+ **列级脱敏清单**（哪些列不出库）；
- 采集/剖析只走 `business_db_pool` 只读通道（已有 SQL Guard）；
- 候选里出现敏感列时，`evidence_refs` 只存**脱敏摘要**，不存原值。

## 五、契约与护栏（复用 F1 成果 + 新增两条）

已落地（`5a84ebae`，15 例守卫）：表命名空间、禁治理写路径、LLM 计量、状态映射。

新增：

| 契约 | 护栏形式 |
|---|---|
| 提案 diff 必须可编译 | `discovery_proposal.diff_json` 入库前跑 `compileManifest`，失败 ⇒ 422；守卫：提案服务必须 import 编译器 |
| `publish` 必须走既有 activate | 守卫：`/proposals/{id}/publish` 的实现只能调 `semantic_release_service` 的 activate（**唯一**写口），不得自己写 `semantic_release` 行 |
| 候选不得写正式本体 | 守卫（已落地）：表名前缀 + 禁 import 写路径 |
| 审核四眼 | `discovery_review.reviewer != proposal.proposer`（同人自审 409） |

## 六、评测与验收指标（不在本方案里虚构阈值）

| 指标 | 采集方式 | 阈值 |
|---|---|---|
| 候选准确率 / 召回率 | G5 的样本标注集（业务签收） | 试点基线后签署 |
| 证据完整率 | 每条候选 `evidence_refs` 非空且可解析 | 100%（硬性） |
| **误合并率** | 供应商/物料自动合并数 | **必须 0**（硬性红线） |
| 变更发布成功率 / 回滚成功率 | 端到端演练 | 100%（演练口径） |
| 下游回归 | Graph/RAG/NL2SQL/Agent 各 1 条冒烟 | 全绿 |
| LLM 成本 | `discovery_run.cost` 合计 | 试点预算内（业务定） |

## 七、与现有工作的关系（避免并行冲突）

- **已在 main**：语义发布链、绑定、Registry v2、6 个场景 Agent、A1 发现底座、F1 契约；
- **在 PR #176（待合）**：文档 AI 上下文 P0–P3、F1 守卫；
- **本方案全部为新分支**（建议 `feat/ontology-evolution`），基于合并后的 main，**不碰** #176 的文件（除 `discovery_*` 新增）。

## 八、风险与开放问题

| 风险 | 缓解 |
|---|---|
| LLM 成本失控（剖析 × 候选批量调用） | `discovery_run` 计量 + 预算上限 + 采样限额；先只跑 2 个对象 |
| 数据授权未到位 | G3 只接 dev 的 THBI Oracle（已有只读账号）；生产另走授权 |
| "AI 写本体"红线被绕过 | F1 守卫（表名/写路径）+ 发布唯一入口 activate |
| 验收被稀释 | 本方案作为 **I-1 独立工作包**，不计入 V0.3 首期 94% 的口径 |
| 候选噪声伤害信任 | 首期只做「候选 + 建议」，不做自动合并；低置信进 `NEEDS_EVIDENCE` |

## 九、需要你拍板的三件事

1. **审核用轻量 `discovery_review`**（建议）还是接 P5 `approval_task`？
2. **首期对象**：确认「供应商 + 物料」两个（WP §9 的建议）？
3. **是否把本方案登记为 I-1**（独立工作包，不改变 V0.3 首期验收口径）？
