# 对《采购本体模型与语义编译器详细设计 V0.1（1109）》的完成度对照

> 核对时间：**2026-11-22** ｜ 基线 `main@9662aa6b` ｜ 迁移 head **0165**
> 设计稿：740 段 / 12 章（现状评估、领域模型、数据模型、源映射、编译器、运行时、API、前端、时序、权限、验收、风险）
> 判定口径：✅ 完成 ／ ◐ 部分 ／ ✗ 未做 ／ ⤳ 走已批准偏差（D1–D3）

## 总体结论

**约 80%**。骨架（发布链、源映射、编译器、Manifest、Registry、运行时消费、API）
**全部落地并有真实数据验证**；缺口集中在两处：

1. **发布校验规则目录**：设计列了 12 个校验码（CLS/PRP/REL/JOIN/KEY/MET/RAG/AGT/GRF），
   实现是**另一套 10 个自研码**，语义只覆盖其中一部分；
2. **前端发布管理页 ✗**：只能通过 API/脚本发布，界面上没有。

---

## 逐章对照

### §1 现状评估与升级结论 ✅
内容已在本仓 M0/M1 的分析与核对中落实（`Harness/changes/2026-11-18-procurement-m0-*`、
`2026-11-19-m1-procurement-object-reconciliation`），十对象逐条处置已留档。

### §2 采购本体领域模型 ◐ 60%

| 设计 | 现状 |
|---|---|
| 2.1 采购本体分层 | ⤳ 复用现有六层（D1），无独立采购层 |
| 2.2 首期采购核心类 | ◐ 库内 **77 个本体类**、**10 条类级关系**、**90 条 JOIN**；但设计列的"首期核心类"未逐条对照勾选 |
| 2.3 采购核心关系定义 | ◐ `ontology_relation` 在用；设计给的关系清单未逐条核对 |
| 2.4 关系必须分三类 | ✅ 已分开：语义关系 `ontology_relation` / 物理 JOIN `ontology_join` / 实例映射 `entity_mapping` |

### §3 现有模型复用与新增数据模型 ◐ 4/6 张表

| 设计表 | 实现 | 说明 |
|---|---|---|
| `procurement_ontology_release` | ⤳ **`semantic_release`**(0142) | 改名并入语义发布家族；`release_code`→`version`，**唯一性是"域内唯一"**（0157） |
| `procurement_ontology_release_check` | ⤳ **`semantic_release_check`**(0155) | 逐项落表（PASS 也落）+ 覆盖写 + 截断合成行 |
| `procurement_source_binding` | ✅ (0154) | 类→物理源表/视图 + 过滤表达式/允许清单 + 确认流 |
| `procurement_property_binding` | ✅ (0154) | 属性→列/受控表达式 |
| `procurement_key_binding` | ⤳ **不建**（D2） | 统一键走 `entity_mapping`，避免第二套键口径 |
| `procurement_agent_run_context` | ⤳ **不建**（D2） | 运行上下文由 `agent_orchestration_run` 三列承载（0145：release_id/version/manifest_hash） |

### §4 源数据映射详细契约 ✅ 约 90%

- 已实现：`filter_expression` / `filter_allowlist`、候选→确认流（`is_candidate` + `confirmed_by/time`）、
  有效期墓碑（`valid_from/valid_to`）、属性级绑定（列/表达式至少一项）
- ⤳ 偏差：设计的状态机 DRAFT/ACTIVE/DISABLED → 实现用「候选 + 确认 + 有效期」；
  设计要求的显式 `mapping_version` 列未建（版本由 release 的 manifest_hash 承担）

### §5 语义编译器 ◐ 约 75%

| 设计项 | 现状 |
|---|---|
| 5.1–5.3 编译器定位/模块/流程 | ✅ `semantic_compiler` + `semantic_domain_slice`；草稿→校验→编译→发布→激活→回滚全链 |
| 5.4 错误级别 ERROR/WARNING/INFO | ✅ 实现为 severity×result 正交落表 |
| **5.5 校验规则目录（12 码）** | ◐ **见下表，覆盖约一半** |
| 5.6 Manifest 结构 | ✅ v2（`schemaVersion` 常量冻结） |
| 5.7 版本兼容规则 | ✅ v2 分发 + 旧 v1 夹具判定（`manifest-v2-version-dispatch`） |

**校验规则覆盖**（设计 12 码 → 实现 10 码）：

| 设计码 | 实现 | 覆盖 |
|---|---|---|
| CLS-001/002 类与业务对象 | — | ✗ |
| PRP-001/002 属性与绑定 | `SOURCE_BINDING_PROPERTY_UNBOUND`(W) | ◐ 只覆盖"属性未绑定" |
| REL-001 类关系 | — | ✗（关系白名单在代码层校验，不在发布校验里） |
| JOIN-001 JOIN 合法性 | `SOURCE_BINDING_JOIN_MISSING`(E) | ◐ 只覆盖"缺 JOIN 绑定" |
| KEY-001 实体键 | — | ⤳ D2（key_binding 不建） |
| MET-001 指标可算 | `METRIC_SOURCE_PRESENT` / `METRIC_NUMERATOR_LANDING` / `METRIC_DENOMINATOR_LANDING` | ✅ 三码 |
| MET-002 KPI 绑定数据源 | — | ✗（在 KPI 发布闸门另处，未进本目录） |
| RAG-001 检索过滤字段 | — | ✗ |
| AGT-001 Agent 域/层/工具 | — | ✗（在 Agent 配置校验别处） |
| GRF-001 图 label/关系白名单 | — | ✗（代码级白名单守卫，运行期 fail-closed） |
| — | `SNAPSHOT_INTEGRITY` / `MANIFEST_SCHEMA` / `DOMAIN_SLICE_CROSS_DOMAIN` / `CHECKS_TRUNCATED` | 实现新增 |

### §6 运行时消费契约 ✅ 约 95%

| 设计 | 现状 |
|---|---|
| 6.1 Semantic Registry 接口 | ✅ `getActive/getById/listReleases/count/invalidate` + 缓存 |
| 6.2 Knowledge Facade 消费 | ✅ 4 通道知识门面（含 registry 过滤） |
| 6.3 NL2SQL 集成 | ✅ 计划/SQL 两阶段**钉同一版**（D15–D17）；`directOnly` 降级路径 |
| 6.4 Neo4j 集成 | ✅ 本体图/业务图隔离 + label/relType 白名单 |
| 6.5 Milvus/RAG 集成 | ✅ Registry v2 集合（`*_embeddings_v2`）+ 治理维度过滤 |

### §7 API 接口契约 ✅ 约 90%

- 发布管理：`/semantic/releases`（active / 详情 / checks / validate / activate / archive / rollback）— 7 个 ✅
- 映射配置：`/procurement/bindings`（effective / 详情 / confirm / expire）— ✅
- 运行时：registry active ✅

### §8 前端模块与页面 ◐ 约 40%

- 源绑定配置页 ✅（`ProcurementBindingsPage`：列表 / 向导 / 确认 / 失效）
- **发布管理页 ✗**：没有对应的前端页面，发布/激活/回滚只能走 API 或脚本

### §9 关键时序 ✅

四条全通：草稿→发布激活、问题→图谱/SQL/RAG、Agent run 固定语义版本
（run 三列钉住）、发布失败回滚（rollback 端点 + 0160→0158→0160 往返演练过）。

### §10 权限与治理 ✅

发布面 admin-only；绑定写入走确认流 + 审计；SQL Guard + 只读账号 + 库侧只读三层。

### §11 开发实施拆分与验收 ◐

工程侧已完成；真实数据验收依赖生产（Oracle 实跑、生产量级数据），与 V0.3 主报告的未完成项同源。

---

## 与主报告（V0.3）的关系

本设计稿是 V0.3 的 **I1（本体语义发布）** 的实现说明书。V0.3 报告里 I1 判 100%，
是按"能力面"判的；本报告按**本稿自己的 12 章契约**逐条判，所以更严——差异主要在：

1. **校验规则目录**：V0.3 只要求"check 能跑"，本稿要求 12 个具体校验码；
2. **前端页面**：V0.3 只要求"前端 4 页"，本稿 §8 要求发布管理 UI。

## 建议补齐顺序（按性价比）

| # | 补什么 | 为什么 |
|---|---|---|
| 1 | **发布管理前端页**（列表/校验详情/激活/回滚） | 现在发布只能靠脚本，运维风险高；纯前端工作 |
| 2 | **校验规则补 6 码**：CLS-001/002、REL-001、KEY-001(→D2 说明)、AGT-001、GRF-001 | 都是读现表就能判，不需新表 |
| 3 | MET-002 KPI 绑定数据源并入本目录 | 现在在别处，发布时看不到 |
| 4 | RAG-001 检索过滤字段 | 依赖文档语料（与 Q04 同阻塞） |
| 5 | §2.2 首期核心类逐条勾选核对 | 一页核对表，确认"该有的类都在" |
