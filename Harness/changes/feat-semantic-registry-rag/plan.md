# P3a RAG 过滤 —— 执行计划（续接 1109 日级排程 D18–D21）

> 上游基线：`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`（D1–D62）
> 上游已合：P0 / P1a（`849de29e`）/ P1b（`4777bfba`）/ P2（`b6af98a1`）
> 本文档只管 P3a（D18–D21）。P3b–P6 另有计划文档，不在本文件内。
> **本批零 DDL**（不改 PG 表结构），不部署生产。

---

## 0. 阶段现状与续接判断

### 0.1 已完成 / 未开工（2026-11-15 实测）

| 阶段 | 日次 | 状态 | 落点 |
|---|---|---|---|
| P0 / P1a / P1b | D1–D11 | ✅ | G1 ✅ / G2 ✅ |
| P2 NL2SQL 影子接入 | D12–D17 | ✅ | G3 3 过 1 不过（`b6af98a1`，不部署） |
| **P3a RAG 过滤** | **D18–D21** | ✅ **完成**（D18 / D19 / D20 / D21 均于 11-15 落地） | 本文件 + `daily/D18.md`…`daily/D21.md` + `summary.md` |
| P3b–P6 | D22–D62 | ⬜ | — |

### 0.2 为什么必须新开集合（实测依据，不靠推测）

`docs/update/1109/p0/数据基线.md` §4（2026-11-09 生产实测）：

- 5 个集合，`enable_dynamic_field=false`，**无域/实体/有效期字段**；
- 维度 1024、索引 HNSW；
- 体量合计 < 7,100 行 ⇒ **新建集合 + 全量回填**成本极低，
  比在 2.4.6 上试探 `add_collection_field` 更稳（该结论是 D18 的前置裁决，直接沿用）。

---

## 1. 目标与范围

**目标**：让向量检索能按「域 / 实体 / 组织 / 有效期 / 语义发布版本」过滤，
并能在开关下切到带过滤维度的新集合；**默认关 ⇒ 今天的行为逐字节不变**。

**本批做**：
1. v2 集合 schema 定稿（含 Milvus 2.4.6 实测出的表达式约束）；
2. 过滤机制（读路径）+ 影子召回比对；
3. 回填脚本（幂等、dry-run、只增不删）；
4. v1/v2 双写 + 切读开关 `ENABLE_SEMANTIC_REGISTRY_RAG`（默认关）。

**本批不做**（范围边界）：
- **不发明「谁能看哪个域」的授权策略**。本仓现有的 ACL 只有 `assertCanModify`
  （admin 级），没有「用户 → 可见域/组织」的策略源。P3a 只提供**过滤能力**与
  **影子评估**，策略接线属受控变更，不在本批。
- 不改 Milvus 2.4.6 版本、不加索引类型、不改维度。
- 不动 `query_embeddings` / `wiki_page_embeddings` / `document_embeddings`
  （各有自己的过滤维度：datasource_id / ACL / security_level）。
- 不部署生产。

---

## 2. 关键设计决策（D18 定稿）

### 2.1 新集合与字段

v2 集合 = v1 字段 + 5 个过滤标量：

| 集合 | 对应 v1 |
|---|---|
| `ontology_class_embeddings_v2` | `ontology_class_embeddings` |
| `ontology_property_embeddings_v2` | `ontology_property_embeddings` |
| `ontology_metric_embeddings_v2` | `ontology_metric_embeddings` |
| `kpi_catalog_embeddings_v2` | `kpi_catalog_embeddings` |

| 字段 | 类型 | 语义 | 哨兵 |
|---|---|---|---|
| `domain_codes` | ARRAY\<VARCHAR\>（cap 8, len 64） | 主域 + 附域（`data_domain.code`） | 空数组 = 未归域 |
| `entity_type` | VARCHAR(32) | 业务对象码（`business_object.code`） | `""` = 非 BO 头类 |
| `org_codes` | ARRAY\<VARCHAR\>（cap 8, len 64） | 组织码（`organizations.code`） | 空数组 = 无组织 |
| `valid_from` | INT64 | 有效期起点（epoch ms） | `0` = 自始 |
| `valid_to` | INT64 | 有效期终点（epoch ms） | `0` = 长期有效 |
| `release_id` | INT64 | 写入时 Registry 当前 ACTIVE 发布 id | `0` = 无激活发布 |

**维度映射（回填时从 PG 治理表推导）**：

- class：`business_object.header_class_id = class.id` ⇒ `domain_codes` = `primary_domain_id`
  ∪ `business_object_domain` 附域；`entity_type` = BO.code；
  `org_codes` = 域的 `primary_org_id` → `organizations.code`（∪ `class.object_owner`
  若能匹配到组织）；有效期 = `class.valid_from/valid_to`。
- property：继承其 class 的全部维度。
- metric：`target_class_id` → class 的维度；无 target_class ⇒ 空维度。
- kpi：`metric_id` → metric.`target_class_id` → class 的维度；无 ⇒ 空维度。

### 2.2 Milvus 2.4.6 表达式约束（2026-11-15 本地 2.4.6 实测）

| 写法 | 结果 |
|---|---|
| ARRAY\<VARCHAR\> 字段创建 / 插入 / 读回 | ✅（含**空数组**） |
| `array_contains(f, "X")` | ✅ |
| `f in ["X"]` | ❌ `value '"X"' in list cannot be casted to Array` |
| `f like "X"` | ❌ `can not comparisons array fields directly` |
| 标量 `==` / 范围 / `and` / `or` / `not` | ✅ |

⇒ **多值维度只能用 `array_contains(...) or ...`；禁止用子串匹配糊弄**
（`like "SUP"` 会误命中 `SUPX`）。

### 2.3 过滤语义（每个维度的判定）

| 维度 | 表达式 | 未归域行的可见性 |
|---|---|---|
| 域 | `array_contains(domain_codes, "X") or …` | **不命中任何域 ⇒ 域过滤生效时被排除**（fail-closed） |
| 组织 | `array_contains(org_codes, "X") or …` | 同上 |
| 实体 | `entity_type == "X"` | `""` 不命中 ⇒ 被排除 |
| 有效期 | `valid_from <= now and (valid_to == 0 or valid_to >= now)` | `valid_to=0` 视为长期有效（fail-open） |
| 发布 | `release_id == N` | `0` 不命中 ⇒ 被排除 |

维度之间用 `and` 组合。

⚠️ **域过滤是「收窄」不是「授权」**：授权在 PG 侧（ACL）做，过滤只决定「召回候选集」。
域覆盖率低时域过滤会显著收窄召回（本地 6/55，见 §3.3）⇒ 必须影子比对量化。

### 2.4 四步方案（建集 / 回填 / 双写 / 切读 / 回退）

| 步 | 动作 | 开关状态 | 可逆性 |
|---|---|---|---|
| ① 建集 | 建 4 个 v2 集合（幂等；HNSW/L2/1024 维持现状） | 任意 | 可删（但 Milvus 无备份 ⇒ 不删） |
| ② 回填 | 从 **v1 集合现有 embedding** + PG 维度 → 写 v2（**不重算 embedding**） | 任意（脚本显式跑） | 只增不删，幂等可重跑 |
| ③ 双写 | 写路径同时写 v1 与 v2（同一 embedding） | ON | 关开关即停 |
| ④ 切读 | 检索读 v2（带过滤） | ON | — |
| ⑤ 回退 | 关开关 ⇒ 写只回 v1、读回 v1 | OFF | **秒级，无需回滚数据** |

**向量不重算**是硬约束：回填必须复用 v1 已有向量，否则影子比对的差异里会混入
「向量漂移」，不可解释。

**开关**：`ENABLE_SEMANTIC_REGISTRY_RAG`（默认关，与 P2 的
`ENABLE_SEMANTIC_REGISTRY_NL2SQL` 同款：默认关 ⇒ 零风险；开启时鉴权必须是真鉴权，
否则报同款启动告警）。

### 2.5 与既有「dual write」的命名区分（防误读）

`app/infrastructure/milvus_dual_write.py::insertEmbeddingsDual` 里的 "dual write"
指的是**早期 `ontology_embeddings` → 按类型拆三集合**的迁移遗留命名，
**不是** v1/v2 双写。本批的新双写另起名字（如 `insertEmbeddingsToVersions`），
并在 docstring 里写明区别。

---

## 3. 实测基线（本地，2026-11-15）

### 3.1 Milvus 集合（本地 `127.0.0.1:19530`）

- `default` 库：`ontology_class_embeddings` / `ontology_property_embeddings` /
  `ontology_metric_embeddings` / `kpi_catalog_embeddings` / `query_embeddings` /
  `wiki_page_embeddings` / `document_embeddings`；
- `qa_test` 库（测试隔离库）：5 个本体/文档集合。

### 3.2 PG 治理数据（本地 dev `qa_metadata`@5433）

| 项 | 值 |
|---|---|
| ontology_class | 55（其中 `valid_to` 非空 18、`object_owner` 非空 5） |
| ontology_property / ontology_metric / kpi_catalog | 3730 / 12 / 12 |
| business_object | 6（`primary_domain_id` 全部有值） |
| data_domain / business_object_domain | 4 / **0**（无附域） |
| organizations | 6 |
| **类被 BO 覆盖（= 有域）** | **6 / 55 ≈ 11%** |

### 3.3 由此得出的设计约束

1. **域覆盖极低**（本地 11%，生产 99 类量级下同样偏低）⇒ 域过滤一旦生效，
   召回会显著收窄。这是**可解释的数据事实**，不是 bug ⇒ D20 的影子比对报告必须
   同时给出「过滤前后召回差异」与「归因缺口计数」，否则无法判断是过滤错了还是
   本体没归域。
2. **无附域数据** ⇒ `domain_codes` 实际只有 1 个元素；ARRAY 仍保留（多域是设计事实，
   只是当前无数据）。
3. **回填体量**：本地 ≈ 3.8k 行，生产 < 7.1k 行 ⇒ 全量回填成本极低。

> **D20 实测回填**（本地隔离实验库 `qa_registry_v2`，v1 只读 `default`）：
> 可见 **3,791** 行（class 37 / property 3,730 / metric 12 / kpi 12）；
> 维度覆盖 **已归域 592 / 未归域 3,199（84%）**、已挂组织 737、已挂实体 592；
> 连跑多轮可见行数恒定 ⇒ **幂等**；影子比对 8 个范围 **泄漏 0**，
> 且 v2 过滤检索与**精确真值**完全一致（详见 `daily/D20.md`）。

> **D21 实测回归**（基线 `b6af98a1` 独立 worktree + 独立 venv 对差）：
> unit 定向选择 370 → **462 全绿（净新增 0）**；`test_search_type_routed` 3 → 5 全绿；
> ontology 集成 12 红、`test_chat_service` 5 红在基线与分支上**逐条同名**；
> ruff 在新增 15 个文件上 15 → 0，既有文件净新增 0（详见 `daily/D21.md`）。

---

## 4. 日次任务卡

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| D18 | schema 定稿 + 双写/回填/切读/回退四步方案 + 表达式实测 | `daily/D18.md`、`docs/update/1109/p0/milvus-方案.md` | ✅ schema 定稿；四步方案明确；实测依据落档 |
| D19 | 过滤机制 + 读路径接线 + 开关 | `daily/D19.md` + 代码（5 新模块 / 6 改文件） | ✅ 双向用例通过（真实 Milvus，5 passed）；开关关时读路径逐字节等于今天；回归净新增 0 |
| D20 | 测试环境建集 + 全量回填 + 影子召回比对报告 | `daily/D20.md` + 2 脚本 + 报告（`reports/D20-shadow-report.md`） | ✅ 回填幂等可重跑（可见 3,791 行恒定，< 7,100）；差异可解释（8 个范围逐项归因）；**无未授权泄漏（泄漏 0 / v2 漏召回 0 / v2 多召回 0）** |
| D21 | 回归 + 文档 + PR | `daily/D21.md`、`summary.md`、PR | ✅ **CI 失败集合净新增 0**（基线 `b6af98a1` 对差：unit 定向 370→462 全绿 / ontology 集成 12 红同名 / chat_service 5 红同名；ruff 净新增 0） |
| D21R | 评审整改（fail-closed 口径补全 / 就绪探针 / 独立维度核对） | `daily/D21R.md` | ✅ RED 10 → GREEN 73；交叉核对 3,791 行 **0 不一致** |
| D21R2 | 二轮评审整改（**作用域语义反转** / 字面量校验 / 探针增报带维度行数 / 核对脚本分层退出码） | `daily/D21R2.md` | ✅ RED 27 → GREEN 89；兼容性核查：删字段兼容成本 = 0（仓内无生产调用方） |

---

## 5. 遗留与风险

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| R-1 | 域覆盖 11% ⇒ 域过滤使召回收窄 | ✅ D20 已量化：未归域 3,199/3,791（84%）在域过滤下 fail-closed 被排除；未归域行在域过滤下 fail-closed，**默认开关关** |
| R-2 | 无「用户 → 可见域」策略源 | 本批只提供机制；策略接线另立变更 |
| R-3 | Milvus 无自动备份 ⇒ drop 不可逆 | 回填脚本**只增不删**；v2 与 v1 并存 |
| R-4 | Milvus 批量 delete 要 release+load 才可见 | v2 写路径沿用同款处理，并在单测钉住 |
| R-5 | 新集合与生产同库同实例 | 回填脚本必须过 `scripts/milvus_write_target_guard.assertSafeMilvusTarget` |

### 5.1 D21R 评审整改新增的遗留（登记，不在本批修）

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| R-6 | 降级痕迹未透传到 API 响应（服务层只返回 hits） | fail-closed 后为空结果 + WARNING 日志；改响应契约要动前端 ⇒ 另立变更 |
| R-7 | `business_object` / `data_domain` 写入口未收 admin ⇒ 域/组织维度可被污染 | 与治理写入口收口合并（受控变更） |
| R-8 | 语义发布维度（`release_id`）恒 0 ⇒ 发布维度不可用 | 属 P3b（D22–D25）发布镜像；`releaseId <= 0` 已判非法输入（fail-closed） |
| R-9 | 就绪探针只判「有没有行」 | 未来加「按类型行数 < PG 实体数」告警 |

### 5.2 D21R2 二轮整改新增的遗留（登记，不在本批修）

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| R-10 | 过滤字面量仍无**字符白名单**（D21R2 只拒了控制字符 / 超长 / 空） | 合法中文与连字符域码不能被白名单误伤 ⇒ 观察项 |
| R-11 | 独立核对脚本**不比对 `release_id`**（本仓尚无「发布 → 向量行」接线，恒为 0） | P3b 接线后再纳入比对 |
| R-12 | 降级原因（`degradedReason`）仍只在服务层日志/返回值里，未进 API 响应 | 与 R-6 同批另立变更（要动前端契约） |

> ✅ D21R2 已就地修掉的：作用域语义反转（删 `isScopeBound`）、字面量 fail fast、
> 探针增报「带治理维度的可见行数」、核对脚本分层退出码（零行比对 = 假绿，exit 3）、
> 启动探针异步化 + 10s 超时、影子报告的「自指」告警。
