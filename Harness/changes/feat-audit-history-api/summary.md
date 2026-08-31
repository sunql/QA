# 变更：feat-audit-history-api

- **日期**：2026-08-30
- **作者**：
- **Phase**：Phase 4.5 扩展
- **状态**：draft

## 1. 需求

暴露 `audit_log` + `kpi_catalog_history` 的查询 API + 治理后台最小可用页面，给「管理员能看到谁在什么时候改了治理对象」提供入口。

**验收标准**：

- `GET /api/v1/audit/logs?entity_type=&entity_id=&actor=&since=&until=&limit=&offset=` 返回 `audit_log` 列表
- `GET /api/v1/audit/kpi-history?kpi_id=&revision=&limit=` 返回 `kpi_catalog_history`（按 revision 升序）
- 两条端点**仅 admin 角色可读**（其他角色 403）
- 前端 `/admin/audit` 页面：列表 + 过滤 + 详情抽屉
- 速率限制：100 req/min/IP（治理后台为内网，慢点用）
- 覆盖率 ≥ 80%

## 2. 设计评审

**权限方案**：已确认「仅 admin 角色」。在 FastAPI dependency 层加 `requireAdmin` 复用。

**分页策略**：limit/offset（与现有 list 接口一致）；最大 limit=200 防止拉爆。

**过滤维度**（audit_log）：

- `entity_type`：kpi_catalog / ontology_class / entity_mapping / data_quality_rule
- `entity_id`：精确匹配
- `actor`：X-User-Id 精确匹配
- `action`：CREATE / UPDATE / DELETE
- `since` / `until`：created_time 范围

**过滤维度**（kpi_catalog_history）：

- `kpi_id`：精确匹配（必填）
- `revision`：可选区间

## 3. 数据模型变更

无（只读）。

## 4. 接口契约变更

**新增 DTO**（`backend/app/domain/schemas.py`）：

```python
class AuditLogRead(CamelModel):
    id: int
    entity_type: str
    entity_id: int
    action: str
    actor: str
    actor_departments: tuple[str, ...] | None
    before: dict | None    # JSONB
    after: dict | None     # JSONB
    created_time: datetime

class KpiHistoryRead(CamelModel):
    id: int
    kpi_id: int | None     # KPI 删除后为 NULL（FK ON DELETE SET NULL）
    revision: int
    snapshot: dict         # JSONB
    changed_by: str
    changed_at: datetime
```

**新增端点**（`backend/app/api/v1/audit.py` 新文件）：

```
GET  /api/v1/audit/logs           → list[AuditLogRead]
GET  /api/v1/audit/logs/{id}      → AuditLogRead
GET  /api/v1/audit/kpi-history    → list[KpiHistoryRead]
```

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/app/api/v1/audit.py` | 新增（3 个端点） |
| `backend/app/services/audit_query_service.py` | 新增（filter / listByEntity / listByActor） |
| `backend/app/services/kpi_history_query_service.py` | 新增 |
| `backend/app/dependencies.py` | 加 `requireAdmin` dependency |
| `backend/app/main.py` | 挂载 audit router |
| `frontend/src/api/audit.ts` | 新增 |
| `frontend/src/pages/AdminAuditPage.tsx` | 新增 |
| `frontend/src/components/admin/AuditLogTable.tsx` | 新增 |
| `frontend/src/components/admin/AuditLogDetailDrawer.tsx` | 新增 |
| `frontend/src/components/admin/KpiHistoryTimeline.tsx` | 新增 |
| `frontend/src/App.tsx` | 注册 `/admin/audit` 路由 |
| i18n `admin.*` 命名空间 | 新增 |

**`requireAdmin` 模板**：

```python
async def requireAdmin(user: CurrentUser = Depends(getCurrentUser)) -> CurrentUser:
    if "admin" not in user.roles:
        raise PermissionDeniedError("需要 admin 角色")
    return user
```

**JSONB 解码**：`before` / `after` 是 JSONB，asyncpg 返回 dict；直接 model_validate 即可。

**前端页面布局**：

```
┌─ /admin/audit ───────────────────────────┐
│ [Tab] 审计日志 | KPI 历史                │
│                                         │
│ 过滤栏: entity_type / entity_id / actor │
│          since / until / action         │
│                                         │
│ 列表（Table）: 时间 | 操作 | 对象 | 人   │
│  点行 → 右侧抽屉显示完整 before / after  │
└─────────────────────────────────────────┘
```

## 6. 测试

**单测**：

- `test_audit_query_service.py`：filter 各种组合、空结果、JSONB 解码
- `test_kpi_history_query_service.py`：KPI 删除后 history.kpi_id 为 NULL 的处理
- `test_require_admin.py`：非 admin 403；admin 通过

**集成**：

- `test_audit_api.py`（新）：
  - admin 拉列表 / 过滤 / 详情
  - 普通 user 拉 → 403
  - 速率限制触发（200 次请求）
- `test_kpi_history_api.py`（新）：配合 KPI 删除场景验证 history.kpi_id IS NULL

**前端 vitest**：

- `AuditLogTable.tsx`：渲染 + 过滤交互
- `AuditLogDetailDrawer.tsx`：JSON diff 展示（before vs after 高亮变更字段）

## 7. 安全审查

- **触发 security-reviewer**：是（读取审计日志 = 读取全部治理操作历史 = 敏感）。
- **关注**：
  - `requireAdmin` 不能被绕过（不接受 client 传 admin，必须解析 JWT/stub 后判定 — 复用 `getCurrentUser`）
  - JSONB 输出是否泄漏密码字段（audit_log.before/after 不应包含 `data_source.password_encrypted` —— 在 audit service 写入前过滤）
  - 速率限制必须实装（slowapi 已就绪，加 `@limiter.limit`）
  - 时间范围过滤防止 DoS（最大 90 天窗口）

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/pytest \
  app/tests/unit/test_audit_query_service.py \
  app/tests/unit/test_kpi_history_query_service.py \
  app/tests/integration/test_audit_api.py \
  app/tests/integration/test_kpi_history_api.py -v

# Live 冒烟
curl -X GET 'http://localhost:8000/api/v1/audit/logs?entity_type=kpi_catalog' \
  -H "X-User-Id: admin1" -H "X-User-Roles: admin" | jq '. | length'
# 期望非空（之前 Phase 4.5 留下的审计）

# 非 admin → 403
curl -X GET 'http://localhost:8000/api/v1/audit/logs' \
  -H "X-User-Id: u1" -H "X-User-Roles: user"
# 期望 403
```

## 9. 真实数据验证

- 跑 Phase 4.5 留下的 KPI 修改操作（创建 3 个 KPI、改 2 个、删 1 个），触发 6 条 audit_log + 5 条 kpi_catalog_history
- 用 admin 头查询 → 应能拉出全部
- 用普通 user 头查询 → 应 403
- 把查询结果 + DB 直接对比贴进本节

## 10. 关联

- 前置：`feat-governance-hardening`（audit_log / kpi_catalog_history 表已存在）
- 前置：`feat-acl-extension-3-entities`（按 entity_type 过滤时 ACL 已稳定）
- 后置：`feat-audit-outbox`（consumer 写出去的数据必须被这套 API 看见）
- 规则：`Harness/rules/权限与安全规范.md`