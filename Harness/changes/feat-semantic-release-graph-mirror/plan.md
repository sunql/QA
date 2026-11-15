# P3b Neo4j 发布镜像 —— 执行计划（续接 1109 日级排程 D22–D25）

> 本文档只管 P3b（D22–D25）。P3c–P6 另有计划文档，不在本文件内。
> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §7。

| 项 | 值 |
|---|---|
| **变更** | P3b Neo4j 发布镜像（SemanticRelease → 图谱快照） |
| **分支** | `feat/semantic-release-graph-mirror`（worktree `/private/tmp/qa-graph-mirror`），base = `origin/main` `b6af98a1` |
| **迁移版本** | **无**（零 DDL：不改 PG 表结构、不加列、不改索引） |
| **关联变更** | [feat-semantic-registry-rag](../feat-semantic-registry-rag/summary.md)（P3a RAG 过滤）· [P1a/P1b 语义发布](../feat-semantic-release-a/summary.md) |
| **状态** | D22 完成（设计 + 实现 + 评审整改 + 真实数据实证；随 PR 合并，**不部署**）；**D23 完成**（物化 Job：幂等 / dry-run / 分批 / 断点续跑 / 状态台账 + 真实数据实证；**不部署**）；**D24 完成**（对账脚本：计划 vs 图逐条求差 + 退出码 0/1/2/3 + 真实数据实证含自愈闭环；**不部署**）；**D25 完成**（「按发布读图」开关 + 一键回退演练实证；**不部署**）⇒ **P3b 全部完成** |

---

## 1. 背景（为什么做）

P1a/P1b 把「语义发布」做成了**自包含快照**（`semantic_release.manifest_json`），
P2 把 NL2SQL 的 schema 渲染接到发布快照上（钉住某一版发布 ⇒ 用那一版的语义）。
但**图谱这一侧完全没有任何发布维度**：

- 节点写路径全是 `MERGE (c:Class {unified_id})` ⇒ **同一个节点被所有发布共享**，
  属性原地覆盖 ⇒ 「A 版发布时的类 X 长什么样」在图里**查不到**；
- 边 `MERGE (a)-[:REL]->(b)` **无属性、无键** ⇒ 边也没有版本；
- 消费方 `MATCH (c:Class)` / `getClassIds()` 等一律**不带发布维度**。

⇒ 结果：图谱只能回答「**现在**的图是什么」，回答不了「**某一版发布**的图是什么」。
发布要回放、要对比、要钉住，图谱这一路**对不上**。

本批交付：**图谱的发布镜像**（每个已发布版本一份**不可变**的图谱快照），
让「按发布版本读图」成为可能，并为后续把图谱接进 NL2SQL/RAG 消费面（P3c/P3d）铺路。

---

## 2. 日次任务卡

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| D22 | 发布镜像**方案设计** + 测试环境实现 | `daily/D22.md`、`app/domain/semantic_graph_mirror_spec.py`（契约）、`app/domain/id_mapping.py`（业务对象字面量 SSOT）、`app/services/semantic_graph_mirror.py`（纯计划构建）、`app/services/semantic_graph_mirror_resolution.py`（PG 解析）、`app/infrastructure/neo4j_mirror_client.py`（镜像写函数，**独立模块**：`neo4j_client.py` 已 1024 行超 800 行上限）、`app/infrastructure/neo4j_write_target_guard.py`（**库层写目标闸门**） | 方案对比留档；镜像节点走**独立 label**（主图零影响）；镜像内容 = manifest 实体/关系（**含 `REFERENCES`**，与主图本体子图同构）；纯逻辑有单测；真实写/回读/幂等/主图零影响/生产拒写/双保险清理有实证 |
| D23 | 物化 Job：幂等 / dry-run / 分批 / 断点续跑 | `scripts/semantic_graph_sync.py` + `app/services/semantic_graph_mirror_job.py`（编排层，IO-free）+ `app/services/semantic_graph_mirror_ledger.py`（台账行转换） | 重复执行结果一致；dry-run 不写图；按 label 分批、每批有耗时与条数记录；失败可续跑 —— **已完成**（详见 `daily/D23.md`） |
| D24 | 一致性对账：镜像 vs Manifest 实体/关系 | `scripts/verify_graph_mirror.py` + `app/services/semantic_graph_mirror_verify.py`（纯对账） | 实体 100% 对账；关系逐条解释（manifest 无 relations 分区 ⇒ 语义关系边**不在对账范围**）—— **已完成**（详见 `daily/D24.md`） |
| D25 | 切换演练 + 回归 + PR | `app/services/semantic_graph_reader.py`（路由器）+ 演练报告（D24+D25 同 PR，D25 产出本来就是 PR） | 「按发布读图」开关打开后读到的是镜像；一键回退到主图 —— **已完成**（详见 `daily/D25.md`） |

---

## 3. 关键约束（recon 事实，2026-11-15 实测/实读）

| # | 事实 | 出处 | 对设计的影响 |
|---|---|---|---|
| C-1 | Neo4j 是 **5.23-community** | `docker/docker-compose.yml` | Community **不支持多 database** ⇒ 「独立镜像库」方案不可行 |
| C-2 | 节点写全是 `MERGE (x:Label {unified_id})` | `app/infrastructure/neo4j_client.py` | 同一节点被所有发布共享 ⇒ 发布维度**不能**落在主节点上（会互相覆盖） |
| C-3 | 边 `MERGE (a)-[:REL]->(b)`，无属性、无键 | 同上 | 边级发布维度要么 delete-then-create，要么**把作用域放到端点上**（本批采纳后者） |
| C-4 | manifest 只有 6 个分区（domains/classes/joins/metrics/kpis/businessObjects），**无 relations 分区** | `app/domain/semantic_manifest_schema.py`、`app/tests/unit/test_semantic_manifest_golden.py` | 语义关系边（`ontology_relation`）**无法从快照自包含重建** ⇒ 镜像不含该类边（显式声明范围） |
| C-5 | manifest 用**名字**标识实体（`className`/`propertyName`/`metricName`/`kpiCode`），图谱用 `unified_id` | golden fixture | 物化前必须做 **name → unified_id** 解析；解析不到 ⇒ 计入「未解析」并逐条报告 |
| C-6 | 业务图规模：BusinessEntity 节点 ~51 万、关系 ~145 万 | `scripts/sync_business_graph_from_thbi.py` 文档串 | 镜像**默认不含业务图**；启用需另做批次方案（D23 附录） |
| C-7 | Neo4j **无写目标闸门、无自动备份** | 仓内无对应 guard（Milvus 有 `scripts/milvus_write_target_guard.py`） | 镜像写函数必须**自带守卫**（只允许写 `Release*` label + 显式 release_id） |
| C-8 | 消费方 `MATCH (c:Class)` / `getClassIds()` 等不带发布维度 | `app/infrastructure/neo4j_client.py` | 镜像节点必须用**独立 label** ⇒ 现有消费方零影响、无需改 |
| C-11 | 镜像写全是 `MERGE`（节点键 = `unified_id + release_id`，边键 = 两端 + relType） | `app/infrastructure/neo4j_mirror_client.py` | 幂等天然具备；但属性**就地覆盖** ⇒ D23 需「已物化默认拒写」的闸门（L-7） |
| C-12 | Neo4j **无自动备份** | 仓内无备份件 | 裁剪**不可逆** ⇒ D23 必须 `--prune --expect-delete N` 双保险 |
| C-13 | PG 侧放台账 = 新表 + 迁移 + **部署** | 本批「零 DDL、不部署」约束 | D23 台账**落图里**（独立 label）⇒ 零迁移 |
| C-14 | 图与台账同库 ⇒ 图被清空时台账随之消失 | Neo4j 无跨库引用 | 语义正确（图没了就该重建）；台账**不是备份** |
| C-15 | 写目标闸门在**库层**（每个写函数自己调） | `app/infrastructure/neo4j_write_target_guard.py` | D23 的物化 Job **天然继承**同一道闸门 |
| C-16 | 台账若用镜像 label 会**混进对账计数** | D24 对账按 4 个镜像 label 计数 | 台账用**独立 label**（`MirrorJobState` / `MirrorJobBatch`） |
| C-17 | 边无独立键 ⇒ 计划外的边只能靠「两端都还在、但这批没写」判定 | `pruneReleaseMirror` 残留边清理逻辑 | 裁剪须**按 relType 分别**清，否则镜像 ≠ 计划 ⇒ 对账永远红 |

---

## 4. 方案对比与裁决（详见 `daily/D22.md`）

| 方案 | 做法 | 结论 |
|---|---|---|
| A 独立镜像库 | 整个 Neo4j database 复制一份 | ❌ Community 不支持多库（C-1） |
| B 主节点加 `release_id` 属性/标签 | 给现有节点打标 | ❌ 同一节点无法同时持有两版属性；标签只增不减 ⇒ 无法回退（C-2） |
| **C 独立镜像节点** | 新建 `:ReleaseClass` 等独立 label，节点身份 = (label, unified_id, release_id)；边连镜像节点 | ✅ **采纳**：镜像不可变、边靠端点继承发布作用域、主图零影响、可回退 |

---

## 5. 遗留与风险

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| R-1 | 镜像**不覆盖**业务图（~51 万节点 / ~145 万边） | 显式声明范围；启用需另做批次方案与耗时预算 |
| R-2 | 镜像**不含**语义关系边（manifest 无 relations 分区，C-4） | 显式声明范围；对账口径同步写明 |
| R-3 | name → unified_id 解析依赖 PG 活数据 | 解析失败 ⇒ 计入「未解析」并报告（改名后重跑会暴露） |
| R-4 | Neo4j 无备份（C-7） | 镜像写**只增不删**（除显式 prune）；prune 必须显式传参 |
| R-5 | 图谱消费面**尚未**接入发布维度（读侧仍是主图） | 读侧切换是 D25；本批只交付「镜像存在且可对账」 |
| R-6 | **图谱镜像不会让 Milvus 的 `release_id` 变非零** | RAG 的 release 维度过滤需要**向量侧发布物化**（另立卡）；本批不冒充已解决 |
| R-7 | **列级 join 信息在图上丢失**：同一对类之间的多条 join 合并成一条 `JOIN` 边（边无属性，C-3） | 与主图同粒度（主图 `linkClassJoin` 也是无属性边）⇒ 不是漏写；但列级回放需要给边加属性，届时单独立项 |
| R-8 | 解析失败有**三种不同原因**（歧义 / 未登记 `id_mapping` / 本体不存在），混成一种会把运维引到错方向 | 三态分开记录（`EntityResolution.unmapped`）+ 各自文案指向具体动作（D22 §3.3） |
| R-9 | **镜像 ≠ 主图本体子图**（评审 HIGH）：manifest 的 `properties[].refClassName` 承载「属性 → 类」引用，主图会物化成 `(:Property)-[:REFERENCES]->(:Class)`；镜像原本没有这一类边 ⇒ D25 读侧切换会**静默丢引用/外键导航路径** | **已修**：relType 白名单 5→**6**（加 `REFERENCES`）+ 计划器 + 端点类型契约；悬空引用归一为 `null` |
| R-10 | **写目标闸门**：Neo4j 侧**无** Milvus 那样的闸门件（C-7）⇒ 误写生产图库不可逆 | **已修**：闸门下沉到**库层** `app/infrastructure/neo4j_write_target_guard.py`（**每个**镜像写函数自己调 ⇒ D23 的物化 Job 天然继承）；判定按**端口白名单**（只准 7688；无端口按 7687 判 ⇒ 生产被拒）；脚本层另加 `--expect-delete` 双保险 |
| R-11 | **续跑死锁**：台账说「成功」但图上东西被人工删了 ⇒ 按台账跳过 ⇒ **永远修不好** | **已修**（D23 真实踩到）：跳过条件加「**图上计数 == 计划计数**」⇒ 原则「**台账 = 意图，图 = 事实**」；另有依赖失效规则（节点批次重跑 ⇒ 边批次不许跳过） |
| R-12 | **已物化快照可被覆盖**（L-7）：MERGE 键 = (unified_id, release_id) ⇒ 重跑会就地覆盖属性 | **已修**（D23）：作业状态 `materialized` ⇒ 默认拒写（退出码 2）；台账哈希与快照哈希不一致也拒写；重建必须显式 `--force-rebuild` |
| R-13 | **dry-run 被「已物化」闸门挡住**：运维想看「会怎么分批」却拿不到任何信息 | **已修**（D23）：闸门移到 dry-run 分支**之后** ⇒ 只挡写、不挡看 |
| R-14 | **台账不是备份**（C-14）：图库整体清空时台账一并消失 | 语义正确（图没了就该重建）；已在 `daily/D23.md` 遗留项 L-11 登记，运维须知 |
| R-15 | **物化无跨系统事务**：PG 发布状态与图侧物化状态可能不一致（L-8） | 图侧有状态台账；PG 侧不写状态（会引入 DDL）⇒ D24 对账脚本负责判定 |
| R-16 | **对账基准选谁**：重算计划 vs 发布时冻结计划 | **D24 裁决：重算计划**（与物化同一计划器 ⇒ 永不失配；冻结要加存储破坏零 DDL）。代价：解析漂移（L-13）表现为「成对差异」⇒ 报告给足判断材料，机器不替人下结论 |
| R-17 | **「不完整」≠「不一致」**：图忠实于计划但计划有未解析（解析缺口） vs 图和计划对不上（镜像缺陷）——混成一个退出码会把运维引到错方向 | **D24 已拆**：exit 3 = 不完整（去治理面补解析）/ exit 1 = 不一致（去图查差异）；不一致优先于不完整 |
| R-18 | **现有主图消费方误切到镜像**：图同步差分是写侧（写主图必须读主图）；状态计数报治理面 | **D25 已声明**：路由器只服务「按发布读」的新消费面；两个现有消费方刻意不切（切了是 bug） |
| R-19 | **开关非法值静默回主图 = 假切换**（运维以为切了，实际没切） | **D25 已防**：非数字开关值显式报错；`0` 视同不钉住 |
| R-20 | **镜像的关系维度覆盖只有 SUBCLASS_OF**（ontology_relation 边不在镜像） | 显式范围声明（docstring + D25.md）；按发布读要完整语义关系 ⇒ 关系镜像另立卡 |
