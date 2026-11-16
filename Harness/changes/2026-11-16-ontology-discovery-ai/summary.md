# 变更：2026-11-16-ontology-discovery-ai（A1 发现底座）

- **日期**：2026-11-16
- **作者**：Claude / phase3-reconciler
- **Phase**：WP-ONT-AI 批次 A1「发现底座（不可变快照 + 语义差异引擎）」
- **状态**：done（本批代码与测试完成；**不部署生产**）
- **关联变更**：[feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b 发布镜像，提供发布侧基线）、[feat-semantic-release-a](../feat-semantic-release-a/summary.md)（P1a 语义发布，Compiler/Registry 是本批的上游）
- **迁移版本**：`0146_discovery_snapshot.py`（26 字符，建两表）/ `0147_snapshot_immutability.py`（29 字符，库级不可变触发器）/ `0148_audit_reuse_action.py`（26 字符，审计动作 `REUSE`）—— 文件名均 ≤ 32 字符，版本号均 4 字符（`alembic_version.version_num` 是 `varchar(32)`）
- **MEMORY**：[`qa-system-ontology-discovery-a1.md`](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-ontology-discovery-a1.md)
- **计划**：[`plan.md`](./plan.md)（A1–A4 全量计划）；**证据**：[`daily/A1.md`](./daily/A1.md)

---

## 1. 需求

**背景**：语义发布把「某一刻的本体」冻结成 Manifest 快照，但本体治理表本身是**原地 UPDATE**
的 ⇒ 发布之后本体被谁改了什么、改动会不会打破下游 NL2SQL / RAG / 图谱消费方，
**事后答不上来**。设计稿 `AI驱动本体发现与持续演进_功能设计_V1.0.md`（F02 / F06）要求的
「不可变元数据快照」与「语义差异与影响分析」在本仓**尚无实现**。

**目标**：补齐**发布面之前**缺失的底座，让「发布后本体被改了什么、风险多大」成为
**可机读、可复算、可引用**的事实。

**验收标准（用户视角）**

1. 授权数据源在某一刻的 schema 结构能被**冻结**成一行证据，事后可引用；
2. 内容没变时**不产生新行**（幂等），内容变了才**新增**一行（旧行逐字不变）；
3. 给定「已发布的那一版」与「当前治理表」，能输出**结构化差异**（不是自然语言），
   并区分**破坏性**与**向后兼容**；
4. 差异计算**可复算**：同一输入两次结果完全相同；
5. 治理写动作（登记发现源 / 采集快照）**只有管理员**能做；只读元数据所有登录用户可看；
6. **不写业务库、不写治理表**：发现域只产出候选，人工裁决，机器发布（红线）。

**本批不做**：LLM 候选生成（A3）、列级剖析与样本（A2）、提案 + 四眼审核 + 发布适配（A4）、
前端页面、metrics/kpis/domains/businessObjects 分区差异。

---

## 2. 设计评审

方案取舍的完整论证见 [`plan.md`](./plan.md) D-1…D-10，此处只列**决定性对比**。

### 2.1 快照存哪：复用可变缓存 vs 新建不可变表

| 维度 | A：复用 `schema_cache` | **B：新建 `metadata_snapshot`（采纳）** |
|---|---|---|
| 历史 | ❌ `introspectAndCache` 内容变化即 `UPDATE` 覆盖 | ✅ 只增不改，`captured_at` 可引用 |
| 证据可寻址 | ❌ hash 挂在会变的缓存行上 | ✅ `schema_hash` 是稳定标识 |
| 额外成本 | 0 | 一张新表 + 一份迁移 |

### 2.2 哈希口径：复用哪把尺子

复用 `semantic_manifest_schema.manifestHash`（规范化 JSON → SHA-256，
`sort_keys=True, ensure_ascii=False, separators=(",",":"), allow_nan=False`）。
理由：① 与 Manifest 哈希同族 ⇒ **同一把尺子**；② 仓库已有该口径的单点事实源，不另造；
③ `_schemaVersion` 已是同一算法。

### 2.3 差异引擎：纯函数 vs 带 IO 的服务

**采纳纯函数** `diffManifests(base, candidate) -> SemanticDiff`：零 IO、不改入参、确定性排序。
理由：① 可在单测里逐条断言；② A3 的 LLM 候选 manifest 走**同一个函数**；
③ 「差异」是派生事实，持久化归 A4 的提案层，避免两个真相源。

### 2.4 破坏性判据与风险分级

- **显式判据表**（D-5），不做启发式改名检测：改名会呈现为「删除 + 新增」——
  启发式改名会产生**假重命名**（两个本就该分开的东西被并到一起），比漏一个改名更危险。
- **风险分级**（D-6）：`BREAKING→HIGH` / `ADDITIVE→LOW` / `COSMETIC→INFO`。
  **不引入「兼容百分比」这类虚构指标**；兼容判据 = 是否存在 BREAKING 变更。

### 2.5 快照内容边界

**只收结构，不收样本**（表 / 列 / 类型 / 可空 / 主键 / 外键 / 注释）。
样本涉及脱敏与采样白名单，属 A2 且依赖业务侧 Q3 授权 ⇒ 把 PII 带进元数据库的风险
远大于当前价值。

### 2.6 多视角意见（两轮评审子代理）

本批走了**两轮** `code-reviewer` + `security-reviewer` 并行审查（每轮两个代理，
只报问题不改代码）。**设计层面被质疑并采纳**的点：

| 轮次 | 审查结论 | 采纳的关键设计决定 |
|---|---|---|
| 首轮 | code-reviewer：CRITICAL 0 / **HIGH 3** / MEDIUM 10 / LOW 5<br>security-reviewer：CRITICAL 0 / **HIGH 2** / MEDIUM 6 / LOW 7 | 差异引擎的**纯函数契约**（不改入参、不改全局状态）、快照写入的**并发冲突**处理（SAVEPOINT + 冲突后重查）、写入口的**生产装配守卫**（不能只在测试 app 上验证 403）、**库级不可变触发器**（D-11）、访问策略**只承认已实现的键**（D-12）、「授权可撤销」要有**真的路径**（D-13） |
| 复审轮（对修复后的树再审） | code-reviewer：CRITICAL 0 / HIGH 0 / MEDIUM 5 / LOW 6<br>security-reviewer：CRITICAL 0 / HIGH 0 / MEDIUM 5 / LOW 6 | 净化收敛为**单一 SSOT**（D-16）、审计动作 `REUSE`（D-17）、端点内显式限流（D-18）、读入口门禁的**显式安全决议**（D-19）、列长度/精度**延后 A2**（D-20）、「不可擦除」是**设计属性**（D-21） |

⚠️ 第 1 轮的「发现域缺启动自检」（H-2）在第 2 轮被**反向处置**：该自检与既有
`validateProduction()` 触发条件**完全相同**（生产 + stub 鉴权）⇒ 硬闸已经在
**进程起不来**的层面挡住 ⇒ 再加一条「只告警不阻塞」的域级检查是**冗余**，
且会让人误以为本域另有更强的守卫 ⇒ **删除**该函数（LOW-1），并留下守卫测试
断言它**不再出现**（防「以后有人又加回来」）。

**逐条处置表见第 7 段**（每条：发现 → 处置 → 证据）。

---

## 3. 数据模型变更

**纯加法 DDL，只新增两张表、两个索引；不触碰任何既有表。**

### 3.1 `discovery_source`（发现源 = 授权目录，不新建连接）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `name` | VARCHAR(100) | NOT NULL，**唯一** `uq_discovery_source_name` | |
| `datasource_id` | BIGINT | NOT NULL，FK → `data_source.id` **RESTRICT**（`fk_discovery_source_datasource`） | 只引用既有连接（D-9） |
| `owner` | VARCHAR(100) | NOT NULL | 业务责任人 |
| `access_policy` | JSONB | NOT NULL，默认 `'{}'::jsonb` | 采集时校验 `mode=READ_ONLY` |
| `status` | VARCHAR(20) | 默认 `'ACTIVE'`，CHECK `status IN ('ACTIVE','DISABLED')` | |
| `note` / `created_by` / `created_time` | | | |

索引：`ix_discovery_source_datasource(datasource_id)`。

### 3.2 `metadata_snapshot`（不可变元数据快照，**只增不改**）

| 列 | 类型 | 约束 / 默认 | 说明 |
|---|---|---|---|
| `id` | BIGINT PK | | |
| `source_id` | BIGINT | NOT NULL，FK → `discovery_source.id` **RESTRICT**（`fk_metadata_snapshot_source`） | |
| `schema_hash` | VARCHAR(64) | NOT NULL，**源内唯一** `uq_metadata_snapshot_source_hash`，CHECK `schema_hash ~ '^[0-9a-f]{64}$'` | 内容寻址键 |
| `snapshot_json` | JSONB | NOT NULL，CHECK `jsonb_typeof(snapshot_json) = 'object'` | 规范化结构快照 |
| `table_count` / `column_count` | INTEGER | 默认 0，CHECK `>= 0` | 冗余计数（便于列表展示） |
| `captured_at` | TIMESTAMPTZ | NOT NULL | 冻结时刻 |
| `created_by` / `created_time` | | | |

索引：`ix_metadata_snapshot_source(source_id)`。

> **刻意无 `updated_time`**：快照只增不改 ⇒ 没有「最后更新时间」语义（登记于 `daily/A1.md` L-3）。

### 3.3 迁移

| 文件 | revision | 内容 |
|---|---|---|
| `0146_discovery_snapshot.py` | `0146`（down `0145`） | 纯加法：`CREATE TABLE` ×2 + `CREATE INDEX` ×2；`downgrade()` 先删索引再删表 |
| `0147_snapshot_immutability.py` | `0147`（down `0146`） | `metadata_snapshot` 的 **库级 `BEFORE UPDATE OR DELETE` 行级触发器**（D-11）。⚠️ **TRUNCATE 不触发行级触发器** ⇒ 测试夹具的「每测试清库」不受影响 |
| `0148_audit_reuse_action.py` | `0148`（down `0147`） | `ck_audit_log_action` 白名单新增 **`REUSE`**（D-17）：内容寻址复用**没有新建** ⇒ 审计不得记 `CREATE` |

⚠️ 审计动作白名单**三处同改**（`AuditService._VALID_ACTIONS` + ORM `CheckConstraint` +
DB `CHECK`）：只改两处 ⇒ 运行时报错或库层拒绝写入。

- **ORM 与迁移的列注释 / CHECK 表达式逐字一致**，由 `test_discovery_models.py`（17 例）双向钉住
  —— 因为 `alembic check` **只比名不比式**，只补名会假绿。
- `alembic check` 对这两张新表**零漂移**（唯一报出的 `remove_table po_lines_sandbox` 是
  `test_l2_cte_avg_of_ratios.py` 留下的既有残留噪音）。

---

## 4. 接口契约变更

前缀 `/api/v1/ontology-discovery`（设计稿草案为 `/api/ontology-discovery`；
本仓所有 v1 路由都在 `/api/v1` 下 ⇒ **对齐本仓约定**，偏离登记于 `daily/A1.md` L-1）。

| 方法 | 路径 | 门禁 | 请求 | 响应 |
|---|---|---|---|---|
| POST | `/api/v1/ontology-discovery/sources` | **`requireAdmin`** | `{name, datasource_id, owner, access_policy?, note?}`（`extra="forbid"`） | `201` `DiscoverySourceRead` |
| PATCH | `/api/v1/ontology-discovery/sources/{sourceId}` | **`requireAdmin`** | `{status?, owner?, access_policy?, note?}`（**禁改 `datasource_id` / `name`**） | `200` `DiscoverySourceRead` |
| GET | `/api/v1/ontology-discovery/sources` | 登录 | — | `DiscoverySourcePage`（含 `snapshot_count`、`total` = **全量**计数） |
| GET | `/api/v1/ontology-discovery/snapshots` | 登录 | `?sourceId=&limit=&offset=` | `DiscoverySnapshotPage`（新→旧） |
| GET | `/api/v1/ontology-discovery/snapshots/{snapshotId}` | 登录 | — | `DiscoverySnapshotRead`（含 `snapshot` 内容） |
| POST | `/api/v1/ontology-discovery/snapshots` | **`requireAdmin`** | `?sourceId=` | `SnapshotCaptureRead`（含 `reused`）；**限流 10/分钟** |
| GET | `/api/v1/ontology-discovery/diff` | 登录 | `?domainCode=` | `SemanticDiffRead`（含 `covered_fields` / `truncated` / `dropped_breaking_count`） |

新增 DTO（`app/domain/discovery_schemas.py`）：`DiscoverySourceCreateRequest`、
`DiscoverySourceRead` / `DiscoverySourcePage`、`DiscoverySnapshotSummaryRead` /
`DiscoverySnapshotRead` / `DiscoverySnapshotPage`、`SnapshotCaptureRead`、
`SemanticChangeRead`（含 `risk_level`）、`SemanticDiffRead`（含 `baseline_release_id/version/hash`、
`*_count`、`has_breaking_changes`、`overall_risk_level`、`changes`）。

**状态机**：无。**写面**只写 `discovery_source` / `metadata_snapshot` 两张新表，
**不写治理表**（红线：AI / 发现域只产出候选，人工裁决，机器发布）。

**响应头**：全部端点 `Cache-Control: no-store`（治理元数据不进浏览器与代理缓存）。

**既有契约零变更**：未改任何既有端点、字段或状态机。

---

## 5. 实现要点

| 文件 | 要点 |
|---|---|
| `app/domain/discovery_models.py` | 两表 ORM + 注释常量；列注释与迁移**逐字一致** |
| `app/services/discovery_snapshot_service.py` | `normalizeSnapshot`（**纯函数**：表按 `(owner, tableName)`、列按列名、外键按 `(column, refTable, refColumn)` 排序；**主键保留原序**，复合主键列序有语义；不改入参）→ `computeSnapshotHash`（复用 `manifestHash`）→ `DiscoverySnapshotService`（登记 / 采集 / 复用 / 列表 / 详情 / 计数）。采集走 `SchemaIntrospectionService.introspect`（SQL Guard + 库侧只读）；**只读策略校验**（`mode != READ_ONLY` ⇒ 拒绝）；写入用 **SAVEPOINT**（`session.begin_nested()`）⇒ 唯一冲突只回滚本次插入，冲突后重查命中即复用 |
| `app/services/semantic_diff_service.py` | **纯函数** `diffManifests`：三分区（classes / properties / joins）× D-5 判据表；删类不重复报属性；`isNotNull` 按方向分严重度；确定性排序；`SemanticChange.riskLevel` + `SemanticDiff.overallRiskLevel`（取最高） |
| `app/services/discovery_diff_service.py` | 取数编排：基线 = `semanticRegistry.getActive(session, domainCode=...)`（无 ACTIVE ⇒ 基线为空，非错误），候选 = `compileManifest(session)` |
| `app/api/v1/ontology_discovery.py` | 7 端点（含 `PATCH /sources/{id}`）；写 `requireAdmin` / 读 `getCurrentUser`；`Cache-Control: no-store`；采集端点**端点内显式限流**（D-18） |
| `app/domain/external_text.py` | 净化 **SSOT**（D-16）：`escapeAngleBrackets` / `collapseLineSeparators` / `sanitizeSchemaField` / `sanitizeIdentifier` / `maskPii` / `sanitizeExternalText` |
| `app/infrastructure/rate_limit.py` | 新增 `enforceRouteLimit(request, limitValue, scope)`（D-18）：端点内显式执行一次路由级限流 |
| `app/main.py` | `include_router(ontology_discovery.router, prefix="/api/v1/ontology-discovery", dependencies=[mustChangePasswordGuard, rateLimitDep])` —— **必须直接 include 子路由**（FastAPI 0.141 前缀双重叠加坑） |
| `app/tests/_testapp.py` | 测试 app **同前缀同依赖**挂载 ⇒ 生产 / 测试装配一致 |
| `scripts/ontology_discovery_realdata.py` | 真实数据验证脚本：采集面（写隔离测试库）+ 差异面（只读治理库），见第 8 段 |

**依赖注入点**：FastAPI `Depends(getDb)`（会话）/ `Depends(requireAdmin)` / `Depends(getCurrentUser)`；
服务层 `DiscoverySnapshotService(*, introspectionService=None, datasourceService=None)` 支持替换。

---

## 6. 测试

| 层次 | 文件 | 结果 |
|---|---|---|
| unit | `test_discovery_models.py`（ORM↔迁移逐字一致） | ✅ |
| unit | `test_discovery_snapshot_service.py`（含体积上限 / 按源计数收窄 / 策略校验） | ✅ |
| unit | `test_semantic_diff_service.py`（含 `selectChangesForResponse` 4 例） | ✅ |
| unit | `test_ontology_discovery_api.py`（HTTP 层：`total` 语义 / `extra=forbid` / 审计 `REUSE` / 限流**位置** / `/diff` 覆盖度与截断） | ✅ |
| unit | `test_ontology_discovery_realdata.py`（脚本护栏 / 主机掩码 / 原生读假绿防线） | ✅ |
| unit | `test_external_text.py`（净化 SSOT） | ✅ |
| unit | `test_production_auth_guard.py`（含「域级自检不得再出现」守卫） | ✅ |
| unit | `test_audit_service.py`（`REUSE` 动作） | ✅ |
| integration | `test_governance_write_route_auth.py`（生产装配门禁守卫） | ✅ 全绿 |
| integration | `test_ontology_drift_api.py`（含 `TestOntologyDiscoveryApi` / `TestOntologyDiscoverySourceUpdate`） | ✅ 新增用例全绿／❌ **2 既有红基线** |

- **单测合计 167 passed**（A1 相关 7 个 unit 文件，87.48s）；再加 `test_audit_service.py`
  + `test_nl2sql_schema.py` 的 9 文件集合 **212 passed**（115.62s）。
- **集成**：32 passed / 2 failed（2 失败 = 既有红基线，见下）。
- **净新增失败 0**：2 个失败在 **pristine baseline**（main `5ab1313c` 的独立 worktree
  `qa-main-check` + 独立基线库 `qa_metadata_test_base`）上复现**同样 2 个失败**
  ⇒ 与本批无关。根因：漂移端点统计库内**全部有 `source_table` 的本体类**，
  而集成 autouse fixture `warmAgentCaches` → `seedHeaderOntologyClasses` 会补种 **4 个** header 类
  ⇒ 4 + 3 = 7 ≠ 断言里的 3（测试与 fixture 的既有耦合）。**不在本批修**（已立项建议）。
- **集成用例未新建文件**：并入既有 `test_ontology_drift_api.py`（新文件会按「排序下标 mod 4」
  轮转 CI 分片 ⇒ 假 net-new）；新文件只放 `unit/`（路径哈希分片，不受影响）。
- **真实数据库**：真实 PostgreSQL + 完整 HTTP 链路（`AsyncClient` + `ASGITransport`），
  外部依赖（业务库）以 fake 注入，真实业务库由 A1-7 脚本覆盖。
- **覆盖率**：新增代码（ORM / 服务 / DTO / API / 脚本 / 净化 / 限流）均有对应单测或集成用例
  逐条覆盖，关键分支（重名冲突、停用源、非只读策略、内容变化新增、复用、404、403、
  限流**位置**、截断时 BREAKING 优先）均有断言。

---

## 7. 安全审查

触发条件命中：认证 / 授权（新增 API）、用户输入（发现源名称、策略 JSON）、
外部数据源访问（业务库内省）、凭据（数据源口令解密）、**不可变存储 + 外部不可信内容**。

⚠️ **本段曾经错误地宣称「CRITICAL / HIGH / MEDIUM：无」** —— 那是**第一轮审查之前**
的初稿。实际走了**两轮** `security-reviewer` + `code-reviewer`，结论见下表与逐条处置表。

### 7.1 严重度汇总

| 审查方 | 轮次 | CRITICAL | HIGH | MEDIUM | LOW |
|---|---|---|---|---|---|
| security-reviewer | 首轮 | 0 | **2** | 6 | 7 |
| security-reviewer | 复审轮 | 0 | 0 | 5 | 6 |
| code-reviewer | 末轮 | 0 | **3** | 10 | 5 |

**当前状态：CRITICAL 0；HIGH 3 中 2 项已修、1 项转为显式安全决议；MEDIUM / LOW
全部逐条处置（修或登记）** —— 逐条见 7.3。

### 7.2 本批自身的安全性质核对

| 项 | 结论 |
|---|---|
| 越权 | 3 个写端点（登记 / 采集 / 停用更新）均声明 `requireAdmin`；4 个读端点只要求登录（`getCurrentUser`）⇒ 普通用户能看、不能写。**生产装配**（`createApp()`）与**测试装配**（`_testapp.py`）一致，且由 `test_governance_write_route_auth.py`（生产 app 路由树探测）钉住 |
| SQL 注入 | 业务库访问**只经** `SchemaIntrospectionService.introspect` → `BusinessDbAdapter.execute_read_only`（SQL Guard + 库侧只读事务 + 行数上限 + 超时）；**无自建连接串、无拼接 SQL**。Oracle owner 走既有白名单；PG/MySQL **带 `schema_owner` 会被拒绝**（sec-M-3：静默忽略等于「配了没生效」） |
| 提示注入 / 不可见字符 | 所有进入快照的**外部字符串**（表名 / 列名 / 属主 / 类型名 / 注释）过 `app/domain/external_text.py` 的 SSOT：转义 `<>`、折叠行分隔符全集（含 U+2028/U+2029/U+0085/`\v`/`\f`）、剔除零宽与双向字符；注释额外过**高置信 PII 掩码**（邮箱 / 大陆手机号 / 18 位身份证号） |
| 凭据泄漏 | `discovery_source` 只存 `data_source.id`（FK），**不存口令**；脚本输出只打库名与掩码后的标识，**失败路径也掩码主机**（L-5）；`schema_hash` 展示截断 12 位 |
| 写库闸 | 真实数据脚本**复用** `scripts/kpi_metric_db_guard.assertSafeTarget`（SSOT，不自抄第二份白名单）：判定**引擎实际连的** DSN ⇒ 测试库 `qa_metadata_test*` 直接放行；`qa_metadata`（prod 同源）需 `--allow-prod-write` **且打印将写入的行数**；其它库名一律拒绝 |
| 只增不改 | 应用层**不提供** update / delete 方法 + **库级 `BEFORE UPDATE OR DELETE` 触发器**（0147）双重兜底；单测与真实数据双证 |
| 输入校验 | 发现源名称非空、重名（`ConflictError`）、`owner` 非空、`status` 合法枚举、策略必须 JSON 对象且**只允许 `mode` + `schema_owner`**（未知键显式拒绝）、策略 ≤ 4096 字节、停用源 / 停用数据源拒绝采集 |
| 审计 | 3 个写端点落 `audit_log`；**内容寻址复用记 `REUSE` 而非 `CREATE`**（D-17）；脚本写入也落审计（actor = `script:ontology_discovery_realdata`） |
| 限流 | 采集端点 10/分钟，端点内显式执行（D-18）⇒ 超限 429 + 标准信封 + `Retry-After` |

### 7.3 逐条处置表

| # | 发现（来源） | 处置 | 证据 |
|---|---|---|---|
| 1 | **H-1** 发现源无停用/更新路径 ⇒ `status=DISABLED` 是空承诺（安全 HIGH） | ✅ 新增 `PATCH /sources/{id}`（`requireAdmin`，可改 `status`/`owner`/`access_policy`/`note`，**禁改 `datasource_id`**）+ 服务 `updateSource` + 审计 | `test_ontology_drift_api.py::TestOntologyDiscoverySourceUpdate` |
| 2 | **H-2** 发现域缺启动自检（安全 HIGH） | ⛔ **反向处置**：与既有 `validateProduction()`（生产 + stub 鉴权 ⇒ **进程起不来**）触发条件完全相同 ⇒ 域级「只告警不阻塞」的检查是**冗余**。删除该函数，并留守卫测试断言它**不再出现** | `test_production_auth_guard.py::test_本体发现域不再有独立的鉴权自检` |
| 3 | **M-1 / M-2** 策略承诺了未实现的 `schemas` 白名单；`schema_owner` 与业务责任人同名异义（安全 MEDIUM） | ✅ 白名单收紧为 `mode` + `schema_owner`，**未知键显式拒绝**，≤ 4096 字节；`schema_owner` **仅 Oracle 生效**，PG/MySQL 带它 ⇒ 拒绝 | D-12；`test_discovery_snapshot_service.py` |
| 4 | **M-3** `metadata_snapshot` 不可变只靠应用层纪律（安全 MEDIUM） | ✅ 库级 `BEFORE UPDATE OR DELETE` 行级触发器（0147） | `test_discovery_models.py` |
| 5 | **M-4** 写端点不落审计（安全 MEDIUM） | ✅ 三个写端点均落 `audit_log` | 集成用例断言审计动作 |
| 6 | **M-5** 注释未清洗控制字符（安全 MEDIUM） | ✅ 收敛到 `app/domain/external_text.py` SSOT，**所有**外部字符串过净化 | `test_external_text.py` |
| 7 | **M-6** 停用数据源仍会被连（安全 MEDIUM） | ✅ `captureSnapshot` 前校验 `datasource.is_active` | 服务层单测 |
| 8 | **HIGH-1** diff 边界按**字符串**比较 ⇒ 漏判破坏性变更（code HIGH） | ✅ 改为**结构化比较**（类型 / 可空 / 长度 / 精度 / 约束分别判定） | `test_semantic_diff_service.py` |
| 9 | **HIGH-2** 脚本独立重算**不带 `schema_owner`** ⇒ 与采集面哈希口径可能不同（code HIGH） | ✅ 独立重算用 `normalizePolicy(source.access_policy)["schema_owner"]`（与采集面同一口径） | 真实数据脚本「独立内省」输出与落库哈希一致 |
| 10 | **HIGH-3** 读入口把业务库 schema 开放给登录用户，与「治理 IP 收 admin」先例冲突（code HIGH） | 📋 **显式安全决议**（D-19）：事实核查发现既有 `GET /datasources/{id}/schema` 与 `/ontology-drift` **同样**开放登录 ⇒ 先例是**两套并存**而非自相矛盾；本域写面已收 admin；**不盲改门禁**，「读入口 admin 化」作为**可选方案**登记待产品决策 | D-19；`plan.md` |
| 11 | **MEDIUM-1** `/sources` 的 `total` 取的是**本页长度**（code MEDIUM） | ✅ 改用 `countSources` 全量计数 | `test_ontology_discovery_api.py::test_发现源列表total是全量总数而非本页长度` |
| 12 | **MEDIUM-2** `/diff` 截断按原序 ⇒ 排在后面的 BREAKING 被丢掉（code MEDIUM） | ✅ `selectChangesForResponse`：**破坏性优先** + 显式回填 `droppedBreakingCount` | `test_semantic_diff_service.py::TestSelectChangesForResponse` + API 层截断断言 |
| 13 | **MEDIUM-3 / LOW-2** `registerSource` 不校验 `status` / `owner`（code MEDIUM/LOW） | ✅ 非法 `status` / 空 `owner` ⇒ 拒绝；`IntegrityError` 转成指名占用者的文案 | 服务层单测 |
| 14 | **MEDIUM-4** 快照不含列长度 / 精度 ⇒ `NUMBER(10,2)` 与 `NUMBER(10,4)` 同哈希（code MEDIUM） | 📋 **延后 A2**：要改**共享** `ColumnSchemaRead` + 12 方言内省 SQL ⇒ 口径升级风险远大于本批收益 | D-20 |
| 15 | **MEDIUM-5** 覆盖度只披露到**分区**级（code MEDIUM） | ✅ 追加**字段级**披露 `coveredFields`（分区 → 字段名） | API 层 `/diff` 断言 |
| 16 | **MEDIUM-6** `allowedValues` 顺序不同即报 BREAKING（code MEDIUM） | ✅ 集合相同 ⇒ `COSMETIC`（顺序不算语义变更） | `test_semantic_diff_service.py` |
| 17 | **MEDIUM-7** `ConnectOverride` 被适配器缓存吃掉 ⇒ 覆盖无效（code MEDIUM） | ✅ 覆盖生效时**释放该数据源**的池化适配器 | 服务层单测 |
| 18 | **MEDIUM-8 / sec-L-2** 复用既有快照却审计记 `CREATE`（code MEDIUM / 安全 LOW） | ✅ 新增审计动作 `REUSE`（D-17；三处同改：service / ORM / DB） | `test_ontology_discovery_api.py::test_复用既有快照时审计动作记为复用` |
| 19 | **MEDIUM-9** 脚本内函数超 50 行（code MEDIUM） | ✅ 拆成 `CaptureEvidence` / `_captureOnce` / `_runCaptureChecks` / `_buildCaptureReport` / `_capturePhase` / `_runCapture` | 脚本结构 |
| 20 | **MEDIUM-10** `GET /sources` 为算快照数而**全表 GROUP BY**（code MEDIUM） | ✅ `countSnapshotsBySource` **只统计当前页 id** | 服务层单测 |
| 21 | **sec-M-3** `schema_owner` 对 PG/MySQL **静默无效**（安全 MEDIUM） | ✅ 非 Oracle 数据源带 `schema_owner` ⇒ 拒绝 | 服务层单测 |
| 22 | **sec-M-4** 高成本端点无限流 + 快照详情无体积上限（安全 MEDIUM） | ✅ 采集端点 10/分钟（端点内显式，D-18）+ 快照体积上限（超限拒绝） | API 层限流**位置**断言 + 服务层体积上限用例 |
| 23 | **sec-M-5** 不可变 + 全员可读 + 外部不可信内容 ⇒ 敏感注释**无法擦除**（安全 MEDIUM） | ✅ 两半分开：PII ⇒ 源头掩码（D-16）；「不可擦除」⇒ 视为**证据日志的设计属性**（更正 = 追加新行，与 `audit_log` 不可变同构） | D-16 / D-21 |
| 24 | **LOW-1** 域级鉴权自检冗余（code LOW） | ✅ 删除（同 #2） | 守卫测试 |
| 25 | **LOW-3** `properties` 不是顶层分区（code LOW） | ✅ 覆盖度披露里如实体现 | `/diff` 断言 |
| 26 | **LOW-4** 纯函数单测绑真实 PG（code LOW） | ✅ 纯函数用例与 PG 用例分文件（`test_semantic_diff_service.py` 纯函数 / `test_discovery_snapshot_service.py` PG） | 文件结构 |
| 27 | **LOW-5** `_normalizePolicy` 是实例方法却不用 self（code LOW） | ✅ 提为**模块级纯函数** `normalizePolicy` | 服务层 + 脚本复用 |
| 28 | **sec-L-3** `access_policy=None` 静默重置 + POST DTO 未禁未知字段（安全 LOW） | ✅ `None` 显式拒绝；`DiscoverySourceCreateRequest` 补 `extra="forbid"` | API 层 422 断言 |
| 29 | **sec-L-4** 脚本**另写**一份 DSN 白名单（安全 LOW） | ✅ 复用 `scripts/kpi_metric_db_guard.assertSafeTarget`（SSOT） | `test_ontology_discovery_realdata.py` |
| 30 | **sec-L-5** 脚本失败路径泄漏内网主机（安全 LOW） | ✅ 失败路径 `_redact` 掩码主机 / DSN | `test_ontology_discovery_realdata.py` |
| 31 | **sec-L-6** 脚本写入不落 `audit_log`（安全 LOW） | ✅ 脚本写 `discovery_source` / `metadata_snapshot` 均落审计 | 真实数据后 `audit_log` 三行（CREATE / CREATE / REUSE） |
| 32 | **sec-L-7** 净化改变被哈希内容 ⇒ 「只差控制字符的注释」不算内容变更（安全 LOW） | 📋 **不是 bug，是设计**：内容寻址以**入库口径**（净化后）为准 ⇒ 决定写进 D-16 | D-16 |

### 7.4 剩余风险（登记在案）

| 项 | 说明 |
|---|---|
| 生产写库无备份前置 | 真实数据脚本对 `--allow-prod-write` 之外**无备份前置**——本脚本只 INSERT 自己那两张新表（无 UPDATE / DELETE，已读代码确认）⇒ 不存在破坏既有数据的路径，且默认闸门只允许隔离测试库 |
| **全仓装饰器限流是死代码** | D-18 只修了本域端点。**全仓 `@limiter.limit` 装饰器限额（含 `datasources.py` 的 introspect 10/分钟）当前均未被执行**（`_auto_check=False` + 无 `SlowAPIMiddleware` + `rateLimitDep` 按 URL 路径查而 slowapi 按 `module.func` 登记）⇒ 修它是**全仓语义变更**（会一次性激活大量休眠限额）⇒ **单独立项**，本批只登记 |
| 读入口门禁 | D-19 的显式安全决议；「读入口 admin 化」待产品决策 |

---

## 8. 部署验证

**本批不部署生产**：0146/0147/0148 迁移未上生产，生产仍跑旧代码（旧镜像 alembic head = 0143）。
以下为**本地验证**证据（复审轮修复**之后**重新实测）。

### 8.1 生产装配冒烟（端点枚举）

```bash
cd backend && export PYTHONPATH=/private/tmp/qa-graph-mirror/backend
.venv/bin/python -c "
from app.main import createApp
app = createApp()
for p in sorted(x for x in app.openapi()['paths'] if 'ontology-discovery' in x):
    print(p, sorted(app.openapi()['paths'][p].keys()))
"
```

输出：

```
/api/v1/ontology-discovery/diff ['get']
/api/v1/ontology-discovery/snapshots ['get', 'post']
/api/v1/ontology-discovery/snapshots/{snapshotId} ['get']
/api/v1/ontology-discovery/sources ['get', 'post']
/api/v1/ontology-discovery/sources/{sourceId} ['patch']
```

⇒ **7 个端点**（5 路径）已在**生产装配**注册，方法与门禁分层与第 4 段契约一致。

### 8.2 迁移状态

```bash
export DATABASE_URL='postgresql+asyncpg://qa_user:***@127.0.0.1:5434/qa_metadata_test'
.venv/bin/alembic current   # ⇒ 0148 (head)
.venv/bin/alembic check     # ⇒ 仅 remove_table po_lines_sandbox（既有残留噪音；新两表零漂移）
```

- `0148` 的 `ck_audit_log_action` 白名单实测已含 `REUSE`（`\d audit_log` 可见）。
- `metadata_snapshot` 的 `tr_metadata_snapshot_immutable` 触发器已在库层生效。

### 8.3 真实数据验证脚本输出（A1-7，复审轮修复后重跑）

**采集面**（写隔离测试库 `qa_metadata_test`，真实 MySQL 业务库 `asset_demo`）：

```
[ontology_discovery_realdata] 元数据库：qa_metadata_test（不打印凭据）
[ontology_discovery_realdata] 模式：采集（写）
  [INFO] 演示数据源 id=9 name=asset-demo-mysql type=mysql db=asset_demo
  [PASS] 首次采集落库（内容寻址） — 落库 hash=e24075af83db… vs 独立重算=e24075af83db…
  [PASS] 首次采集的行数变化符合内容寻址语义 — 采集前 1 行 → 采集后 1 行（reused=True）
  [PASS] 落库行的表/列计数与快照内容一致 — tableCount=2 columnCount=7
  [PASS] 二次采集复用既有快照（内容寻址幂等） — reused=True snapshotId=1
  [PASS] 二次采集不新增快照行（只增不改） — 采集后 1 行 → 二次采集后 1 行
  [PASS] 二次采集未改写既有快照行（原生读比对） — captured_at=2026-11-16T03:24:05.067704+00:00
  [PASS] 快照 JSON 形状合规（版本/排序） — 无问题
  [PASS] 哈希可由落库 JSON 独立重算（口径可复现） — 重算=e24075af83db… 落库=e24075af83db…
REALDATA RESULT: PASS
```

> 「独立重算」与「落库」哈希一致 ⇒ **HIGH-2 已修**：独立重算路径带上了
> `schema_owner`（与采集面同一口径）⇒ 两个哈希不会来自两个不同的库。

**脚本写入的审计留痕**（`qa_metadata_test` 库内实测）：

```
 id |  action |    entity_type    | entity_id |               actor
----+--------+-------------------+-----------+------------------------------------
  3 |  REUSE  | metadata_snapshot |         1 | script:ontology_discovery_realdata
  2 | CREATE  | metadata_snapshot |         1 | script:ontology_discovery_realdata
  1 | CREATE  |  discovery_source |         1 | script:ontology_discovery_realdata
```

⇒ **sec-L-6 已修**（脚本写入落审计）+ **D-17 已修**（复用记 `REUSE` 而非 `CREATE`）。

**差异面**（**只读**治理库 `qa_metadata`，真实 ACTIVE 发布 `v-p2-d12`）：

```
  [PASS] 差异计算确定性（两次结果完全相同） — 变更数 3 vs 3
  [PASS] 风险分级与严重度一致（BREAKING→HIGH / ADDITIVE→LOW / COSMETIC→INFO） — 3 条变更
  [PASS] 整体风险等级 = 各变更最高级 — overall=LOW
baseline: releaseId=1 version=v-p2-d12 manifestHash=4d46898611e8acb9…
diff: changeCount=3 breakingCount=0 additiveCount=3 overallRiskLevel=LOW
  CLASS_ADDED  ADDITIVE  LOW  classes  资产台账
  CLASS_ADDED  ADDITIVE  LOW  classes  资产类别
  JOIN_ADDED   ADDITIVE  LOW  joins    56|CATEGORY_CODE->57|CATEGORY_CODE
REALDATA RESULT: PASS
```

⇒ 真实答案：**治理表相对已发布基线多出 2 个类 + 1 条 JOIN，全是向后兼容（LOW），无破坏性变更。**

---

## 9. 关联

- **设计稿**：`docs/update/1109/AI驱动本体发现与持续演进_功能设计_V1.0.md`（F02 快照 / F06 差异）
- **计划**：[`plan.md`](./plan.md)（A1–A4；D-1…D-21 关键设计决定）
- **证据**：[`daily/A1.md`](./daily/A1.md)（recon 事实表 / 测试输出 / 归因 / 两轮审查处置 / 遗留登记）
- **Wiki**：[`Harness/wiki/ontology-discovery.md`](../../wiki/ontology-discovery.md)（本体发现与差异引擎）、
  [`Harness/wiki/data-model.md`](../../wiki/data-model.md)（数据模型）、
  [`Harness/wiki/api-reference.md`](../../wiki/api-reference.md)（接口契约）
- **Rules**：[`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md)、
  [`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md)、
  [`Harness/rules/编码规范.md`](../../rules/编码规范.md)
- **Memory**：`qa-system-ontology-discovery-a1.md`
  （+ 索引行 `MEMORY.md`）
- **关联变更（predecessor）**：[feat-semantic-release-a](../feat-semantic-release-a/summary.md)（P1a 语义发布）、
  [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b 发布镜像）
- **后续变更（successor）**：A2 剖析与证据（待 Q3）、A3 候选生成（待 Q5）、A4 提案与发布（待 Q4）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段迁移文件名 ≤ 32 字符（26 / 29 / 26）；版本号 ≤ `varchar(32)`（均 4 字符）
- [x] 第 7 段触发了 security-reviewer ⇒ 已填严重度结论 + **逐条处置表**（32 条）
- [x] 第 8 段生产装配冒烟命令 + 输出贴出（8.1/8.2/8.3）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新（数据模型 / 接口契约 / 新增「本体发现与差异引擎」）
- [x] 至少 1 条 MEMORY 索引已在 `MEMORY.md` 添加
- [x] 涉及真实 SQL/DB 改动时 `scripts/ontology_discovery_realdata.py` 已跑通（两模式均 PASS）

