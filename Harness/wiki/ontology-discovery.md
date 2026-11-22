# 本体发现与差异引擎（WP-ONT-AI）

**日期**：2026-11-16 · **变更**：[2026-11-16-ontology-discovery-ai](../changes/2026-11-16-ontology-discovery-ai/summary.md)
**批次**：A1「发现底座」—— 零 LLM、零外部授权；**A2a**「列长度 / 精度进快照」（L-9 解冻）。

## 一句话

把「某个授权数据源在某一刻的 schema 结构」冻结成**只增不改**的证据行，并把
「已发布的本体」与「当前治理表」的差别算成**结构化差异 + 破坏性分类 + 风险分级**。

## 为什么需要它

语义发布把「某一刻的本体」冻结成 Manifest 快照，但**本体治理表本身是原地 UPDATE 的**
⇒ 发布之后本体被谁改了什么、改动会不会打破下游 NL2SQL / RAG / 图谱消费方，**事后答不上来**。

A1 补的正是「发布面之前」缺的两件底座：

1. **不可变元数据快照**（结构层证据）；
2. **语义差异引擎**（发布后的演进差异分析）。

## 三条不可破的边界

| 边界 | 落地方式 |
|---|---|
| 发现域**不写治理表、不写业务库** | 写面只有 `discovery_source` / `metadata_snapshot` 两张新表；业务库访问**只经** `SchemaIntrospectionService.introspect` → `BusinessDbAdapter.execute_read_only`（SQL Guard + 库侧只读事务） |
| 快照**只增不改** | 应用层无 update/delete 路径 + **库层触发器** `tr_metadata_snapshot_immutable`（`BEFORE UPDATE OR DELETE`，迁移 0147）双保险 |
| 差异引擎是**纯函数** | `diffManifests(base, candidate) -> SemanticDiff`：零 IO、零 DB、不改入参、确定性排序 ⇒ A3 的 LLM 候选 manifest 走**同一个函数** |

> 库层触发器只拦 `UPDATE` / `DELETE`；`TRUNCATE` **不触发行级触发器** ⇒ 测试清库不受影响
> （这是刻意取舍，不是漏洞：清库是整表级运维动作，不在「逐行篡改证据」的风险面内）。

## 快照：内容寻址

```
内省（只读）→ 规范化 JSON → SHA-256 → content hash
                                    ↓
        同源同内容 ⇒ 复用既有行（reused=true，幂等）
        内容变化   ⇒ 新增一行（旧行逐字不变）
```

- **同一把尺子**：`schema_hash` 复用 `app/domain/semantic_manifest_schema.manifestHash`
  （规范化 JSON + SHA-256）⇒ 与 Manifest 哈希同口径、可互相比对。
- **规范化**（决定哈希稳定性的关键）：表按 `(owner, tableName)` 排序、列按列名排序、
  外键按 `(columnName, refTable, refColumn)` 排序；**主键保留原序**（复合主键的列序本身有语义）。
- **只收结构，不收样本**：表 / 列 / 类型 / 可空 / 主键 / 外键 / 注释。行级样本属 A2
  （依赖 Q3 采样授权白名单）⇒ 不把 PII 带进元数据库。
- **注释是外部不可信文本**：业务库注释由低权限账号可写 ⇒ 入库前净化。
- **列精度 / 长度进快照（A2a，L-9 解冻）**：快照列项除 `dataType`（**只有类型名**）外，
  另有 `numericPrecision` / `numericScale` / `characterMaxLength` 三个**独立**字段 ⇒
  `NUMBER(10,2)` 与 `NUMBER(10,4)`、`VARCHAR(10)` 与 `VARCHAR(50)` **哈希不同**
  ⇒ 发现引擎看得见「精度 / 长度变了」。未知一律 `null`（**不用 0 兜底**）。
  采集口径与落地：

  | 业务库类型 | 原生列 | 统一别名（`_mergeRows` 的取键口径） |
  |---|---|---|
  | Oracle（`ALL_TAB_COLUMNS`） | `DATA_PRECISION` / `DATA_SCALE` / `CHAR_LENGTH`（**字符数**，与 PG/MySQL 同量纲） | `numeric_precision` / `numeric_scale` / `character_max_length` |
  | PostgreSQL / MySQL（`information_schema.columns`） | `NUMERIC_PRECISION` / `NUMERIC_SCALE` / `CHARACTER_MAXIMUM_LENGTH` | 同上 |

  - 快照结构版本 `schemaVersion` 随之上 `v1 → v2` ⇒ **旧 v1 快照与新快照不可比**
    （同一 schema 跨版本采集会新增一行快照）——**预期行为**，版本号就是用来显式记录形状变更的。
    ⚠️ **读侧必须按 `schemaVersion` 分流**：v1 行没有精度三键，与 v2 行直接比对会得出「精度全部新增」的假差异。
  - 精度**不进 NL2SQL prompt**（`nl2sql_schema` 只用 `dataType`）⇒ prompt 文本零变化。
    逐路径已核实：导入增强、数据质量规则生成、NL2SQL 三条路径都不读这三个字段。
  - 内省**只覆盖 3 种业务库类型**（mysql / postgresql / oracle）⇒ 方言注册表的 12 个方言
    与 schema 内省无关。
  - **方言不在快照 JSON 里**：权威来源是 `metadata_snapshot.source_id → discovery_source.datasource_id
    → data_source.type`；而 `schema_hash` 的唯一约束是 `(source_id, schema_hash)`
    ⇒ 哈希**本来就只在源内可比**。读 JSON 的人必须顺着 `source_id` 查方言。
  - ⚠️ **各方言口径不同（刻意不归一，本地实测）**：

    | 列定义 | PostgreSQL | MySQL |
    |---|---|---|
    | `int` / `bigint` | `32/0` / `64/0` | `10/0` / `19/0`（**显示宽度**） |
    | `decimal`（未声明） | `null/null` | `10/0` |
    | `decimal(12,4)` | `12/4` | `12/4` |
    | `varchar(50)` | `50` | `50` |
    | `text` | `null` | **`65535`**（引擎上限，不是声明长度） |

    ⇒ 副作用是**整数列改宽也会被看见**（正是想要的效果）；但「同一个 bigint 主键」在 PG 记 64、
    MySQL 记 19 ⇒ **跨方言比较这三个数字毫无意义**（快照按源分源，本就不是这个场景）。
  - **已知缺口（登记在案）**：MySQL `INT` 与 `INT UNSIGNED` 三字段完全相同 ⇒ 同哈希（符号性在
    `COLUMN_TYPE`）；时间列小数秒精度（`datetime(3)` vs `datetime(6)`）在独立的 `datetime_precision`
    列，**未采** ⇒ 看不见；Oracle 分支未做真实库取证。
  - **最低支持版本**：Oracle 8i+ / PG 9.0+ / MySQL 5.7+ / MariaDB 10.0+。三列是 `SELECT` 列表成员
    ⇒ 老版本 / 裁剪版库缺列会让**整条内省失败**（不是只丢精度）；接这类库前必须先验列存在。
  - **披露决策（显式记录，不是副产品）**：列宽 / 精度属**结构元数据**，判定为**可公开**；
    已知边际推断是定长敏感列的格式长度（如手机列 `maxLength=20`），价值极低且被既有的
    「列名 + 类型 + 注释 + 主外键图」暴露严格支配。**要收紧就收读门禁**（`GET /snapshots/{id}`
    现在登录可见），不要在采集层过滤——快照不可擦除，过滤对已入库行无效。

## 外部文本净化（SSOT：`app/domain/external_text.py`）

**全部**外部来源文本（库 / 表 / 列注释、标识符等）入库前一律走**单一净化入口**，
不自抄第二套正则：

| 函数 | 作用 |
|---|---|
| `escapeAngleBrackets` | 转义 `<` / `>`（防 HTML / 标签注入） |
| `collapseLineSeparators` | 折叠 Unicode 行分隔符全集（U+2028 / U+2029 / U+0085 / U+000B / U+000C …）与零宽、双向字符 |
| `sanitizeSchemaField` / `sanitizeIdentifier` | 字段名 / 标识符净化 |
| `maskPii` | 掩码邮箱 / 大陆手机号 / 18 位身份证号 |
| `sanitizeExternalText` | 组合入口 |

- 净化在**入库前**做 ⇒ **源头掩码**，不是事后擦除（PII 一旦落库就不可擦除，见下）。
- ⚠️ 净化会**改变被哈希的内容**：内容寻址以**入库口径**（净化后）为准 ⇒ 只差控制字符的
  注释**不算**内容变更。**这是设计，不是 bug**（记为 L-11）。

## 不可擦除是设计属性

快照一旦落库**不可 UPDATE / DELETE** ⇒ 敏感内容只能**追加新行**更正（与 `audit_log`
不可变同构）。因此 PII 必须在**源头**掩码（D-16 净化 SSOT），而不是指望事后清理。

## 差异引擎：判据与分级

**破坏性判据（D-5，显式表，不做启发式改名检测）**

| 变更 | 严重度 |
|---|---|
| 类 / 属性 / JOIN **删除** | BREAKING |
| 属性 **dataType 变更** | BREAKING |
| 属性 **isNotNull false→true**（收紧） | BREAKING |
| 属性 **isNotNull true→false**（放宽） | COSMETIC |
| **refClassName 变更** | BREAKING |
| **join 列变更** | BREAKING |
| 约束**收紧**（抬高 minValue / 压低 maxValue / 枚举集变小 / 新增约束 / regex 变更） | BREAKING |
| 约束**放宽**（相反方向） | COSMETIC |
| 类 / 属性 / JOIN **新增** | ADDITIVE |
| 描述 / 别名 / 单位变更 | COSMETIC |

- **不做启发式改名检测**：改名会呈现为「删除 + 新增」；启发式改名会产生**假重命名**
  （两个本就该分开的东西被并到一起），比漏一个改名更危险 ⇒ 如实登记为 REMOVE + ADD。
- **风险分级（D-6）**：`BREAKING→HIGH` / `ADDITIVE→LOW` / `COSMETIC→INFO`；整体取最高。
  **不引入「兼容百分比」这类虚构指标**。
- **覆盖度必须披露**：当前只比 **classes / properties / joins** 三分区 ⇒ 结果里带
  `covered_sections` / `uncovered_sections` / `partial`，「无破坏性变更」只在该披露范围内成立。
  > 响应序列化后为 **camelCase**（`CamelModel` 的 `alias_generator=to_camel`）⇒
  > 实际字段名是 `coveredSections` / `uncoveredSections` / `partial`。
- **截断必须披露且破坏性优先**：变更条数超上限时按 **破坏性优先**排序截断（破坏性变更
  排在前面，不会被增量变更挤掉），并如实报告 `truncated` / `totalChangeCount` /
  `changeCount` / `droppedBreakingCount` / `breakingCount` ⇒ 调用方能区分
  「真的没有破坏性变更」与「有但被截断没看全」。

## 访问策略契约

`discovery_source.access_policy` 是 JSON 对象，**只承认已实现的键**：

| 键 | 含义 |
|---|---|
| `mode` | 访问模式，当前只允许 `READ_ONLY`（发现域只读是红线） |
| `schema_owner` | **库模式属主**，内省时的目标命名空间 —— **仅 Oracle 有意义** |

- **未知键直接拒绝**（不静默忽略）——「我配了白名单、以为生效了其实没生效」是最危险的失败模式。
- **`schema_owner` 仅允许 Oracle 数据源**（PG / MySQL 带上它 ⇒ **拒绝登记**）。
  Oracle 是 owner 命名空间；PG / MySQL 配了它属于「配了但没生效」⇒ 同属危险失败模式。
- 策略 JSON ≤ 4096 字节。
- ⚠️ `schema_owner`（库模式属主）与 `discovery_source.owner`（**业务责任人**）是两件事，
  刻意不同名，代码里不可混用。

## API（`/api/v1/ontology-discovery`，7 个端点）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/sources` | **admin** | 登记发现源（数据源引用 + 访问策略） |
| PATCH | `/sources/{sourceId}` | **admin** | 停用 / 改策略（**禁改 `datasource_id`**） |
| GET | `/sources` | 登录 | 分页列出发现源 |
| GET | `/snapshots` | 登录 | 分页列出快照 |
| GET | `/snapshots/{snapshotId}` | 登录 | 快照详情 |
| POST | `/snapshots` | **admin** | 采集（内省 ⇒ 净化 ⇒ 规范化 ⇒ 哈希 ⇒ 落库 / 复用） |
| GET | `/diff` | 登录 | 差异（基线 = 已发布 ACTIVE，候选 = 治理表编译结果） |

- 全部响应带 `Cache-Control: no-store`。
- 写面 = `requireAdmin`；读面 = 登录可见。**读入口门禁**是**显式安全决议**（D-19）：
  既有 `GET /datasources/{id}/schema`、`/ontology-drift` 同样开放登录 ⇒ 先例两套并存，
  本批**不盲改**；「读入口 admin 化」登记为可选方案，待产品决策（L-8）。
- 写端点**落审计**。

## 审计动作：新增 `REUSE`

内容寻址下「同源同内容 ⇒ 复用既有行」**没有创建任何东西** ⇒ 记 `CREATE` 会**虚增台账**。
故审计白名单新增 `REUSE`，**三处必须同改**：

| 位置 | 内容 |
|---|---|
| `AuditService._VALID_ACTIONS` | 服务层白名单 |
| ORM `CheckConstraint` | `ck_audit_log_action` |
| DB CHECK | 同名约束（迁移 **0148**） |

> 改一处不改其余 ⇒ 一边放行一边报错（或反过来）。三处同改是硬约束。

## 限流：端点内显式执行（D-18）

**本仓的 `@limiter.limit` 装饰器限额是死代码**（已核查 slowapi 源码 + 本仓装配）：

1. `limiter._auto_check = False` ⇒ 装饰器自检与中间件自检都关；
2. `main.py` **未挂** `SlowAPIMiddleware`；
3. 唯一执行路径 `rateLimitDep` 按 **URL 路径**查 `limiter._route_limits`，
   而 slowapi 把装饰器限额登记在 **`module.func`** 键上 ⇒ **命不中**。

⇒ **全仓**装饰器限额（含 `datasources.py` 的 introspect 10/分钟）**均未被执行**。

本域的做法：`app/infrastructure/rate_limit.py::enforceRouteLimit(request, limitValue, scope)`
—— 端点内用 `slowapi.wrappers.LimitGroup` + `limiter._Limiter__evaluate_limits` 显式执行，
桶键 = (key_func 结果, scope)，`scope` 是**常量**（如 `"ontology-discovery:capture"`），**不是 URL 路径**
⇒ 绕开了上面的键空间错配。

> 全仓装饰器限流的修复是**全仓语义变更**（会一次性激活大量休眠限额）⇒ **单独立项**（L-7），
> 不在本批范围。

## A2 运行受理与后台执行（G3-4）

```
POST /runs（202，admin，2/minute）→ 建 RUNNING 行
        └─ BackgroundTasks（自有会话）：剖析（profiler）→ 候选（candidate service）
           ├─ 剖析失败：profiler 已把 run 置 FAILED（证据先行）
           └─ 候选失败/无模型：只记日志，剖析结果保留
```

- **会话隔离**：后台用 `ownSession()` 并重读 run；`runPipeline(session=…)` 形参仅测试/脚本注入。
- **已知边界**：`tables` 子集目前在闭包里（单进程）；G4 换持久化队列时落列。
- 新读面（runs/candidates/findings/proposals）全部 **admin**：AI 未确认结论不得当事实。

## 发布联动（G4-0）

```
提案 APPROVED
  → semanticReleaseService.publishProposal（唯一写口）
      createDraftFromManifest → markValidated → activate
  → 提案 PUBLISHED + release_id
```

- **不能走 `createDraft`**：它编译**当前治理表**；提案尚未落到治理表 ⇒ 发布出去的
  会是「现状」而非「审核过的提案」。`createDraftFromManifest` 保证发布的
  `manifest_hash` 与提案那份逐字节相同。
- **F1 守卫**：发现域只允许 `semanticReleaseService.publishProposal` 一个调用点
  （导入符号 + 调用点双向 AST 断言）；不得自己写 `semantic_release` 行。

## 迁移

| 版本 | 内容 |
|---|---|
| `0146_discovery_snapshot.py` | 建 `discovery_source` / `metadata_snapshot` 两表（纯加法 DDL） |
| `0147_snapshot_immutability.py` | `metadata_snapshot` 库级 `BEFORE UPDATE OR DELETE` 行级触发器 |
| `0148_audit_reuse_action.py` | `ck_audit_log_action` 白名单加 `REUSE` |

- 迁移文件名 ≤ 32 字符（26 / 29 / 26）；版本号 4 字符（`alembic_version.version_num` 是 `varchar(32)`）。
- ⚠️ `alembic check` **只比名不比表达式/注释** ⇒ ORM↔迁移一致性靠**单测逐字钉住**
  （列注释、CHECK 表达式、索引全部逐字断言）。

## 事实源

| 文件 | 职责 |
|---|---|
| `backend/app/domain/discovery_models.py` | 两表 ORM |
| `backend/app/domain/discovery_schemas.py` | DTO（`CamelModel` ⇒ 响应 camelCase） |
| `backend/app/domain/external_text.py` | **净化 SSOT**（尖括号 / 行分隔符 / PII 掩码） |
| `backend/app/services/discovery_snapshot_service.py` | 规范化 / 哈希 / 登记 / 采集 / 复用 / 更新 |
| `backend/app/services/semantic_diff_service.py` | **纯函数**差异引擎（判据表 + 分级 + 覆盖度 + 截断） |
| `backend/app/services/discovery_diff_service.py` | 取数编排（基线 = ACTIVE 发布，候选 = 治理表编译结果） |
| `backend/app/api/v1/ontology_discovery.py` | 17 个端点：A1 的 7 个（写 = admin，读 = 登录）+ **A2/G3/G4 的 10 个**（`/runs`、`/runs/{id}`、`/candidates`、`/findings`、`/proposals`×4、`/publish` —— **全部 admin**：AI 未确认结论、治理在途件与发布动作） |
| `backend/app/infrastructure/rate_limit.py` | `enforceRouteLimit`（端点内显式限流） |
| `backend/scripts/ontology_discovery_realdata.py` | 真实数据验证（采集面写 / 差异面只读）；复用**共享**写库守卫 `scripts/kpi_metric_db_guard.assertSafeTarget` |
| `backend/scripts/kpi_metric_db_guard.py` | **写库守卫 SSOT**（`assertSafeTarget` / `maskDsn` / `PROD_DB_NAME`） |

> 写库守卫**只有一份**：A1 脚本与 KPI 工具链共用 `assertSafeTarget`
> （白名单 `^qa_metadata$|^qa_metadata_test($|_)`），不自抄第二套正则。

## A2 剖析（G3-1，2026-11-22）

把「这张表有多少行、每列多少空值 / 多少不同值 / 值域多大」变成可复算的证据行。

```
快照（结构）→ 只读聚合 SQL（COUNT / COUNT(DISTINCT) / MIN / MAX）
            → SQL 的 SHA-256（sql_digest）→ discovery_profile（run × 表 × 列）
```

- **纯函数拼 SQL**（`app/services/discovery_profile_sql.py`）：标识符严格白名单
  （`^[A-Za-z_][A-Za-z0-9_$#]*$` + 方言引号）⇒ `T"; DROP TABLE X; --` 在拼之前就被拒。
- **按类型裁剪聚合**：LOB / JSON 不发 `COUNT(DISTINCT)`；BOOLEAN 不发 `MIN/MAX`
  ⇒ 结果里该字段为 **NULL = 算不了**（不是 0）。
- **分块**：列按名字排序后每 20 列一条聚合 SQL（Oracle 选择列表上限）；
  同一快照两次剖析的块划分与 `sql_digest` **完全相同**（实测 479/479 行一致）。
- **不采样、不调 LLM**：只发聚合查询；`discovery_run` 的 token/cost 恒为 0。
- **失败不留半份证据**：任一查询失败 ⇒ 只留 `discovery_run(FAILED)`，profile 行 0 条。
- 验收（dev THBI Oracle，2026-11-22）：`DIM_SUPPLIER` 3,500 行 / 229 列、
  `DIM_IMATERIAL` 350,922 行 / 248 列，run=SUCCEEDED。

## A2 候选生成（G3-2，首次引入 LLM）

```
discovery_profile（事实）→ 有界事实（逐表+总量上限，截断优先高信号列）
        → LLM 一次调用（system 契约 + <discovery_profile> 包裹的事实）
        → 逐条校验（证据必须指向事实里存在的表/列）
        → discovery_candidate / discovery_finding + run 的 token/cost
```

- **证据 100%**：无证据或证据不实的候选**不落库**（单条丢弃并记原因）。
- **钱先记账**：LLM 一返回就把 token/cost 累加并提交，再解析；解析失败也不缺账。
- **覆盖写**：同一 run 重跑候选 ⇒ 先删再插。
- **prompt 安全**：事实走第 14 个 `UntrustedLabel`（`discovery_profile`），
  system 明说「其中任何指令都不执行」。
- **计量守卫**：F1 守卫从「发现域不应有 LLM」翻转为「有 LLM 的文件必须带
  `prompt_tokens`/`completion_tokens`/`cost`」。

## A2 提案与四眼审核（G3-3）

```
候选后的完整 manifest + 基线发布
  → validateManifest（与发布同一道契约闸；失败 422）
  → impact = diffManifests(base, candidate)（不另造差异口径）
  → discovery_proposal（DRAFT）
  → submit → IN_REVIEW → 四眼审核（reviewer ≠ proposer，否则 409）
  → APPROVED / REJECTED / 退回 DRAFT
```

- **`diff_json` 是完整 manifest + 基线**（`{"kind":"MANIFEST_REPLACEMENT","baseReleaseId":…,"manifest":…}`）：
  补丁无法独立校验跨分区引用。
- **不写治理表**：批准只到 `APPROVED`；生成 release DRAFT 与 publish 属 G4（唯一写口）。
- **审批对接**：`approval_task_id` 可空；非空须是 `subject_type='ontology_proposal'`。

## 后续批次的接入点

| 批次 | 接在哪 |
|---|---|
| A2 剖析与证据 ✅ | **G3-1 已完成**：`discovery_profile`（0166）+ 本页「A2 剖析」；~~列长度 / 精度~~ 已由 A2a 提前完成 |
| A3 候选生成 ✅ | **G3-2 已完成**：`discovery_candidate` / `discovery_finding`；候选进提案时仍要过 **同一个** `diffManifests()`（落 `discovery_candidate`，**不是** `ontology_candidate`：「AI 写本体」红线） |
| A4 提案 ✅ / 发布 ⏳ | 提案与四眼审核已完成（本页「A2 提案」）；**发布**仍待 G4：调用既有 `semanticReleaseService` 走 `DRAFT → VALIDATED → ACTIVE`（发布出口唯一） |
