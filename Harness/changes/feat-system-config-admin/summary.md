# 变更：feat-system-config-admin

- **日期**：2026-09-11
- **作者**：
- **Phase**：Phase 4.5 扩展（运维治理后台）
- **状态**：draft

## 1. 需求

`system_config` 表的当前值（如 `ENABLE_L4_AGENT_LOOP`）只能通过 `psql` 直接改 + 重启服务，运营极不友好。补一个 admin UI：列表 + 编辑 value。

**验收标准**：

- `GET /api/v1/admin/system-config` 返回全部行（任意登录用户；admin-only 走 menu grant 隐式约束）
- `GET /api/v1/admin/system-config/{key}` 返回单行（admin-only，404 if not found）
- `PUT /api/v1/admin/system-config/{key}` 更新 value（admin-only；audit_log 同事务写入；404 if not found）
- 非 admin → 403；未知 key → 404；value=null 允许（业务表达「清空/未配置」）
- 前端 `/admin/system-config` 页面：Table + 编辑 Modal
- 菜单项 `item.adminSystemConfig` 落入 `section.systemConfig`
- 后端集成 + 前端 vitest 双覆盖；覆盖率 ≥ 80%

## 2. 设计评审

**权限方案**：

- 列表 `getCurrentUser`（任何登录用户能看；admin-only 由 menu grant 隐式约束：菜单不可见则路径不可达）
- 单行 GET / PUT 走 `getAdminOnlyActor`（与 `agent_tools` 同模式）

**审计**：service 层 `audit_service.record()` 同事务写入 UPDATE（actor + before.value + after.value）；`key` 是 PK 但 `entity_id` 不可知，写 0（与 agent_tool_config 风格一致）。

**value=null 语义**：允许。业务表达「清空 / 未配置」，与 agent_tool_config 一致；不强制 string。schema `value: str | None = Field(default=None, max_length=4096)`。

**updated_time 显示**：`updated_time` 是 DB 列 `timestamp with time zone`，默认 `now()`；PUT 时 service 显式 `datetime.now(timezone.utc)` 刷新（DB 无 trigger）。

**ORM 修正**：`SystemConfig` 不再继承 `TimestampMixin`（该 mixin 加 created_time，DB 只有 updated_time）；改为显式字段 `updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, server_default=func.now())`。

## 3. 数据模型变更

无新表（`system_config` 表 0052 migration 已存在）。仅 ORM 模型字段收敛（去掉 created_time）。

## 4. 接口契约变更

**新增 DTO**（`backend/app/domain/schemas.py`）：

```python
class SystemConfigRead(CamelModel):
    key: str
    value: str | None
    description: str | None
    updated_time: datetime

class SystemConfigUpdate(CamelModel):
    value: str | None = Field(default=None, max_length=4096)
```

**新增端点**（`backend/app/api/v1/system_config.py` 新文件）：

```
GET  /api/v1/admin/system-config          → list[SystemConfigRead]
GET  /api/v1/admin/system-config/{key}    → SystemConfigRead
PUT  /api/v1/admin/system-config/{key}    → SystemConfigRead
```

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/app/models/system_config.py` | 去除 TimestampMixin；显式 updated_time |
| `backend/app/domain/schemas.py` | 追加 SystemConfigRead / SystemConfigUpdate |
| `backend/app/services/system_config_service.py` | 新增（listAll / getByKey / updateValue+audit） |
| `backend/app/api/v1/system_config.py` | 新增（3 个端点） |
| `backend/app/main.py` | include router |
| `backend/app/tests/_testapp.py` | 注册 system_config 路由（集成测试 app） |
| `backend/scripts/seed_menu_config.py` | 加 `item.adminSystemConfig` (sort_order=580, icon=setting) |
| `frontend/src/api/systemConfig.ts` | 新增 |
| `frontend/src/pages/AdminSystemConfigPage.tsx` | 新增 |
| `frontend/src/App.tsx` | 注册 `/admin/system-config` 路由 |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 加 `menu.item.adminSystemConfig` + `systemConfig.*` 命名空间 |

**前端页面布局**：

```
┌─ /admin/system-config ──────────────────┐
│ [刷新]                                    │
│                                          │
│ Table: key | value | description | time   │
│   key: blue Tag                            │
│   value: 字符串 or "空" (null 显示)        │
│   操作: [编辑] → Modal                    │
│                                          │
│ Modal: key 不可改 / description 只读      │
│        value TextArea (maxLength=4096)    │
│        [取消] [保存]                      │
└──────────────────────────────────────────┘
```

## 6. 测试

**后端集成**（`backend/app/tests/integration/test_system_config_admin.py`，9 cases）：

- 列表：含 ENABLE_L4_AGENT_LOOP / 非 admin 也允许 200 / admin 通过
- 单行 GET：admin / 非 admin 403 / 未知 key 404
- PUT：admin + audit_log 写入 + 二次 GET 确认 / 非 admin 403 / 未知 key 404 / value=null 允许
- 数据隔离：每个 arrange 用 `INSERT ... ON CONFLICT DO UPDATE` 直接 SQL 注入 seed（conftest TRUNCATE 后）

**前端 vitest**（`frontend/src/tests/AdminSystemConfigPage.test.tsx`，7 cases）：

- 渲染 rows / null 显示「空」/ 工具栏有刷新按钮 / 每行编辑按钮
- 点编辑打开 modal / 改 value 调 updateSystemConfig / mock reject 触发 ant-message-error

## 7. 安全审查

- **触发 security-reviewer**：是（写入 = 影响 routing 层 L4 开关 = 影响全链路行为）
- **关注**：
  - `getAdminOnlyActor` 不能被绕过（必须解析 JWT/stub 后判定，非 header 注入）
  - audit_log 必须同事务写入（避免 UPDATE 后 crash 留下「改了但无审计」gap）
  - `value` 长度限制 max_length=4096（防 DoS / 内存膨胀）
  - 不暴露 DB 内部字段（key/description 是元数据，只 value 可改）

## 8. 部署验证

```bash
# 部署到 qa-backend 容器
docker cp ../backend/app/. qa-backend:/app/app/
docker restart qa-backend

# 验证菜单已 seed
docker exec qa-postgres psql -U qa_user -d qa_metadata -c \
  "SELECT code, path FROM menu_config WHERE code='item.adminSystemConfig'"
# 期望 1 行 /admin/system-config

# API 冒烟
TOKEN=...
curl -s http://localhost:8000/api/v1/admin/system-config \
  -H "Authorization: Bearer $TOKEN" | jq

curl -s -X PUT http://localhost:8000/api/v1/admin/system-config/ENABLE_L4_AGENT_LOOP \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value":"true"}' | jq

# 浏览器打开 http://localhost:5173/admin/system-config
# 点编辑改 value → 保存 → 表格刷新
# 切到 /chat 问探索性问题 → routing_layer=L4

# 测试
cd backend && pytest app/tests/integration/test_system_config_admin.py -v
cd frontend && pnpm vitest run src/tests/AdminSystemConfigPage.test.tsx
```

## 9. 真实数据验证

- 测试库 9 cases 全过；前端 7 cases 全过
- 浏览器 admin 头 → 看到 1 行 `ENABLE_L4_AGENT_LOOP=false`
- 改 value=true → audit_log 出现 1 条 UPDATE 记录（actor=sysconfig-admin, before=false, after=true）
- 切 /chat → routing_layer=L4

## 10. 关联

- 前置：`feat-ai-ready-plan`（system_config 表 + L4 开关依赖）
- 前置：Phase 4.5（audit_service.record 同事务写入模式）
- 复用：`app/services/agent_tool_config_service.py`（同款 admin service 模式）
- 复用：`frontend/src/pages/AdminToolsPage.tsx`（同款 Table+Modal 骨架）
- 规则：`Harness/rules/权限与安全规范.md`（admin-only 走 getAdminOnlyActor）