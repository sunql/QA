# 本体发现与差异引擎（WP-ONT-AI）

**日期**：2026-11-16 · **变更**：[2026-11-16-ontology-discovery-ai](../changes/2026-11-16-ontology-discovery-ai/summary.md)
**批次**：A1「发现底座」—— 零 LLM、零外部授权。

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
| `backend/app/api/v1/ontology_discovery.py` | 7 个端点（写 = admin，读 = 登录） |
| `backend/app/infrastructure/rate_limit.py` | `enforceRouteLimit`（端点内显式限流） |
| `backend/scripts/ontology_discovery_realdata.py` | 真实数据验证（采集面写 / 差异面只读）；复用**共享**写库守卫 `scripts/kpi_metric_db_guard.assertSafeTarget` |
| `backend/scripts/kpi_metric_db_guard.py` | **写库守卫 SSOT**（`assertSafeTarget` / `maskDsn` / `PROD_DB_NAME`） |

> 写库守卫**只有一份**：A1 脚本与 KPI 工具链共用 `assertSafeTarget`
> （白名单 `^qa_metadata$|^qa_metadata_test($|_)`），不自抄第二套正则。

## 后续批次的接入点

| 批次 | 接在哪 |
|---|---|
| A2 剖析与证据 | 快照 JSON 之外新增列级 metrics（`profile_result`），引用 `schema_hash` 作证据锚点；**并在本批补列长度 / 精度**（`NUMBER(10,2)` 与 `NUMBER(10,4)` 当前同哈希 ⇒ 需改共享 `ColumnSchemaRead` + 12 方言内省 SQL，D-20 延后） |
| A3 候选生成 | LLM 产出的候选 manifest → **同一个** `diffManifests()` 出差异 → 落 `ontology_candidate` |
| A4 提案与发布 | 差异 ⇒ 提案 ⇒ 四眼审核 ⇒ 调用既有 `semanticReleaseService` 走 `DRAFT → VALIDATED → ACTIVE`（发布出口唯一） |
