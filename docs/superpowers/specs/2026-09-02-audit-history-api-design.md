# Audit History API 增强 + 治理写入补齐（feat-audit-history-api）设计方案

> 日期：2026-09-02 | 状态：draft → 评审中 | 关联：`feat-agent-tool-binding`（已发）
> 前置：`feat-agent-runtime-mvp`、`feat-agent-registry`、`feat-agent-vocabulary`、`feat-agent-tool-binding`
> 关联既有：`Phase 4.5 治理 API`（audit_log 表 + 4 endpoints + audit_worker）

## 1. 背景与目标

### 1.1 现状

`audit_log` 表与查询 API 已在 **Phase 4.5** 落地，但有 3 类不足：

1. **查询体验差**：`GET /api/v1/audit` 仅 `entity_type / action / actor / limit / offset`，不支持 `actor_departments` / `created_at` 区间 / 模糊 `actor`；分页 total 是前端 hack（AdminAuditPage.tsx:78「前端记录总数」）
2. **导出缺失**：admin 不能离线分析；现有最大 1000 行截断
3. **写入覆盖不全**：仅 `feature_definition_service.py` + `outbox_service.py` 调用 `audit.record()`；12 类治理实体（kpi/ontology/entity_mapping/data_source/agent_def/document/data_quality/chat agent run/agent scheduler run）的 create/update/delete 无审计落库

### 1.2 目标

把 audit_log 系统从「仅 2 类实体可追溯」升级到「**治理 + Agent 运行全实体可追溯 + admin 完整查询/导出能力**」，支撑治理合规审计。

### 1.3 非目标（明确不做）

- Outbox 化重构（既有同步直写模式与 feature_definition_service 一致，迁移成本高，独立排期）
- 历史回放（point-in-time entity state recovery，由 `history-replay-api` 单独承接，本期不合并）
- 实时通知（无 WebSocket / SSE 需求）
- 多租户隔离（单租户架构，未引入 row-level security）

## 2. 已对齐的设计决策

| 维度 | 选型 | 理由 |
|---|---|---|
| 范围 | Audit List API 补齐 + 12 类实体写入补齐 + 后端 export | 治理强需求；与 6 Phase 长线计划衔接 |
| 写覆盖 | 治理 + Agent 运行 + 文档 + 数据质量 = 12 类 | admin 视图覆盖全部业务实体；FEATURE_DEFINITION 既有 audit 复用 |
| 写 pattern | 手动调用 `audit.record()`（与 feature_definition_service 一致） | 语义清晰；不引入 outbox 复杂度；与既有风格一致 |
| 过滤 | 模糊 `actor`（ILIKE）+ `actor_departments` 子串 + `since/until` 区间 | admin UX；ILIIKE 与 admin 输入习惯匹配 |
| 旧端点 | 保留 `by-entity/{t}/{id}` / `by-actor/{actor}` / `{id}` | 详情页跳转；admin detail UX 不变 |
| Export | 后端 `/audit/export?format=csv|json` 流式 | 大数据量；StreamingResponse 控制内存 |
| 日期 UX | 单 `RangePicker`（合并 since+until） | antd 5 标准；少一次 click |
| ACL | admin only（替换既有 `getCurrentUser`） | 治理强化；与 acl-extension 模式一致 |
| Total count | 后端 `AuditLogPage.total`；前端替换 hack | 与既有 `Page[T]` 模式对齐 |

## 3. 数据模型

### 3.1 无新列（仅新索引）

`audit_log` 表已存在；不增列。本期增 1 个 btree 索引：

```python
# Alembic 0036
op.create_index("idx_audit_log_actor", "audit_log", ["actor"])
op.create_index("idx_audit_log_created_at", "audit_log", ["created_at"])
```

`idx_audit_log_actor` 加速 ILIKE 子串搜索（PG btree 对 `LIKE 'xxx%'` 前缀匹配有效；纯子串需 `pg_trgm` 扩展，本期不引入，前缀场景足够）。
`idx_audit_log_created_at` 已有 ORM `index=True`（确认）则跳过；否则本迁移加。

### 3.2 Response Envelope（不破坏既有契约）

```python
# backend/app/domain/schemas.py（新增）
class AuditLogPage(CamelModel):
    """审计日志分页响应。"""
    rows: list[AuditLogRead]
    total: int
```

**向后兼容**：`GET /api/v1/audit` 既有调用方期望 `list[AuditLogRead]`。本期切到 `AuditLogPage`，前端 AdminAuditPage 同步对齐；既有外部调用方（无）需同步更新（grep 全仓确认无外部依赖）。

## 4. Runtime / API 契约

### 4.1 `GET /api/v1/audit` 扩展

```python
@router.get("", response_model=AuditLogPage)
async def listAuditLogs(
    entity_type: Annotated[str | None, Query()] = None,       # exact（不变）
    action: Annotated[str | None, Query()] = None,            # exact（不变）
    actor: Annotated[str | None, Query()] = None,             # ILIKE 模糊（变）
    actor_departments: Annotated[str | None, Query()] = None, # NEW：子串
    since: Annotated[datetime | None, Query()] = None,        # NEW：created_at >= since
    until: Annotated[datetime | None, Query()] = None,        # NEW：created_at < until
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    _admin: CurrentUser = Depends(getAdminOnlyActor),          # 收紧：原 getCurrentUser
    db: AsyncSession = Depends(getDb),
) -> AuditLogPage:
    rows, total = await _service.listAll(db, ...)
    return AuditLogPage(rows=[AuditLogRead.model_validate(r) for r in rows], total=total)
```

### 4.2 `GET /api/v1/audit/export` 新增

```python
@router.get("/export", response_class=StreamingResponse)
async def exportAuditLogs(
    format: Annotated[Literal["csv", "json"], Query()] = "csv",
    entity_type: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    actor: Annotated[str | None, Query()] = None,
    actor_departments: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    db: AsyncSession = Depends(getDb),
) -> StreamingResponse:
    """流式导出审计日志（admin only）。最大 100k 行。"""
    EXPORT_MAX = 100_000
    rows_gen = _service.iterAll(db, ..., max_rows=EXPORT_MAX)
    if format == "csv":
        return StreamingResponse(_stream_csv(rows_gen),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="audit.csv"'})
    return StreamingResponse(_stream_json(rows_gen),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.jsonl"'})
```

CSV 列：`id,created_at,entity_type,entity_id,action,actor,actor_departments,before_json,after_json`。
JSON Lines：每行一个 JSON 对象（与 csv 等字段）。

### 4.3 既有 3 个端点 ACL 收紧

`/audit/by-entity/{t}/{id}`、`/audit/by-actor/{actor}`、`/audit/{id}` 全部把 `_user: CurrentUser = Depends(getCurrentUser)` 替换为 `_admin: CurrentUser = Depends(getAdminOnlyActor)`。

### 4.4 `AuditService.listAll` 增强

```python
async def listAll(
    self,
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = None,              # 变：模糊 ILIKE '%actor%'
    actor_departments: str | None = None,  # NEW
    since: datetime | None = None,         # NEW
    until: datetime | None = None,         # NEW
    limit: int = _DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """返回 (rows, total)。"""
    where = []
    if entity_type: where.append(AuditLog.entity_type == entity_type)
    if action: where.append(AuditLog.action == action)
    if actor: where.append(AuditLog.actor.ilike(f"%{actor}%"))
    if actor_departments: where.append(AuditLog.actor_departments.ilike(f"%{actor_departments}%"))
    if since: where.append(AuditLog.created_at >= since)
    if until: where.append(AuditLog.created_at < until)

    base_stmt = select(AuditLog).where(*where).order_by(AuditLog.created_at.desc())
    count_stmt = select(func.count()).select_from(base_stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = base_stmt.limit(limit).offset(offset)
    rows = list((await session.execute(stmt)).scalars().all())
    return rows, total
```

### 4.5 `AuditService.iterAll` 新增

```python
async def iterAll(
    self,
    session: AsyncSession,
    *,
    entity_type: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    actor_departments: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    max_rows: int = 100_000,
) -> AsyncIterator[AuditLog]:
    """流式 yield；超过 max_rows 截断并 log warning。"""
    # 用 SQLAlchemy 2.0 async streaming (session.stream) 避免一次加载
    stmt = select(AuditLog).where(...).order_by(AuditLog.created_at.desc()).limit(max_rows)
    result = await session.stream(stmt)
    async for row in result.scalars():
        yield row
```

## 5. ACL：Admin-only Actor Helper

```python
# backend/app/dependencies.py（新增）
async def getAdminOnlyActor(
    user: CurrentUser = Depends(getCurrentUser),
) -> CurrentUser:
    """admin-only 写路径的 actor 派生（与 acl-extension 一致）。

    非 admin 抛 HTTPException 403（与既有 assertCanModify 模式一致）。
    """
    if "admin" not in (user.roles or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅 admin 可访问审计 API",
        )
    return user
```

与 `acl_service.assertCanModify(actor, ...)` 平行（前者 admin only，后者 per-entity 权限）。两者不冲突——audit 是治理路径，独立 ACL rule。

## 6. 写路径补齐 — 12 类实体

### 6.1 统一调用 Pattern

```python
from app.services.audit_service import AuditService
_audit = AuditService()

async def createEntity(self, session, dto, *, actor: str,
                       actor_departments: tuple[str, ...] | None = None):
    entity = Entity(...)
    session.add(entity)
    await session.flush()  # 取 entity.id
    await _audit.record(
        session,
        entity_type="ENTITY_MAPPING",
        entity_id=entity.id,
        action="CREATE",
        actor=actor,
        actor_departments=actor_departments,
        after=entity.to_dict(),
    )
    # 不 commit；调用方 commit（与 feature_definition_service 一致）
```

**事务语义**：`record()` 只 `session.add`，不 commit。调用方 commit 让「业务写入 + 审计写入」同事务（要么都成功，要么都回滚）。这是 audit_service.py:9-16 的既有约定。

**actor 注入**：所有 service 必须从 controller 透传 `actor: str` + `actor_departments: tuple[str, ...] | None`；缺失则本 phase 0 同步补。

### 6.2 12 类实体改动列表

| # | entity_type | 服务文件 | write 方法 | 备注 |
|---|---|---|---|---|
| 1 | `ONTOLOGY_CLASS` | `ontology_service.py` | `createClass` / `updateClass` / `deleteClass` | 既有 actor 上下文 |
| 2 | `ONTOLOGY_PROPERTY` | 同上 | `createProperty` / `updateProperty` / `deleteProperty` | |
| 3 | `ONTOLOGY_METRIC` | 同上 | `createMetric` / `updateMetric` / `deleteMetric` | |
| 4 | `ONTOLOGY_JOIN` | 同上 | `createJoin` / `updateJoin` / `deleteJoin` | |
| 5 | `KPI_CATALOG` | `kpi_catalog_service.py` | `createKpi` / `updateKpi` / `deleteKpi` | |
| 6 | `ENTITY_MAPPING` | `entity_mapping_service.py` | `createEntityMapping` / `updateEntityMapping` / `deleteEntityMapping` | |
| 7 | `DATA_SOURCE` | `datasource_service.py` | `createDataSource` / `updateDataSource` / `deleteDataSource` | |
| 8 | `AGENT_DEFINITION` | `agent_registry_service.py` | `createAgent` / `updateAgent` / `deprecateAgent` / `deleteAgent` | 与 feat-agent-tool-binding 复用 |
| 9 | `AGENT_RUN` | `chat_service.py` | `runAgent` 结束时 `record-on-finish` | 异步一次性记录 |
| 10 | `AGENT_SCHEDULER_RUN` | `agent_scheduler_service.py` | 每次 schedule run 完成 | |
| 11 | `DOCUMENT` | `document_service.py` | `createDocument` / `updateDocument` / `deleteDocument` | |
| 12 | `DATA_QUALITY_SCORE` | `data_quality_score_service.py` | `createScore` / `updateScore` / `deleteScore` | |

每个 service 1 个独立 commit，便于 review 与回滚。

### 6.3 actor 链路补齐

部分 service 当前未在 controller 层透传 `actor`（catalog review 阶段 — phase 0）。缺失则需：

1. 在 controller 加 `actor: str` / `actor_departments: tuple[str, ...] | None` 参数
2. 从 `CurrentUser.userId` / `CurrentUser.departments` 派生
3. service 层透传到 `audit.record(actor=..., actor_departments=...)`

不走 `getCurrentUser()` 直接在 service 内取（保持 service 与 ACL 解耦）。

## 7. 前端 AdminAuditPage

### 7.1 types/audit.ts 扩展

```typescript
export interface AuditLogPage {
  rows: AuditLog[];
  total: number;
}

export interface AuditLogFilters {
  // 既有字段保持
  entityType?: string;
  entityId?: string;       // 保留但本期后端未用（by-entity 端点）
  actor?: string;
  since?: string;
  until?: string;
  limit?: number;
  offset?: number;
  // NEW
  actorDepartments?: string;
  action?: string;
}
```

### 7.2 api/audit.ts 扩展

```typescript
export async function listAuditLogs(
  filters: AuditLogFilters = {},
): Promise<AuditLogPage> {  // 变：AuditLog[]
  const res = await httpClient.get<AuditLogPage>(BASE, { params: filters });
  return res.data;
}

export async function exportAuditLogs(
  filters: AuditLogFilters,
  format: "csv" | "json",
): Promise<Blob> {
  const res = await httpClient.get(`${BASE}/export`, {
    params: { ...filters, format },
    responseType: "blob",
  });
  return res.data;
}
```

### 7.3 AdminAuditPage.tsx 改动

1. **RangePicker 替换两个 DatePicker**：
   ```tsx
   <RangePicker
     showTime
     onChange={(dates, dateStrings) => {
       updateFilter("since", dateStrings[0] ?? "");
       updateFilter("until", dateStrings[1] ?? "");
     }}
   />
   ```

2. **「导出」按钮**（Dropdown）：
   ```tsx
   <Dropdown menu={{ items: [
     { key: "csv", label: "CSV", onClick: () => void handleExport("csv") },
     { key: "json", label: "JSON", onClick: () => void handleExport("json") },
   ]}}>
     <Button icon={<DownloadOutlined />}>导出</Button>
   </Dropdown>
   ```

3. **真实 total**：替换 AdminAuditPage.tsx:78 hack：
   ```typescript
   setTotal(res.total);
   ```

4. **i18n 新增**：`audit.export` / `audit.export.csv` / `audit.export.json` / `audit.export.loading` × 2 语言。

5. **actorDepartments filter Input** + **action filter Select**（与 entityType 同样 select 风格）。

### 7.4 路由核对

AdminAuditPage 路由层应已限制 admin only（既有约定）。本 phase 0 grep 确认；缺失则同步收紧。

## 8. 测试覆盖

### 8.1 Unit

| 文件 | 用例 | 数量 |
|---|---|---|
| `tests/unit/test_audit_service.py`（扩展） | `listAll` ILIKE 命中 / actor_departments 命中 / since/until 区间 / total count 与 rows 一致 / `iterAll` yield 行为（超 max_rows 截断） | 8 |

### 8.2 Integration — API

| 文件 | 用例 | 数量 |
|---|---|---|
| `tests/integration/test_audit_api.py`（扩展） | `listAuditLogs` 返回 `AuditLogPage` shape / ILIKE 模糊命中 / actor_departments 命中 / since/until 区间 / 非 admin 403 / 既有 4 端点 ACL 一致收紧 | 10 |
| `tests/integration/test_audit_export_api.py`（新增） | CSV 200 + 行数 / JSON Lines 200 + 行数 / 超 100k 截断 + log warning / 非 admin 403 | 4 |

### 8.3 Integration — 写路径（12 服务 × 3 action）

12 service 写路径测试，每服务 3 action × 2 case（成功 + actor_departments 正确注入）= 6 用例；用 `parametrize` 折叠为单文件。共 ~72 用例分摊 12 文件，每文件 6 用例。

### 8.4 Frontend

| 文件 | 用例 | 数量 |
|---|---|---|
| `frontend/src/tests/AdminAuditPage.test.tsx`（新增） | RangePicker 触发 since/until / 导出 CSV / 导出 JSON / `AuditLogPage.total` 真实生效 / actorDepartments filter 触发请求 | 5 |

### 8.5 E2E（admin 权限链路）

启动 admin session → AdminAuditPage → RangePicker → 导出 CSV → 文件下载 → 验证行数与后端一致。1 case。

## 9. 迁移计划

### 9.1 Commit 序列

| # | Commit | 范围 |
|---|---|---|
| 1 | `docs(spec): audit-history-api 设计方案` | spec |
| 2 | `docs(plan): audit-history-api implementation plan` | plan |
| 3 | `feat(audit): 0036 actor btree index + AuditLogPage + admin-only ACL helper` | schema + ACL |
| 4 | `feat(audit): listAll ILIKE + actor_departments + since/until + total count` | query 增强 |
| 5 | `feat(audit): iterAll yield + export csv/json endpoint` | export |
| 6 | `feat(frontend): AdminAuditPage RangePicker + real total + export + i18n` | 前端对齐 |
| 7 | `feat(audit): ontology_service audit writes (4 entity types)` | 治理组 1 |
| 8 | `feat(audit): kpi_catalog_service audit writes` | 治理组 2 |
| 9 | `feat(audit): entity_mapping_service audit writes` | 治理组 3 |
| 10 | `feat(audit): datasource_service audit writes` | 治理组 4 |
| 11 | `feat(audit): agent_registry_service audit writes (4 actions)` | 治理组 5 |
| 12 | `feat(audit): chat_service AGENT_RUN record-on-finish` | agent 运行 |
| 13 | `feat(audit): agent_scheduler_service audit writes` | scheduler |
| 14 | `feat(audit): document_service audit writes` | 文档 |
| 15 | `feat(audit): data_quality_score_service audit writes` | 数据质量 |
| 16 | `docs(harness): audit-history-api change summary` | harness docs |

共 16 commit。

### 9.2 启动顺序保证

```
migrate: alembic upgrade head  # 应用 0036
启动: lifespan seed → warmUp
```

无新增启动流程。

### 9.3 回滚预案

- **commit 3-6**：revert 即回滚（schema 索引 DROP、query 字段删、ACL 还原）
- **commit 7-15**：每 service 独立 revert；audit 写入即停，业务写入不受影响
- **commit 16**：harness docs revert

## 10. 风险登记

| 风险 | 缓解 |
|---|---|
| `AuditService.record()` 不 commit —— 调用方忘 commit 则审计丢失 | 集成测试断言「业务提交后 audit_log 同步可见」；与既有约定一致 |
| 12 service 同步改，commit 量大 —— 单 service 失败阻断其余 11 | 每 service 一独立 commit；CI 按顺序跑，单失败不阻后续（git revert 单 commit 即回滚） |
| 部分 service 当前无 `actor` 参数（业务层未透传） | catalog review：phase 0 列 12 service 当前 actor 上下文链路；缺失则同步补（受 acl-extension 既有模式约束） |
| audit_log 数据增长后 ILIKE 慢 | 现有 index on `created_at` + `(entity_type, entity_id)`；commit 3 加 `actor` btree index |
| Export 100k 行内存压力 | `iterAll` yield + `session.stream` + StreamingResponse；前端 blob 下载 |
| ACL 收紧是 breaking change —— 非 admin 现有调用方会 403 | AdminAuditPage 路由层核对 admin only；CI grep `getCurrentUser.*audit` 检查无残留；Harness docs 文档化 |
| `actor_departments` 是逗号拼接字符串 —— 子串匹配误命中（"财务"误匹"财务管理部"） | 接受（admin 过滤是辅助视图，正式定位走精确查询）；文档说明 |
| `AuditLogPage` 替代 `list[AuditLogRead]` 是 breaking response shape | 全仓 grep 确认无外部依赖；前端 AdminAuditPage 同步对齐 |
| 12 service actor_departments 字段未统一（有的传 tuple、有的传 list、有的不传） | phase 0 校核；测试断言 `actor_departments` 持久化为字符串格式 |

## 11. 与既有 change 的关系

| 既有 change | 关系 |
|---|---|
| `Phase 4.5 治理 API`（已发） | audit_log 表 + 4 endpoints + audit_worker 复用，本期扩 `listAll` 与新增 `export` |
| `feat-agent-tool-binding`（已发） | `agent_registry_service` 4 个写方法本期加 audit（commit 11） |
| `feat-acl-extension-3-entities`（已发） | admin-only ACL 沿用既有模式（`getAdminOnlyActor` 与 `assertCanModify` 平行） |
| `feat-agent-vocabulary`（已发） | entity_type 字符串不受词表约束（保持自由字符串，与 admin 输入框选项一致） |

## 12. 未来演进（非本期）

- Outbox 化 audit 写入（异步 + worker 消费，独立排期）
- pg_trgm 扩展启用 `gin_trgm_ops` 索引，全字段 fuzzy 性能提升
- 多租户 row-level security
- 实时通知（WebSocket / SSE）
- 历史回放与 audit 关联（point-in-time entity state recovery）

---

**状态**：draft → 评审中
**下一步**：进入 writing-plans 阶段，生成 16-task implementation plan。
