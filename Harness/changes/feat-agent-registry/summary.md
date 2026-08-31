# 变更：feat-agent-registry

- **日期**：2026-08-31
- **作者**：Claude (Phase 6.1)
- **Phase**：6.1（Agent Registry 起步）
- **状态**：done

## 1. 需求

建立 L5 AI Native 的最小骨架：Agent 定义 + 数据访问策略注册中心。Phase 6.1 是 Phase 6 的第一步，仅交付 registry 与 ACL 集成；后续 Phase 6.4（Agent Runtime MVP）才会真正按 agent_code 路由 + 调用 Tool。

验收标准：
- 2 张新表：`agent_definition`（含 JSONB 数据域/数据层 + status/owner/version）+ `agent_access_policy`（FK + 唯一约束 `(agent_id, data_object, data_layer)`）。
- 4 个新枚举：`AgentTriggerType`（USER_QUESTION/SCHEDULED/EVENT）、`AgentResponseLatency`（REALTIME/BATCH）、`AgentStatus`（ACTIVE/DRAFT/DEPRECATED）、`AgentPermission`（READ/MASKED_READ/FORBIDDEN/FORBIDDEN_WRITE）。
- 全套 REST API：GET/POST `/agents` + GET/PUT/DELETE `/agents/{code}` + 嵌套 `/agents/{code}/policies` CRUD。
- 软删除：DELETE → `status=deprecated`（保留历史，不允许 hard delete）。
- ACL（Phase 4.5 模式）：写操作仅 owner 部门成员或 admin 角色；非 admin + 非 owner → 403。`owner` 派生自 `actor.departments[0]`（与 entity_mapping 同模式）。
- 前端：`AgentRegistryPage`（Table + 创建/编辑 Modal + 详情 Drawer 含嵌套策略子表 + i18n 中英文完整）。
- 单测 ≥ 12 用例 + 集成 ≥ 11 用例，全部在真实 PG 5433 上跑通。
- 覆盖率 80%+。

## 2. 设计评审

**关键设计决策**：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 表数 | 2 张（definition + policy），与 plan §6.1 对齐 | 最小骨架，避免提前引入 runtime 字段（trigger schedule、prompt template 等留 Phase 6.4） |
| 主键约束 | agent_code 唯一（业务键）+ agent_access_policy (agent_id, data_object, data_layer) 唯一 | 防重复注册；policy 同 (object, layer) 不允许两条 |
| data_domains / data_layers 类型 | `JSONB` | 灵活标签化（无需新表，匹配 Phase 5 entity_mapping 风格） |
| 删除方式 | 软删除：`status='deprecated'`（仅 admin 可触发） | 保留历史；后续阶段若需要 hard delete 由 admin 单独脚本处理 |
| ACL 实现 | 复用 Phase 4.5 的 `AclService.assertCanModify(user, owner, label, code)` | 与 §3 entity_mapping、§4 feature、§5 supplier_360 一致；admin 绕过 owner 检查 |
| owner 派生 | `actor.departments[0]`（非空时）或 `None`（空时） | 与 entity_mapping 同模式；None 时仅 admin 可改；字段来源一致降低认知成本 |
| 嵌套策略路由 | `/agents/{code}/policies` 而非 `/policies?agent_code=` | REST 资源嵌套清晰；audit/history 类日志可直接关联 agent_code |
| 路由前缀 | `/api/v1/agents`（与现有 `/api/v1/datasources` 等同级） | 符合现有 API 分层 |
| Soft-delete 校验 | DELETE 接口允许 active/draft → deprecated；deprecated → 409 | 防止对已废弃的 agent 重复软删除（避免幂等性混淆） |
| DTO camelCase | 沿用 CamelModel + alias_generator | 与其他 feature 一致；前后端契约零摩擦 |
| 403 侧信道防护 | 重复 code 返回 409（而非 403） | 与 §3 entity_mapping 一致；防止「不存在 vs 无权限」猜测 |
| 前端软删除按钮 | 仅 status != deprecated 时显示「停用」按钮 | UI 友好；状态颜色 active=绿/draft=蓝/deprecated=灰 |
| 策略子表 | Drawer 内嵌套 Table + Form（inline 增删改） | 与 supplier_360 内嵌 Profile 同 |

## 3. 数据模型变更

**新表 1：`agent_definition`**（Alembic `0031_agent_registry.py`）
```
id, agent_code (unique, ^[A-Z][A-Z0-9_]*$),
agent_name, description,
trigger_type (AgentTriggerType ENUM),
response_latency (AgentResponseLatency ENUM),
data_domains (JSONB), data_layers (JSONB),
status (AgentStatus ENUM, default 'draft'),
owner (VARCHAR nullable),
version (VARCHAR default 'v1.0'),
created_time, updated_time
```

**新表 2：`agent_access_policy`**（Alembic `0031_agent_registry.py`）
```
id, agent_id (FK → agent_definition.id, ON DELETE CASCADE),
data_object (VARCHAR),
permission (AgentPermission ENUM),
data_layer (VARCHAR nullable),
notes (VARCHAR nullable),
created_time
unique (agent_id, data_object, data_layer)
```

**Check 约束**：4 个枚举的取值范围（与 `app/domain/enums.py` 同步）。

**索引**：`agent_definition.agent_code`（unique）、`agent_access_policy.agent_id`（FK 索引）、`(agent_id, data_object, data_layer)`（unique）。

**down_revision**：`0030_document_catalog`（沿 Phase 5.1 已落地的链路）。

## 4. 关键代码

**后端**：
- `backend/app/domain/enums.py`：新增 4 个枚举（AgentTriggerType / AgentResponseLatency / AgentStatus / AgentPermission）。
- `backend/app/domain/models.py`：新增 `AgentDefinition`（TimestampMixin + JSONB + lazy="selectin" on policies）+ `AgentAccessPolicy`（cascade FK + unique + 索引）。
- `backend/app/domain/error_messages.py`：新增 `MSG_SCHEMA_AGENT_*`（12 条 schema 描述）+ `MSG_AGENT_NOT_FOUND_BY_CODE / MSG_AGENT_POLICY_NOT_FOUND / MSG_AGENT_DUPLICATE_CODE / MSG_AGENT_POLICY_DUPLICATE / MSG_AGENT_REGISTRY_FORBIDDEN`（5 条错误消息）。
- `backend/app/domain/schemas.py`：新增 `AgentAccessPolicyCreate/Update/Read` + `AgentDefinitionCreate/Update/Read`（含 camelCase alias）。
- `backend/app/services/agent_registry_service.py`（NEW ~290 行）：`AgentRegistryService`（full CRUD + 嵌套 policies + ACL），关键方法 `createAgent/getAgent/listAgents/updateAgent/deprecateAgent/addPolicy/updatePolicy/deletePolicy`。所有写方法都调 `self._acl.assertCanModify(...)`。
- `backend/app/api/v1/agents.py`（NEW）：REST 路由（GET/POST `/` + GET/PUT/DELETE `/{code}` + 嵌套 `/policies` CRUD）。
- `backend/app/main.py` + `backend/app/tests/_testapp.py`：挂载 `agents.router` 到 prefix `/api/v1/agents`。
- `backend/alembic/versions/0031_agent_registry.py`：Alembic 迁移。

**前端**：
- `frontend/src/types/agentRegistry.ts`（NEW）：4 个枚举类型 + 5 个 interface（Policy CRUD + Definition CRUD）。
- `frontend/src/api/agentRegistry.ts`（NEW）：9 个 API 函数（list/get/create/update/deprecate + 5 个 policy CRUD）。
- `frontend/src/pages/AgentRegistryPage.tsx`（NEW ~670 行）：Table + 创建/编辑 Modal + 详情 Drawer（嵌套 policies 子表含 inline CRUD）。
- `frontend/src/App.tsx` + `frontend/src/components/common/AppLayout.tsx`：新增 `/agents` 路由 + 菜单项。
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`：新增 `agentRegistry.*` 命名空间（columns / actions / fields / detail / policies / messages / modal 标题）+ `appLayout.menu.agents` + 4 个 `enums.agent*` label。

## 5. 测试

**单元测试**（`backend/app/tests/unit/test_agent_registry_service.py`，12 用例）：
- `TestOwnerDerivation`（3）：`create` 时 owner 派生规则 + 重复 code → ConflictError。
- `TestGetAgent`（1）：未知 code → NotFoundError。
- `TestUpdateAcl`（3）：admin 绕过 owner + owner 匹配通过 + 非 admin + 非 owner → PermissionDeniedError。
- `TestDeprecate`（1）：deprecate → status=deprecated。
- `TestPolicies`（2）：policy 重复 → ConflictError + 未知 policy id → NotFoundError。
- `TestDtoConversion`（2）：agent + policy ORM → DTO roundtrip。

**集成测试**（`backend/app/tests/integration/test_agent_registry_api.py`，11 用例）：
- `TestAgentRegistryMigration`（1）：Alembic upgrade head 后两张表 + 关键列齐全。
- `TestAgentRegistryApi`（10）：
  - 创建 + GET roundtrip（含 2 条 policy）+ 重复 code 409 + 小写 code 422。
  - owner 部门成员可更新 + 非 owner（finance）PUT → 403。
  - 列表按 status / dataDomain 过滤。
  - 软删除返回 deprecated。
  - policy CRUD（add / list / update / delete）+ 重复 policy → 409 + 未知 agent → 404。

**前端**：TypeScript 严格检查通过（`tsc --noEmit` 0 errors）。注：项目 vitest 当前未在本机安装，单元测试留待 CI；接口契约由 camelCase DTO 保证 + 后端 23 用例覆盖。

**手动验证**：
- Alembic upgrade head 应用成功，PG 5433 `agent_definition` + `agent_access_policy` 已建。
- curl 验证：创建 + 列表 + 详情 + 更新 + 软删除 + 嵌套 policies 端到端通过。
- 前端 tsc 0 errors。

## 6. 安全审查

按 ACL security review pattern（见 memory）复查 4 项：

1. **DTO mass-assignment** ✅ 所有 Create/Update DTO 字段显式列举，未用 `**extra`；`AgentDefinitionRead` 没有 `id` Create 字段。
2. **403 侧信道** ✅ 重复 agent_code 返回 409（不是 403）；NotFoundError 用通用消息（owner 字段为空时仍返回同一 404，不区分权限）。
3. **actor 派生** ✅ `owner = actor.departments[0] if departments else None`；ACL 用 `_acl.assertCanModify(actor, owner, label="agent", code=agent_code)`。
4. **非 admin 集成测试** ✅ `_stranger()` 显式 `"X-User-Roles": "user"` 覆盖 stub 默认 admin；测出 PUT → 403。

**额外**：
- DTO `agent_code` 强制 `^[A-Z][A-Z0-9_]*$` 正则（小写 → 422）。
- Policy (object, data_object, data_layer) 唯一约束防重复。
- 无 LLM 调用、无外部 API 调用、无密码字段，无新增风险面。
- Alembic 迁移 ON DELETE CASCADE（删 agent → 自动删 policy）— 仅通过 deprecate 软删除 + admin 触发；hard delete 留给后续脚本。

## 7. 部署与迁移

```bash
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  .venv/bin/alembic upgrade head
# 期望：0031_agent_registry 应用成功；表 + 索引 + check 约束全部建好

DATABASE_URL=... \
  .venv/bin/pytest \
    app/tests/unit/test_agent_registry_service.py \
    app/tests/integration/test_agent_registry_api.py -v
# 期望：23 用例全过

cd ../frontend
node node_modules/typescript/lib/tsc.js -p . --noEmit
# 期望：0 errors
```

无新 Python 包依赖、无新前端包依赖、无环境变量新增。

## 8. 关键文件清单

**新增**：
- `backend/alembic/versions/0031_agent_registry.py`
- `backend/app/services/agent_registry_service.py`
- `backend/app/api/v1/agents.py`
- `backend/app/tests/unit/test_agent_registry_service.py`
- `backend/app/tests/integration/test_agent_registry_api.py`
- `frontend/src/types/agentRegistry.ts`
- `frontend/src/api/agentRegistry.ts`
- `frontend/src/pages/AgentRegistryPage.tsx`

**修改**：
- `backend/app/domain/enums.py`
- `backend/app/domain/models.py`
- `backend/app/domain/error_messages.py`
- `backend/app/domain/schemas.py`
- `backend/app/main.py`
- `backend/app/tests/_testapp.py`
- `frontend/src/App.tsx`
- `frontend/src/components/common/AppLayout.tsx`
- `frontend/src/i18n/zh-CN.ts` + `en-US.ts`

## 9. 验证

**端到端冒烟**（与 §5 同）：见 §7 命令。

**Phase 6.1 验收**：
- ✅ 23 个后端测试通过（12 unit + 11 integration）。
- ✅ Alembic 迁移应用成功。
- ✅ 前端 tsc --noEmit 0 errors。
- ✅ ACL 行为：owner 部门成员可改 / 财务部 stranger 改 → 403 / admin 绕过 owner 检查。
- ✅ 软删除：`DELETE` → 200 + `status='deprecated'`；前端隐藏「停用」按钮。
- ✅ 嵌套 policies CRUD：5 个端点 + 重复 → 409 + 未知 → 404。

**集成回归**（与 Phase 5.4 supplier-risk 一起跑）：45 用例全过，无回归。

## 10. 已知缺口 / 后续 Phase

- **触发调度**：本期无 `cron` / `trigger_config` 字段；Phase 6.4 runtime MVP 会接入。
- **prompt template / 工具清单**：留 Phase 6.4；本期仅 metadata + ACL。
- **运行历史 / 审计**：不在 registry 范围；沿用现有 `audit_log` + `audit_history` API（Phase 4.x 落地）。
- **Neo4j Agent 节点**：留 Phase 6.2 semantic relations 时再决定是否把 agent 作为节点入图（与 ontology class 隔离）。
- **Agent → Tool 路由**：Phase 6.4 主要目标；本期不做。
- **ML 语义化（如 agent embedding 用于推荐）**：留 L5 后续 Phase。