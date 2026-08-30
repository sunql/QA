# 变更：feat-acl-extension-3-entities

- **日期**：2026-08-30
- **作者**：
- **Phase**：Phase 4.5 扩展
- **状态**：✅ implemented + security reviewed (2026-08-30)

## 1. 需求

把 Phase 4.5 的 owner-based ACL 从 `kpi_catalog` 扩展到 3 张治理对象：

| 表 | 当前 owner 字段 | 备注 |
|---|---|---|
| `ontology_class` | `object_owner` (Phase 3.4 已加) | 直接复用 |
| `entity_mapping` | **无 owner 列** | 新增迁移 `0026_entity_mapping_owner` |
| `data_quality_rule` | **无 owner 列** | 新增迁移 `0026_data_quality_rule_owner`（合并到一条迁移） |

**验收标准**：

- 3 张表的 `createXxx` / `updateXxx` / `deleteXxx` 均走 `AclService.assertCanModify`
- `actor.roles ⊇ {'admin'}` OR `entity.owner ∈ actor.departments` 才能修改
- 非授权用户拿到 403（`PermissionDeniedError`）
- 所有改动配套单元测试（service）+ 集成测试（API 链路 + 真实 PG 5433）
- 覆盖率 ≥ 80%

## 2. 设计评审

**方案对比**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. 每个 service 手动调 acl.assertCanModify**（推荐） | 复用 Phase 4.5 模式；零抽象成本 | 3 个 service 各写 3 处调用（重复但直白） |
| B. SQLAlchemy event listener 自动拦截 | 一次写完所有表 | 调试栈不友好；绕过 ORM 的 raw SQL 无法拦截 |
| C. 数据库行级安全（PG RLS） | 最底层保证 | 与现有 ACL 接口错位；要重写 stub auth 测试 |

**最终决定**：A。理由：与 Phase 4.5 kpi_catalog 模式一致；测试模式可复用；review 容易看懂。

**ACL service 改造**：`assertCanModify` 已稳定，新增 `entity_type` 参数供未来审计日志使用（当前 kpi_catalog 传 "KPI"，新 3 张表分别传 "ONTOLOGY_CLASS" / "ENTITY_MAPPING" / "DATA_QUALITY_RULE"）。

## 3. 数据模型变更

**迁移**：`backend/alembic/versions/0026_entity_mapping_owner.py`

```python
def upgrade() -> None:
    op.add_column("entity_mapping", sa.Column("owner", sa.String(length=100), nullable=True))
    op.create_index("ix_entity_mapping_owner", "entity_mapping", ["owner"])
```

**注**：data_quality_rule.owner 列在 Phase 1.1 已存在；ontology_class 已有 object_owner（Phase 3.4）。仅 entity_mapping 需要新增列 + 索引。

**回滚**：`downgrade()` 删索引 + 删列。线上数据若已填 owner 需评估是否保留。

**部署门禁**：alembic upgrade head 后 `information_schema.columns` 应出现 owner 列、`pg_indexes` 应出现 ix_entity_mapping_owner。

**回填**：可选；本表 owner 可空，存量数据不强制填。

## 4. 接口契约变更

**3 个 service 改造**：

| Service | 方法 | 加什么 |
|---|---|---|
| `OntologyService` | createClass / updateClass / deleteClass | updateClass + deleteClass 调 `acl.assertCanModify(actor, old_entity.object_owner, ...)` |
| `EntityMappingService` | createMapping / updateMapping / deleteMapping | 同上，调 `entity.owner` |
| `DataQualityService` | createRule / updateRule / deleteRule | 同上，调 `rule.owner` |

**API DTO**（**修订**：经 security-reviewer 发现，owner 不应作为客户端可写字段——见 §7 CRITICAL/HIGH）：

- DB 列：`entity_mapping.owner`、`data_quality_rule.owner`、`ontology_class.object_owner` 全部保留（query/filter 可用）
- **Create / Update DTO**：**全部移除** `owner` / `object_owner` 字段（防 mass-assignment 越权转移）
- **Read DTO**：保留 `owner` / `object_owner`（服务端派生，只读展示）
- 写入路径：服务端从 `actor.departments[0]` 派生 owner（无部门时为 NULL），由 ACL 校验读取到的 owner 与 actor 部门是否匹配

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/alembic/versions/0026_entity_mapping_owner.py` | 仅 entity_mapping 加 owner 列 + 索引（其他 2 张表已有） |
| `backend/app/domain/models.py` | `EntityMapping` 加 `owner` + `Index("ix_entity_mapping_owner", "owner")` |
| `backend/app/domain/schemas.py` | **`Create`/`Update` DTO 移除 owner**（CRITICAL 修复）；`Read` DTO 保留 owner（server-derived，display-only） |
| `backend/app/services/acl_service.py` | **修正 403 消息为通用模板**（MEDIUM #3 修复）：不再泄漏 owner / entity_code / user.departments |
| `backend/app/services/ontology_service.py` | `__init__` 注入 `acl`，`createClass` 派生 `object_owner=actor.departments[0]`（HIGH #2 修复），`updateClass`/`deleteClass` 调 `assertCanModify(actor, entity.object_owner, "ONTOLOGY_CLASS", class_name)` |
| `backend/app/services/entity_mapping_service.py` | 同上模式：`createMapping` 派生 owner；`updateMapping`/`deleteMapping` 调 `assertCanModify(actor, entity.owner, "ENTITY_MAPPING", str(entity.id))` |
| `backend/app/services/data_quality_service.py` | 同上模式 |
| `backend/app/services/local_import_service.py` | `execute_import` / `_create_class_with_properties` 接收 `actor: CurrentUser`，透传到 `createClass(session, dto, actor)` |
| `backend/app/api/v1/entity_mapping.py` | PUT/DELETE handler 加 `user: CurrentUser` 注入；POST 把 `actor=user` 传给 `service.createMapping` |
| `backend/app/api/v1/data_quality.py` | 同上 |
| `backend/app/api/v1/ontology.py` | updateClass/deleteClass handler 加 user 注入；POST 把 `actor=user` 传给 `service.createClass` |
| `backend/app/api/v1/local_import.py` | `importExecute` 把 `actor=user` 传给 `service.execute_import` |
| `frontend/src/types/entityMapping.ts` | **`Create`/`Update` 类型移除 owner**（CRITICAL 修复）；`Base`/`Read` 保留 owner（display-only） |
| `frontend/src/pages/EntityMappingPage.tsx` | **移除 owner Form.Item**；owner 列表列保留为只读显示 |
| `frontend/src/tests/entityMappingApi.test.ts` | 测试 fixture 保留 `owner` 字段为只读 read-only 校验 |

**ACL 调用模板**：

```python
self._acl.assertCanModify(
    actor,
    entity_owner=entity.owner,
    entity_label="ENTITY_MAPPING",
    entity_code=str(entity.id),
)
```

**ACL 失败时**：抛 `PermissionDeniedError`，由 main.py 异常处理器映射 403。

## 6. 测试

**单测**（`backend/app/tests/unit/`）：

- `test_governance_extension_acl.py`（新，11 个测试）
  - `TestEntityMappingAcl` (4)：admin / owner 部门 / 跨部门 PUT / 跨部门 DELETE
  - `TestDataQualityRuleAcl` (3)：owner 部门 PUT / 跨部门 PUT / 跨部门 disableRule
  - `TestOntologyClassAcl` (4)：owner 部门 PUT / 跨部门 PUT / admin DELETE / 跨部门 DELETE
- `test_entity_mapping_service.py` 现有 18 个测试加 `_adminActor()` helper（admin 绕过 ACL）— 全绿
- `test_acl_service.py` 修订：原 `test_other_department_denied` + 新增 `test_message_is_generic_no_owner_or_user_departments_leak` 校验 403 消息**不**包含 owner / entity_code / user.departments（防枚举侧信道）
- `test_ontology_governance_fields.py` 修订：原访问 `dto.object_owner` 的 3 个测试改为 `assert not hasattr(dto, "object_owner")`（DTO 已移除该字段）

**集成**（`backend/app/tests/integration/`，真实 PG 5433 + 完整 API 链路）：

- `test_governance_extension_acl_api.py`（新，14 个测试）
  - `TestEntityMappingAcl` (6)：admin / owner 部门 / 跨部门 / 空 owner 仅 admin / DELETE 跨部门 403 / DELETE owner 部门 200
  - `TestDataQualityRuleAcl` (4)：admin / owner 部门 / 跨部门 / disableRule 跨部门 403
  - `TestOntologyClassAcl` (4)：admin DELETE / owner 部门 PUT / 跨部门 PUT / 跨部门 DELETE 403
- `test_data_quality_api.py`：新增 `test_owner_dept_can_update_and_other_dept_blocked`（MEDIUM #4 修复验证）
- `test_ontology_api.py`：新增 `test_ontology_class_owner_dept_can_modify_cross_dept_blocked`（MEDIUM #4 修复验证）
- `test_ontology_governance_integration.py`：5 个测试改用 `X-User-Departments=procurement` header（替代 body `objectOwner`），断言 `response.owner == "procurement"`（server-derived）
- `test_ontology_class_versioning.py`（17 个 createClass 调用）：`_ADMIN` 常量 + `actor=_ADMIN` 参数（service 直调）
- `test_ontology_property_service.py`：`_createProp` helper 接受 `actor=_ADMIN`
- `test_local_import_service.py`：8 个 `execute_import` 调用追加 `actor=_ADMIN`（修复 actor 透传）
- 现有 5 个集成测试文件补 admin header（X-User-Roles=admin）：
  - `test_entity_mapping_api.py` / `test_data_quality_api.py` / `test_data_quality_score_api.py` / `test_ontology_api.py`

**测试结果**（2026-08-30）：
- 新增 25 个 ACL 测试（11 单 + 14 集成）全绿
- 现有回归测试 90+ 全绿
- 5 个新非 admin 集成测试（MEDIUM #4）全绿
- 19 个预存失败（sqlite+JSONB / local_import fixture 基础设施问题）**非本 change 引入**，git log 已确认

**目标覆盖率**：service 95%+；integration 全路径覆盖（admin / 同部门 / 跨部门 / 未带部门 / 空 owner / actor 派生）。

## 7. 安全审查（已执行）

**触发 security-reviewer**：是（ACL 是核心安全边界）。两次审查 + 修复：

### 第一轮：原始设计问题（CRITICAL + HIGH + MEDIUM）

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| 1 | **CRITICAL** | `EntityMappingUpdate.owner` / `DataQualityRuleUpdate.owner` / `OntologyClassUpdate.object_owner` 客户端可写 → mass-assignment 越权转移 | **移除 Update DTO 中的 owner 字段**（CRITICAL 修复） |
| 2 | **HIGH** | `Create` DTO 也含 owner → 客户端可伪造新记录的 owner → 绕开 ACL | **移除 Create DTO 中的 owner 字段**；`createXxx` 服务端从 `actor.departments[0]` 派生 owner |
| 3 | **MEDIUM** | `AclService` 抛 `PermissionDeniedError(message=f"...{entity_code}")` 直接泄露 entity_code 到 HTTP 403 响应 → 枚举侧信道 | **通用 403 消息**「无权修改该资源：仅 owner 部门成员或 admin 角色可操作」；详细信息走服务端日志（`logger.info("ACL denied: label=%s code=%s actor=%s owner_set=%s", ...)`） |
| 4 | **MEDIUM** | 仅 admin 路径覆盖；非 admin 跨部门 / owner 部门集成测试缺失 | 集成测试新增 owner 部门 / 跨部门 PUT 路径（见 §6） |

### 第二轮：回归审查（test_acl_service.py）

- 旧测试断言 403 消息包含 `KPI_SUPPLIER_OTD` / `采购部`（与新通用消息冲突）→ **修订为断言消息不包含这些敏感字符串**
- 旧 ontology_governance_fields 单元测试访问 `dto.object_owner`（DTO 已移除）→ **改为 `assert not hasattr(dto, "object_owner")`**

### 关注点检查清单

- [x] owner 字段走 DTO 校验（不再写入，仅 Read 暴露）
- [x] ACL service 的 admin 短路安全（`ADMIN_ROLE` 常量硬编码 vs 配置化——沿用 Phase 4.5 设计，不变）
- [x] 跨部门边界测试覆盖完整（admin / owner 部门 / 跨部门 / 未带部门 / 空 owner 五档）
- [x] 测试用 ASCII 部门 token（`procurement` / `finance` / `quality`）避免 HTTP header 编码问题
- [x] 服务端 403 消息不泄漏 owner / entity_code / user.departments（防枚举侧信道）
- [x] Create/Update DTO 不暴露 owner 写入接口（防 mass-assignment）
- [x] 服务端从 `actor.departments[0]` 派生 owner（无部门时为 NULL）

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/pytest \
  app/tests/unit/test_acl_service.py \
  app/tests/unit/test_entity_mapping_service.py \
  app/tests/unit/test_data_quality_service.py \
  app/tests/integration/test_entity_mapping_governance.py \
  app/tests/integration/test_data_quality_governance.py \
  app/tests/integration/test_ontology_class_governance.py \
  -v --cov=app --cov-fail-under=80

# Live 冒烟（先 alembic upgrade head）
DATABASE_URL=... alembic upgrade head
# 模拟同部门 user 创建 entity_mapping → 200（owner 由服务端从 header 派生，不在 body）
curl -X POST http://localhost:8000/api/v1/entity-mappings \
  -H "X-User-Id: u1" -H "X-User-Roles: user" \
  -H "X-User-Departments: procurement" \
  -H "Content-Type: application/json" -d '{...}'  # body 不含 owner
# 模拟跨部门 user 改 → 403（PUT body 也不能带 owner，否则 422 schema 拒绝）
curl -X PUT http://localhost:8000/api/v1/entity-mappings/1 \
  -H "X-User-Id: u2" -H "X-User-Roles: user" \
  -H "X-User-Departments: finance" \
  -d '{"sourceCode":"V000001-X"}'  # 期望 403（无权）+ 通用 403 消息不泄漏 owner
```

## 9. 真实数据验证

**Live DB 验证**（2026-08-30，连接到 `qa_metadata` 真实 PG 5433）：

```bash
$ alembic current
0026_entity_mapping_owner (head)  ✓ 迁移已应用

$ SELECT column_name FROM information_schema.columns
    WHERE table_name='entity_mapping' AND column_name='owner';
owner_column: ['owner']  ✓ 列存在

$ SELECT indexname FROM pg_indexes
    WHERE tablename='entity_mapping' AND indexname='ix_entity_mapping_owner';
owner_index: ['ix_entity_mapping_owner']  ✓ 索引存在
```

**集成测试覆盖的端到端场景**（test_governance_extension_acl_api.py，14 个测试均通过真实 PG）：

| 场景 | 表 | user 头 | entity.owner | 期望 | 实测 |
|---|---|---|---|---|---|
| admin DELETE | ontology_class | `X-User-Roles=admin` | procurement | 204 | ✓ |
| owner 部门 PUT | entity_mapping | `X-User-Departments=procurement` | procurement | 200 | ✓ |
| 跨部门 PUT | data_quality_rule | `X-User-Departments=finance` | procurement | 403 | ✓ |
| 跨部门 disableRule | data_quality_rule | `X-User-Departments=finance` | quality | 403 | ✓ |
| 空 owner + 非 admin PUT | entity_mapping | `X-User-Departments=procurement` | `""` | 403 | ✓ |
| 空 owner + admin PUT | entity_mapping | `X-User-Roles=admin` | `""` | 200 | ✓ |
| 多部门匹配 | entity_mapping | `X-User-Departments=procurement,finance` | finance | 200 | ✓ |
| server-derived owner | ontology_class | `X-User-Departments=procurement`（body 无 objectOwner） | procurement | 200 + response.owner="procurement" | ✓ |
| 403 消息通用性 | entity_mapping | `X-User-Departments=finance`（owner=procurement） | procurement | 403 消息不包含 `procurement` / `finance` / entity_code | ✓ |

## 10. 关联

- 前置：`feat-governance-hardening`（AclService 已存在）
- 后置：`feat-audit-history-api`（审计查询按 owner 过滤）
- 关联：`feat-jwt-keycloak`（JWT 接入后将替换 stub auth，ACL 接口无需改）
- 规则：`Harness/rules/开发流程规范.md`「数据库迁移同步」+「每轮真实数据验证」
- 设计：本文件即设计稿（已含 4 个方案对比 + ACL 调用模板）