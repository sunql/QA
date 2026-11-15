# 变更：P3a RAG 过滤（语义发布 Registry 的向量召回可过滤化）

- **日期**：2026-11-15
- **作者**：Claude（with user direction）
- **Phase**：1109 WP-ONT-AI · P3a（D18–D21）+ **D21R 评审整改轮**
- **状态**：**代码完成、可合**；**CI 失败集合净新增 0**；**生产不部署**
- **分支**：`feat/semantic-registry-rag`（worktree `/private/tmp/qa-rag-p3a`），base = `origin/main` `b6af98a1`
- **迁移版本**：**无**（本批**零 DDL**：不改 PG 表结构、不加列、不改索引）
- **上位计划**：[plan.md](./plan.md)（续接 `docs/update/1109/智能采购平台升级计划_日级排程_1109.md` §6）
- **日常证据**：[D18](./daily/D18.md) · [D19](./daily/D19.md) · [D20](./daily/D20.md) · [D21](./daily/D21.md) · **[D21R](./daily/D21R.md)**（评审整改）

> **零部署**：开关 `ENABLE_SEMANTIC_REGISTRY_RAG` **默认关**，关态读路径逐字节等于今天
> （读 v1、不碰 v2、不过滤；写路径同理不写 v2）⇒ 合并后不触发发版。

---

## 1. 需求

P1a/P1b/P2 已把「语义发布」做成自包含快照并接进 NL2SQL 的 schema 渲染，
但**向量召回这一侧完全无法过滤**：本体向量集合里没有域 / 实体 / 组织 / 有效期任何维度，
任何召回都只能全量返回。P3a 补上这一段：

1. **schema**：新建 4 个 v2 集合，在 v1 字段之上加 5 个治理维度标量（D18）；
2. **机制**：过滤表达式 + 读路径闸门 + 写路径 v1/v2 双写 + 一键回退（D19）；
3. **回填与影子评估**：全量回填 + 「差异可解释 / 无未授权泄漏」的比对报告（D20）；
4. **收口**：回归对差 + 文档 + PR（D21）。

---

## 2. 落地（四个日次）

| 日次 | 提交 | 落点 | 结论 |
|---|---|---|---|
| D18 | `9a3ee66e` | v2 集合 schema（5 个过滤字段）+ 四步方案 + Milvus 2.4.6 表达式实测 | schema 定稿 |
| D19 | `fcc0707d` | 过滤维度对象 / 纯函数表达式 / 读路径唯一出口闸门 / 写路径双写 / 治理维度解析 | 双向用例通过 |
| D20 | `90652543` | 回填脚本 + 影子比对脚本 + 报告 | 泄漏 0、漏召回 0、多召回 0 |
| D21 | 见 PR | 回归对差 + 文档 | 净新增失败 0 |
| D21R | 见 PR | **评审整改**：fail-closed 口径补全 + 就绪探针 + 影子脚本委托生产构造器 + 死代码清理 + `--limit` 修复 + **独立维度交叉核对** | RED 10 → GREEN 73；交叉核对 3,791 行 **0 不一致** |

### 2.1 关键设计

- **闸门是唯一出口**：`semantic_registry_rag_gate.searchRegistryEmbeddings` ——
  `ontology_service.searchByKeyword/searchByEmbedding` 都走它 ⇒ 过滤口径只有一处。
- **有作用域一律 fail-closed**（D21R 补全为**四种**情形）：
  ① v2 缺失；② v2 检索异常；③ 过滤表达式非法（含 `releaseId <= 0` 这种非法发布版本）；
  ④ **开关关**（这条路径没有能力施加收窄）；⑤ **作用域已声明但取值为空**（deny-all）。
  五种都**返回空结果**，**绝不回落成无过滤的 v1**（回落即泄漏）；**无作用域**才回落 v1。
- **「声明了作用域」与「没有作用域」必须能区分**：新增 `RegistryFilter.isScopeBound` ——
  「我这一轮有权看的域是空集」= **什么都不许看到**，不是「随便看」。
- **维度解析不猜**：查不到治理事实 ⇒ 写空维度（显式「未归域」）⇒ 在域过滤下被排除，
  行为可解释；写路径**不原地改调用方的 record**（返回新行）。
- **不重算向量**：回填直接搬 v1 现有 embedding —— 一旦重算，影子比对的差异会混入向量漂移，
  差异就不可解释了。
- **Milvus 2.4.6 实测约束**（本地 2.4.6 锁死）：ARRAY 字段上 `in` / `like` **报错** ⇒
  多值维度只能用 `array_contains(...) or ...`；`not array_contains` **命中空数组行** ⇒
  排除语义必须显式枚举允许值。
- **影子/核对脚本不自己写表达式**：一律委托生产构造器 `buildRegistryFilterExpr` ——
  自建的那份不认识字面量转义与哨兵语义 ⇒ 验证的就不是线上路径 ⇒ 证据失效。

---

## 3. 数据与安全

| 项 | 结论 |
|---|---|
| DDL | **零**（不动 PG 表/列/索引） |
| Milvus 写入 | 只写**新建的 v2 集合**；v1 一个字节不动；回填脚本过 `milvus_write_target_guard.assertSafeMilvusTarget`（写 `default` 库必须显式 `--allow-prod`） |
| 幂等 | 先删后插 ⇒ 重跑不留重复行（**判据用可见去重行数**，禁用 `num_entities` —— 那是含墓碑的物理行数） |
| 授权 | **本批不发明「用户 → 可见域」策略**：域过滤是「收窄候选集」，不是授权；授权在 PG 侧 ACL |
| 生产 | 本批**未触碰生产**（v2 只落在本地隔离实验库 `qa_registry_v2`，v1 只读 `default`） |

---

## 4. 验证证据

### 4.1 D20 影子召回比对（报告：`reports/D20-shadow-report.md`）

| 范围 | 查询数 | 真值行数 | 过滤后命中 | v2漏召回 | v2多召回 | 泄漏 |
|---|---|---|---|---|---|---|
| domain `PROCUREMENT` | 20 | 50 | 50 | 0 | 0 | **0** |
| org `procurement` | 20 | 50 | 50 | 0 | 0 | **0** |
| entity `AR`/`GR`/`INVOICE`/`MATERIAL`/`PO`/`SUPPLIER` | 各 16 | 各 26 | 各 26 | 0 | 0 | **0** |

- 回填可见 **3,791** 行（class 37 / property 3,730 / metric 12 / kpi 12），**< 7,100**；
- 维度覆盖：已归域 **592** / 未归域 **3,199（84%）** / 已挂组织 737 / 已挂实体 592；
- **判据修正（关键）**：**不能拿 v1 的召回当基准** —— v1 是 HNSW 近似检索，
  且同一段文本被写进多个 `ontology_id`（实测 **698 组向量逐字节相同**）⇒
  基线近似误差比过滤影响还大（实测连 d=0.0 的「自己」都漏）。
  故基准改为 numpy 暴力 L2 的**精确真值** ⇒ 反直觉结论：**带过滤的检索比不过滤更准**
  （候选集收窄 ⇒ ANN 退化为对收窄集合的精确扫描）。

### 4.2 D21 回归对差（基线 = `b6af98a1`，独立 worktree + 独立 venv）

| 套件 | 基线 | 分支 | 净新增失败 |
|---|---|---|---|
| `unit -k "milvus or ontology or kpi_vector or knowledge_compil or registry"` | 370 passed / 0 failed | **462 passed / 0 failed** | **0** |
| `integration/test_search_type_routed.py` | 3 passed | 5 passed | **0** |
| `integration/test_ontology_api.py` + `test_ontology_reconcile_sync.py` | 12 failed / 63 passed | 12 failed / 63 passed | **0** |
| `unit/test_chat_service.py` | 5 failed / 156 passed | 5 failed / 156 passed | **0** |

既红基线（embedding 缺密钥、提示词 JSON 解析）在基线与分支上**逐条同名** ⇒ 净新增 0。

### 4.3 D21R 独立维度交叉核对（补「真值自指」的缺口）

D20 的「泄漏 0」若只在 v2 行内部自洽 ⇒ 可能是**假绿**。新增
`scripts/verify_registry_v2_dimensions.py`：**独立 SQL 路径**从 PG 治理表重新推导
期望维度，与 v2 行实际存量逐行比对（只读，实测于本地隔离实验库）：

| 项 | 值 |
|---|---|
| v2 行数 / 比对行数 | **3,791 / 3,791** |
| **不一致行** | **0** |
| 孤儿行 | 0 |
| PG 有实体但无向量行 | 18（正常） |

### 4.4 D21R 回归（评审整改后）

| 套件 | 结果 |
|---|---|
| `unit -k "milvus or ontology or kpi_vector or knowledge_compil or registry"` | 见 `daily/D21R.md`（整改后复跑） |
| 新增纯逻辑用例 | 闸门/表达式/作用域/就绪探针/独立核对：**73 + 6 + 17 全绿** |

---

## 5. 遗留与后续

| ID | 遗留 | 处置 |
|---|---|---|
| L-1 | 域覆盖仅 16%（592/3,791）⇒ 域过滤会让召回显著收窄 | **默认关**；生产开闸前需先补治理维度（治理动作，非本批） |
| L-2 | 无「用户 → 可见域」策略源 | 本批只提供机制；策略接线另立变更 |
| L-3 | Milvus 无自动备份 ⇒ drop 不可逆 | 回填**只增不删**；v2 与 v1 并存；不 compact |
| L-4 | 生产回填需当日 `pg_dump` + 显式 `--allow-prod` | 列入生产落地前置条件 |
| L-5 | v2 物理行 14,920 vs 可见 3,730（墓碑待回收） | 评估 compact 时机（生产落地前） |
| L-6 | 降级痕迹（`degradedReason`）**未透传到 API 响应**（服务层只返回 hits） | fail-closed 之后 = 空结果 + WARNING 日志（不构成越权）；改响应契约要动前端 ⇒ 另立变更 |
| L-7 | `business_object` / `data_domain` 写入口**未收 admin** ⇒ 域/组织维度可被污染 | 与治理写入口收口合并（受控变更） |
| L-8 | 语义发布维度（`release_id`）当前**恒 0** ⇒ 发布维度事实上不可用 | 属 **P3b（D22–D25）** 发布镜像；接线后再放开 `releaseId` 过滤 |
| L-9 | 就绪探针只判「有没有行」，判不出「行数是否少于预期」 | 未来可加「按类型行数 < PG 实体数」告警 |

**续接**：1109 排程 **P3b（D22–D25）** —— Neo4j 发布镜像。
