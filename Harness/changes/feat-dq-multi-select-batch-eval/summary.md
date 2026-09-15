# 变更：feat-dq-multi-select-batch-eval

- **日期**：2026-09-14
- **作者**：
- **Phase**：Phase 1.1 体验优化（在 feat-dq-rule-list-filters 之上）
- **状态**：shipped

## 1. 需求

`/data-quality` 页面在批量评估时一次评估「当前列表里所有 enabled 规则」，用户没法挑出只想评估的几条；
筛选栏也缺一个按本体类（className）过滤的入口；目标表只能选一个。

**用户原话**（三句话拆三个独立 feature，统一编号）：

> 1. 在质量评估界面，需要增加一个多选的功能，在批量评估的时候，可以选择具体批量评估哪些；
> 2. 增加一个类名的过滤查询条件，任何一个对象都会属于某个类名，一个类名唯一对应一个表名。
> 3. 选择数据源后，按目标表过滤可以多选，后端可以查询多个目标表；

## 2. 设计评审

### 后端

- `listRules` 在 6 字段之上新增 2 个 query 参数：
  - `targetTables: list[str] | None`：精确 IN 匹配（FastAPI 自动支持 `?targetTables=A&targetTables=B` 与 `?targetTables=A,B`）；
    **与单值 `targetTable` ILIKE 不能同时传——`targetTables` 优先**，避免语义歧义
  - `sourceClassId: int | None`：本体类 id 精确（`data_quality_rule.source_class_id` 由 wizard 写入，
    公开 create API 不接受该字段；过滤类即过滤该类的所有规则）
- `listOptions` 新增 `classOptions: list[ClassOption]`（active 本体类全量，`valid_to IS NULL` 过滤墓碑）
- 新增 schema `ClassOption(id, class_name)`，与既有 `DatasourceOption` 同 shape
- `RuleOptionsRead.class_options` 字段追加（追加字段不破坏既有消费者，向后兼容）
- **EvaluateBatchRequest 无需改**：本来就是 `rule_ids: list[int]`，接受任意子集

### 前端

- `FilterValues.targetTable: string` → `targetTables: string[]`；新增 `sourceClassId: number | undefined`
- 新增 `selectedIds: Set<number>` state + `Table.rowSelection={{ preserveSelectedRowKeys: true }}`
- `handleBatchEvaluate` 行为从「全表已启用」改为「按 selectedIds 走」：
  - 未勾任何行 → 弹「请先勾选」警告，**不发**请求
  - 勾选全 disabled → 弹「没有已启用」警告，**不发**请求
  - 勾选含 disabled → 弹「已忽略 N 条」警告 + 仅送 enabled 子集给后端
  - 全 enabled → 直接送
- 批量评估按钮文案动态：`selectedIds.size > 0` 时显示「批量评估（已选 N）」，否则「批量评估」
- 新增 `<Select data-testid="filter-class-name" />`，放在规则名称之前（类名与数据源/表无强约束，独立）
- 目标表改为 `mode="multiple" maxTagCount="responsive"`
- `refresh` 每次拉数据后剔除 `selectedIds` 里已被删的 id（避免 stale 选中挂在已删除行上）
- `resetFilters` 同时清空 `selectedIds`
- `listRules` API 客户端把数组 params 透传给 axios（自动展开为 `?key=A&key=B` 重复同名参数）

### 关键设计取舍

- **不抽离 `Select 复用组件`**：mode="multiple" 与 single 共用 antd `Select`，靠 props 切；为这一个页面抽组件违反 YAGNI。
- **批量按钮分组 vs 多选行**：选择行首 checkbox（Table 内置）+ 按钮共存，antd 默认就支持全选/取消全选，不必手写。
- **`source_class_id` 公开写 API 仍不暴露**：该字段仅由 wizard 内部写入（保护数据完整性）；本次新增的过滤入参属于只读读取，不破坏既有写入策略。

## 3. 数据模型变更

无。仅扩展过滤参数与读取响应。

## 4. 接口契约变更

**追加 schema**（`backend/app/domain/schemas.py`）：

```python
class ClassOption(CamelModel):
    id: int
    class_name: str

class RuleOptionsRead(CamelModel):
    rule_names: list[str] = Field(default_factory=list)
    datasource_ids: list[DatasourceOption] = Field(default_factory=list)
    target_tables: list[str] = Field(default_factory=list)
    severities: list[str] = Field(default_factory=list)
    class_options: list[ClassOption] = Field(default_factory=list)  # ← 新增
```

**`listRules` 新增 query 参数**：

| 参数 | 类型 | 语义 |
|---|---|---|
| `targetTables` | `list[str] \| None` | 多选精确 IN 匹配（**与 `targetTable` ILIKE 二选一**，targetTables 优先） |
| `sourceClassId` | `int \| None` | 本体类 id 精确匹配 |

**前端 types 扩展**（`frontend/src/types/dataQuality.ts`）：

```typescript
export interface DataQualityRuleListParams {
  // ...既有字段
  targetTables?: string[];     // ← 新增
  sourceClassId?: number;      // ← 新增
}

export interface ClassOption { id: number; className: string }  // ← 新增

export interface RuleOptions {
  ruleNames: string[];
  datasourceIds: DatasourceOption[];
  targetTables: string[];
  severities: Severity[];
  classOptions: ClassOption[];  // ← 新增
}
```

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/domain/schemas.py` | 新增 `ClassOption`；`RuleOptionsRead` 加 `class_options` 字段 |
| `backend/app/services/data_quality_service.py` | `listRules` 加 `targetTables` / `sourceClassId`（targetTables IN 优先于 targetTable ILIKE）；`listOptions` 加 `classOptions` 查询 |
| `backend/app/api/v1/data_quality.py` | 路由 `listDataQualityRules` 加 2 个 Query 参数并转发 |
| `backend/app/tests/integration/test_data_quality_api.py` | 新增 `TestDataQualityRuleMultiFilters` 4 测试 |
| `frontend/src/types/dataQuality.ts` | `DataQualityRuleListParams` 加 2 字段；新增 `ClassOption`；`RuleOptions` 加 `classOptions` |
| `frontend/src/api/dataQuality.ts` | `listRules` 透传数组 params（axios 自动展开） |
| `frontend/src/hooks/useDataQualityFilterOptions.ts` | `EMPTY` 占位补 `classOptions: []` |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | 新增 `filterClassName` / `evaluateBatchSelected(count)` / `evaluateBatchNoSelection` / `evaluateBatchHasDisabled(count)` |
| `frontend/src/pages/DataQualityPage.tsx` | filter 加 className Select；targetTable 改 multi；Table 加 rowSelection；`handleBatchEvaluate` 改 selectedIds 路径；按钮文案动态；refresh 清 stale 选中 |
| `frontend/src/tests/DataQualityPage.batch.test.tsx` | **新增**——6 用例覆盖批量评估多选 4 例 + sourceClassId 过滤 1 例 + targetTables 多选 1 例 |
| `frontend/src/tests/DataQualityPage.test.tsx` | 所有 `listRuleOptions.mockResolvedValue` 加 `classOptions: []`；旧「没有已启用」批量测试改写为符合新行为的「请先勾选」测试 |
| `frontend/src/pages/__tests__/DataQualityPage.filters.test.tsx` | `MOCK_OPTIONS` 加 `classOptions` |

## 6. 测试

**后端集成**（`backend/app/tests/integration/test_data_quality_api.py::TestDataQualityRuleMultiFilters`）：

- `test_list_rules_target_tables_in`：3 条不同 target_table，`?targetTables=PORDER&targetTables=PO_HEADER` 只返这 2 条
- `test_list_rules_target_tables_combined_with_other`：targetTables + datasourceId 复合
- `test_list_rules_filter_by_source_class_id`：造 2 类 + 2 规则，`?sourceClassId=X` 只返 X 类下的规则（ORM 直写 `source_class_id`，因公开 API 不接受该字段）
- `test_list_options_includes_class_options`：`/rules/options.classOptions` 含 active 类、不含墓碑

结果：**22 passed**（18 既有 + 4 新）。

**前端 vitest**（`DataQualityPage.batch.test.tsx` + 既有 `filters.test.tsx` + `test.tsx`）：

- 勾 1 条 + 批量评估：`evaluateBatch.ruleIds` 收到那 1 个 id
- 勾 3 条（2 enabled + 1 disabled）：弹「已忽略」+ 送 enabled 子集
- 未勾任何行：弹「请先勾选」+ **不发**请求
- 勾选全 disabled：弹「没有已启用」+ **不发**请求
- 选类名 Supplier：`listRules` 入参含 `sourceClassId=11`
- 选 datasource + 选 2 个 targetTable：`listRules` 入参含 `targetTables: ["T_ORDER", "PORDER"]`

结果：**22 passed**（10 既有 + 6 新 + 6 filters）。

**TypeScript check**：`npx tsc --noEmit` 干净（修了一处 `.at(-1)` 不在 ES2022 lib 之前的类型问题）。

### 踩坑记录（值得下次注意）

- **antd 5 `Select mode="multiple"` 在 jsdom 下浮层不自动关闭**：选中一项后下拉挂着，可以继续点同一浮层里的第二项 option。**不要** 再次 `fireEvent.mouseDown(.ant-select-selector)` 去「重打开」下拉——`.ant-select-selector` 的子节点全部 `pointer-events: none`，会立刻报 pointer-events 错。详见 `~/.claude/projects/.../memory/antd-mode-multiselect-test-pattern.md`。
- **vitest `Array.at(-1)`**：项目 tsconfig target lib < ES2022 时 `.at` 不存在，用 `arr[arr.length - 1]` 替代。
- **既有测试断言过时**：旧 `expect(warnSpy).toHaveBeenCalledWith("没有已启用的规则")` 在新行为下不该再触发，因为未勾选先走 noSelection 分支；同步更新测试断言是必要的。

## 7. 安全审查

未触发 security-reviewer。本次改动：

- 仅扩展既有 list 接口的过滤维度，**不引入新的写入端点 / 鉴权变更**
- 新增 query 参数（`targetTables` / `sourceClassId`）走 SQLAlchemy `IN` 与 `==` binding，无 SQL 注入
- `class_options` 选项来自 `OntologyClass` 表（active + valid_to IS NULL），不泄露软删除墓碑
- 前端多选走用户主动勾选，无越权边界

## 8. 部署验证

```bash
# 后端（容器是 baked image，必须 docker cp + restart）
docker cp backend/app/. qa-backend:/app/app/
docker restart qa-backend

# 前端（vite 打包构建，不是文件直读）
cd docker && docker compose -f docker-compose.yml build frontend \
  && docker compose -f docker-compose.yml up -d frontend
```

## 9. 真实数据验证

真机 `qa_metadata` 冒烟（用户主库 THBI Oracle）：

```bash
# 1) 新增 sourceClassId 过滤
curl -s 'http://localhost:8000/api/v1/data-quality/rules?sourceClassId=4' | jq length

# 2) 新增 targetTables 多值 IN 过滤
curl -s 'http://localhost:8000/api/v1/data-quality/rules?targetTables=ODS_BPSUPPLIER&targetTables=PORDER' | jq 'map(.ruleCode)'

# 3) /options 新增 classOptions
curl -s 'http://localhost:8000/api/v1/data-quality/rules/options' | jq '.classOptions | length'
# 期望 > 0，含当前 active 类
```

## 10. 关联

- **前置**：`feat-dq-rule-list-filters`（5 字段级联筛选的基线，本次在其上加 2 字段 + 1 行选）
- **后置**：
  - `feat-dq-rule-auto-generation` 写入 `source_class_id` 的逻辑（已存在，不变；本次新增的过滤读取它）
  - 后续可选：`feat-dq-rule-batch-batch-eval-job`（把单次 evaluateBatch 升级为后台 job，本次不做）
- **关联**：
  - `Harness/rules/测试规范.md` §「真实数据库测试」——后端 4 个集成测试走真实 PG + 完整 API 链路，无 sqlite mock
  - `Harness/wiki/data-quality.md`（**尚未存在**，与前置 feat 一致暂缓；建议 Phase 6 收尾时合并建一份）
- **规则**：`Harness/rules/开发流程规范.md` §10 阶段；TDD 强制 RED → GREEN → IMPROVE