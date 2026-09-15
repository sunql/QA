# 数据质量规则结构化参数（Rule Params）— 设计文档

- 日期：2026-09-15
- 分支：feat/dq-rule-params（待建）
- 状态：已获用户批准的设计（七节确认通过 + 隔离边界确认）

## 1. 目标

把数据质量规则的配置方式从「手写 SQL 谓词文本」改造为「结构化参数 + 后端编译器」。覆盖五种规则类型（完整性 / 唯一性 / 有效性 / 引用性 / 一致性），准确、易维护、语义可读。同时作为独立特性交付：

- **DB 共用**：在现有 `data_quality_rule` 表加可空 JSONB 列，存量规则零迁移
- **前后端独立**：新菜单、新页面、新 API、新服务，不动现有任何方法
- **评估器零改动**：写入时编译，evaluator 照旧读 `rule_expression` 执行
- **稳定后再合并**：v1 只做手工表单创建；与现有推导引擎/LLM 路径的衔接留二期

## 2. 已确认的关键决策

| 决策点 | 结论 |
|---|---|
| SSOT | 结构化模式下 `rule_params` 为语义事实源；`rule_expression` 为编译产物（人读/审计） |
| 编译时机 | **写入时编译**（create/update）；评估器零改动，存量评估字节级回归 |
| 互斥 | `params IS NULL` ↔ 自定义 SQL 模式；`params IS NOT NULL` ↔ 结构化模式；服务端强制互斥 |
| 复杂度 | CONSISTENCY 仅支持单条子句；多子句 AND/OR 走自定义 SQL 逃逸舱（YAGNI） |
| NULL 语义 | VALIDITY/CONSISTENCY 下 NULL 一律 FAIL；NULL 容忍由 COMPLETENESS 规则单独管（语义不重叠） |
| v1 范围 | 手工表单创建结构化规则；**不接入**自动推导/LLM，避开对现有 generator/llm_service 的改动 |
| 隔离 | DB 共用 + 后端/前端独立文件 + 新菜单 `数据质量 / 规则配置(结构化)`；稳定后再合二期推导 |

## 3. 架构与数据流

```
┌──────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│ RuleParamsForm   │       │ RuleParams API   │       │ existing         │
│ (新菜单页面)      │ ───▶  │ /api/v1/dq-rule- │ ───▶  │ data_quality_rule│
│                  │ POST  │ params/rules     │ INSERT│ +rule_params 列   │
└──────────────────┘       └────────┬─────────┘       └──────────────────┘
                                    │
                                    ▼
                       ┌────────────────────────┐
                       │ compileRuleParams      │
                       │ + validate_expression  │ ──▶ rule_expression
                       │ （写入时编译）            │     （评估器读这列）
                       └────────────────────────┘
                                    │
                                    ▼ 评估期
                       ┌────────────────────────┐
                       │ 现有 5 个 evaluator    │（零改动）
                       │ 读 rule_expression     │
                       └────────────────────────┘
```

数据流要点：

1. **写入**：结构化模式提交 `rule_params` → Pydantic 字段级校验 → `compileRuleParams` 纯函数 → 产物再过 `validate_expression` 白名单 → 入库 `rule_params + rule_expression`
2. **评估**：evaluator 永远读 `rule_expression`，无论该表达式是结构化编译产物还是手写原文
3. **存量**：旧规则无 `rule_params` → API/服务不触碰；evaluator 走原文路径

## 4. 数据模型

### 4.1 新增列（共用表）

`data_quality_rule.rule_params JSONB NULL`，Alembic 迁移 `0063_data_quality_rule_params.py`：

```sql
ALTER TABLE data_quality_rule ADD COLUMN rule_params JSONB NULL;
COMMENT ON COLUMN data_quality_rule.rule_params IS
  '结构化规则参数；NULL=自定义SQL模式(legacy)，非NULL=结构化模式';
```

字段可空 → 零回填、零迁移风险、存量规则全部按原 `rule_expression` 评估。

### 4.2 参数 Schema（按 rule_type）

| rule_type | kind | params 字段 | 编译产物示例 |
|---|---|---|---|
| COMPLETENESS | `not_null` | — | `<col> IS NOT NULL`（evaluator 不读，仅展示） |
| UNIQUENESS | `unique` | — | `UNIQUE(<col>)`（同上） |
| VALIDITY | `range` | `min?: Decimal, max?: Decimal`（至少一边） | `<col> BETWEEN 0 AND 100000` |
| | `in_set` | `values: string[](≥1, ≤50, 禁 ' \\ 控制字符)` | `<col> IN ('A','B')` |
| | `regex` | `pattern: string(1..500)` | `<col> ~ '^[0-9]+$'` |
| | `compare` | `op ∈ > >= < <= = !=`,`value: Decimal` | `<col> > 0` |
| REFERENTIAL | `ref` | `ref_table, ref_column`(标识符白名单) | `REF SUPPLIER.SUPPLIER_KEY` |
| CONSISTENCY | `cross_column` | `left, op, right, factor?: Decimal(≠0, 默认1.0)` | `RECEIVED_QTY <= ORDER_QTY * 1.05` |

Pydantic discriminated union on `kind`，单文件 `app/domain/schemas_dq_rule_params.py`：

```python
class _NotNullParams(BaseModel):      kind: Literal["not_null"]
class _UniqueParams(BaseModel):       kind: Literal["unique"]
class _RangeParams(BaseModel):
    kind: Literal["range"]
    min: Decimal | None = None
    max: Decimal | None = None
    @model_validator: 至少给一边
class _InSetParams(BaseModel):
    kind: Literal["in_set"]
    values: list[str] = Field(min_length=1)
    @field_validator(values): 复用 _ALLOWED_VALUE_RE 风格
class _RegexParams(BaseModel):
    kind: Literal["regex"]
    pattern: str = Field(min_length=1, max_length=500)
    @field_validator(pattern): re.compile 预编译
class _CompareParams(BaseModel):
    kind: Literal["compare"]
    op: Literal[">",">=","<","<=","=","!="]
    value: Decimal
class _RefParams(BaseModel):
    kind: Literal["ref"]
    ref_table:  str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ref_column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
class _CrossColumnParams(BaseModel):
    kind: Literal["cross_column"]
    left:   str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    op:     Literal[">",">=","<","<=","=","!="]
    right:  str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    factor: Decimal | None = None
    @model_validator: factor != 0

RuleParams = Annotated[
    Union[_NotNullParams, _UniqueParams, _RangeParams, _InSetParams,
          _RegexParams, _CompareParams, _RefParams, _CrossColumnParams],
    Discriminator("kind"),
]
```

### 4.3 互斥规则

服务端模型校验（`DataQualityRuleParamsCreate/Update`）：

| rule_type | rule_params | rule_expression | 行为 |
|---|---|---|---|
| VALIDITY / CONSISTENCY / REFERENTIAL | 非 NULL | NULL | 结构化模式：忽略 expression，编译 params 落库 |
| | NULL | 非 NULL | 自定义模式：原样存 expression |
| | 非 NULL | 非 NULL | **422**「结构化模式忽略客户端 expression」 |
| | NULL | NULL | **422**「该规则类型需要 params 或 expression」 |
| COMPLETENESS / UNIQUENESS | 可选 | 可选 | 评估器均不依赖；展示用，模式判定按 params 是否存在 |
| TIMELINESS | NULL | — | 不支持（evaluator 未实现） |

## 5. 编译器

新模块 `backend/app/services/data_quality_evaluators/rule_params_compiler.py`，纯函数、无 IO、与 `_common.py` 同目录复用 `validate_identifier` / `validate_expression`。

```python
def compileRuleParams(ruleType: RuleType, params: dict) -> str: ...
def _compileRange(rule, p) -> str: ...
def _compileInSet(rule, p) -> str: ...
def _compileRegex(rule, p) -> str: ...
def _compileCompare(rule, p) -> str: ...
def _compileRef(rule, p) -> str: ...
def _compileCrossColumn(rule, p) -> str: ...
def _compileNotNull(rule, p) -> str: ...
def _compileUnique(rule, p) -> str: ...
```

每个 `_compile*` 统一执行：

1. Pydantic 校验（字段类型/必填/值域）→ 422 字段级错误
2. 标识符过 `validate_identifier`（表/列名）
3. 字面量按 kind 各自校验（值禁 `'/\\`/控制字符；数字 `Decimal`；正则预编译）
4. 生成 SQL 片段
5. **产物过 `validate_expression` 兜底**（纵深防御——即便前四步有疏漏，评估期 SQL 不逃白名单）

## 6. 新增文件清单（隔离交付）

### 6.1 后端（全部新文件）

```
backend/app/
├── api/v1/
│   └── data_quality_rule_params.py          # 新 router
├── services/
│   ├── data_quality_rule_params_service.py  # CRUD + 编译编排
│   └── data_quality_evaluators/
│       └── rule_params_compiler.py          # 纯函数编译器
├── domain/
│   └── schemas_dq_rule_params.py            # Pydantic 模型独立命名空间
└── alembic/versions/
    └── 0063_data_quality_rule_params.py     # 新增可空列
```

`main.py` 仅追加一行 `include_router(...)`，不修改现有导入或路由。

### 6.2 后端（明确不动）

- 5 个 evaluator、`_common.py`、`data_quality_evaluator.py`（**写入时编译设计让其零改动**）
- `data_quality_service.py`、现有 `app/api/v1/data_quality.py`
- `data_quality_rule_generator.py`、`data_quality_rule_llm_service.py`
- `data_quality_rule_generate_service.py`、`data_quality_score_service.py`、`data_quality_violation_sample_service.py`
- 现有所有集成测试套（评估路径字节级一致）

### 6.3 前端（全部新文件）

```
frontend/src/
├── pages/
│   └── DataQualityRuleParamsPage.tsx        # 新菜单入口页
├── components/dq/
│   └── RuleParamsForm.tsx                   # 按 rule_type + kind 分发表单
├── utils/
│   └── ruleParamsSummary.ts                 # params → 中文/英文语义摘要
└── api/
    └── dataQualityRuleParams.ts             # 新 API 客户端
```

`App.tsx` 加新路由；`scripts/seed_menu_config.py` 加新菜单项（`menu.item` 命名空间）；i18n `zh-CN.ts` / `en-US.ts` 加新条目。

### 6.4 前端（明确不动）

- `DataQualityPage.tsx`、`DataQualityRuleBatchCreatePage.tsx`
- `ruleExpressionTemplates.ts`（现有规则类型表单沿用其模板）
- 现有 `api/dataQuality*.ts` 客户端

## 7. 前端表单（RuleParamsForm）

按 `rule_type + params.kind` 分发到子表单：

| 规则类型 / kind | 表单控件 | 数据源 |
|---|---|---|
| VALIDITY `range` | 区间模式开关 + min/max `InputNumber` | — |
| VALIDITY `in_set` | 多值 `Select mode="tags"` 或 `Input.Tag` | 本体 allowed_values(若有) |
| VALIDITY `regex` | `Input` + `re.compile` 实时高亮报错 | — |
| VALIDITY `compare` | 操作符 `Select` + `InputNumber` | — |
| REFERENTIAL `ref` | 引用表 `Select` + 引用列 `Select` | schema 缓存（与 `RuleBatchStepColumns` 共用 endpoint） |
| CONSISTENCY `cross_column` | 左列 `Select` + 操作符 + 右列 `Select` + 容差 `InputNumber`(默认 1) | 同上 |

页面顶部「结构化 / 自定义 SQL」开关，默认结构化；自定义模式保留原 `Input.TextArea`。列表/详情页响应带 `config_mode: "structured"|"custom"`，结构化规则展示**语义摘要行**（「收货数量 ≤ 订单数量 × 1.05」）+ 次要展示编译产物 expression。

摘要渲染函数 `ruleParamsSummary.ts` 与编译器 kind 一一对应（同定义、同测试对称），纯函数覆盖 8 种 kind。

## 8. 测试策略

**后端（TDD 红→绿）**：
1. `rule_params_compiler` 单测：每 kind 至少 4 例（正常 / 边界 / 失败 / 注入）
2. Round-trip：每 kind 编译产物必过 `validate_expression`
3. Schema 校验：每 kind Pydantic 对错误 payload 给精确字段路径
4. API 集成测试（**真实 PG**，按 `Harness/rules/测试规范.md`）：
   - 结构化模式 create → DB params+expr 正确、`config_mode='structured'`
   - 自定义模式 create → DB 仅 expr、`config_mode='custom'`
   - 互斥违例 → 422 + 具体消息
   - 结构化规则评估（validity range / unique / ref / cross_column 走真实 PG + fixture）
   - **存量规则评估路径不变 → 既有集成测试套字节级回归**
5. Alembic 迁移：`upgrade head` → 列存在 + 默认 NULL；`downgrade -1` → 列删除；老 migration 不破坏

**前端（vitest + RTL）**：
- `RuleParamsForm`：每 kind 渲染、回填、字段错误显示、模式切换保互斥
- `ruleParamsSummary`：每 kind 输出文案
- `DataQualityRuleParamsPage` 集成：开关交互、表单提交体符合契约、菜单可达

**门禁**：≥80% 覆盖率（项目硬约束）、`code-reviewer` 审查、`Harness/rules/SQL审查清单.md` 对 Alembic 迁移审查。

**端到端验证**（真机部署后）：
- 新建 5 类各 1 条结构化规则 → DB params/expr 落库正确 → 评估 PASS/FAIL 符合预期
- 存量自定义规则评估行为不变
- 编辑某结构化规则 → 重编译产物稳定（同一 params → 同一 expression）

## 9. 端到端验证清单

| 项 | 验证方式 |
|---|---|
| 结构化创建规则 | UI 填表单 → 后端落库 → DB params/expr 正确 |
| 自定义规则无影响 | 现有 DataQualityPage 创建规则 → 行为不变 |
| 评估路径不变 | 既有集成测试全绿 |
| 编译器安全 | 注入测试用例（`'; DROP`、`UNION SELECT`、超长 pattern）→ 422 |
| 菜单可达 | 新菜单渲染、权限路由通 |
| i18n | 中英文切换文案完整 |

## 10. 二期方向（v1 不做）

- 推导引擎 `_deriveForProperty` 各分支改为产 `rule_params`，编译产物做展示
- LLM parse-descriptions 路径直接产 params 写入 `data_quality_rule.rule_params`
- CONSISTENCY 多子句 AND/OR 结构化支持
- 跨数据源 schema 列选择器通用化（v1 限单数据源内）

二期在 v1 稳定、合并回主分支后另起设计文档与实施计划。
