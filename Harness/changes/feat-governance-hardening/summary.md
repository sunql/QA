# 变更：Governance Hardening（Owner-based ACL / 审计 / 历史快照）

- **日期**：2026-08-30
- **作者**：AI 助手
- **Phase**：Phase 4.5（治理加固；遗留项 #68）
- **状态**：done
- **范围**：KpiCatalog 单一实体（先做基线，后续可扩展到 ontology_class / entity_mapping / data_quality_rule）
- **前置**：feat-kpi-catalog-governance（KpiCatalog 表 + CRUD）

## 1. 需求

Phase 4.1 KPI Catalog 上线后遗留的 3 项治理能力：

| 能力 | 现状 | 目标 |
|---|---|---|
| Owner-based ACL | 任何登录用户都能改任何 KPI | 只有 owner 部门成员或 admin 能改/删 |
| 审计 | 无 | CREATE / UPDATE / DELETE 写入 audit_log，附 actor + before/after |
| 历史快照 | 无 | 每次变更完整快照写入 kpi_catalog_history（FK ON DELETE SET NULL） |

为 Phase 5 Supplier 360°、Phase 6 Agent 平台提供「治理可审计 / 可回放」基线；为多部门协作（采购+财务双岗）提供 owner 边界。

## 2. 设计评审

**ACL 语义**：用户能修改 entity = `user.roles ⊇ {admin}` **OR** `entity.owner ∈ user.departments`（部门字符串匹配）。
- 多部门成员：headers 可一次携带多个部门逗号分隔。
- owner 为空：仅 admin 可改（避免「无主 KPI 任意改」）。
- **CREATE 不做 ACL**：无既有 entity，无 owner 比较意义；任何部门都能创建新 KPI（治理层鼓励贡献）。

**审计 vs 历史快照分工**：

| 维度 | audit_log（通用） | kpi_catalog_history（专用） |
|---|---|---|
| 范围 | 任意 entity_type | 仅 kpi_catalog |
| 数据 | before/after 差异 | 完整 snapshot |
| 用途 | 合规审计 / 谁动了什么 | 时间回放 / 任意 revision 还原 |
| FK | 无（log 与 entity 解耦） | kpi_id FK ON DELETE SET NULL（删 KPI 历史保留） |
| 写入策略 | 必写（任何 entity 都审计） | 必写（治理层要求可回放） |

**事务语义**：audit + history 与业务写入绑定同一事务。AuditService.record / HistoryService.snapshot 都只 `session.add`，由 service 层 commit。任一失败 → 全部回滚（业务成功 + 审计失败的不一致不允许存在）。

**stub 鉴权**：当前 `getCurrentUser` 从 `X-User-Id` / `X-User-Roles` / `X-User-Departments` 头解析用户（`app/dependencies.py`）。真实生产应由 JWT/IdP 替换；stub 模式保证 ACL 接口稳定，鉴权接入后无需改 ACL 规则。

## 3. 数据模型

### Alembic `0024_governance_hardening`

新增 2 张表：

```
audit_log (
  id PK,
  entity_type VARCHAR(64) NOT NULL,
  entity_id BIGINT NOT NULL,
  action VARCHAR(16) NOT NULL,  -- CHECK IN ('CREATE','UPDATE','DELETE')
  actor VARCHAR(128) NOT NULL,
  actor_departments VARCHAR(512),  -- 逗号拼接
  before_json JSONB,
  after_json JSONB,
  created_at TIMESTAMP NOT NULL,
  ix_audit_log_entity (entity_type, entity_id),
  ix_audit_log_actor (actor)
)

kpi_catalog_history (
  id PK,
  kpi_id BIGINT FK → kpi_catalog(id) ON DELETE SET NULL,
  revision INT NOT NULL,
  snapshot_json JSONB NOT NULL,
  changed_by VARCHAR(128),
  changed_at TIMESTAMP NOT NULL,
  ix_kpi_history_kpi_revision (kpi_id, revision)
)
```

关键约束：
- `audit_log.action` 用 `CheckConstraint("action IN ('CREATE','UPDATE','DELETE')")` 兜底
- `kpi_catalog_history.kpi_id` 用 `ON DELETE SET NULL`：删除 KPI 时历史保留，仅 kpi_id 置 NULL
- 两表 `created_at / changed_at` 用 `_utcnow()` server_default（不依赖 TimestampMixin，显式单列）

### CurrentUser 扩展

`app/dependencies.py` 加 2 字段：

```python
@dataclass(frozen=True)
class CurrentUser:
    userId: str = "anonymous"
    tenantId: str = "default"
    roles: tuple[str, ...] = ("user",)
    departments: tuple[str, ...] = ()  # 新增
```

`getCurrentUser` 新增 header 解析：

```
X-User-Id: <user_id>           # 默认 "anonymous"
X-Tenant-Id: <tenant_id>       # 默认 "default"
X-User-Roles: "admin,user,..."  # 默认 ["user"]
X-User-Departments: "采购部,..." # 默认 []
```

## 4. 接口契约

**无新增 API 端点**。复用 Phase 4.1 的 5 端点（list/get/create/update/delete @ `/api/v1/kpi-catalog`），PUT/DELETE 现自动走 ACL；不通过 → 403。

**错误响应**：
- ACL 不通过 → `PermissionDeniedError` → HTTP 403 + ErrorResponse
- ACL 通过的 update/delete 走 audit_log + kpi_catalog_history 写入（无新错误）

## 5. 实现要点

### 5.1 服务层（`backend/app/services/`）

| 文件 | 行数 | 责任 |
|---|---|---|
| `acl_service.py` | 59 | `AclService.assertCanModify(user, entity_owner, entity_label, entity_code)`：admin 优先 → owner ∈ departments → 抛 PermissionDeniedError |
| `audit_service.py` | 87 | `AuditService.record(session, entity_type, entity_id, action, actor, actor_departments, before, after)`：参数校验 + session.add（不 commit） |
| `history_service.py` | 65 | `HistoryService.snapshot(session, kpi, changed_by)`：按 `kpi.__table__.columns.keys()` 提取列 → JSONB → session.add |

### 5.2 业务服务接入（`kpi_catalog_service.py` 重写）

| 方法 | ACL | 审计 | 历史 |
|---|---|---|---|
| `createKpi` | ✗ | CREATE + after | revision=0 |
| `updateKpi` | ✓ | UPDATE + before+after | revision=new |
| `deleteKpi` | ✓ | DELETE + before | ✗（已删，写 history 含义模糊） |

`_entityToDict(entity)`：按 `entity.__table__.columns.keys()` 提取列；Decimal/datetime 原样保留（导出层 pydantic 序列化）。

### 5.3 API 路由接入（`api/v1/kpi_catalog.py`）

POST/PUT/DELETE 三个写入端点：从 `_user` 改名为 `user`（语义变更：实际传给 service 用），get/list 仍用 `_user`（未用）。ACL 校验在 service 层（保持 route 层薄）。

### 5.4 错误映射（`main.py`）

`_statusFor` 新增 `PermissionDeniedError → 403` 分支：

```python
if isinstance(exc, PermissionDeniedError):
    return 403
```

### 5.5 文案（`messages_zh.py`）

```
MSG_GOVERNANCE_PERMISSION_DENIED = "无权修改 KPI「{kpi_code}」（owner={owner}，当前用户部门={user_departments}）；仅 owner 部门或 admin 角色可改"
MSG_GOVERNANCE_KPI_NOT_FOUND = "KPI Catalog id={id} 不存在"
```

AclService 当前未引用这俩 message（用 f-string 内嵌消息即可），但保留常量供 i18n 扩展。

## 6. 测试

**单元测试**（待 #73 完成时落地）：见 §6.1 设计。

**集成测试**（待 #73 完成时落地）：见 §6.2 设计。

## 6.1 单元测试设计（计划）

`app/tests/unit/test_acl_service.py`：
- `test_admin_role_passes_any_owner` `user.roles={"admin"}` + owner=任意 → 通过
- `test_owner_department_match_passes` `user.departments={"采购部"}` + owner="采购部" → 通过
- `test_other_department_denied` `user.departments={"财务部"}` + owner="采购部" → 抛 PermissionDeniedError
- `test_empty_owner_denies_non_admin` owner="" + user.roles=("user",) → 抛
- `test_empty_owner_admin_passes` owner="" + "admin" ∈ roles → 通过
- `test_message_contains_entity_label_and_code` 异常 message 含 "KPI" + entity_code

`app/tests/unit/test_audit_service.py`：
- `test_create_requires_after` action=CREATE + after=None → ValueError
- `test_delete_requires_before` action=DELETE + before=None → ValueError
- `test_update_requires_both` action=UPDATE + before/after 任一 None → ValueError
- `test_invalid_action_rejected` action="INVALID" → ValueError
- `test_record_does_not_commit` 验证只 session.add，调用方未 commit 时 DB 无数据
- `test_departments_joined_with_comma` actor_departments=("采购部","财务部") → actor_departments="采购部,财务部"
- `test_none_departments_stored_as_none` None → DB 中存 NULL

`app/tests/unit/test_history_service.py`：
- `test_snapshot_uses_kpi_columns` 验证 snapshot_json 含所有 `kpi.__table__.columns.keys()`
- `test_revision_matches_kpi_revision_count` kpi.revision_count=3 → history.revision=3
- `test_snapshot_does_not_commit` 同 audit

## 6.2 集成测试设计（计划）

`app/tests/integration/test_kpi_catalog_governance.py`（真实 PG 5433，完整 API 链路）：

种子：`KPI_SEEDS` 中 1 个 `owner="采购部"` + 1 个 `owner="财务部"`，DELETE 范围限定种子 kpi_code。

- `test_create_writes_audit_and_history`：X-User-Id=alice X-User-Departments=采购部 → POST /api/v1/kpi-catalog → DB 中 audit_log 有 1 条 CREATE + kpi_catalog_history 有 1 条 revision=0
- `test_update_owner_dept_passes`：alice (采购部) PUT /kpi-catalog/{id} owner=采购部 → 200 + audit UPDATE + history revision=1
- `test_update_other_dept_returns_403`：bob (财务部) PUT 同一个 → 403 + ErrorResponse
- `test_update_admin_any_owner_passes`：X-User-Roles=admin → 200
- `test_delete_owner_dept_passes`：alice DELETE → 204 + audit DELETE（before 有完整字段）+ KPI 删后 history.kpi_id 变 NULL
- `test_delete_other_dept_returns_403`：bob DELETE → 403
- `test_revision_increments_on_each_update`：连续 3 次 PUT → history revision=1,2,3
- `test_audit_actor_records_user_id`：audit.actor == X-User-Id
- `test_audit_actor_departments_records_comma_joined`：audit.actor_departments == "采购部,财务部"
- `test_history_snapshot_preserves_full_kpi_state`：PUT 改 formula → history.snapshot_json["formula"] 是改前值（旧值）
- `test_create_does_not_require_acl`：任何部门都能 POST（即便 owner 不属于本部门）
- `test_empty_owner_only_admin_can_modify`：seed 1 个 owner=""; alice (采购部, no admin) → 403；admin → 200

## 7. 审查

待 #73 完成测试后由 code-reviewer + security-reviewer 并行审查：
- 重点审查 ACL 边界（empty owner / 多部门 / 角色优先）
- 重点审查 audit 写入失败的事务回滚（不要静默吞错）
- 重点审查 history.snapshot 完整性（不要漏列 / 不要带 SQLAlchemy 内部属性）

## 8. 部署与验证

**Alembic 升级**：

```bash
cd backend
.venv/bin/alembic upgrade head
# INFO [alembic.runtime.migration] Running upgrade 0023_kpi_catalog -> 0024_governance_hardening
```

**降级**（如紧急回滚）：

```bash
.venv/bin/alembic downgrade 0023_kpi_catalog
# 两张表 DROP；FK 关联先解
```

**冒烟（curl）**：

```bash
# admin 可改任意 owner 的 KPI
curl -X PUT http://localhost:8000/api/v1/kpi-catalog/1 \
  -H "X-User-Id: alice" -H "X-User-Roles: admin" \
  -H "Content-Type: application/json" \
  -d '{"owner": "采购部"}' | jq
# 期望：200，audit_log +1 UPDATE 行

# 非 admin + 非 owner 部门 → 403
curl -X PUT http://localhost:8000/api/v1/kpi-catalog/1 \
  -H "X-User-Id: bob" -H "X-User-Departments: 财务部" \
  -H "Content-Type: application/json" \
  -d '{"owner": "采购部"}' | jq
# 期望：403 + "无权修改 KPI「KPI_SUPPLIER_OTD」（owner=采购部，当前用户部门=财务部）..."
```

**审计查询**：

```sql
-- 查某 KPI 的全部变更
SELECT action, actor, actor_departments, created_at
FROM audit_log
WHERE entity_type = 'kpi_catalog' AND entity_id = 1
ORDER BY created_at DESC;

-- 查某 KPI 的历史快照回放
SELECT revision, changed_by, changed_at, snapshot_json->>'kpi_name' AS kpi_name
FROM kpi_catalog_history
WHERE kpi_id = 1 OR (kpi_id IS NULL AND snapshot_json->>'kpi_code' = 'KPI_SUPPLIER_OTD')
ORDER BY revision;
```

## 9. 真实数据验证（Harness 门禁）

待 #73 完成测试后验证：
- 真实 PG 5433：ACL 拒绝路径返回 403
- 真实 PG 5433：audit_log 表有 CREATE/UPDATE/DELETE 行
- 真实 PG 5433：kpi_catalog_history 表有 revision=0,1,2,3 行（删 KPI 后 kpi_id=NULL 但 snapshot_json 仍可查）
- 真实 PG 5433：12 个集成测试全部通过
- 数据语义对齐：admin 优先于 owner 检查；FK ON DELETE SET NULL 真实生效

## 10. 决策与遗留

**已落地的决策**：
1. **ACL 语义 = 部门/角色匹配**（用户决策「部门/角色 e.g. owner='采购部门'」）— owner 是字符串，非 userId 严格匹配
2. **CREATE 不做 ACL** — 鼓励贡献；后续可加「同部门才能 CREATE 同 owner」策略
3. **审计 vs 历史快照分开存** — 通用 / 专用两层，避免 audit_log 变成 JSON 垃圾场
4. **stub auth 用 X-User-* 头** — JWT 接入零代码改动（仅替换 getCurrentUser 实现）
5. **事务绑定** — audit + history 与业务同事务；不可「业务成功审计失败」
6. **CheckConstraint 兜底** — action IN (...) 双层保护（service 校验 + DB CHECK）
7. **FK ON DELETE SET NULL** — 历史不丢；snapshot_json 是真理之源
8. **error message 含 owner 与当前用户部门** — 调试/合规友好；用户能立即知道为什么被拒

**遗留（下次迭代）**：
- ACL 扩展到 ontology_class（已有 `object_owner` 字段）/ entity_mapping / data_quality_rule / 后续其他治理实体
- ACL 角色细化：除 `admin` 外可加 `data_steward` / `analyst` 等
- audit_log 查询 API（治理后台用）— 当前仅 SQL 直查
- kpi_catalog_history 回放 API（点 revision 看快照 + diff 前一版）
- JWT/IdP 真实鉴权接入替换 stub headers
- audit_log 写入失败可考虑 outbox 模式（当前事务绑定风险：审计写失败 → 业务回滚 → 用户看到 500）

**相关 Change**：
- 前置：`feat-kpi-catalog-governance`（KpiCatalog 表 + CRUD API）
- 前置：`feat-kpi-catalog-seed`（12 条 KPI 种子；ACL 测试数据来源）
- 前置：`feat-schema-drift-detector`（schema 漂移防护，本变更新增 2 表必须先 alembic upgrade，否则服务启动 fail-fast）
- 后继：ACL 扩展（待 Phase 4.6+ 评估）；审计查询 API（治理后台）；Phase 5 Supplier 360° / Phase 6 Agent 平台消费 owner 字段
