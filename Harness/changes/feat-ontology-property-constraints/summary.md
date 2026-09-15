# 变更：feat-ontology-property-constraints

- **日期**：2026-09-14
- **作者**：qa-system
- **Phase**：完成
- **状态**：done

## 1. 需求

规则生成向导（`/data-quality/generate`）LLM 推荐面板里，「采纳并沉淀」按钮目前**仅**对 `kind === "allowed_values"` 生效；其它 kind（`not_null` / `range` / `pattern`）用户点击后直接弹 `messages.adoptNotApplicable` 警告，提示「请联系管理员扩展 ontology_property 字段」。

**目标：** 让 4 类 LLM 建议（`allowed_values` / `not_null` / `range` / `pattern`）都能沉淀到本体，并提供本体管理页手工配置入口。

**验收标准：**
- `ontology_property` 表加 4 列（`is_not_null BOOL`、`min_value VARCHAR(50)`、`max_value VARCHAR(50)`、`regex_pattern VARCHAR(255)`）
- 后端 `applySuggestion` service 按 `kind` 派发写入
- 前端向导按钮按 kind 调 `applySuggestion`，管理页编辑表单可手动维护 4 个字段
- LLM prompt 同步支持 4 类 kind 输出
- `parseDescriptions.persisted_property_ids` 覆盖 4 类 kind（当前仅查 `allowed_values`）

## 2. 设计评审

| 维度 | 选择 | 理由 |
| --- | --- | --- |
| 字段类型 | `is_not_null BOOL NULL` / `min/max VARCHAR(50) NULL` / `regex VARCHAR(255) NULL` | min/max 兼容日期 / 数字 / 枚举 key，str 兜底；regex 业内常见上限 |
| 派发位置 | backend service 层 | schema 层无法校验 kind↔字段一致性（kind 在 schema 但字段在 service 决定） |
| 前端 payload 形态 | discriminated union `{ kind, ... }` | TS 编译期保证 kind↔字段一致 |
| 前端 min/max 录入控件 | `InputNumber` | 数值友好；后端转字符串入库（兼容性兜底） |
| 迁移号 | `0071_ontology_constraints` | 原计划 `0071_ontology_property_constraints` (35 字符) 因 `alembic_version.version_num` varchar(32) 截断失败；缩短到 25 字符 |
| Range 校验 | best-effort float 比 min/max | 字符串 lex 比较 `"100" > "50"` 是 False，数字/日期/枚举 key 都可能，纯字符串比会误判 |

## 3. 数据模型变更

### `ontology_property` 表加 4 列

```python
# backend/app/domain/models.py:299
is_not_null: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
min_value: Mapped[str | None] = mapped_column(String(50), nullable=True)
max_value: Mapped[str | None] = mapped_column(String(50), nullable=True)
regex_pattern: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

### Alembic 迁移：`0071_ontology_constraints`

- `down_revision = "0070_community_topic"`
- 4 个 `op.add_column`，全部 nullable=True、无 default
- 可逆：`alembic downgrade -1`

**部署验证：** dev 库已 `alembic upgrade head` → head = `0071_ontology_constraints`。

## 4. 接口契约变更

### 后端 schema

| Schema | 字段 |
| --- | --- |
| `OntologyPropertyUpdate` | `is_not_null / min_value / max_value / regex_pattern` 4 个 Optional 字段 + `_validateRegexPattern` 字段验证器 |
| `OntologyPropertyRead` | 4 个字段（Optional） |
| `ApplySuggestionRequest` | `kind: str = Field(default="allowed_values")` + `allowed_values / min_value / max_value / regex_pattern`；字段验证器 `_validateKind` 白名单、`_validateRegexPattern`、`_validateAllowedValuesNoQuotes` |
| `ApplySuggestionResponse` | `kind: str` + 4 个 echo 字段 |
| `PropertyConstraintSuggestionRead` | 3 个新字段 `min_value / max_value / regex_pattern` |

### 前端 API（`applySuggestion`）

```typescript
// src/api/dataQualityGenerate.ts
export type SuggestionKind = "allowed_values" | "not_null" | "range" | "pattern";
export interface ApplySuggestionPayload {
  kind: SuggestionKind;
  allowedValues?: string[];
  minValue?: string;
  maxValue?: string;
  regexPattern?: string;
}
export async function applySuggestion(
  propertyId: number,
  payload: ApplySuggestionPayload | string[],   // 兼容旧 string[] = allowed_values
): Promise<void>
```

### kind ↔ 字段派发表

| kind | 必传字段 | 写入 ontology_property 列 | 422 触发 |
| --- | --- | --- | --- |
| `allowed_values` | `allowed_values[]` | `allowed_values` | 含单引号 / 空列表 |
| `not_null` | — | `is_not_null = True` | — |
| `range` | `min_value + max_value` | `min_value, max_value` | 缺一 / min>max(数字 best-effort) |
| `pattern` | `regex_pattern` | `regex_pattern` | regex compile 失败 |
| 其它 | — | — | `unsupported kind` |

## 5. 实现要点

### 后端关键文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/domain/models.py:299` | OntologyProperty 加 4 列 |
| `backend/alembic/versions/0071_ontology_constraints.py` | 新建迁移 |
| `backend/app/domain/schemas.py:537,602,2939,2946` | Update/Read/ApplySuggestion schema |
| `backend/app/services/data_quality_rule_generate_service.py:442` | `applySuggestion` 按 kind 派发 + outbox 记录 before/after + kind |
| `backend/app/services/data_quality_rule_llm_service.py:88,177` | LLM prompt 扩 4 类 + `persisted_property_ids` 改为 `or_(...)` 覆盖 4 列 |

### 前端关键文件

| 文件 | 改动 |
| --- | --- |
| `src/types/ontology.ts:79,109` | OntologyProperty / Update 加 4 字段 |
| `src/types/dataQualityGenerate.ts:81` | `PropertyConstraintSuggestion` 加 3 个 optional 字段 + `ConstraintKind` 扩到 4 |
| `src/api/dataQualityGenerate.ts:97` | `applySuggestion` 改为 discriminated payload |
| `src/pages/DataQualityRuleGeneratePage.tsx:260,295` | `handleApply` 去掉 `kind !== "allowed_values"` 早返；`buildApplyPayload` 按 kind 派发 |
| `src/pages/OntologyPropertyAdminPage.tsx:282` | 编辑弹窗在 description 后插 4 个 Form.Item（Switch / InputNumber / InputNumber / Input） |
| `src/i18n/zh-CN.ts + en-US.ts` | 加 4 个字段的 label/hint/placeholder key |

### 关键算法

**`applySuggestion` 派发（节选）：**
```python
update_values: dict = {}
if payload.kind == "allowed_values":
    update_values["allowed_values"] = payload.allowed_values
elif payload.kind == "not_null":
    update_values["is_not_null"] = True
elif payload.kind == "range":
    if not payload.min_value or not payload.max_value:
        raise ValidationError("range 需同时传 min_value 和 max_value")
    try:
        if float(payload.min_value) > float(payload.max_value):
            raise ValidationError("min_value 不能大于 max_value")
    except (TypeError, ValueError):
        pass  # 字符串不可转 float（日期/枚举）放过
    update_values["min_value"] = payload.min_value
    update_values["max_value"] = payload.max_value
elif payload.kind == "pattern":
    import re
    re.compile(payload.regex_pattern)  # 422 on failure
    update_values["regex_pattern"] = payload.regex_pattern
else:
    raise ValidationError(f"unsupported kind: {payload.kind}")
```

**`buildApplyPayload`（前端 TS discriminated union）：**
```typescript
function buildApplyPayload(item: PropertyConstraintSuggestion):
  | { kind: "allowed_values"; payload: { kind: "allowed_values"; allowedValues: string[] } }
  | { kind: "not_null"; payload: { kind: "not_null" } }
  | { kind: "range"; payload: { kind: "range"; minValue: string; maxValue: string } }
  | { kind: "pattern"; payload: { kind: "pattern"; regexPattern: string } }
  | { kind: null } {
  switch (item.kind) {
    case "allowed_values":
      return { kind: "allowed_values", payload: { kind: "allowed_values", allowedValues: item.values ?? [] } };
    case "not_null":
      return { kind: "not_null", payload: { kind: "not_null" } };
    case "range":
      if (!item.minValue || !item.maxValue) return { kind: null };
      return { kind: "range", payload: { kind: "range", minValue: item.minValue, maxValue: item.maxValue } };
    case "pattern":
      if (!item.regexPattern) return { kind: null };
      return { kind: "pattern", payload: { kind: "pattern", regexPattern: item.regexPattern } };
  }
}
```

## 6. 测试

### 后端（`backend/app/tests/integration/test_dq_rule_generate_llm_api.py`）

新增 10 个用例：
- `test_apply_suggestion_not_null_sets_flag`
- `test_apply_suggestion_range_persists_min_max`
- `test_apply_suggestion_pattern_persists_regex`
- `test_apply_suggestion_range_rejects_missing_min_422`
- `test_apply_suggestion_range_rejects_min_greater_than_max_422`
- `test_apply_suggestion_pattern_rejects_invalid_regex_422`
- `test_apply_suggestion_unsupported_kind_422`
- `test_apply_suggestion_outbox_records_kind_and_all_fields`（处理 asyncpg JSONB dict vs string 双形态）
- `test_persisted_property_ids_includes_is_not_null`
- `test_parse_descriptions_recognizes_range_and_pattern_kinds`

**结果：** `26 passed in 8.56s`（含原有 16 个）

### 后端回归

- `test_ontology_api.py`：1 个 RBAC 用例失败（**pre-existing**，与本特性无关）
- `test_data_quality_rule_generator.py`：1 个 join-edge 用例失败（**pre-existing**，与本特性无关）
- 其余 76 个用例全 pass

### 前端（`src/pages/__tests__/DataQualityRuleGeneratePage.adoptContract.test.ts`）

扩 6 个 contract 用例：
- removes the legacy `kind !== "allowed_values"` early-return
- routes not_null kind to applySuggestion with kind:'not_null' and no extra fields
- routes range kind to applySuggestion with kind:'range' + minValue + maxValue
- routes pattern kind to applySuggestion with kind:'pattern' + regexPattern
- api applySuggestion signature accepts discriminated kind payload (4 kinds)
- buildApplyPayload warns via message.warning when LLM output is incomplete (kind=null)

**结果：** 17/17 pass

### 前端新建（`src/pages/__tests__/OntologyPropertyAdminPage.contract.test.ts`）

9 个 contract 用例：
- EditFormValues interface declares the 4 constraint fields
- renders isNotNull as Switch with valuePropName='checked'
- renders minValue and maxValue as InputNumber
- renders regexPattern as Input (string)
- openEdit pre-populates the 4 constraint fields from the record
- onSubmit sends the 4 fields inside OntologyPropertyUpdate payload
- OntologyProperty + OntologyPropertyUpdate types declare the 4 camelCase fields
- i18n zh-CN defines 4 label keys
- i18n en-US defines 4 label keys

**结果：** 9/9 pass

### 总体前端

`npx vitest run src/pages/__tests__/DataQualityRuleGeneratePage.adoptContract.test.ts src/pages/__tests__/OntologyPropertyAdminPage.contract.test.ts` → 26/26 pass
`npx tsc --noEmit` → 0 errors

### 前端回归（其它文件）

40 failed / 965 passed（全部 pre-existing：ChatPage / DatasourcePage / AgentRegistryPage 等，与本特性无关）

## 7. 安全审查

未触发 security-reviewer。本特性仅扩展 Pydantic schema 字段 + 添加白名单 kind 校验器，无：
- 硬编码 secret
- 新鉴权路径
- 新外部 API 调用
- SQL 注入面（仍走 SQLAlchemy ORM + Alembic migration）

`_validateKind` 字段验证器对 `ApplySuggestionRequest.kind` 做白名单（仅允许 `allowed_values / not_null / range / pattern`），防止误传任意字符串绕过派发。

## 8. 部署验证

1. **迁移：**
   ```
   docker exec qa-backend alembic current
   → 0071_ontology_constraints (head)
   ```
2. **后端集成测试：**
   ```
   docker exec qa-backend pytest app/tests/integration/test_dq_rule_generate_llm_api.py
   → 26 passed
   ```
3. **前端 TS 编译：**
   ```
   npx tsc --noEmit → 0 errors
   ```
4. **前端 contract 测试：** 26/26 pass
5. **手工验证：**
   - 进 `/data-quality/generate`，选类
   - LLM 面板点 `not_null` 行的「采纳并沉淀」→ 不再弹「请联系管理员」警告；弹「已沉淀到本体属性」
   - 进 `/ontology-properties`，查那个 property 的编辑弹窗：isNotNull Switch 已 ON，allowedValues 仍为空
   - 取消 isNotNull、保存 → 再点 LLM 面板「采纳并沉淀」应能再次成功（不再是已采纳）

## 9. 关联

- 设计稿：`Harness/changes/_template/summary.md`（模板）
- Wiki：`Harness/wiki/`（未新增 wiki 条目，约束字段是模型细节，归入模型 wiki）
- 规则：`Harness/rules/开发流程规范.md`、`Harness/rules/测试规范.md`

## 已知限制

- **Range 不区分数字 vs 日期 vs 枚举**：min_value / max_value 统一存 VARCHAR(50)，LLM 自行决定；service 只做 best-effort float 比较，字符串不可转 float 时放过。
- **regex_pattern 无服务端校验超长**：VARCHAR(255) 在 Pydantic 层 max_length=255 拦截。
- **四类约束并存合法**：UI 一次只能编辑 kind 之一，不强约束互斥；后端写完即覆盖（多次采纳会覆盖之前的约束）。
- **未改 `parseDescriptions` 的 `confidence` / `rationale` 字段**：仅加 3 个新字段（min/max/regex_pattern），原 LLM 输出契约不破坏。