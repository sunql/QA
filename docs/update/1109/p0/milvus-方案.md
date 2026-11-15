# P3a RAG 过滤 —— Milvus 方案（V1.0）

> 阶段：P3a / D18（2026-11-15）
> 上游：`docs/update/1109/智能采购平台升级计划_日级排程_1109.md` §6
> 基线：`docs/update/1109/p0/数据基线.md` §4（2026-11-09 生产实测）
> 变更记录：`Harness/changes/feat-semantic-registry-rag/daily/D18.md`
> 本文只定方案，**不含代码**；代码在 D19–D20。

---

## 1. 问题

本体向量（Milvus）目前**只有向量与文本，没有任何治理维度**：

| 现状字段 | 内容 | 能回答的问题 |
|---|---|---|
| `id` / `ontology_id` / `type` / `name` / `alias` / `description` / `embedding` / `external_id` | 文本与向量 | 「哪些本体条目语义相近」 |

答不了的问题（本批要补的）：

1. 这个类属于**哪个业务域**？
2. 它挂在哪个**业务对象（实体）**下？
3. 它归哪个**组织**？
4. 它**什么时候生效、什么时候失效**？
5. 它属于**哪个语义发布版本**（`release_id`）？

没有这五维，RAG 召回就**无法按治理语义收窄**，只能全量向量搜。

---

## 2. 现状实测基线

### 2.1 生产 Milvus（2026-11-09，`p0/数据基线.md` §4）

- 5 个集合：`ontology_class_embeddings` / `ontology_property_embeddings` /
  `ontology_metric_embeddings` / `query_embeddings` / `wiki_page_embeddings`；
- `enable_dynamic_field=false` ⇒ **加字段必须改 schema**；
- 维度 1024，HNSW + L2；
- 总量 < 7,100 行 ⇒ **重建成本极低**。

### 2.2 为什么是「新建集合 + 全量回填」而不是 `add_collection_field`

`p0/数据基线.md` §4 裁决 #4 已定：Milvus 2.4.6 的
`add_collection_field` 不可靠，而集合总量 < 7,100 行 ⇒ 新建集合 + 全量回填
远比在 2.4.6 上试探动态加字段更稳。**本方案沿用该裁决，不重新论证。**

### 2.3 本地开发库实测（2026-11-15）

| 项 | 值 |
|---|---|
| ontology_class | 55（`valid_to` 非空 18、`object_owner` 非空 5） |
| ontology_property / ontology_metric / kpi_catalog | 3730 / 12 / 12 |
| business_object | 6（`primary_domain_id` 全部有值） |
| data_domain / business_object_domain | 4 / **0**（无附域数据） |
| organizations | 6 |
| **有域的类（BO 头类）** | **6 / 55 ≈ 11%** |

---

## 3. Milvus 2.4.6 表达能力实测（2026-11-15 本地 2.4.6，库 `qa_test`）

探针脚本：`/tmp/probe_array.py`、`/tmp/probe_array2.py`（自建自删探针集合，跑完即清理）。

### 3.1 结论表

| 能力 | 结果 | 实测输出 |
|---|---|---|
| ARRAY\<VARCHAR\> 建集合 | ✅ | 建集合成功 |
| ARRAY 插入（含**空数组**） | ✅ | `insert with empty arrays OK, entities: 3` |
| ARRAY 读回 | ✅ | `query rows: … {'domain_codes': ['SUP'], …} / {'domain_codes': [], …}` |
| `array_contains(f, "X")` | ✅ | `OK array_contains → [('SUPPLIER', ['SUP','PROC'])]` |
| `f in ["X"]` | ❌ | `value '"SUP"' in list cannot be casted to Array` |
| `f like "X"` | ❌ | `can not comparisons array fields directly` |
| 标量 `==` | ✅ | `OK scalar eq` |
| 范围 `a <= b and (c == 0 or c >= b)` | ✅ | `OK range`（命中 2 行，含 `valid_to=0` 的长期有效行） |
| `and` 组合多维 | ✅ | `OK and scalar+array` |
| `not array_contains(f,"X")` | ⚠️ | **命中空数组行**：`not contains → [([], []), (['MAT'],…)]` |
| `output_fields` 取 ARRAY | ✅ | 见上 query rows |

### 3.2 由实测得出的三条硬约束

1. **多值维度只能用 `array_contains(f, "X") or array_contains(f, "Y")`**。
   `in` / `like` 在 ARRAY 字段上**直接报错**（不是返回空，是 query plan 解析失败）。
   ⇒ 不能用 `like "SUP"` 糊弄——那会误命中 `SUPX`，而且根本跑不通。
2. **`not array_contains` 会命中空数组行**。⇒ 「排除某域」不能写成
   `not array_contains(domain_codes, "X")`，否则未归域的行会被当成「非该域」放进来。
   排除语义必须显式枚举允许域。
3. **空数组是合法值**。未归域的行用空数组表达，不是 `null`、不是缺字段
   （`enable_dynamic_field=false` ⇒ 缺字段会写不进去）。

---

## 4. 目标 schema

### 4.1 集合命名

| v1（现状） | v2（本批新增） | 说明 |
|---|---|---|
| `ontology_class_embeddings` | `ontology_class_embeddings_v2` | 类 |
| `ontology_property_embeddings` | `ontology_property_embeddings_v2` | 属性 |
| `ontology_metric_embeddings` | `ontology_metric_embeddings_v2` | 指标 |
| `kpi_catalog_embeddings` | `kpi_catalog_embeddings_v2` | KPI 目录 |

v1 **保持不动**（不删不改），v2 与之并存 ⇒ 回退只需关开关。

### 4.2 字段

v2 = v1 全部字段 + 5 个过滤字段（`id` / `ontology_id` / `type` / `name` /
`alias` / `description` / `embedding` / `external_id` 全部沿用）。

| 字段 | 类型 | 语义 | 哨兵 |
|---|---|---|---|
| `domain_codes` | ARRAY\<VARCHAR\>（cap 8, len 64） | 主域 + 附域的 `data_domain.code` | `[]` = 未归域 |
| `entity_type` | VARCHAR(32) | 业务对象码 `business_object.code` | `""` = 非 BO 头类 |
| `org_codes` | ARRAY\<VARCHAR\>（cap 8, len 64） | 组织码 `organizations.code` | `[]` = 无组织 |
| `valid_from` | INT64 | 有效期起点（epoch ms） | `0` = 自始 |
| `valid_to` | INT64 | 有效期终点（epoch ms） | `0` = 长期有效 |
| `release_id` | INT64 | 写入时 Registry 当前 ACTIVE 发布 id | `0` = 无激活发布 |

> 维度选 **epoch 毫秒（INT64）** 而非日期字符串：Milvus 的范围比较对
> INT64 最稳（实测 OK），且字符串日期比较语义在 2.4.6 上未实测 ⇒ 不冒险。

### 4.3 PG → Milvus 维度映射规则

**类（class）**

| 目标字段 | 来源 |
|---|---|
| `domain_codes` | `business_object.primary_domain_id` → `data_domain.code` ∪ `business_object_domain` 附域 |
| `entity_type` | `business_object.code`（`business_object.header_class_id = class.id`） |
| `org_codes` | 域的 `data_domain.primary_org_id` → `organizations.code`；∪ `class.object_owner` 若能匹配到组织 |
| `valid_from` / `valid_to` | `ontology_class.valid_from` / `valid_to` |
| `release_id` | 语义发布 Registry 当前 ACTIVE 的发布 id |

**属性（property）**：继承其 `class_id` 对应类的全部维度。

**指标（metric）**：`ontology_metric.target_class_id` → 类的维度；无 target ⇒ 全空维度。

**KPI**：`kpi_catalog.metric_id` → metric → class 的维度；链路断 ⇒ 全空维度。

> 链路断了就留空数组 / `0`：**宁可显式「未归域」，不可瞎猜一个域**。
> 影子比对会把这类行单列为「归因缺口」计数。

---

## 5. 过滤表达式

### 5.1 构造规则（纯函数，D19 实现并单测）

```
域：  array_contains(domain_codes, "A") or array_contains(domain_codes, "B")
组织：array_contains(org_codes,   "X") or array_contains(org_codes,   "Y")
实体：entity_type == "SUPPLIER"
有效期：valid_from <= now and (valid_to == 0 or valid_to >= now)
发布：release_id == 42
```

各维度间用 `and` 连接；未指定的维度**不产生条件**（不收窄）。

### 5.2 语义表

| 维度 | 未归维的行 | 语义 |
|---|---|---|
| 域 | **被排除**（`[]` 不命中任何 `array_contains`） | fail-closed |
| 组织 | **被排除** | fail-closed |
| 实体 | **被排除**（`""` 不等于任何码） | fail-closed |
| 有效期 | `valid_to=0` **可见**（长期有效） | fail-open |
| 发布 | `release_id=0` **被排除** | fail-closed |

### 5.3 ⚠️ 域过滤是「收窄」不是「授权」

- **授权**在 PG 侧（本仓现有的 ACL 只有 `assertCanModify`，admin 级；
  至今**没有**「用户 → 可见域/组织」的策略源）；
- **过滤**只决定「召回候选集」。

⇒ 本批交付**过滤机制**，不发明授权策略（策略接线属受控变更，另立）。

⇒ 且因域覆盖仅 11%（§2.3），域过滤一旦生效会**显著收窄召回**。
这要求 D20 的影子比对必须同时给出：

1. 过滤前/后召回数与重合度；
2. **归因缺口计数**（未归域而被排除的条数）——
   否则无法区分「过滤错了」与「本体没归域」。

---

## 6. 四步方案（建集 → 回填 → 双写 → 切读 → 回退）

| 步 | 动作 | 开关 | 可逆性 |
|---|---|---|---|
| ① 建集 | 建 4 个 v2 集合（幂等；HNSW/L2/1024 沿用现状） | 任意 | 可删，但 Milvus 无备份 ⇒ **不删** |
| ② 回填 | 从 **v1 现有 embedding** + PG 维度 → 写 v2 | 任意（脚本显式跑） | 只增不删，幂等可重跑 |
| ③ 双写 | 写路径同时写 v1 与 v2（**同一 embedding**） | ON | 关开关即停 |
| ④ 切读 | 检索读 v2（带过滤） | ON | — |
| ⑤ 回退 | 关开关 ⇒ 只写 v1、读回 v1 | OFF | **秒级，不回滚数据** |

### 6.1 硬约束

1. **回填不重算 embedding**。必须复用 v1 已有向量，否则影子比对的差异里会混入
   「向量漂移」，差异不可解释。
2. **回填只增不删**。Milvus **没有自动备份**（`Harness/rules/数据存储防护.md`）⇒
   误删不可逆；本仓有前科：2026-09-30 一次裸 pytest 把线上向量删空。
3. **回填脚本必须过 `scripts/milvus_write_target_guard.assertSafeMilvusTarget`**：
   生产库 `default` 需显式 `--allow-prod`，测试库 `qa_test` 免 opt-in。
4. **v2 与 v1 并存**，不原地改 v1。

### 6.2 开关

`ENABLE_SEMANTIC_REGISTRY_RAG`（默认 **关**），与 P2 的
`ENABLE_SEMANTIC_REGISTRY_NL2SQL` 同款约定：

- 默认关 ⇒ 今天的读路径**逐字节不变**（v1 集合 + 无过滤）；
- 开启时若鉴权不是真鉴权 ⇒ 启动告警（与 P2 同款 `*AuthMisconfiguration` 口径）。

### 6.3 ⚠️ 与既有「dual write」的命名区分

`app/infrastructure/milvus_dual_write.py::insertEmbeddingsDual` 里的 "dual write"
是**早期 `ontology_embeddings` → 按类型拆三集合**的迁移遗留命名，
**不是** v1/v2 双写。本批的新双写另起名字（如 `insertEmbeddingsToVersions`），
并在 docstring 写明区别，避免后人误读。

---

## 7. 写路径接线点（D19 实现依据）

| 调用点 | 现状 | P3a 改法 |
|---|---|---|
| `ontology_service.syncEmbedding`（同步方法） | `milvus.deleteByOntologyIdDual` + `insertEmbeddingsDual` | 保留 v1 写入；新增维度解析后写 v2（开关 ON 时） |
| `ontology_service.syncMissingClassEmbeddings` | 整批 `insertEmbeddingsDual(records)` | 批量解析维度 → 同批写 v2 |
| `kpi_vector_graph_sync.syncKpiVectorNow` | `await asyncio.to_thread(insertEmbeddingsDual, …)` | KPI 维度（metric→class）解析 → 写 v2 |

维度解析需要 DB（异步 session），而 Milvus 写入是同步的 ⇒ 解析放**服务层**（有 session），
写 v2 仍走 `asyncio.to_thread`（与现状一致）。

新增模块（建议）：

| 模块 | 职责 |
|---|---|
| `app/services/ontology_dimension_service.py` | PG 治理表 → `DimensionSet`（不可变 dataclass） |
| `app/infrastructure/milvus_registry_v2.py` | v2 schema / 建集 / 写入 / 带过滤检索 / `buildRegistryFilterExpr`（纯函数） |

---

## 8. 读路径接线点

| 调用点 | 现状 | P3a 改法 |
|---|---|---|
| `milvus_search.searchEmbeddingsByTypeRouted` | 无 `expr` 支持 | 增加可选 `filters` 参数 → 构造 `expr` |
| `ontology_service.searchByKeyword` / `searchByEmbedding` | 透传 | 透传 filters |
| `chat_recall`（`self._ontology.searchByKeyword`） | 无过滤 | 开关 ON 且有可见域集合时透传 filters |

> 过滤器来源：当前**无策略源**（§5.3）⇒ filters 由调用方显式传入，
> 无策略时为空 ⇒ 不收窄。机制由 D19 的双向用例证明。

---

## 9. 验收口径（D20 / D21）

**D20（测试环境建集 + 回填 + 影子比对）**

- 全量回填 < 7,100 行（生产量级；本地 ≈ 3.8k 行）；
- 回填**幂等可重跑**（重跑后行数不变、维度不漂移）；
- 影子召回比对报告：过滤前/后差异**可解释**（含归因缺口计数）；
- **无未授权泄漏**：带过滤的检索不返回越权域的行（硬断言）。

**D21（回归 + 文档 + PR）**

- CI 失败集合**净新增 0**（判据不是全绿）；
- 开关关闭时读路径行为与今天一致（回归断言）。

---

## 10. 风险

| ID | 风险 | 处置 |
|---|---|---|
| R-1 | 域覆盖 11% ⇒ 域过滤显著收窄召回 | 影子比对量化 + 归因缺口计数；默认开关关 |
| R-2 | 无「用户 → 可见域」策略源 | 本批只交付机制；策略接线另立变更 |
| R-3 | Milvus 无自动备份 ⇒ drop 不可逆 | 回填只增不删；v2 与 v1 并存 |
| R-4 | 批量 delete 要 release+load 才可见 | v2 删除路径沿用同款处理，单测钉住 |
| R-5 | 生产与本地同 Milvus 实例 | 回填脚本过 `assertSafeMilvusTarget` |
| R-6 | `not array_contains` 命中空数组行 | 排除语义一律显式枚举允许域（§5.2） |
