# 菜单层级重新设计

> 日期：2026-09-01
> 类型：前端 + 后端协同（结构改造，预留权限接口）
> 关联：`Harness/changes/feat-menu-hierarchy/`（执行期创建）

## 概述

QA System 前端当前为 20 项扁平菜单，所有项平铺在一个 Ant Design `Menu` 内，无层级、无分组、无图标、无权限控制。本次改造将菜单按业务域划分为 6 个一级类（AI Agent / 智能分析 / 业务配置 / 业务基础信息 / 系统信息配置 / 审计安全），并把菜单定义从「前端硬编码常量」迁移到「后端 DB 表 + API」，为后续在管理界面动态调整顺序、增减菜单项、按角色/权限过滤做准备。

## 目标

1. **业务可读性**：6 类一级分组对应清晰的业务域，新人 5 分钟内能找到入口。
2. **结构可扩展**：新增菜单项仅需 DB 一行 + seed 脚本；无需前端发版。
3. **接口预留**：`permissionCode` / `roles` 字段在数据契约中存在但本期不参与过滤，后续接入 ACL 服务即可启用。
4. **降级兜底**：后端 API 失败时，前端回退到旧硬编码菜单，不白屏。
5. **零功能回归**：保留全部 20 个现有路由（无新增、无删除、无重命名）。

## 非目标

1. **本期不实现后端按用户角色/权限过滤菜单**：接口字段保留，过滤逻辑留待后续接入。
2. **本期不实现管理界面**（拖拽调整 / 增删菜单）：架构上保证可加，UI 不做。
3. **本期不做菜单变更历史 / 审计**：DB 表不记录变更操作人/时间。
4. **本期不引入多租户 / 多环境菜单隔离**：所有用户看到同一份菜单。
5. **本期不重命名或迁移任何路由 path**：仅菜单分组；URL 路径保持原样。

## 架构

```
前端 AppLayout
   │
   ├─ fetch GET /api/v1/menu-config ──> 后端 MenuConfigRouter
   │                                              │
   │                                              ▼
   │                                  MenuConfigService.list_sections()
   │                                              │
   │                                              ▼
   │                                      PostgreSQL menu_config
   │
   ├─ 本地缓存（内存 useState）
   │
   ├─ 渲染：Ant Design Menu + 嵌套 SubMenu
   │
   └─ openKeys 持久化：localStorage["menu.openKeys"]
```

降级路径：API 失败 / 超时 → fallback 渲染 `frontend/src/components/common/fallbackNav.ts`（原 NAV_KEYS 复制为静态常量）。

## 数据模型

```sql
-- 自引用单表存所有菜单节点（一级类 + 叶子项）
CREATE TABLE menu_config (
    id              BIGSERIAL PRIMARY KEY,
    code            VARCHAR(64) UNIQUE NOT NULL,     -- "section.aiAgent" / "item.supplier360"
    parent_id       BIGINT REFERENCES menu_config(id), -- 一级类为 NULL
    label_key       VARCHAR(128) NOT NULL,            -- i18n key："menu.section.aiAgent"
    path            VARCHAR(256),                     -- 一级类为 NULL；叶子项填路由
    icon_code       VARCHAR(64),                      -- 一级类与叶子项都填
    sort_order      INT NOT NULL DEFAULT 0,           -- 同级内排序（间隔 10，便于插入）
    permission_code VARCHAR(64),                      -- 预留：单个权限码（本期不消费）
    roles           VARCHAR(512),                     -- 预留：逗号分隔多角色（本期不消费）
    visible         BOOLEAN NOT NULL DEFAULT TRUE,    -- 整体可见性
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_menu_config_parent ON menu_config(parent_id);
CREATE INDEX idx_menu_config_sort   ON menu_config(parent_id, sort_order, id);
```

### 6 个一级类的最终定义

| sort_order | code | 中文 | en-US | i18nKey | icon_code | 子项数 |
|---|---|---|---|---|---|---|
| 100 | `section.aiAgent` | AI Agent | AI Agent | `menu.section.aiAgent` | `robot` | 3 |
| 200 | `section.analytics` | 智能分析 | Smart Analytics | `menu.section.analytics` | `fund` | 2 |
| 300 | `section.bizConfig` | 业务配置 | Business Config | `menu.section.bizConfig` | `setting` | 6 |
| 400 | `section.foundation` | 业务基础信息 | Foundation | `menu.section.foundation` | `database` | 5 |
| 500 | `section.systemConfig` | 系统信息配置 | System Config | `menu.section.systemConfig` | `api` | 3 |
| 600 | `section.auditSecurity` | 审计安全 | Audit & Security | `menu.section.auditSecurity` | `safety` | 1 |

### 20 个叶子项的最终定义

| parent | sort_order | code | 中文 | en-US | i18nKey | path | icon_code |
|---|---|---|---|---|---|---|---|
| aiAgent | 110 | `item.chat` | AIChatService | AIChatService | `menu.item.chat` | `/chat` | `message` |
| aiAgent | 120 | `item.agentRuntime` | Agent 运行时 | Agent Runtime | `menu.item.agentRuntime` | `/agents/run` | `thunderbolt` |
| aiAgent | 130 | `item.agents` | Agent Registry | Agent Registry | `menu.item.agents` | `/agents` | `appstore` |
| analytics | 210 | `item.supplier360` | 供应商 360° | Supplier 360° | `menu.item.supplier360` | `/supplier-360` | `barchart` |
| analytics | 220 | `item.supplierRisk` | 供应商风险 | Supplier Risk | `menu.item.supplierRisk` | `/supplier-risk` | `alert` |
| bizConfig | 310 | `item.ontology` | 本体管理 | Ontology | `menu.item.ontology` | `/ontology` | `partition` |
| bizConfig | 320 | `item.dataQuality` | 数据质量 | Data Quality | `menu.item.dataQuality` | `/data-quality` | `audit` |
| bizConfig | 330 | `item.lineage` | 数据血缘 | Data Lineage | `menu.item.lineage` | `/lineage` | `node` |
| bizConfig | 340 | `item.entityMapping` | 编码映射 | Code Mapping | `menu.item.entityMapping` | `/entity-mapping` | `code` |
| bizConfig | 350 | `item.kpiCatalog` | KPI 目录 | KPI Catalog | `menu.item.kpiCatalog` | `/kpi-catalog` | `number` |
| bizConfig | 360 | `item.features` | 特征目录 | Feature Catalog | `menu.item.features` | `/features` | `cluster` |
| foundation | 410 | `item.datasource` | 数据源 | Data Sources | `menu.item.datasource` | `/datasource` | `database` |
| foundation | 420 | `item.documents` | 文档中心 | Document Center | `menu.item.documents` | `/documents` | `file` |
| foundation | 430 | `item.usage` | 用量看板 | Usage Dashboard | `menu.item.usage` | `/usage` | `dashboard` |
| foundation | 440 | `item.graph` | Neo4j 图库 | Neo4j Graph DB | `menu.item.graph` | `/graph` | `apartment` |
| foundation | 450 | `item.vectors` | Milvus 向量库 | Milvus Vector DB | `menu.item.vectors` | `/vectors` | `heart` |
| systemConfig | 510 | `item.models` | 模型配置 | Model Config | `menu.item.models` | `/models` | `api` |
| systemConfig | 520 | `item.embeddings` | Embedding 服务 | Embedding | `menu.item.embeddings` | `/embeddings` | `node` |
| systemConfig | 530 | `item.status` | 服务状态 | Service Status | `menu.item.status` | `/status` | `heart` |
| auditSecurity | 610 | `item.adminAudit` | 审计日志 | Audit Logs | `menu.item.adminAudit` | `/admin/audit` | `audit` |

注：`icon_code` 取值见前端 `ICON_REGISTRY`（后端只存字符串，前端查表渲染 React 组件）。

## API

### `GET /api/v1/menu-config`

**鉴权**：Required（Bearer JWT）。

**Query**：本期不接任何参数；预留 `?roles=...&permissions=...` 供后续扩展。

**Response 200**：

```json
{
  "version": "2026-09-01",
  "sections": [
    {
      "code": "section.aiAgent",
      "labelKey": "menu.section.aiAgent",
      "iconCode": "robot",
      "sortOrder": 100,
      "permissionCode": null,
      "roles": [],
      "children": [
        { "code": "item.chat", "labelKey": "menu.item.chat",
          "path": "/chat", "iconCode": "message", "sortOrder": 110,
          "permissionCode": null, "roles": [] },
        ...
      ]
    },
    ...
  ]
}
```

**Response 401**：未登录或 token 无效。

**性能要求**：本地 PG，查询 < 50ms；前端缓存 5 分钟（前端层控制，不在后端做）。

**`version` 字段语义**：当前 seed 数据的发布日期（`YYYY-MM-DD`），用于前端缓存键 / 调试排查菜单异常；本期不参与缓存失效逻辑。

### 数据契约（Pydantic，CamelModel）

```python
class MenuItemRead(CamelModel):
    code: str
    label_key: str
    icon_code: str | None
    sort_order: int
    permission_code: str | None = None
    roles: list[str] = Field(default_factory=list)
    path: str | None = None        # 一级类为 None，叶子项填路由

class MenuSectionRead(MenuItemRead):
    children: list[MenuItemRead] = Field(default_factory=list)

class MenuConfigRead(CamelModel):
    version: str
    sections: list[MenuSectionRead]
```

`alias_generator=to_camel` 自动产出 `labelKey` / `sortOrder` 等驼峰键，与现有 API 一致。

## 后端组件

| 文件 | 职责 |
|---|---|
| `backend/app/models/menu_config.py` | SQLAlchemy ORM `MenuConfig` |
| `backend/app/schemas/menu_config.py` | Pydantic DTO（见上） |
| `backend/app/services/menu_config_service.py` | `list_sections()` —— 单次查询 + 内存分组构造嵌套结构 |
| `backend/app/api/v1/menu_config.py` | `GET /menu-config` router |
| `backend/alembic/versions/<ts>_add_menu_config.py` | 建表 + 索引 |
| `backend/scripts/seed_menu_config.py` | 幂等 upsert 6 类 20 项（基于 code 唯一键） |

`menu_config_service.list_sections()` 算法：
1. 单查询 `SELECT * FROM menu_config WHERE visible = TRUE ORDER BY sort_order, id`
2. 内存里分两遍：第一遍建立 `code → node` 字典，第二遍按 `parent_id` 链接父子
3. 一级类按 `sort_order` 升序，叶子项也按 `sort_order` 升序
4. 任何 `path` 缺失或 `code` 重复 → 抛 `RuntimeError`（fail-fast，seed 阶段就能发现）

## 前端组件

### `frontend/src/components/common/menuIcons.ts`

```ts
import { /* 21 个 Ant Design icons */ } from "@ant-design/icons";

export const ICON_REGISTRY: Record<string, React.ComponentType> = {
  robot: RobotOutlined, message: MessageOutlined, thunderbolt: ThunderboltOutlined,
  appstore: AppstoreOutlined, barchart: BarChartOutlined, alert: AlertOutlined,
  partition: PartitionOutlined, database: DatabaseOutlined, audit: AuditOutlined,
  file: FileTextOutlined, dashboard: DashboardOutlined, node: NodeIndexOutlined,
  apartment: ApartmentOutlined, code: CodeOutlined, fund: FundProjectionScreenOutlined,
  number: NumberOutlined, setting: SettingOutlined, api: ApiOutlined,
  heart: HeartOutlined, safety: SafetyCertificateOutlined, cluster: ClusterOutlined,
};

export const renderIcon = (code?: string): React.ReactNode => {
  if (!code) return null;
  const Icon = ICON_REGISTRY[code];
  return Icon ? <Icon /> : null;
};
```

未知 `icon_code` → 返回 `null`（不抛错）。

### `frontend/src/components/common/fallbackNav.ts`

旧 `NAV_KEYS` 数组复制为独立导出常量。当 `/menu-config` 失败时使用。

### `frontend/src/i18n/zh-CN.ts` 与 `en-US.ts`

新增 6 个 `menu.section.*` + 20 个 `menu.item.*` key。保留旧 `appLayout.menu.*` 不删（路由跳转兼容期预留；后续清理单独 commit）。

### `frontend/src/components/common/AppLayout.tsx` 重构

- 删除硬编码 `NAV_KEYS`（移到 `fallbackNav.ts`）。
- 新增 `useEffect`：fetch `/api/v1/menu-config` → setState。
- Ant Design `Menu` 用 `items` prop 渲染嵌套 SubMenu：
  ```ts
  const items: MenuProps["items"] = sections.map(s => ({
    key: s.code, icon: renderIcon(s.iconCode), label: t(s.labelKey),
    children: s.children.map(c => ({
      key: c.code, icon: renderIcon(c.iconCode), label: t(c.labelKey),
    })),
  }));
  ```
- `selectedKeys`：根据 `location.pathname.startsWith(item.path)` 计算（支持 `/agents/run` 这种前缀）。
- `openKeys`：state + localStorage 同步（key=`menu.openKeys`，JSON 数组）。
- 加载态：`<Spin size="small" />`。
- 错误态：catch → setState fallback → 渲染 `fallbackNav`。

### 路由变更

无。`App.tsx` 的 `<Routes>` 与现有路由 path 一字不改。

## 测试策略

### 后端

| 测试 | 关键断言 |
|---|---|
| `tests/unit/test_menu_config_service.py::test_list_sections_returns_nested_six` | 返回 6 sections + 20 leaves + 顺序按 sort_order |
| `tests/unit/test_menu_config_service.py::test_list_sections_skips_invisible` | visible=false 行不出现 |
| `tests/unit/test_menu_config_service.py::test_permission_code_and_roles_passthrough` | 字段透传正确，类型 list[str] |
| `tests/integration/test_menu_config_api.py::test_get_menu_config_unauthenticated_returns_401` | 无 token → 401 |
| `tests/integration/test_menu_config_api.py::test_get_menu_config_authenticated_returns_200` | 有 token → 200 + sections 数组非空 + 字段驼峰 |
| `tests/integration/test_seed_menu_config.py::test_seed_idempotent` | 跑两次 seed，code 不重复、行数稳定 26 |
| `tests/integration/test_seed_menu_config.py::test_all_paths_match_routes` | 遍历 item.path，与 App.tsx 的 Route path 集合做交集对比（前端 fixture 用 json 文件注入） |

### 前端

| 测试 | 关键断言 |
|---|---|
| `__tests__/AppLayout.test.tsx::renders 6 submenus after fetch` | mock fetch 返回 6 sections，DOM 含 6 个 SubMenu 标题 |
| `__tests__/AppLayout.test.tsx::selected key matches current path` | `location.pathname=/chat` → 选中 chat 项 |
| `__tests__/AppLayout.test.tsx::openKeys persisted to localStorage` | 展开 SubMenu → localStorage["menu.openKeys"] 含其 code |
| `__tests__/AppLayout.test.tsx::falls back to static nav on fetch failure` | fetch reject → 渲染 fallbackNav 项 |
| `__tests__/menuIcons.test.ts::all 21 codes resolve` | 遍历种子数据所有 icon_code，断言 ICON_REGISTRY 都有 |

### 端到端冒烟

```bash
# 启动 backend + frontend
DATABASE_URL=... uv run alembic upgrade head
DATABASE_URL=... uv run python scripts/seed_menu_config.py
cd frontend && npm run dev

# 浏览器手测
- 6 类 SubMenu 全部展开
- 点击每个叶子项能跳转到对应路由
- 刷新页面后已展开的 SubMenu 保持展开
- 杀掉 backend → 刷新页面 → 仍能看到菜单（fallback）
```

## 验证步骤

```bash
# 后端
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/unit/test_menu_config_service.py \
    app/tests/integration/test_menu_config_api.py \
    app/tests/integration/test_seed_menu_config.py -v

# 全量回归 + 80% 覆盖率门槛
TEST_DATABASE_URL=... uv run pytest app/tests/ --cov=app --cov-fail-under=80

# 真实 PG：跑迁移 + seed
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  uv run alembic upgrade head
DATABASE_URL=... uv run python scripts/seed_menu_config.py

# 验证 API
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/menu-config \
  | jq '.sections | length'   # 应输出 6
curl -s -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/menu-config \
  | jq '[.sections[].children[]] | length'   # 应输出 20

# 前端
cd frontend
npm test -- --coverage --collectCoverageFrom='src/components/common/AppLayout.tsx'
npm run typecheck
npm run build
```

## 关键文件清单

后端新增：
- `backend/app/models/menu_config.py`
- `backend/app/schemas/menu_config.py`
- `backend/app/services/menu_config_service.py`
- `backend/app/api/v1/menu_config.py`
- `backend/alembic/versions/<ts>_add_menu_config.py`
- `backend/scripts/seed_menu_config.py`
- `backend/app/tests/unit/test_menu_config_service.py`
- `backend/app/tests/integration/test_menu_config_api.py`
- `backend/app/tests/integration/test_seed_menu_config.py`

后端修改：
- `backend/app/api/v1/__init__.py`（注册 router）
- `backend/app/services/acl_service.py` 或新建 `menu_visibility.py`（预留过滤入口，本期不调用）

前端新增：
- `frontend/src/components/common/menuIcons.ts`
- `frontend/src/components/common/fallbackNav.ts`
- `frontend/src/components/common/__tests__/AppLayout.test.tsx`
- `frontend/src/components/common/__tests__/menuIcons.test.ts`

前端修改：
- `frontend/src/components/common/AppLayout.tsx`
- `frontend/src/i18n/zh-CN.ts`（新增 26 个 key）
- `frontend/src/i18n/en-US.ts`（新增 26 个 key）

文档新增：
- `Harness/changes/feat-menu-hierarchy/summary.md`（执行期创建）
- `Harness/wiki/frontend.md`（追加菜单架构小节）

## 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| `/menu-config` API 启动期失败导致前端白屏 | 高 | fallbackNav.ts + try/catch + 错误日志 |
| icon_code 拼写错误 → 菜单无图标 | 低 | 前端 ICON_REGISTRY 未知 code → null；menuIcons.test 覆盖所有种子 code |
| seed 脚本被反复运行 → 行数膨胀 | 中 | `INSERT ... ON CONFLICT (code) DO UPDATE`；test_seed_idempotent 守门 |
| i18n key 漏翻译 → 菜单显示 key 原文 | 中 | CI 校验 zh-CN / en-US key 集合对称 |
| 路由 path 与菜单 item.path 不一致 → 点击菜单 404 | 高 | test_all_paths_match_routes 集成测试 + 启动时后端日志警告未注册 path |

## 收尾

- `Harness/changes/feat-menu-hierarchy/summary.md` 记录本次结构改造决策、数据契约、seed 内容、fallback 策略。
- `Harness/wiki/frontend.md` 追加「菜单架构」小节：DB 驱动 + SubMenu + 权限预留字段。
- 不删旧 `appLayout.menu.*`（保留一段时间，确保直接调用 `t("appLayout.menu.xxx")` 的代码不破），后续清理单独 commit。
- 不改任何路由 path。
- 不实现后端权限过滤（接口字段保留即可）。
