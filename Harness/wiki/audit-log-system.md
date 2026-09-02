---
created: 2026-09-02
updated: 2026-09-02
sources:
  - docs/superpowers/plans/2026-09-02-audit-history-api.md
  - Harness/changes/2026-09-02-audit-history-api.md
  - backend/alembic/versions/0036_audit_actor_index.py
  - backend/app/api/v1/audit.py
  - backend/app/services/audit_service.py
  - backend/app/dependencies.py
  - frontend/src/pages/AdminAuditPage.tsx
tags:
  - audit
  - admin
  - acl
  - observability
  - governance
---

# 审计日志系统（Audit Log System）

「谁在什么时候改了治理对象」的可观测系统 — `audit_log` 表 + 写入 API + 查询 API +
流式导出 + 治理后台页面 + 12 类实体的写路径覆盖。

## 数据模型

### `audit_log` 表（Phase 4.5 既有）

| 列 | 类型 | 备注 |
|---|---|---|
| `id` | `int` PK | |
| `created_at` | `timestamp(tz)` | |
| `entity_type` | `varchar(64)` | **约定**：与 ORM `__tablename__` 一致（lowercase snake_case）。**Parked exception**：`ontology_*` 4 类仍是 UPPERCASE（T5 漂移） |
| `entity_id` | `int` | |
| `action` | `varchar(16)` | CHECK 约束限定 `CREATE` / `UPDATE` / `DELETE` |
| `actor` | `varchar(64)` | X-User-Id |
| `actor_departments` | `varchar(256)` | 逗号拼接存储 |
| `before_json` | `jsonb` | 变更前快照（CREATE 时 NULL） |
| `after_json` | `jsonb` | 变更后快照（DELETE 时 NULL） |
| `outbox_id` | `int` FK→outbox NULL | 仅 outbox 消费路径填写 |

### 索引

| 索引名 | 列 | 来源 | 用途 |
|---|---|---|---|
| `ix_audit_log_actor` | `(actor, created_at DESC)` | 0024 复合 | actor + 时间范围复合查询 |
| `idx_audit_log_actor` | `(actor)` | **0036 新增** | 单 actor 等值过滤 |
| `idx_audit_log_created_at` | `(created_at)` | **0036 新增** | 全表倒序排序 / 时间范围扫描 |

0036 新增的两个单列 B-tree 是与 0024 复合索引**共存**（不替换），
原因见 [`Harness/changes/2026-09-02-audit-history-api.md` §3](../changes/2026-09-02-audit-history-api.md)。

## 写入层

### `AuditService.record()` 模式

```python
# backend/app/services/audit_service.py:46-104
async def record(
    self,
    session: AsyncSession,
    entity_type: str,
    entity_id: int,
    action: str,
    actor: str,
    actor_departments: tuple[str, ...] | None = None,
    before: dict | None = None,
    after: dict | None = None,
    outbox_id: int | None = None,
) -> AuditLog:
```

**关键不变量**：

1. **只 `session.add`，不 commit**。调用方在同一事务 commit，
   让审计与业务同生共死（要么都成功，要么都回滚）。
2. **action 校验**：CREATE 必须有 after，UPDATE 必须有 before+after，DELETE 必须有 before。
3. **DB CHECK 约束兜底**：`action` 字段在 DB 层有 CHECK，代码校验先于 DB 校验。

### 写入模式（2 种并存）

#### 直写模式（多数服务）

```python
# service 层
session.add(entity)
await session.flush()
await self._audit.record(
    session,
    entity_type="data_source",
    entity_id=ds.id,
    action="UPDATE",
    actor=user.userId,
    actor_departments=user.departments,
    before=ds_dict_before,
    after=ds_dict_after,
)
await session.commit()
```

**优点**：调用栈短，调试直观。
**缺点**：审计失败 → 业务回滚（牺牲可用性换一致性）。
**适用**：业务量低、对一致性要求高的治理类操作（CRUD 频率不高）。

#### Outbox 模式（异步解耦）

```python
# service 层
session.add(entity)
await session.flush()
await self._outbox.enqueue(
    session,
    payload={
        "entity_type": "kpi_catalog",
        "entity_id": kpi.id,
        "action": "UPDATE",
        "actor": user.userId,
        "actor_departments": list(user.departments or []),
        "before": kpi_dict_before,
        "after": kpi_dict_after,
    },
)
await session.commit()
# 后台 AuditWorker.drainOnce() 异步消费 → _audit.record()
```

**优点**：审计失败不回滚业务（牺牲一致性换可用性）。
**缺点**：审计延迟可见（毫秒级），调试链路多一跳。
**适用**：高 QPS / 审计失败容忍度高的路径。

**当前采用 Outbox 的服务**：`kpi_catalog_service`、`entity_mapping_service`、
`feature_definition_service`。
其余服务均采用直写模式。

## 查询层

### `AuditService.listAll()` — 全局查询 + 分页

```python
async def listAll(
    self,
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = None,                    # ILIKE 模糊
    entity_id: str | None = None,                # CAST TEXT ILIKE
    actor_departments: str | None = None,        # ILIKE 模糊
    since: datetime | None = None,               # >=
    until: datetime | None = None,               # <
    limit: int = 100,                            # max 1000
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
```

**关键点**：

- 返回 `tuple[rows, total]`，前端要真实分页
- `total` 通过 `func.count().select_from(base_stmt.subquery())` 子查询计算，
  复用同一 `where` 条件保证语义一致
- 默认 `limit=100`，上限 `_MAX_LIMIT=1000`
- 所有过滤维度**可选**（None 即不筛选）

### `AuditService.iterAll()` — 流式 yield（导出专用）

```python
async def iterAll(
    self,
    session: AsyncSession,
    *,
    ... 同 listAll 参数 ...,
    max_rows: int = 100_000,
) -> AsyncIterator[AuditLog]:
```

- `session.stream(...)` 流式 yield，避免一次性加载到内存
- 超过 `max_rows` 时 `logger.warning` + 截断（不抛错）
- 仅导出端点使用，常规分页走 `listAll`

### 既有方法（admin-only 收紧后保留）

- `listByEntity(entity_type, entity_id)` — 单实体审计轨迹
- `listByActor(actor)` — 单用户操作历史
- `getById(id)` — 单条审计详情

## API 层

### `GET /api/v1/audit` — 全局查询

| 参数 | 类型 | 说明 |
|---|---|---|
| `entity_type` | str | 精确匹配 |
| `action` | str | `CREATE` / `UPDATE` / `DELETE` |
| `actor` | str | ILIKE 模糊 |
| `entity_id` | str | CAST TEXT ILIKE |
| `actor_departments` | str | ILIKE 模糊 |
| `since` | ISO datetime | `>=` |
| `until` | ISO datetime | `<` |
| `limit` | int (1-1000) | 默认 100 |
| `offset` | int (≥0) | |

**响应**：`AuditLogPage { rows: AuditLogRead[], total: int }`（camelCase）

### `GET /api/v1/audit/export?format=csv|json` — 流式导出

- **CSV**：列 `id, created_at, entity_type, entity_id, action, actor, actor_departments, before_json, after_json`
  - `csv.writer` 处理 RFC 4180 转义（含逗号/引号/换行的 JSONB 安全）
  - `Content-Disposition: attachment; filename="audit.csv"`
  - `media_type: text/csv; charset=utf-8`
- **NDJSON**（`format=json`）：每行一个 JSON 对象，camelCase 字段
  - `media_type: application/x-ndjson`
  - `Content-Disposition: attachment; filename="audit.jsonl"`
- 上限：100,000 行（`EXPORT_MAX`）
- ACL：admin-only

### 既有 3 端点 ACL 收紧

`GET /api/v1/audit/{id}` / `/by-entity/{entity_type}/{entity_id}` / `/by-actor/{actor}`
均改为 `Depends(getAdminOnlyActor)`。

## ACL

### `getAdminOnlyActor`（dependencies.py）

```python
async def getAdminOnlyActor(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    if "admin" not in (user.roles or []):
        raise PermissionDeniedError(detail="仅 admin 可访问审计 API", ...)
    return user
```

**设计要点**：

- **依赖 `getCurrentUser`**（已解析 JWT/stub），不接受 client 直接传 admin
- 403 而非 401（ACL 是「角色不足」语义，不是「未认证」）
- stub auth 默认 `DEFAULT_STUB_ROLES = ("user", "admin")`，
  所以 dev/test 默认放行；CI 必须显式 `X-User-Roles=user` 验证 403

## 前端层

### `AdminAuditPage`（`frontend/src/pages/AdminAuditPage.tsx`）

**布局**：

```
┌─ /admin/audit ───────────────────────────────────┐
│ [Filter Bar]                                     │
│  entity_type Select | action Select |             │
│  actor Input | actorDepartments Input |           │
│  DatePicker.RangePicker (since - until)            │
│                                                  │
│ [Results]                                        │
│  total: 1234                                     │
│  ┌────────────────────────────────────────────┐   │
│  │ created_at | action | entity_type | ...    │   │
│  └────────────────────────────────────────────┘   │
│  [Pagination]                                    │
│                                                  │
│ [Export Dropdown]                                │
│  CSV | JSON                                      │
└──────────────────────────────────────────────────┘
```

**关键交互**：

- `DatePicker.RangePicker` 替代旧版两个 DatePicker（`showTime: true` → ISO datetime）
- `Dropdown` 触发导出，loading 状态用 `Button.loading`
- 真实 `total` 渲染（非「has next」探测）
- `actorDepartments` Input 单独过滤（之前混在 actor 字段）

### `api/audit.ts`

```typescript
export function listAuditLogs(params: AuditLogFilters): Promise<AuditLogPage>;
export function exportAuditLogs(
  params: AuditLogFilters,
  format: "csv" | "json",
): Promise<Blob>;
```

### i18n

5 新增 key × 2 语言（zh-CN / en-US）：

- `audit.export` — 导出按钮文案
- `audit.export.csv` — CSV 菜单项
- `audit.export.json` — JSON 菜单项
- `audit.export.loading` — 导出中状态
- `audit.filters.actorDepartments` — 部门输入框 placeholder

## 覆盖的 12 类实体

| `entity_type` 实际值 | 服务 | 模式 | 来源 |
|---|---|---|---|
| `ontology_class` *(实际 `ONTOLOGY_CLASS`)* | ontology_service | 直写 | T5 |
| `ontology_property` *(实际 `ONTOLOGY_PROPERTY`)* | ontology_service | 直写 | T5 |
| `ontology_metric` *(实际 `ONTOLOGY_METRIC`)* | ontology_service | 直写 | T5 |
| `ontology_join` *(实际 `ONTOLOGY_JOIN`)* | ontology_service | 直写 | T5 |
| `kpi_catalog` | kpi_catalog_service | Outbox | Phase 4.5 + T6 |
| `entity_mapping` | entity_mapping_service | Outbox | T7 |
| `data_source` | datasource_service | 直写 | T8 |
| `agent_definition` | agent_registry_service | 直写 | T9（4 actions） |
| `agent_run_log` | chat_service | 直写 | T10（record-on-finish） |
| `agent_schedule` | agent_scheduler_service | 直写 | T11 |
| `document_catalog` | document_service | 直写 | T12 |
| `data_quality_score` | data_quality_score_service | 直写 | T13 |

## 测试覆盖

| 层 | 文件 | 用例数 |
|---|---|---|
| 后端单测 | `tests/unit/test_audit_service.py` | 8 |
| 后端集成 | `tests/integration/test_audit_api.py` | 10 |
| 后端集成 | `tests/integration/test_audit_export_api.py` | 4 |
| 后端集成 | `tests/integration/test_*_audit.py` × 9 | 54 |
| 前端 | `tests/AdminAuditPage.test.tsx` | 5 |

## 相关条目

- [[Acl Security Review Pattern]] — ACL 扩展必查 4 项（DTO mass-assignment / 403 侧信道 / actor 派生 / 非 admin 集成测试）
- [[变更 2026-09-02-audit-history-api]] — 本次变更 SSOT
- [[qa-system Agent Vocabulary]] — `actor_departments` 词表约束（与 AGENT_DATA_DOMAINS 不同维度，仅用于审计追踪）