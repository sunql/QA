# 变更：feat-dq-rule-list-filters

- **日期**：2026-09-10
- **作者**：
- **Phase**：Phase 4.5 扩展
- **状态**：shipped

## 1. 需求

数据质量规则列表 `/data-quality` 只能用「规则类型」单字段过滤，找不到想要的规则；用户要求按 5 个字段筛选 + 模糊查询。

**用户原话**：「数据质量界面，可以按照规则名称、数据源、目标表、规则类型、严重程度、启用进行查询，可以模糊查询。」

**明确范围**（来自 AskUserQuestion 回复原文）：

- 规则名称 → 下拉框（从已有规则中选，showSearch 即可输入筛选）
- 数据源 → 下拉框
- **目标表 → 选择数据源后才能模糊查询**（级联依赖）
- 规则类型 → 下拉框
- 严重程度 → 下拉框
- 启用 → 下拉框（三态：all / enabled / disabled）

## 2. 设计评审

**后端**：

- `listRules` 在现有 `ruleType / targetTable / enabledOnly` 基础上新增 4 个 query 参数：`ruleName / datasourceId / severity / enabled`
- `targetTable` 由精确匹配改为 `ILIKE '%value%'` 模糊匹配（用户明确要求）
- 新增 `enabled: Literal["all","enabled","disabled"]` 三态替代旧 `enabledOnly: bool`（保留兼容）
- 新增 `GET /rules/options` 返回 4 个下拉的可选值（DISTINCT + 全量 active datasource + 静态 severities）
- **路由顺序陷阱**：`/options` 必须注册在 `/{ruleId}` 之前，否则被路径参数吃掉返回 422

**前端**：

- 6 个 Select + Reset + Create + Refresh（横向 Space wrap）
- `FilterValues` interface 单 state + `updateFilter(key, value)` 单字段更新（cascade: datasourceId 变更清空 targetTable）
- 目标表 Select `disabled={datasourceId === undefined}`，placeholder 切换为「先选数据源」
- `useDataQualityFilterOptions` 模块级 `_cache` + `_inflight` 模式，跨组件共享 /options 响应
- i18n 新增 6 个 placeholder + 3 态 + 级联提示 key

**模块级缓存为何不抽 React Query**：当前只有一处调用方，简洁优先（YAGNI）；若未来需 invalidate，加 reload() 已留好接口。

## 3. 数据模型变更

无。

## 4. 接口契约变更

**新增 schema**（`backend/app/domain/schemas.py`）：

```python
class DatasourceOption(CamelModel):
    id: int
    name: str

class RuleOptionsRead(CamelModel):
    rule_names: list[str]                       # DISTINCT rule_name
    datasource_ids: list[DatasourceOption]      # 全量 active 数据源
    target_tables: list[str]                    # DISTINCT target_table
    severities: list[str]                       # 静态 [HIGH, MEDIUM, LOW, INFO]
```

**`listRules` 新增 query 参数**：

| 参数 | 类型 | 语义 |
|---|---|---|
| `ruleName` | `str \| None` | 规则名称 ILIKE 模糊 |
| `datasourceId` | `int \| None` | 数据源精确匹配 |
| `severity` | `str \| None` | 严重程度精确匹配 |
| `enabled` | `Literal["all","enabled","disabled"] \| None` | 三态 |
| `targetTable` | `str \| None` | **改为 ILIKE 模糊** |
| `ruleType` | `str \| None` | 保留（精确） |

`enabledOnly: bool` 参数保留兼容（marked deprecated，未来下线）。

**新增端点**（`backend/app/api/v1/data_quality.py`）：

```
GET /api/v1/data-quality/rules/options → RuleOptionsRead
```

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/app/domain/schemas.py` | 追加 `DatasourceOption` + `RuleOptionsRead` |
| `backend/app/services/data_quality_service.py` | `listRules` 扩参 + `listOptions` 新增 |
| `backend/app/api/v1/data_quality.py` | 注册 4 个新 Query + `/options` 路由 |
| `backend/app/tests/integration/test_data_quality_api.py` | 追加 `TestDataQualityRuleFilters` 7 个测试 |
| `frontend/src/types/dataQuality.ts` | 追加 `RuleOptions` + `DatasourceOption` + 扩 `DataQualityRuleListParams` |
| `frontend/src/api/dataQuality.ts` | 追加 `listRuleOptions()` |
| `frontend/src/hooks/useDataQualityFilterOptions.ts` | 新增（模块级缓存） |
| `frontend/src/pages/DataQualityPage.tsx` | filter 区重构（6 个 Select + 级联） |
| `frontend/src/pages/__tests__/DataQualityPage.filters.test.tsx` | 新增 6 个 vitest |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 新增 6 个 filter key + `common.reset` 兜底 |

**路由顺序硬约束**（代码注释里写明）：

```python
# 注意：必须注册在 /{ruleId} 之前，否则会被路径参数吃掉返回 422。
@router.get("/options", response_model=RuleOptionsRead)
```

## 6. 测试

**后端集成**（`backend/app/tests/integration/test_data_quality_api.py::TestDataQualityRuleFilters`）：

- `test_list_rules_filter_by_rule_name_ilike`：ruleName='PO' → 命中含 PO 的规则
- `test_list_rules_filter_by_datasource_id`：datasourceId=1 → 仅 ds=1 的规则
- `test_list_rules_filter_by_severity`：severity='HIGH' → 仅 HIGH
- `test_list_rules_filter_enabled_three_states`：enabled='all'/'enabled'/'disabled' 三态
- `test_list_rules_target_table_ilike`：targetTable='PO' → PO_HEADER/PO_LINE
- `test_list_rules_combined_filters`：5 字段 AND 叠加
- `test_list_options_returns_distinct_values`：DISTINCT + 全量 ds + 静态 severities

结果：**18 passed**（11 原 + 7 新）。

**前端 vitest**（`DataQualityPage.filters.test.tsx`）：

- 挂载时拉取 /rules/options + 渲染 6 个筛选 Select
- 数据源未选时目标表 disabled
- 数据源选中后目标表解除 disabled
- 切换数据源时清空目标表（级联）
- 改 filter 触发 listRules 且带正确 query 参数
- 「重置」按钮清空所有 filter 并刷新列表

结果：**6 passed**。

**踩坑记录（留在测试代码注释里）**：

- antd Select 在 jsdom 下通过 `fireEvent.mouseDown(.ant-select-selector)` 触发下拉浮层，`user.click` 直接点根 div 不工作
- 下拉选项用 `screen.findByText(label, { selector: ".ant-select-item-option-content" })` 定位
- antd Button 中文 label 带 word-spacing（"重 置"），`getByRole({ name: "重置" })` 不匹配，需用 `{ name: "重 置" }`（带空格）
- 模块级 cache 跨测试污染：用 `_resetCache()` 在 beforeEach 复位
- vitest 跑组件必须包 `<I18nextProvider i18n={i18n}>`（项目自定义 `useTranslation` 基于 react-i18next）

## 7. 安全审查

未触发 security-reviewer。本次改动：

- 仅扩展既有 list 接口的过滤维度，**不引入新的写入端点 / 鉴权变更 / 数据外泄**
- 新增 `/options` 端点复用既有 `Depends(getDb)`，依赖 `getCurrentUser` ACL（与既有 list 一致）
- query 参数 ILIKE 走 SQLAlchemy parameter binding，无 SQL 注入
- 路由顺序约束仅是 URL 解析问题，非安全

## 8. 部署验证

```bash
# 后端
docker cp backend/app/. qa-backend:/app/app/
docker restart qa-backend

# 前端（必须 rebuild — vite 是打包构建，不是文件直读）
cd docker && docker compose -f docker-compose.yml build frontend && docker compose -f docker-compose.yml up -d frontend
```

后端 `tsc -b && vite build` 一次性通过（修复了 3 处 TS6133 'container 未使用' + `.at(-1)` 在 ES2022 lib 之前的类型问题）。

## 9. 真实数据验证

`qa_metadata` 真机冒烟（prod 数据）：

```
=== 1. /rules/options ===
  rule_names: 10 种
  datasource_ids: 1 个 (THBI Oracle)
  target_tables: 2 个 (DIM_MATERIAL, DIM_SUPPLIER)
  severities: [HIGH, MEDIUM, LOW, INFO]

=== 2. ruleName=KEY 模糊 ===
  matched: 4 rule(s) (MATERIAL_KEY / SUPPLIER_KEY *)

=== 3. datasourceId=1 + enabled ===
  matched: 12 enabled rule(s) on ds=1

=== 4. targetTable=MATERIAL ILIKE ===
  matched: 5 rule(s)

=== 5. severity=HIGH ===
  matched: 8 rule(s)

=== 6. 5 字段组合 ===
  matched: 3 rule(s) for 5-field AND

=== 7. enabled 三态 ===
  enabled=all → 12 / enabled=enabled → 12 / enabled=disabled → 0
```

全部按预期返回。

## 10. 关联

- 后置：可被 `feat-coverage-remediation` 跟踪（前端 functions 覆盖率仍受其他 feature 文件拖累，本次未引入新 0% funcs 文件）
- 关联：`Harness/wiki/data-quality.md`（如未来存在）
- 规则：`Harness/rules/开发流程规范.md` §10 阶段；TDD 强制 RED → GREEN → IMPROVE
