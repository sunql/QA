# 变更：feat-semantic-release-graph-mirror

- **日期**：2026-11-15
- **作者**：Claude
- **Phase**：P3b Neo4j 发布镜像（SemanticRelease → 图谱快照）
- **状态**：in-review（D22 已交付并实证；D23–D25 续接中）
- **关联变更**：[feat-semantic-release](../feat-semantic-release-a/summary.md)（P1a/P1b 语义发布，快照来源）· [feat-semantic-registry-rag](../feat-semantic-registry-rag/summary.md)（P3a RAG 过滤，向量侧发布维度）
- **迁移版本**：无（**零 DDL**：不改 PG 表结构、不加列、不改索引）
- **MEMORY**：[qa-system-graph-mirror-release](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-graph-mirror-release.md)

---

## 1. 需求

**背景**：P1a/P1b 把「语义发布」做成了自包含快照（`semantic_release.manifest_json`），
P2 把 NL2SQL 的 schema 渲染接到发布快照上。但**图谱这一侧完全没有任何发布维度**：

- 节点写路径全是 `MERGE (c:Class {unified_id})` ⇒ 同一个节点被所有发布共享，属性原地覆盖
  ⇒「A 版发布时的类 X 长什么样」在图里**查不到**；
- 边 `MERGE (a)-[:REL]->(b)` 无属性、无键 ⇒ 边也没有版本；
- 消费方 `MATCH (c:Class)` / `getClassIds()` / `getJoinPairs()` / `getRelationTriples()`
  一律不带发布维度。

⇒ 图谱只能回答「**现在**的图是什么」，回答不了「**某一版发布**的图是什么」。
发布要回放、要对比、要钉住，图谱这一路**对不上**。

**验收标准（用户视角、可测量）**

| # | 标准 | 判据 |
|---|---|---|
| 1 | 镜像存在时**主图一个字节都不变** | 写完镜像后主图 `:Class` 节点计数不变；镜像走独立 label |
| 2 | 同一发布**重复物化**结果完全一致 | 写两次，节点/边计数逐类相等 |
| 3 | 镜像内容与该发布 manifest **逐条对得上** | 实体 100% 解析成功；解析不到的**逐条**列出（类型/名字/原因），不静默跳过 |
| 4 | 「按发布读图」可行且**一键回退** | 读侧切换（D25）；本批保证镜像存在、可对账、可回放 |

## 2. 设计评审

三个候选（详见 [`daily/D22.md`](daily/D22.md) §二）：

| 维度 | **A 独立镜像库** | **B 主节点加发布属性/标签** | **C 独立镜像节点（采纳）** |
|---|---|---|---|
| 做法 | 整个图复制到另一个 Neo4j database | 给现有节点加 `release_id` 属性 / `Release_x` 标签 | 新建 `:ReleaseClass` 等 label，节点身份 = (label, unified_id, release_id) |
| 发布间隔离 | 完美 | ❌ 同一节点被所有发布共享，属性原地覆盖 ⇒ 查不到旧版 | ✅ 完美 |
| 回退到旧发布 | 指针切库 | ❌ 标签只增不减、属性已被覆盖 ⇒ **回不去** | ✅ 指针切换 |
| 主图影响 | 零（但不可行） | ❌ `MATCH (c:Class)` 会命中多份 ⇒ 全量改造消费方 | ✅ 零：独立 label ⇒ **现有消费方一行都不用改** |
| 边怎么带发布维度 | 天然 | ❌ 边无键无属性 ⇒ 只能 delete-then-create | ✅ **端点继承**：边连的是镜像节点 ⇒ 边天然属于该发布，**不需要给边加属性、不需要删边重建** |
| 可行性 | ❌ Community **不支持多 database** | 可行但语义错误 | ✅ 可行 |

**裁决：C（独立镜像节点）。** 三条决定性理由：

1. **不可变**（与仓内「不可变数据」原则一致）：镜像是发布的快照，不随本体变化而变；
2. **边靠端点继承作用域**：把「图边无键无属性」这个死结整个绕开；
3. **主图零影响**：现有 4 个生产消费方无需改动。

**方案 C 引出的三个设计决定**

| 决定 | 内容 | 理由 |
|---|---|---|
| 解析放 PG 侧 | manifest 用**名字**、图谱用 `unified_id` ⇒ 物化前批量解析 | 解析结果与写入解耦；纯逻辑可单测 |
| 解析失败**分三态** | 歧义（同名多行）/ 未登记（`id_mapping` 缺行）/ 本体不存在 | 混成一种会把运维引到**错方向**（实测踩过：把「未登记」说成「不存在」） |
| 写路径**独立模块** | `app/infrastructure/neo4j_mirror_client.py` | `neo4j_client.py` 已 1024 行，超「文件 ≤ 800 行」上限；镜像写路径自带独立白名单，混在一起反而看不清边界 |

## 3. 数据模型变更

**PG 侧：无**（零 DDL、无迁移、无索引变化、无 CheckConstraint 变化）。

本批只**读** `ontology_class` / `ontology_property` / `ontology_metric` / `kpi_catalog` /
`ontology_join` / `id_mapping`，全部走 SQLAlchemy 参数化查询。

**图谱（Neo4j）侧：新增 4 类节点 + 6 种关系**

| 实体 | 镜像 label | 节点身份（MERGE 键） | 属性 |
|---|---|---|---|
| 类 | `ReleaseClass` | (`unified_id`, `release_id`) | name / alias / description / sourceTable + `release_id` / `release_version` / `mirror_of` |
| 属性 | `ReleaseProperty` | 同上 | name / alias / dataType / sourceColumn / isPrimaryKey / isForeignKey + 发布标识 |
| 指标 | `ReleaseMetric` | 同上 | name / alias / formula / aggFunction + 发布标识 |
| KPI | `ReleaseKpiCatalog` | 同上 | code / name / status / owner / unit + 发布标识 |

关系类型**白名单**（**6** 种）：`HAS_PROPERTY` / `SUBCLASS_OF` / `JOIN` / `DERIVED_FROM` /
`MEASURED_BY` / `REFERENCES`。边**不带属性、不带键**（与主图同款）——发布作用域由**端点**承载。

> ⚠️ **`REFERENCES` 是评审补齐的一项**（原本只有 5 种）：manifest 的
> `classes[].properties[].refClassName` 承载「属性 → 类」的引用，主图会物化成
> `(:Property)-[:REFERENCES]->(:Class)`（`neo4j_client.linkPropertyReferences`）。
> 镜像漏了它 ⇒ **镜像 ≠ 主图本体子图** ⇒ D25 读侧切换会**静默丢掉引用/外键导航路径**。
> ⇒ 已进白名单与计划器；悬空引用（指向 manifest 里没有的类）由编译器归一为 `null`，不产生噪声边。

**明确不在镜像范围**（声明，不是遗漏）：业务图（~51 万节点 / ~145 万边）；
语义关系边（`ontology_relation` 驱动，manifest 无 relations 分区 ⇒ 无法从快照自包含重建）。

## 4. 接口契约变更

**HTTP API：无变更**（不新增/不修改任何端点、DTO、状态机）。

本批新增的是**内部契约**（`app/domain/semantic_graph_mirror_spec.py`），供 service 与
infrastructure 共用，避免两份白名单漂移：

| 常量 | 值 |
|---|---|
| `RELEASE_MIRROR_LABELS` | class→`ReleaseClass`、property→`ReleaseProperty`、metric→`ReleaseMetric`、kpi→`ReleaseKpiCatalog` |
| `RELEASE_MIRROR_NODE_FIELDS` | 每类实体的属性名与**顺序**（顺序按主图节点字段顺序，不是字母序） |
| `RELEASE_MIRROR_REL_TYPES` | **6** 种白名单（`frozenset`） |
| `RELEASE_MIRROR_REL_ENDPOINT_TYPES` | relType → (源类型, 目标类型) 契约（如 `HAS_PROPERTY` ⇒ class→property）⇒ 端点类型不符即拒写 |
| `RELEASE_MIRROR_ENTITY_BUSINESS_OBJECTS` | 实体类型 → 业务对象段（class→`CLASS`…）⇒ `unified_id` 段不符即拒写 |
| `RELEASE_MIRROR_ENTITY_TYPES` | `("class", "property", "metric", "kpi")` |

**内部函数契约**

| 函数 | 职责 |
|---|---|
| `buildEntityResolution(*, classRows, propertyRows, metricRows, kpiRows)` | 纯函数：PG 行 ⇒ name→unified_id 解析表（含歧义 / 未登记） |
| `resolveManifestEntities(session, manifest)` | 连 PG：查活数据 + `id_mapping` ⇒ 解析表（LEFT OUTER JOIN 保留未登记实体）；**名单分块查**（`_QUERY_CHUNK_SIZE = 2_000`，PG 单条语句参数上限 65535） |
| `buildMirrorPlan(manifest, resolution, *, releaseId, releaseVersion)` | 纯函数：manifest + 解析表 ⇒ `{节点, 边, 未解析}`（无 IO、确定性）；拆成 `_planClasses` / `_planProperty` / `_planReferenceEdge` / `_planSubclassEdges` / `_planJoinEdges` / `_planMetrics` / `_planKpis` |
| `assertSafeNeo4jTarget(uri, *, allowProdWrite=False)` | **写目标闸门**（库层）：默认只准写**测试实例**（7688），其它目标（含生产 7687、无端口的 `bolt://neo4j`）一律 `ValueError` |
| `upsertReleaseMirrorNodes(entityType, releaseId, releaseVersion, rows)` | 连 Neo4j：批量幂等写节点（UNWIND + MERGE） |
| `upsertReleaseMirrorEdges(releaseId, edges)` | 连 Neo4j：批量幂等写边（端点用 MATCH，命中数不足即抛错） |
| `countReleaseMirrorNodes/Edges(releaseId)` | 按类型 / relType 计数（对账用；**两端 label 约束** ⇒ 不扫整图） |
| `pruneReleaseMirror(releaseId, keepUnifiedIds, *, confirm)` | 清理该发布下**计划外**的镜像节点与**两端都保留但已不在计划里的**残留边（必须显式 `confirm=True`） |

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/app/domain/semantic_graph_mirror_spec.py` | label / 字段 / relType / 端点类型契约（service 与 infrastructure 共用） |
| `backend/app/domain/id_mapping.py` | **业务对象字面量 SSOT**：`BUSINESS_OBJECT_CLASS/PROPERTY/METRIC/KPI`（原先字面量散落两处，写错会让解析静默落到「未登记统一 ID」⇒ 误导运维） |
| `backend/app/services/semantic_graph_mirror.py` | 纯计划构建（`buildMirrorPlan` + `buildEntityResolution`）+ 未解析清单（**无 IO**） |
| `backend/app/services/semantic_graph_mirror_resolution.py` | 连 PG 的解析（`resolveManifestEntities`）：名单分块查，避免撞 PG 参数上限 |
| `backend/app/infrastructure/neo4j_mirror_client.py` | 镜像写 / 读 / 清理；driver 复用 `neo4j_client.getDriver()`，**不重复连接逻辑**；每个写函数**自己**调闸门 |
| `backend/app/infrastructure/neo4j_write_target_guard.py` | **写目标闸门（库层）**：默认只准测试实例（7688）；按**端口**判定（无端口 ⇒ 7687 ⇒ 生产）；描述里剥掉 userinfo（不打印凭据） |
| `backend/scripts/semantic_graph_mirror_realdata.py` | 真实数据验证（默认只读；`--write` 才写；`--cleanup` 必须配 `--expect-delete N`；报告只印 `host:port`，不带凭据） |

**关键算法**

1. **解析**：4 条 SQL，`ontology_*` ⟕ `id_mapping`（LEFT OUTER JOIN ⇒ 未登记实体也可见），
   类只取 `valid_to IS NULL`（与编译器规则一致）；同名多行 ⇒ 歧义（值为 `None`）。
2. **计划构建**：类 → 属性（级联：类未解析 ⇒ 属性记未解析）、`SUBCLASS_OF`、
   `JOIN`（按 relType/源/目标去重）、`DERIVED_FROM`、`MEASURED_BY`；
   **节点按 (类型序, unified_id) 排序、边按 (relType, 源, 目标) 排序、未解析按 (kind, 类型, 名字, 原因) 排序**
   ⇒ 同一输入两次构建**逐字节一致**（幂等的前提）。
3. **写入**：节点 `MERGE (n:Label {unified_id, release_id})` + `SET`；边
   `MATCH (a) MATCH (b) MERGE (a)-[r:REL]->(b)`，**命中数 < 请求数即抛错**（端点缺失必须可见，
   且不会凭空造出孤儿节点）；批大小 5000。
4. **守卫**：entityType / relType 白名单（label、relType 在 Cypher 里**不可参数化**，
   只能拼字符串 ⇒ 拼接前必须校验）；行内 `unified_id` 非空；行内 `release_id` 必须与参数一致；
   `prune` 必须显式 `confirm=True`。

**依赖注入点**：无新增 FastAPI 依赖（本批不新增端点）；Neo4j driver 走
`app.infrastructure.neo4j_client.getDriver()` 单例；PG 会话走 `getSessionFactory()`。

## 6. 测试

| 文件 | 用例 | 关键场景 |
|---|---|---|
| `backend/app/tests/unit/test_semantic_graph_mirror.py` | **35** | 字段顺序按 spec、发布标识、label 隔离、**6** 种边（含 `REFERENCES`）、级联未解析、歧义、**三态原因**、确定性（打乱输入同输出）、重复实体去重、指标/KPI 各类未解析分支 |
| `backend/app/tests/unit/test_neo4j_mirror_client.py` | **22** | 守卫（非 Release 类型 / 非白名单 relType / 缺 `unified_id` / `unified_id` 业务对象段不符 / `release_id` 不符 / 缺发布标识字段 / 端点类型不符）、**生产目标拒写**（节点 / 边 / prune）、CQL 出现 `:ReleaseClass` 且**从不出现** `:Class`、端点缺失必须报错、计数（两端 label 约束）、prune 需显式确认 + 残留边清理 |
| `backend/app/tests/unit/test_semantic_graph_mirror_guard.py` | **9** | 写目标闸门本体：测试端口放行 / 生产端口拒绝 / **无端口按 7687 判** / 描述剥掉 userinfo |
| `backend/app/tests/integration/test_semantic_release_service.py`（**改已有文件**） | +2 | 真实 PG：名字 → `unified_id` 对上 `id_mapping`；缺失实体不进解析表 |

- **覆盖率（实测，`pytest --cov`）**：
  `semantic_graph_mirror_spec.py` 100% · `neo4j_mirror_client.py` 96% ·
  `neo4j_write_target_guard.py` 82% · `semantic_graph_mirror.py` 99% ·
  `semantic_graph_mirror_resolution.py` 100% ⇒ **合计 97%**（门槛 ≥ 80%）。
- **集成用例加在已有文件里**（不新建集成测试文件）：CI 按「文件名排序下标 mod 4」分片，
  新建集成文件会让其后所有文件的分片归属整体轮转 ⇒ 制造「假净新增失败」。
- **TDD**：先写用例（RED，模块 ImportError）→ 实现（GREEN）→ 重构；
  实现期间发现「未登记」被并进「不存在」，**改的是实现（三态分开），不是改文案糊过去**。

## 7. 安全审查

已触发 **security-reviewer** 与 **code-reviewer**（新增代码涉及外部图库写入、Cypher 拼接、
脚本级破坏性操作 ⇒ 命中仓内「安全审查触发条件」）。

**审查结论**

| # | 来源 | 级别 | 发现 | 处置 |
|---|---|---|---|---|
| 1 | code-reviewer | **HIGH** | **`REFERENCES` 缺口**：manifest 用 `properties[].refClassName` 承载「属性 → 类」引用，主图会物化成 `(:Property)-[:REFERENCES]->(:Class)`；镜像原本没有这一类边 ⇒ **镜像 ≠ 主图本体子图** ⇒ D25 读侧切换会静默丢引用/外键导航路径 | **已修**：进 relType 白名单（5→**6**）+ `_planReferenceEdge` 计划器 + 端点类型契约（property→class）+ 4 条新单测；悬空引用归一为 `null` |
| 2 | code-reviewer | **HIGH** | `buildMirrorPlan` 单函数 **251 行**（超出「函数 < 50 行」） | **已修**：拆成 7 个按实体分的计划器 + `_PlanContext` 累加器 |
| 3 | security-reviewer | HIGH | 写目标闸门「**端口缺失时放行**」 | **已修（且评审读的是旧版）**：闸门改为 **deny-by-default**，端口缺失按默认端口 **7687** 判 ⇒ 生产目标被拒；并补单测 `无端口按 7687 判` |
| 4 | code-reviewer | HIGH | 闸门是「**黑名单式**」（只挡 7687，换端口/换主机即绕过） | **已修**：改为**白名单端口**（只准测试实例 7688）+ 库层守卫（每个写函数自己调）⇒ 换端口/换主机都绕不过去 |
| 5 | code-reviewer | MEDIUM | 闸门只在**脚本层** ⇒ D23 的物化 Job 直接调库函数时会绕过 | **已修**：闸门下沉到 `app/infrastructure/neo4j_write_target_guard.py`，**库函数自己调**（CLI 只传 `allowProdWrite`） |
| 6 | code-reviewer | MEDIUM | `prune` 会遗留「两端都保留但已不在计划里」的边（manifest 删了一条 JOIN ⇒ 边永远残留 ⇒ 镜像 ≠ 计划，对账一直红） | **已修**：`_pruneStaleEdges` 按 relType 单独清理 |
| 7 | code-reviewer | MEDIUM | 写目标校验**先于**输入校验 ⇒ 环境问题盖掉校验错误信息（单测一度被掩盖） | **已修**：改成「先校验输入、再看写目标」 |
| 8 | code-reviewer | LOW | CQL 断言写成 `":ReleaseClass)-[r:HAS_PROPERTY]->(:ReleaseProperty"` ⇒ 与实际 `->(b:ReleaseProperty)` 不符（假红） | **已修**：断言改成实际形状，并补「剔除 `Release*` 后不得出现 `:Class`/`:Property`/`:Metric`/`:KpiCatalog`」的反向断言 |

**⚠️ 流程教训（已登记）**：3 与 4 两条 HIGH 是**评审读到了旧版脚本**得出的结论
（闸门当时已重写为 deny-by-default + 白名单端口）。⇒ **评审必须给出「读到的文件版本/commit」**，
否则「按旧版结论整改」会引入回归。下次派审前先确认工作区已落盘。

**复核（整改后重跑）**：85 个用例全绿（66 单测 + 19 集成）；
覆盖率 97%（见第 6 段）；`ruff check` 干净。

## 8. 部署验证

**零 DDL、不部署生产**：本批没有表结构变化、没有新端点，不需要迁移，也不改变生产行为
（镜像写路径目前只有脚本可调用，**没有**任何 API 触发它）。

**docker compose 栈（本地，实测）**

```
qa-postgres      Up 10 days (healthy)   0.0.0.0:5433->5432/tcp
qa-neo4j         Up 10 days (healthy)   0.0.0.0:7687->7687/tcp
qa-neo4j-test    Up 4 days               0.0.0.0:7688->7687/tcp
qa-pg-a1         Up 9 days (healthy)    0.0.0.0:5434->5432/tcp
```

**真实数据验证**（`backend/scripts/semantic_graph_mirror_realdata.py`，dev 库 5433 `qa_metadata`
+ **测试** Neo4j 7688）：

```
$ python -m scripts.semantic_graph_mirror_realdata --write --cleanup --expect-delete 3328
[semantic_graph_mirror_realdata] 数据库：qa_metadata（只读，不打印凭据）
{
  "releaseId": 0, "releaseVersion": "synthetic-local",
  "manifest": {"classes": 37, "joins": 13, "metrics": 12, "kpis": 12},
  "plan": {"nodes": {"class": 37, "property": 3267, "metric": 12, "kpi": 12},
           "nodeTotal": 3328,
           "edges": {"HAS_PROPERTY": 3267, "JOIN": 11, "MEASURED_BY": 12,
                     "REFERENCES": 3},
           "edgeTotal": 3293, "unresolvedTotal": 0},
  "deterministic": true, "unresolved": [],
  "neo4j": {"target": "neo4j://localhost:7688",
            "writtenNodes": 3328, "writtenEdges": 3293,
            "readBackNodes": {"class": 37, "property": 3267, "metric": 12, "kpi": 12},
            "readBackEdges": {"DERIVED_FROM": 0, "HAS_PROPERTY": 3267, "JOIN": 11,
                              "MEASURED_BY": 12, "REFERENCES": 3, "SUBCLASS_OF": 0},
            "idempotent": true, "mainGraphUnchanged": true,
            "mainGraphNodes": {"Class": 2, "Property": 3, "Metric": 0, "KpiCatalog": 0},
            "cleanedUp": 3328}
}
[semantic_graph_mirror_realdata] PASS：镜像节点 3328、边 3293
```

**写目标闸门实证（拒写生产）**

| 目标 | 期望 | 实测 |
|---|---|---|
| `bolt://localhost:7688`（测试实例） | 放行 | ✅ 写入成功（上面的 3328 / 3293） |
| `bolt://localhost:7687`（生产） | 拒绝 | ✅ `ValueError：拒绝向 neo4j://localhost:7687 写镜像` |
| `bolt://neo4j`（**无端口** ⇒ 按 7687 判） | 拒绝 | ✅ 拒绝 |

**破坏性操作的双保险**：`--cleanup` 必须配 `--expect-delete N`；
脚本先数**实际将删多少**，与 N 不等 ⇒ **拒绝执行**
（实测 `--expect-delete 999` ⇒ 「预期删 999 个，实际将删 3328 个 ⇒ 拒绝执行」）。

逐条对照验收标准：

| 验收标准 | 实测证据 |
|---|---|
| 主图零影响 | 主图 4 个 label 写前 = 写后：`Class` **2** / `Property` **3** / `Metric` **0** / `KpiCatalog` **0** |
| 重复物化幂等 | 再写一次，**节点与边**计数都不变（3328 / 3293） |
| 与 manifest 逐条对得上 | 计划节点 3328 + 边 3293，回读**逐类 / 逐 relType 相等**；未解析 0 |
| 可清理 | `--cleanup --expect-delete 3328` 删掉 3328 个镜像节点，回读镜像节点 0 / 镜像边 0 |

**JOIN 11 ≠ manifest 13 是去重不是漏写**：13 条 join 里有 2 组是**同一对类之间的不同
joinKey**（`DWD_GOODS_RECEIPT_DTL → DWD_PURCHASE_ORDER_DTL` 的 PO_LINE_NO / PO_NO，
`DWD_ARRIVAL_ORDER_DTL → DWD_PURCHASE_ORDER_DTL` 的 PO_LINE_NO / PO_NO）。
图边无属性 ⇒ 同类对的多条 join 在图上是同一条边 ⇒ 计划按 (relType, 源, 目标) 去重。
主图的 `linkClassJoin` 同样是无属性边 ⇒ **镜像与主图同粒度**；列级 join 信息在图上丢失
已登记为遗留项 L-1 / R-7。

## 9. 关联

- **设计稿 / 每日证据**：[`daily/D22.md`](daily/D22.md)（方案对比、recon 事实 C-1..C-10、
  三态解析、批次方案 D22-B、遗留项 L-1..**L-7**）· [`plan.md`](plan.md)（D22–D25 任务卡、约束、风险 R-1..**R-10**）
- **Wiki**：[`Harness/wiki/architecture.md`](../../wiki/architecture.md)「发布维度：语义发布（P1）与图谱镜像（P3b）」
- **Rules**：[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md) ·
  [`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md) ·
  [`Harness/rules/测试规范.md`](../../rules/测试规范.md) ·
  [`Harness/rules/工程结构.md`](../../rules/工程结构.md)
- **Memory**：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-graph-mirror-release.md`
- **关联变更（predecessor / successor）**：
  [`feat-semantic-release`](../feat-semantic-release-a/summary.md)（快照来源）·
  [`feat-semantic-registry-rag`](../feat-semantic-registry-rag/summary.md)（向量侧发布维度，另一件事）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（3 个：A / B / C）
- [x] 第 3 段迁移文件名 ≤ 32 字符（本批**零 DDL**，无迁移）
- [x] 第 7 段已触发 security-reviewer / code-reviewer，结论已填
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Wiki / Rules / Memory）
- [x] 相关 wiki 文档已更新（`Harness/wiki/architecture.md` 新增发布维度章节）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 添加
- [x] 真实数据验证脚本 `scripts/semantic_graph_mirror_realdata.py` 已跑通（含写 + 回读 + 幂等 + 清理）
