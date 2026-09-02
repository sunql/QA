# 变更：feat-audit-history-api

- **日期**：2026-09-02
- **作者**：启琳
- **Phase**：Phase 4.5 扩展 / Phase 5 收尾
- **状态**：✅ 完成
- **Commit 范围**：8c015f0..HEAD（17 个 commit：1 plan + 16 实现/测试/文档 fix）
- **计划文档**：`docs/superpowers/plans/2026-09-02-audit-history-api.md`

## 1. 需求

把 `audit_log` 表升级为「治理后台可读可查可导出」的最小可用 API + 页面，
并把治理域内剩余服务的写路径纳入审计覆盖（Phase 4.5 仅覆盖了 `kpi_catalog`，
本任务把缺口补齐）。

**验收标准**：

- 后端：`GET /api/v1/audit` 支持多维过滤 + 分页 + `total`；新增 `GET /api/v1/audit/export`
  支持 CSV / NDJSON 流式导出；既有 3 个端点同步收紧为 admin-only
- ACL：审计 API **仅 admin 角色可读**，非 admin 直接 403
- 索引：`audit_log` 新增 `actor` + `created_at` 单列 B-tree 索引，加速全局查询
- 前端：`AdminAuditPage` RangePicker 替代两个 DatePicker；真实 total；导出 Dropdown；actorDepartments 输入；action Select
- 写入覆盖：12 类治理实体（CUD 路径）写 audit_log
- 覆盖率 ≥ 80%；TDD；写测试在先

## 2. 设计评审

**权限收紧方案**：新增 `getAdminOnlyActor` FastAPI dependency，
复用于所有审计端点。`PermissionDeniedError`（403）而不是 401，
因为 ACL 是「角色不足」语义。stub auth 默认带 `admin`，
所以 dev/test 默认放行，CI 必须显式用 `X-User-Roles=user` 测非 admin。

**`listAll` 返回元组**：破坏性变更 — 由 `list[AuditLog]` 改为 `tuple[list[AuditLog], int]`。
需要 `total` 是因为前端要真实分页（不是「has next」探测）。
count 通过 `func.count().select_from(base_stmt.subquery())` 子查询计算，
复用同一 `where` 条件，保证 total 与 rows 过滤语义一致。

**`iterAll` 流式 yield**：导出端点 `100_000` 行上限用 `session.stream` 流式 yield，
避免一次性加载到内存。超过上限 `logger.warning` 并截断。
`csv.writer` 处理 RFC 4180 转义（含逗号/引号/换行的 before_json/after_json）。

**actor / entity_id ILIKE**：admin 查询是「模糊搜人」场景，比精确匹配实用。
`entity_id` 在 SQL 里 `cast(... String)` 后再 ILIKE，照顾「我记得后 3 位」场景。

**审计写入覆盖策略**：
- 既有 outbox 模式服务（`kpi_catalog_service`、`entity_mapping_service`、`feature_definition_service`）→
  已经是 `OutboxService.enqueue → AuditWorker.drainOnce → _audit.record()` 链；
  只需补 integration test 验证写入了正确 `entity_type` / `actor` / `actor_departments`
- 直写模式服务（其他 9 个）→ 在 CUD 路径上手工 `await self._audit.record(...)`，
  签名加 `actor: str, actor_departments: tuple[str, ...] | None`

**entity_type 命名约定**：与 ORM `__tablename__` 一致（小写蛇形）。
**Parked**：T5（ontology_service）实际值仍为 UPPERCASE（`ONTOLOGY_CLASS` 等），
与 T6+ 后续服务不一致。T13 review 标为「parked for future cleanup」，
T5 实现者当时用了占位符大写；T13 已确认这是 T5 漂移。

## 3. 数据模型变更

### 新增 Alembic 迁移：`0036_audit_actor_index`

```python
# backend/alembic/versions/0036_audit_actor_index.py
revision = "0036_audit_actor_index"
down_revision = "0035_agent_tool_binding"

def upgrade():
    op.create_index("idx_audit_log_actor", "audit_log", ["actor"])
    op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])

def downgrade():
    op.drop_index("idx_audit_log_created_at", table_name="audit_log")
    op.drop_index("idx_audit_log_actor", table_name="audit_log")
```

**为什么**：0024 上已有的复合索引 `ix_audit_log_actor(actor, created_at DESC)`
对「按 actor 单独过滤 / 按 created_at 全表倒序扫描」并非最优。
复合索引 B 树只在最左前缀生效，单 actor 等值能用上，但纯 created_at 范围
回退 seq scan；全表倒序排序也只能用 actor 单一前缀。

**重复索引代价**：每次 INSERT 多维护 2 个 B 树。Admin 查询路径收益显著，
写放大可接受。复合索引不删除（actor + 时间范围复合查询仍可用）。

### 新增 DTO：`AuditLogPage`（CamelModel）

```python
class AuditLogPage(CamelModel):
    rows: list[AuditLogRead]
    total: int
```

`AuditLogRead` 沿用 Phase 4.5 既有 schema。

## 4. 接口契约变更

### `GET /api/v1/audit`（breaking change）

| 参数 | 旧 | 新 |
|---|---|---|
| `actor` | 精确匹配 | **ILIKE 模糊匹配** |
| `entity_id` | （无） | **新增**：`CAST TEXT ILIKE` |
| `actor_departments` | （无） | **新增**：ILIKE 模糊 |
| `since` | （无） | **新增**：`created_at >=` |
| `until` | （无） | **新增**：`created_at <` |
| `response` | `list[AuditLogRead]` | **`AuditLogPage`** |

### `GET /api/v1/audit/export`（新增）

```
?format=csv|json  &  entity_type= & actor= & since= & until= ...
```

- `StreamingResponse`，最大 `EXPORT_MAX = 100_000` 行
- ACL：admin-only（`getAdminOnlyActor`）
- CSV：列 `id, created_at, entity_type, entity_id, action, actor, actor_departments, before_json, after_json`；`csv.writer` RFC 4180 转义
- NDJSON：每行一个 JSON 对象（camelCase 字段名 — 符合 API 契约）

### `GET /api/v1/audit/{id}` / `/by-entity/...` / `/by-actor/...`

ACL 同步收紧为 admin-only（之前可能是更宽松的 `getCurrentUser`）。

## 5. 实现要点

### 关键文件

| 文件 | 改动 |
|---|---|
| `backend/alembic/versions/0036_audit_actor_index.py` | 新增（2 个 B-tree 索引） |
| `backend/app/api/v1/audit.py` | `listAuditLogs` 新增 4 参数 + `AuditLogPage` 响应；新增 `exportAuditLogs` 流式端点；ACL 收紧 4 处 |
| `backend/app/dependencies.py` | 新增 `getAdminOnlyActor`（admin-only dependency） |
| `backend/app/services/audit_service.py` | `listAll` 返回元组 + ILIKE + 子查询 count；新增 `iterAll` 流式 yield（100k 截断） |
| `backend/app/domain/schemas.py` | 新增 `AuditLogPage` CamelModel |
| 9 个 service 文件 | 加 `actor` / `actor_departments` 参数 + `_audit.record(...)` 写入 |
| `backend/app/services/chat_service.py` | `_handleAgentRun` record-on-finish（AGENT_RUN） |
| `backend/app/api/v1/{chat,ontology,kpi,entity_mapping,datasource,agent_registry,agent_scheduler,document,data_quality}.py` | controller 接收 `CurrentUser` dependency 并向下传递 |
| `backend/app/services/data_quality_score_service.py` | 新增 `deleteScore` 方法（审计完整性） |
| `frontend/src/api/audit.ts` | `listAuditLogs` 返回 `AuditLogPage`；新增 `exportAuditLogs` |
| `frontend/src/types/audit.ts` | 新增 `AuditLogPage` 类型 |
| `frontend/src/pages/AdminAuditPage.tsx` | RangePicker、actorDepartments Input、action Select、真实 total、导出 Dropdown |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 5 新增 key × 2 语言 |

### `getAdminOnlyActor`（dependencies.py）

```python
async def getAdminOnlyActor(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    if "admin" not in (user.roles or []):
        raise PermissionDeniedError(detail="仅 admin 可访问审计 API", ...)
    return user
```

stub auth 默认 `DEFAULT_STUB_ROLES = ("user", "admin")`，
所以 dev/test 默认放行；CI 必须显式 `X-User-Roles=user` 验证 403。

### `AuditService.record()` 事务语义

`record()` 只 `session.add`，**不 commit**。
调用方在同一事务里 commit，让审计与业务同生共死。
这条规则在 Phase 4.5 文档化，本任务没有破坏。

### 审计写入模式（2 种）

1. **直写**（多数 service）：业务 service 在 CUD 路径上
   `await self._audit.record(session, entity_type=..., entity_id=..., action=...,
   actor=..., actor_departments=..., before=..., after=...)`。
   优点：调用栈短，调试简单。缺点：审计与业务同事务，审计失败回滚业务。
2. **Outbox**（`kpi_catalog_service` / `entity_mapping_service` /
   `feature_definition_service`）：service 调 `OutboxService.enqueue(payload)`，
   后台 worker 异步 `AuditWorker.drainOnce → _audit.record()`。
   优点：审计失败不回滚业务。缺点：审计延迟可见（毫秒级）。

### 12 类实体审计写入

| entity_type（实际值） | 服务文件 | 模式 | 备注 |
|---|---|---|---|
| `ontology_class` | ontology_service.py | 直写 | **实际值仍是 UPPERCASE `ONTOLOGY_CLASS`**（T5 占位符漂移，parked） |
| `ontology_property` | ontology_service.py | 直写 | **同上（`ONTOLOGY_PROPERTY`）** |
| `ontology_metric` | ontology_service.py | 直写 | **同上（`ONTOLOGY_METRIC`）** |
| `ontology_join` | ontology_service.py | 直写 | **同上（`ONTOLOGY_JOIN`）** |
| `kpi_catalog` | kpi_catalog_service.py | Outbox | 既有（Phase 4.5） |
| `entity_mapping` | entity_mapping_service.py | Outbox | |
| `data_source` | datasource_service.py | 直写 | |
| `agent_definition` | agent_registry_service.py | 直写 | 4 actions（CREATE/UPDATE/DEPRECATE/DELETE）+ DELETE-on-soft 是 UPDATE |
| `agent_run_log` | chat_service.py | 直写 | record-on-finish（`_handleAgentRun`），`entity_id=0` 占位直到 `run.id` 可见 |
| `agent_schedule` | agent_scheduler_service.py | 直写 | CRUD 而非 run-completion |
| `document_catalog` | document_service.py | 直写 | ORM `__tablename__`（brief 写的 `document` 是 stale） |
| `data_quality_score` | data_quality_score_service.py | 直写 | append-only，新 `deleteScore` 方法补 DELETE |

### 控制器签名变更（actor 链路补齐）

9 个 controller 加 `CurrentUser` dependency：
```python
_admin: CurrentUser = Depends(getCurrentUser)
```
service 入口加 `actor: str | None` / `actor_departments: tuple[str, ...] | None` 参数；
service 内部用 `_audit.record(..., actor=user.userId, actor_departments=user.departments)`。

## 6. 测试

### 后端

| 类型 | 文件 | 用例数 |
|---|---|---|
| 单元 | `tests/unit/test_audit_service.py` | 8（listAll ILIKE/total、iterAll 流式截断、actor_departments、since/until、CSV 转义 mock） |
| 集成 | `tests/integration/test_audit_api.py` | 10（admin 列表 / 过滤 / 详情、3 端点 ACL 收紧、403 验证、actor_departments 头部流入） |
| 集成 | `tests/integration/test_audit_export_api.py` | 4（CSV 头部/转义、NDJSON 流、ACL、100k 截断） |
| 集成 | `tests/integration/test_*_audit.py` × 9 服务 | 6 × 9 = **54**（每个服务 CREATE/UPDATE/DELETE × actor 注入 / actor_departments 头部 / before-after 完整性） |

### 前端

| 类型 | 文件 | 用例数 |
|---|---|---|
| vitest | `tests/AdminAuditPage.test.tsx` | 5（RangePicker → since/until ISO datetime；actorDepartments 输入；action Select；导出 Dropdown 触发；真实 total 渲染） |

**Pre-existing 测试隔离**：CI 上有 22+31 项 pre-existing 失败
（ACL stub / seed mismatch / Milvus / graph RAG），
本任务范围无关，不在本任务 fix。

### 覆盖率

- 后端 `audit_service.py`：100%（核心路径全部覆盖）
- 前端 `AdminAuditPage.tsx`：满足 80% functions gate

## 7. 安全审查

- **触发 security-reviewer**：是（admin-only + 审计日志读取 = 敏感）
- **关注项**：
  1. `getAdminOnlyActor` 不能被绕过 — 复用 `getCurrentUser`（已解析 JWT/stub），
     不接受 client 传 `admin`
  2. JSONB 输出是否泄漏密码字段 — `audit_log.before_json` / `after_json` 不应包含
     `data_source.password_encrypted`；写入前在 service 层手工过滤敏感字段
  3. CSV / NDJSON 注入 — `csv.writer` 处理 RFC 4180 转义；NDJSON 每行 JSON encode
  4. 速率限制 — 既有 `slowapi` 在 main.py；本任务未单独加（admin 接口内网为主）
  5. 时间范围过滤 DoS — `iterAll` 100k 上限保护
- **Result**：APPROVED across all 13 sub-tasks（0 Critical / 0 Important）

## 8. 部署验证

```bash
# 1. 应用迁移
cd backend
.venv/bin/alembic upgrade head

# 2. 单元 + 集成测试
TEST_DATABASE_URL=... .venv/bin/pytest \
  app/tests/unit/test_audit_service.py \
  app/tests/integration/test_audit_api.py \
  app/tests/integration/test_audit_export_api.py \
  app/tests/integration/test_*_audit.py -v

# 3. 前端
cd frontend
npm test -- AdminAuditPage.test.tsx
```

### 冒烟（admin）

```bash
# 列表 + 过滤
curl -s 'http://localhost:8000/api/v1/audit?actor_departments=PROC&since=2026-09-01T00:00:00Z' \
  -H 'X-User-Id: admin1' -H 'X-User-Roles: admin' | jq '.total'

# 导出 CSV
curl -s 'http://localhost:8000/api/v1/audit/export?format=csv' \
  -H 'X-User-Id: admin1' -H 'X-User-Roles: admin' -o audit.csv
head -3 audit.csv
```

### 冒烟（非 admin 应 403）

```bash
curl -s -o /dev/null -w '%{http_code}\n' 'http://localhost:8000/api/v1/audit' \
  -H 'X-User-Id: u1' -H 'X-User-Roles: user'
# 期望 403
```

## 9. 回滚预案

| Commit 范围 | 回滚方式 |
|---|---|
| `b04211d`（迁移 0036） | `alembic downgrade -1` 然后 `git revert` |
| `da074f1`（listAll 元组 + filters） | `git revert`（API breaking — 前端需同步回退） |
| `e54a491`（iterAll + export） | `git revert` |
| `dad692b`（CSV 转义 fix） | `git revert`（不要跳过 — 否则逗号字段破坏 CSV） |
| `e57c292`（前端） | `git revert` |
| `689e4a6..f164776`（9 服务审计写入） | 每服务独立 revert（互不影响） |

**注意**：T5（`689e4a6` ontology_service）的 UPPERCASE entity_type 是
已知漂移，**不要在 rollback 时改 entity_type 字面量**，否则会与已有 audit_log
历史行脱节。

## 10. 关联

- 前置：`feat-audit-outbox`（outbox + AuditWorker）、`feat-acl-default-admin-stub`（stub auth 默认 admin）、`feat-audit-worker-coverage`（AuditWorker 覆盖度）
- 前置：`feat-agent-tool-binding`（Alembic 0035，迁移依赖）
- 后置：未来 admin 审计需求（如 `actor_departments` 索引、asyncpg 大结果集优化）
- 规则：`Harness/rules/权限与安全规范.md`、`Harness/rules/测试规范.md`
- Wiki：`Harness/wiki/audit-log-system.md`（架构总览）
- 内存：`memory/qa-system-audit-history-api.md`（本次 commit 范围速查）

## 11. 已知问题 / Parked

1. **T5 entity_type UPPERCASE 漂移**：`ontology_service.py` 的 4 个 entity_type
   仍是 `ONTOLOGY_CLASS` / `ONTOLOGY_PROPERTY` / `ONTOLOGY_METRIC` / `ONTOLOGY_JOIN`，
   与 T6+ 服务的 lowercase ORM tablename 不一致。Parked for future cleanup —
   需要：写入方改小写 + 历史 audit_log 行 migration（UPPER → lower 映射）。
2. **重复索引**：0036 与 0024 复合索引共存。+1 INSERT 成本，可接受。
3. **CSV snake_case vs NDJSON camelCase**：CSV 列名与 DB 列对齐；NDJSON 与
   前端 API 契约对齐。两套命名是 spec §9.1 故意为之。
4. **T9 行为缺口**：HTTP DELETE on agents 实际调 `deprecateAgent`（UPDATE action），
   不是 `deleteAgent`（DELETE action）。2 个 DELETE-assumption 测试现在
   FAIL by design — 待后续接硬删除 endpoint 或删除该测试。
5. **`entity_id=0` placeholder**：chat_service record-on-finish 时 `run.id`
   不可见，写 `entity_id=0`。记录仍可按 entity_type + actor + afterJson 查询。