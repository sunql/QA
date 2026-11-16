# WP-ONT-AI「AI 驱动本体发现与持续演进引擎」实施计划 — A1–A4

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development
> （每个任务 RED → GREEN → REFACTOR）；实施本计划时逐任务打勾。
>
> **来源设计稿**：`docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md`（功能设计 V1.0）
> **上游基线**：P1a/P1b 语义发布（Compiler/Registry）· P2 NL2SQL 影子 · P3a RAG 过滤 ·
> P3b 图谱镜像 · P3c/P3d 钉住读侧 —— 全部已合入 main（`55fa8188`）。

**Goal:** 把「本体发现与演进」从设计稿变成可交付的代码能力：先补齐**发布面之前**缺失的
五件事（不可变元数据快照 / 候选 / 提案 / 差异与影响分析 / 语义兼容分类），再把既有
Compiler + Registry 作为**唯一发布出口**接在提案之后——保证「AI 只产出候选，人工裁决，
机器发布」三条红线不被绕过。

**Architecture:** 新增「本体发现」域（`ontology-discovery`），与既有本体治理面**并列**而非
嵌入：发现域只写自己的表（`discovery_*`），**不直接写治理表**；经「提案 + 审核」后由
适配层调用既有 `semanticReleaseService` 走 `DRAFT → VALIDATED → ACTIVE`。差异分析是
**纯函数**（manifest in / diff out，无 IO、无 DB），可在单测里逐字节对等验证。

**Tech Stack:** Python 3.12 / FastAPI / SQLAlchemy 2.x async / PostgreSQL 16 / pytest + httpx AsyncClient。
**不升级任何依赖版本**。

---

## 0. 设计稿 §10 待确认问题的现状回答

| # | 问题 | 现状（本仓代码级侦察结论） | 处置 |
|---|---|---|---|
| Q1 | 现有本体/服务/库/图谱/RAG/Agent 的代码与部署基线 | 全部就位：本体治理 CRUD（`ontology_service.py`）、发布（`semantic_release_service.py`）、图谱（`semantic_graph_mirror*`）、RAG（`semantic_registry_rag_gate.py`）、Agent（`agent_tools_semantic_gate.py`） | **已回答**（见 §A1 设计评审） |
| Q2 | Compiler/Registry 实现程度 | **已实现且完整**：Manifest v1 契约冻结 + 确定性编译 + 发布状态机 + 三层发布前校验 + 库侧双保险约束 + 审计同事务 + 四面下游消费与影子比对脚本族 | **已回答** ⇒ 缺口不在发布面，而在**发布面之前** |
| Q3 | 可授权连接范围 / 敏感字段 / 数据质量基线 | 业务决策，本仓无法回答 | **阻塞 A2/A3**（需数据 Owner 授权清单）；A1 不依赖 |
| Q4 | 供应商/物料统一标识与映射责任人 | 业务决策 | **阻塞 A4**（审核工作台的分派规则） |
| Q5 | LLM 部署方式 / 调用安全策略 / 预算 / 评测集 | 部分可答：LLM 底座齐（`infrastructure/llm/`），但**无「按用途选模型」路由**、**Prompt 版本无落库字段**、无产品级评测集 | **阻塞 A3**；A1 不依赖 |
| Q6 | 与 V0.2 WBS 的依赖/资源/排期/变更审批 | 本工作包**不在** D1–D62 排程内，属新增范围，需变更评审 | **已登记**（本计划 + 排程文档另立 WP 行，**不计入原工期**） |

> 结论：Q3/Q4/Q5 属业务与治理决策，**不阻塞 A1**（发现底座零 LLM、零外部数据授权）。
> A1 交付后 A2/A3 启动前需业务侧回 Q3；A4 前需回 Q4。

---

## Global Constraints（逐条来自仓库规则与用户指令，每个任务隐含适用）

1. **不可变数据**：`metadata_snapshot` 只增不删（内容寻址）；所有服务返回**新对象**，
   禁止原地修改入参（dict/list）。
2. **TDD**：每个任务 RED（先看它失败）→ GREEN → REFACTOR；新增代码覆盖率 ≥ 80%。
3. **真实数据库测试**：集成测试必须真实 PostgreSQL + 完整 HTTP 链路（`AsyncClient` +
   `ASGITransport`），禁止 sqlite 内存库或直接调 service。测试库白名单 `^qa_metadata_test($|_)`。
4. **不新增 integration 测试文件**：新文件会按「排序下标 mod 4」轮转 CI 分片归属 ⇒
   假 net-new。集成用例**并入既有文件**（`test_ontology_api.py` /
   `test_governance_write_route_auth.py`）；新文件只放 `unit/`（路径哈希分片，不受影响）。
5. **SQL 安全**：所有业务库探查只走 `BusinessDbAdapter.execute_read_only`
   （SQL Guard + 库侧只读事务 + 行数上限 + 超时），**不得**自建连接串。
6. **治理写入口一律 `requireAdmin`**，并登记到
   `backend/app/tests/integration/test_governance_write_route_auth.py` 的 `_WRITE_ROUTES`
   （同时扩展该守卫的路径前缀过滤）。
7. **小文件**：单文件 ≤ 800 行（宜 200–400），函数 < 50 行，嵌套 ≤ 4。
8. **显式错误处理**：每个降级点 `logger.warning(..., exc_info=True)` + 回落，不静默吞。
9. **ORM 改动必须同 PR 带 Alembic 迁移**；迁移文件名 ≤ 32 字符；启动期 schema drift 三层校验须过。
10. **CI 合并判据 = 失败集合对差净新增 0**（不是全绿）；`gh` 走
    `HTTPS_PROXY=http://127.0.0.1:12450`。
11. **不部署、不做生产写**：本工作包全程只改代码与测试；生产仍在旧 alembic 版本跑旧代码。
12. **红线（来自设计稿 §1.2，落到代码即护栏）**：
    - 不自动改 ERP/SRM/MDM 主数据；发现域**不写治理表**（只提案）。
    - LLM 不得直接写正式本体或自动激活。
    - 不把业务数据实例当本体对象类型。
    - 不凭文本相似度自动合并供应商/物料（歧义必须显式暴露）。
    - 不绕过组织权限/数据分级/脱敏/审计。

## 测试环境（每个任务的测试命令都先导出这些）

```bash
cd /private/tmp/qa-graph-mirror/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
export MILVUS_DB_NAME=qa_test
```

> 停 pytest 用 SIGINT；**不要**跑后端全量 unit 套件当门禁（27min/28 红已知基线），只跑定向文件。

---

## 批次总览

| 批次 | 范围 | 交付 | 依赖 | 状态 |
|---|---|---|---|---|
| **A1** | 发现底座 | `discovery_source` / `metadata_snapshot` 不可变快照 + **语义差异引擎**（纯函数）+ API + 真实数据脚本 | 无（零 LLM、零外部授权） | ✅ 已合 main `26059c95`（PR #137，不部署） |
| **A2a** | 列长度 / 精度进快照 | 解冻 D-20 / L-9；快照列项 +3 字段（`v1 → v2`） | 无 | ✅ 已合 main `6e5376e8`（PR #139，不部署） |
| A2 | 剖析与证据 | `profile_result` 列级 metrics + 证据引用（脱敏 + 采样策略） | Q3（采样白名单） | 待排期 |
| A3 | 候选生成 | `discovery_run` + `ontology_candidate` + LLM 结构化发现 + Prompt/模型版本落库 | A1/A2 + Q5 | 待排期 |
| A4 | 提案与发布 | `ontology_change_proposal` + `ontology_review`（四眼）+ 影响分析 + 发布适配（→ Compiler/Registry） | A3 + Q4 | 待排期 |

> 设计稿 §8 的 `WP-ONT-AI-A/B/C` 与本表对应：A1+A2 ≈ A（辅助初始化）、A3 ≈ B（差异）、
> A4 ≈ C（持续演进）。**批次顺序按依赖倒排，不按设计稿字母序。**

---

## A1：发现底座（不可变快照 + 语义差异引擎）

### 目标

交付两件**互不依赖 LLM**、可独立验收的底座：

1. **不可变元数据快照**（F02 的最小可交付形态）：把「某个授权数据源在某个时刻的
   schema 结构」冻结成**只增不改**的一行，带内容哈希，可被后续候选/剖析/提案引用。
2. **语义差异引擎**（F06 的核心）：给定两份 Manifest（基线 = 已发布快照；候选 = 当前
   治理表编译结果或未来 LLM 候选），产出**结构化差异 + 破坏性分类 + 风险分级**。

### A1 任务卡

- [x] **A1-1** 数据模型与迁移：`discovery_source` / `metadata_snapshot` 两表 + ORM
      （`backend/app/domain/discovery_models.py`）+ 迁移 `0146_discovery_snapshot.py`；
      约束：名称唯一、`schema_hash` 在源内唯一、快照行**只增不改**。
      **评审期增补**：库级不可变触发器（迁移 `0147_snapshot_immutability.py`）。
- [x] **A1-2** 快照服务（`backend/app/services/discovery_snapshot_service.py`）：
      复用 `SchemaIntrospectionService.introspect` 采集 ⇒ 规范化 JSON ⇒ SHA-256 ⇒
      **内容寻址**（同 hash 复用既有行，不新增）⇒ 落库。
      **评审期增补**：`updateSource`（撤销授权的唯一入口）、注释清洗、策略键白名单。
- [x] **A1-3** 差异引擎（`backend/app/services/semantic_diff_service.py`，**纯函数**）：
      `diffManifests(base, candidate) -> SemanticDiff`；三类分区（classes / properties /
      joins）；变更类型与严重度分类；确定性排序。
      **评审期增补**：约束**方向规则**（收紧=破坏性 / 放宽=外观级）与**覆盖度披露**。
- [x] **A1-4** API（`backend/app/api/v1/ontology_discovery.py`）：
      sources 登记/列表/更新、snapshots 采集/列表/详情、diff 查询（基线发布 vs 当前治理表）。
- [x] **A1-5** 权限接线：新写入口 `requireAdmin` + 登记进治理写入口守卫。
- [x] **A1-6** 测试：unit（新文件，路径哈希分片，安全）+ integration（并入既有文件）。
- [x] **A1-7** 真实数据脚本 `backend/scripts/ontology_discovery_realdata.py`：对本地
      MySQL 演示源采集快照 + 对 ACTIVE 发布做 diff，输出可复现证据。
- [x] **A1-8** 文档：变更记录 `summary.md` 9 段 + `daily/A1.md` 证据 + wiki 更新 +
      MEMORY 新条目与索引行。

---

## 关键设计决定（实施前先读，避免走偏）

### D-1 快照存哪：复用可变缓存 vs 新建不可变表

| 维度 | A：复用 `schema_cache` | B：新建 `metadata_snapshot`（采纳） |
|---|---|---|
| 历史 | ❌ `introspectAndCache` 内容变化即 UPDATE 覆盖（`schema_introspection_service.py:382-393`） | ✅ 只增不改，`captured_at` 可引用 |
| 证据可寻址 | ❌ hash 挂在会变的缓存行上 | ✅ `schema_hash` 是稳定标识 |
| 采样策略留痕 | ❌ 表无该字段 | ✅ `sample_policy` 列 |
| 额外成本 | 0 | 一张新表 + 一份迁移 |

**决定 B。** `schema_cache` 保持「当前态缓存」语义不动，两者职责分离（缓存 = 快，*
快照 = 证据）。

### D-2 哈希口径：复用哪把尺子

复用 `semantic_manifest_schema.manifestHash` 的规范化 JSON + SHA-256 口径
（`sort_keys=True, ensure_ascii=False, separators=(",",":"), allow_nan=False`）。
理由：① 与 Manifest 哈希同族 ⇒ 同一把尺子；② `schema_introspection_service._schemaVersion`
已经是同一算法；③ 仓库已有「规范化 JSON → SHA-256」的单点事实源，不另造。
（顺带修正 `models.py:1144` docstring 写 MD5 与实现 sha256 不一致的**文档漂移**。）

### D-3 快照内容边界：只收结构，不收样本

**只收** schema 结构（表 / 列 / 类型 / 可空 / 主键 / 外键 / 注释）。
**不收** 行级样本。理由：① 样本涉及脱敏与采样白名单，属 A2 范围（且依赖 Q3 授权）；
② 结构快照已足以支撑「候选发现 + 差异分析」；③ 收样本会把 PII 带进元数据库，
风险面远大于当前价值。**如实登记为范围限制。**

### D-4 差异引擎的比较基线：谁 vs 谁

| 候选 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A：候选 manifest vs **已发布 ACTIVE** manifest | 直接回答「发布后又被人改了什么」 | 只覆盖本体三分区 | ✅ **采纳**（A1） |
| B：候选 manifest vs **治理表当前态** | 同上但基线会漂移 | 基线不固定 | ❌（基线必须固定才能做「演进建议」） |
| C：只比 schema 快照 vs 治理表 | 覆盖列级漂移 | 不是**语义**差异（类/属性/关系层），无法直接驱动发布 | 归 A2（漂移面） |

**决定 A。** A1 的 diff 以「**已发布发布版本**」为基线（`semantic_release.manifest_json`），
候选侧取「当前治理表编译结果」（`semantic_compiler.compileManifest`）。
后续 A3 的 LLM 候选 manifest 走**同一个 diff 函数**（纯函数 ⇒ 天然可复用）。

### D-5 变更分类与破坏性判据

| 变更 | 严重度 | 理由 |
|---|---|---|
| 类/属性/JOIN **删除** | BREAKING | 下游 NL2SQL / RAG / 图谱引用即刻失效 |
| 属性 **dataType 变更** | BREAKING | 生成的 SQL 谓词与结果类型都变 |
| 属性 **isNotNull false→true** | BREAKING | 下游「该列必有值」的假设成立，收紧会打断既有查询 |
| 属性 **isNotNull true→false** | COSMETIC | 放宽约束，不破坏既有消费 |
| 属性**取值范围收紧**（抬高 minValue / 压低 maxValue / 枚举集变小 / 新增约束 / regex 变更） | BREAKING | **评审期增补**：方向规则 —— 收紧既有约束 ⇒ 打断依赖旧范围的消费 |
| 属性**取值范围放宽**（相反方向） | COSMETIC | 放宽不破坏既有消费 |
| **refClassName 变更** | BREAKING | 引用完整性口径变了 |
| **join 列变更**（source/target columns） | BREAKING | 关联键变了，历史 SQL 不再等价 |
| 类/属性/JOIN **新增** | ADDITIVE | 向后兼容 |
| 描述/别名/单位变更 | COSMETIC | 展示层 |

**不做启发式改名检测**：类改名会呈现为「删除 + 新增」，而不是「改名」。
理由：启发式改名会产生**假重命名**（两个本就该分开的东西被误判为改名），
比漏一个改名更危险 ⇒ 如实登记为 REMOVE+ADD。

**不做 semver**：仓库无 semver 约定；破坏性以「变更类型 + 严重度」表达。

### D-6 风险分级

`BREAKING → HIGH`；`ADDITIVE → LOW`；`COSMETIC → INFO`。
兼容判据 = 「是否存在 BREAKING 变更」。**不引入**「兼容百分比」这类虚构指标。

### D-7 差异引擎是纯函数，不写库

`diffManifests(base: dict, candidate: dict) -> SemanticDiff` **零 IO**：不查库、不写库、
不改入参（全程新建对象）。理由：① 可在单测里逐条断言；② A3 的 LLM 候选也能复用同一函数；
③ 差异是「派生事实」，其持久化由 A4 的提案层负责（避免「差异」和「提案」两个真相源）。

### D-8 API 形状：路径前缀与端点

前缀 `/api/v1/ontology-discovery`（设计稿 §5 的草案前缀是 `/api/ontology-discovery`，
但本仓所有 v1 路由都在 `/api/v1` 下 ⇒ 对齐本仓约定，**在计划中显式登记该偏离**）。

| 方法 | 路径 | 门禁 | 说明 |
|---|---|---|---|
| POST | `/api/v1/ontology-discovery/sources` | `requireAdmin` | 登记发现源（引用既有 `data_source`） |
| GET | `/api/v1/ontology-discovery/sources` | 登录 | 列表 |
| GET | `/api/v1/ontology-discovery/snapshots` | 登录 | 列表（可按 `sourceId` 过滤） |
| GET | `/api/v1/ontology-discovery/snapshots/{id}` | 登录 | 详情（含 manifest 内容） |
| POST | `/api/v1/ontology-discovery/snapshots` | `requireAdmin` | 采集快照（幂等：同 hash 复用） |
| GET | `/api/v1/ontology-discovery/diff` | 登录 | 基线发布 vs 当前治理表的差异 |

### D-9 `discovery_source` 与既有 `data_source` 的关系

**不新建连接**：`discovery_source` 只是「授权与访问策略」的**目录**，指向既有 `data_source.id`
（FK RESTRICT）。理由：① 连接凭据与只读护栏已在 `business_db_pool` 收口；
② 避免出现第二套数据源真相源；③ 主机白名单、加密、审计沿用既有实现。

### D-10 A1 明确**不做**的事（防范围蔓延）

不做 LLM、不做剖析、不做实体解析、不做提案/审核、不做发布适配、不做前端页面、
不覆盖 metrics/kpis/domains/businessObjects 分区的差异（⇒ 如实登记）。

---

## 评审期增补的决定（D-11 … D-15）

A1 实现完成后走了 `code-reviewer` + `security-reviewer` 两轮审查；下列决定是
**审查驱动**的增量（原 D-1…D-10 不变）：

### D-11 快照不可变由**库层**兜底

应用层「不提供 update/delete 路径」只是纪律：误操作、DBA 直连、将来新写的代码路径
都可能绕过 ⇒ 追加迁移 `0147_snapshot_immutability.py`，在 `metadata_snapshot` 上挂
`BEFORE UPDATE OR DELETE` 行级触发器（参照 `0025_audit_immutability.py`）。
**TRUNCATE 不触发行级触发器** ⇒ 测试夹具的「每测试清库」不受影响。

### D-12 访问策略**只承认已实现的键**，未知键显式拒绝

原列注释承诺了「schemas 白名单」，但从未实现 ⇒ 承诺与实现不一致。
处置：白名单收紧为 `mode`（只允许 `READ_ONLY`）+ `schema_owner`（**库模式属主**），
**未知键直接报错**（不静默忽略），策略 JSON ≤ 4096 字节。
`schema_owner` 与 `discovery_source.owner`（业务责任人）**刻意不同名**。

### D-13 「授权可撤销」需要一条**真的路径**

`status` 有 `DISABLED` 取值却没有停用入口 ⇒ 空承诺。处置：新增
`PATCH /sources/{sourceId}`（`requireAdmin`），可改 `status` / `owner` /
`access_policy` / `note`；`datasource_id` 与 `name` **不可改**（前者是授权根）。

### D-14 差异结论必须**披露覆盖度**与**截断**

「`hasBreakingChanges=false`」若不说明比较了哪些分区，会被读成「全量无破坏」。
处置：结果带 `covered_sections` / `uncovered_sections` / `partial`；`/diff` 响应
上限 500 条并回填 `truncated` / `total_change_count`（大清单下响应体可控且不隐瞒总量）。

### D-15 只读内省的**连接地址覆盖**

宿主机跑真实数据脚本时，容器内网名（`host.docker.internal`）不可解析。
处置：服务层加 `ConnectOverride`（**只改本次内省的连接地址**，不改库里的数据源
配置；不经 HTTP 暴露 ⇒ 不构成「任意主机内省」能力）。关键约束：该覆盖**同时作用
于独立重算与采集路径** —— 否则「内省用哪个库」在两面各行其是，两个哈希可能来自
两个不同的库。

### D-16 净化收敛为**单一 SSOT**（`app/domain/external_text.py`）

第二轮审查指出：发现域自己实现了一套净化（剔控制字符 / 转义 `<>`），而本仓已有一套
口径（`nl2sql_schema._sanitizeSchemaField`）。**两份口径必然漂移** ⇒ 新建共享模块
`app/domain/external_text.py` 承载全部口径，`nl2sql_schema` 委托之且**行为零变化**，
发现域用更严变体：

| 函数 | 口径 | 用途 |
|---|---|---|
| `escapeAngleBrackets` | `<`/`>` → HTML 实体 | 防闭合标签注入 |
| `collapseLineSeparators` | 行分隔符全集（含 U+2028/U+2029/U+0085/`\v`/`\f`）→ 空格 | 防「一行伪装成指令行」 |
| `sanitizeSchemaField` | 上面两步 + 剔不可见字符 | 既有 schema 口径（零行为变化） |
| `sanitizeIdentifier` | 上面两步（**不做 PII 掩码**） | 表名 / 列名 / 属主 / 类型名 |
| `maskPii` / `sanitizeExternalText` | 上面两步 + **高置信 PII 掩码** | 注释等自由文本 |

PII 掩码只收**形状几乎不会误伤业务数据**的三类：邮箱、大陆手机号、18 位身份证号。

### D-17 审计动作新增 `REUSE`（内容寻址复用 ≠ 新建）

快照内容没变 ⇒ **复用**既有行（**没有新建**）⇒ 审计若记 `CREATE`，台账会把「没发生
的事」记成发生了。处置：新增审计动作 `REUSE`。⚠️ 三处必须**同改**，否则一处不匹配就
是运行时报错或（更糟）库层拒绝写入：
`AuditService._VALID_ACTIONS` + ORM `CheckConstraint` + DB `CHECK`（迁移 `0148`）。

### D-18 端点内显式限流（`@limiter.limit` 在本仓是**死代码**）

第二轮安全审查要求「高成本端点必须限流」。实施时发现**本仓的装饰器限额不会被执行**：

1. slowapi 的装饰器自检与中间件自检都由 `limiter._auto_check` 控制，而本仓把它**关掉了**；
2. `main.py` **没有**挂 `SlowAPIMiddleware` ⇒ 中间件路径根本不存在；
3. 唯一的执行路径 `rateLimitDep` 按 **URL 路径**查 `limiter._route_limits`，而 slowapi 的
   装饰器把限额登记在 **`module.func`** 键上 ⇒ **命不中**。

⇒ 处置：`rate_limit.enforceRouteLimit(request, limitValue, scope)` 在**端点内**显式执行
一次路由级限流（复用 slowapi 的 `LimitGroup` + `__evaluate_limits`），超限抛
`RateLimitExceeded` ⇒ 走**既有** 429 处理器（与生产语义一致：429 + 标准响应信封 +
`Retry-After`）。限流桶的键是**常量作用域**（`"ontology-discovery:capture"`），
**不用 URL 路径**（挂载前缀会变 ⇒ 路径做键会静默失效）。

> **遗留登记**：全仓 `@limiter.limit` 装饰器限额（含 `datasources.py` 的 introspect 10/分钟）
> **当前均未被执行**。修它是**全仓语义变更**（会一次性激活大量此前休眠的限额）⇒ 不在 A1
> 范围内，单独立项。

### D-19 读入口门禁的**显式安全决议**（第二轮 code-reviewer HIGH-3）

第二轮 code-reviewer 提出：发现域的读端点把**业务库 schema** 暴露给所有登录用户，
与「治理 IP 收 admin」的先例冲突，要求收 admin。**事实核查后不盲改**：

- 本仓既有 `GET /datasources/{id}/schema` 与 `/ontology-drift` **同样**把业务库 schema
  开放给登录用户 ⇒ 该先例并不存在「自相矛盾」，而是**两套并存**的先例
  （`semantic_releases` 收 admin；`datasources` schema 开放登录）；
- 本域的**写面**已收 admin；读面是「已授权发现的结构证据」，与「任意业务库 schema」不同。

⇒ 处置：**登记显式安全决议**——读入口维持「登录可见」，依赖既有生产硬闸
（`validateProduction()`：生产 + stub 鉴权 ⇒ 进程起不来）；「读入口 admin 化」作为
**可选方案**登记，待产品决策。**不盲改门禁**（盲改会把本批扩成一次权限模型变更）。

### D-20 列长度 / 精度不入快照 ⇒ **延后 A2**

第二轮 code-reviewer MEDIUM：快照只收「列名 / 类型 / 可空」，**不收长度 / 精度**
⇒ 类型 `NUMBER(10,2)` 与 `NUMBER(10,4)` 哈希相同 ⇒ 内容寻址漏判「精度变了」。
**不在 A1 修**：要改**共享** `ColumnSchemaRead` + 内省 SQL（12 方言）⇒ 口径升级风险
远大于本批收益，且列长度/精度属 A2「列级剖析」的范围。⇒ **延后 A2**，在此登记。
> **已解冻（2026-11-16）**：**A2a 增量**已把列精度 / 长度采进快照（快照版本 `v1 → v2`）。
> ⚠️ 本段「12 方言」的措辞**不准确**：schema 内省只覆盖 **3 种**业务库类型
> （mysql / postgresql / oracle）；12 个方言是 NL2SQL **SQL 生成**用的注册表。
> 证据见 [`daily/A2a.md`](./daily/A2a.md)。剩余缺口：Oracle 真实库取证（L-14）、
> MySQL `unsigned`（L-15）、时间列小数秒精度（L-16）。

### D-21 「不可擦除」是证据日志的**设计属性**，不是缺陷

第二轮安全审查 M-5：快照只增不改 + 全员可读 + 外部不可信内容 ⇒ 一旦把敏感注释写进
快照，**无法擦除**（只能追加新行）。⇒ 处置：**两半分开处理**——
① **PII** ⇒ 实现高置信掩码（D-16），源头拦掉；
② **「不可擦除」** ⇒ 视为**证据日志的设计属性**：更正的方式是**追加新行**，
不是原地删除（与本仓 `audit_log` 不可变触发器同构）。⇒ 不引入「原地 UPDATE 覆盖」
这条会破坏内容寻址的路径。
