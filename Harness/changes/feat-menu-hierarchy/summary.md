# feat-menu-hierarchy

## 概述
将 QA System 前端 20 项扁平菜单改造为 6 类嵌套 SubMenu 结构，菜单定义从前端硬编码迁移到后端 `menu_config` 表 + `GET /api/v1/menu-config` API。

## 关键决策
- 6 类分组：AI Agent / 智能分析 / 业务配置 / 业务基础信息 / 系统信息配置 / 审计安全
- 顺序：按使用频度（AI Agent 最前，审计安全 最后）
- 数据契约：DB 表 + Pydantic CamelModel + TypeScript 类型镜像
- 兜底：API 失败 → `fallbackNav.ts` 渲染旧硬编码菜单
- 权限预留：`permissionCode` / `roles` 字段在契约中但不消费

## 文件变更

### Backend（Tasks 1-6, 8）
| 文件 | 说明 |
|------|------|
| `backend/alembic/versions/0033_menu_config.py` | 迁移创建 menu_config 表 |
| `backend/app/models/menu_config.py` | MenuConfig ORM 模型（自引用 parent） |
| `backend/app/models/__init__.py` | 导出 MenuConfig |
| `backend/app/domain/models.py` | 移除 MenuConfig（已迁移） |
| `backend/app/schemas/menu_config.py` | MenuConfigRead / MenuItemRead Pydantic DTOs（CamelModel） |
| `backend/app/schemas/__init__.py` | 导出 schema 类 |
| `backend/app/services/menu_config_service.py` | MenuConfigService.list_sections + 结构验证 |
| `backend/app/api/v1/menu_config.py` | GET /api/v1/menu-config 端点 |
| `backend/app/main.py` | 挂载 menu_config router（`app.include_router(...prefix="/api/v1/menu-config")`）—— Task 11 smoke 补登 |
| `backend/scripts/seed_menu_config.py` | 幂等 seed 脚本（6 sections + 20 items） |
| `backend/app/tests/unit/test_menu_config_orm.py` | ORM 单元测试 |
| `backend/app/tests/unit/test_menu_config_schema.py` | Schema 单元测试 |
| `backend/app/tests/unit/test_menu_config_service.py` | Service 单元测试 |
| `backend/app/tests/integration/test_menu_config_service.py` | Service 集成测试 |
| `backend/app/tests/integration/test_menu_config_api.py` | API 集成测试 |
| `backend/app/tests/integration/test_seed_menu_config.py` | Seed 幂等性 + 路径对齐测试 |

### Frontend（Tasks 5, 7-10）
| 文件 | 说明 |
|------|------|
| `frontend/src/types/menuConfig.ts` | TypeScript 类型镜像 MenuItem / MenuSection / MenuConfig |
| `frontend/src/api/menuConfig.ts` | fetchMenuConfig API wrapper |
| `frontend/src/components/common/fallbackNav.ts` | 硬编码兜底菜单（API 失败时使用） |
| `frontend/src/components/common/menuIcons.tsx` | ICON_REGISTRY（21 种 iconCode） + renderIcon helper |
| `frontend/src/i18n/en-US.ts` | 26 个 i18n key（6 sections + 20 items）英文 |
| `frontend/src/i18n/zh-CN.ts` | 26 个 i18n key（6 sections + 20 items）中文 |
| `frontend/src/components/common/AppLayout.tsx` | fetch /menu-config → SubMenu 渲染 + openKeys 持久化 |
| `frontend/src/tests/menuConfig.test.ts` | API wrapper 测试 |
| `frontend/src/tests/menuIcons.test.ts` | ICON_REGISTRY + renderIcon 测试 |
| `frontend/src/tests/menuI18n.test.ts` | i18n key 完整性测试 |
| `frontend/src/tests/AppLayout.test.tsx` | AppLayout 测试 |
| `frontend/src/tests/pages.test.tsx` | 路由对齐测试 |

## 后续可做
- 管理界面（拖拽调整 / 增删菜单）
- 后端按用户角色/权限过滤菜单
- 菜单变更历史 / 审计

## 验证

- 后端回归：1954 passed / 1 skipped / **93% coverage**（≥80% gate）
- 前端：tsc clean / build OK / vitest 415 passed（1 个 EntityMappingPage 预存失败与本特性无关）
- 实时 API smoke（port 8003, real PG 5433）：
  - `GET /api/v1/menu-config` → **HTTP 200**
  - `version = "2026-09-01"`, `sections.length = 6`, 总子项 `20`
  - 顶层 6 类按 sort_order 升序：`section.aiAgent → section.auditSecurity`
- 浏览器手测未在沙箱执行（需人工）。
