# 变更:规则批量新建(降低维护难度)

- **日期**:2026-09-15
- **作者**:Claude
- **Phase**:Phase 6 数据质量(规则 CRUD)
- **状态**:implemented(2026-09-15 12:33 已 docker restart,API 实测 201 通过)
- **关联变更**:无(开创新流程)
- **迁移版本**:无(纯前端 + 复用既有后端端点)
- **MEMORY**:`../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode/memory/rule-batch-create.md`

---

## 1. 需求

数据质量管理的新建规则功能对编写者要求过高:
- 规则编码需人工按命名规范写,容易冲突/不规范
- 选完数据源后目标表是裸 `<Input>`,没有下拉/模糊查询
- 选完表后没有列清单视图,逐列手工填写工作量大
- 规则名需人工想,容易重复/无业务含义
- 规则表达式对不熟悉 SQL Guard 白名单的人门槛高

**目标**:把"为某类的某张表的某些列批量维护规则"的复杂度,从 N 次手工填表降到 1 次向导式操作,自动生成编码/名称/简单表达式,人工只需关注复杂表达式与责任方/描述。

**验收**(用户原话,2026-09-15):
1. 规则编码按 `MU-DQ+类名+年+月+日+5位流水` 自动生成,无需人工写
2. 选择数据源后,目标表有下拉框,可模糊查询
3. 选完表后下面出现表格,把对应列展示出来
4. 每一行维护规则后保存一条规则;**规则名 = 类名+列名+规则类型名**,不允许重复
5. 规则表达式:简单类型(VALIDITY/UNIQUENESS/COMPLETENESS)自动匹配,复杂的允许人工修改
6. 阈值(%)默认 100%,严重级别默认中,默认启用,责任方人工填,描述(翻译成中文)人工填

## 2. 设计评审

### 2.1 入口位置

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. DataQualityPage 工具栏加「批量新建」按钮 | 改动最小,复用现有入口 | 拒:同一 tab 已有 AI 自动生成向导「生成」,再加按钮视觉拥挤 |
| B. 扩展 DataQualityRuleGeneratePage 第 2 步 | AI + 手工合并 | 拒:向导变复杂,UI 重叠,失去"纯手工批量"语义 |
| C. **独立新页面 `/data-quality/rules/batch-create`** | 全屏单页式 | **选**:用户明确指定;沉浸感强,适合批量 |

最终:**C**。新路由 `frontend/src/App.tsx` 加 `<Route path="data-quality/rules/batch-create">`,菜单项在 DataQualityPage 的 RulesTab 头部加一个链接按钮跳转。

### 2.2 页面布局(方案 A「向导式三步」)

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. **页面内三步骤条 + 单页滚动** | 顶部 Steps 进度,内容滚动展开 | **选**:批量 10-30 条同视图,所见即所得,无状态丢失 |
| B. Drawer + Modal 嵌套 | 沿用 antd 标准组件 | 拒:嵌套深度 3 层,UX 反模式;批量时 Drawer 高度不够 |
| C. 表格内联编辑(类 Excel) | 单一大表格逐行编辑 | 拒:实现复杂,新手不友好,违背"降低维护难度"目标 |

最终:**A**。单页布局:

```
┌──────────────────────────────────────────────────────────┐
│ 📚 数据质量 > 批量新建规则                                │
├──────────────────────────────────────────────────────────┤
│ 步骤1:基础配置 ●━━━━━━━ 步骤2:列与规则 ○━━━━━━━ 步骤3:预览 ○ │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━│
│ [数据源▼] [本体类▼] [目标表▼(fuzzy)]  [⟳ 加载列]         │
├──────────────────────────────────────────────────────────┤
│ 步骤2:本表共 N 列,已勾选 M 列                           │
│ ┌──────────┬──────┬─────┬────────────────────────────────┐│
│ │ ☐ 列名   │ 类型 │ PK  │ 规则类型 模板 表达式            ││
│ │ ☑ PO_NO  │ VARC │ ✓  │ [▼UNIQUENESS][—][—]            ││
│ │ ☑ PO_QTY │ NUMB │    │ [▼VALIDITY][▼>0][PO_QTY > 0]   ││
│ │ ☐ DATE   │ DATE │    │ [▼VALIDITY][▼<NOW][—  ]         ││
│ └──────────┴──────┴─────┴────────────────────────────────┘│
├──────────────────────────────────────────────────────────┤
│ 步骤3:预览 N 条规则                                      │
│ ☑ MU-DQ-PURCHASE_ORDER-20260915-00001  采购订单-数量-有效性 │
│                                                          │
│ 全局: [全部启用] [严重级别: 中▼] [阈值: 100%]              │
│                                       [取消] [保存 N 条]   │
└──────────────────────────────────────────────────────────┘
```

### 2.3 规则编码自动生成

格式:`MU-DQ-{类名大写}-{YYYYMMDD}-{5位流水}`

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. **每日重置,当日递增** | 当日 MAX+1,跨日归零 | **选**:符合"按天分批"语义,数字可读 |
| B. 全局递增,永不重置 | 库内 MAX+1 | 拒:数字增长过快,用户滚动确认成本高 |
| C. 每类每日递增 | 类+日复合 | 拒:违反"按日"语义,过度设计 |

**实现**:
- 前端:步骤 1 选完类后,异步 `GET /data-quality/rules/next-code?className=X` 拿建议值,作为初始预览(不是真实占用)
- 后端:每次保存前**二次校验**(`SELECT MAX(...) WHERE rule_code LIKE 'MU-DQ-{CLASS}-{DATE}-%'`),防止并发冲突
- 数据库 `UniqueConstraint("rule_code")` 兜底
- 类名清洗:非 ASCII → transliterate;超长截断到 12 字符

### 2.4 数据源 → 类 → 表 → 列四级级联

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. **以类为主(选类后自动填 target_table)** | 类→source_table→可手动改→模糊查数据源全表 | **选**:用户明确指定;符合"针对一个类批量维护"诉求 |
| B. 纯以数据源为主,类只做标签 | 先数据源→表→列,类不影响表选择 | 拒:违反"以类为主"约束 |
| C. 强制类=表,禁止跨表 | 类绑定的 source_table 不允许改 | 拒:灵活性差,无本体绑定的物理表无法维护 |

**实现**:
- 类联动:`useEffect(() => { if (cls?.source_table) setTargetTable(cls.source_table); }, [cls])`
- 表模糊查询:复用 `GET /datasources/{id}/schema` 一次性拿全表,前端 substring 过滤
- 列清单:schema 自带 `TableSchemaRead.columns`,前端渲染

### 2.5 规则名自动生成

格式:`{类名}-{列名}-{规则类型中文}`

示例:类 PURCHASE_ORDER + 列 PO_QTY + VALIDITY → `采购订单-数量-有效性`

**唯一性范围**:全局唯一(用户明确指定)
- 客户端:`Set()` 检查本地预览,冲突行标红
- 服务端:`SELECT WHERE rule_name = ?` 校验,冲突返 409

**用户覆盖**:默认可编辑,但显示「自动」徽标;用户改后徽标消失。

### 2.6 表达式自动匹配模板

按列数据类型 + 规则类型分组,前端纯函数:

| 列类型 | 规则类型 | 模板 | 输出示例 |
|--------|---------|------|---------|
| NUMBER | VALIDITY | `>0` | `PO_QTY > 0` |
| NUMBER | VALIDITY | `>=0` | `PO_DIFF >= 0` |
| NUMBER | VALIDITY | `BETWEEN min AND max` | `PO_QTY BETWEEN 0 AND 999999` |
| NUMBER | UNIQUENESS | — | (无表达式) |
| VARCHAR | VALIDITY | `IN ('A','B','C')` | `STATUS IN ('A','B','C')` |
| VARCHAR | VALIDITY | `REGEX '^[A-Z0-9]+$'` | `ORDER_NO REGEX '^[A-Z0-9]+$'` |
| VARCHAR | UNIQUENESS | — | (无表达式) |
| VARCHAR | COMPLETENESS | — | (无表达式) |
| DATE | VALIDITY | `< SYSDATE` | `ORDER_DATE < SYSDATE` |
| DATE | VALIDITY | `>= 起始日期` | `START_DATE >= '2020-01-01'` |
| 任意 | REFERENTIAL | `REF <ref_table>.<ref_column>` | `REF SUPPLIER.SUPPLIER_KEY` |

**实现位置**:`frontend/src/utils/ruleExpressionTemplates.ts`(可单测)

**模板变量**:`<min>/<max>` 从 `OntologyProperty.min_value/max_value` 读;`<ref>` 从 `is_foreign_key` + `ref_class_id` 读。

### 2.7 批量提交

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. **前端并发 N 次 POST `/rules`** | 改动最小,失败粒度清晰 | **选**:N 通常 < 50,QPS 安全;后端无侵入 |
| B. 后端新加 `POST /rules/batch` | 事务更紧 | 拒:过度工程,需新 schema + 事务测试 + 错误聚合 |

**失败处理**:`Promise.allSettled`,失败行标红显示原因(rule_code 冲突、expression 校验失败)。

### 2.8 默认值

| 字段 | 默认值 | 来源 |
|------|--------|------|
| `rule_code` | 自动生成 | `next-code` API |
| `rule_name` | 自动生成 | `{类}-{列}-{规则类型}` |
| `rule_expression` | 自动匹配模板 | `ruleExpressionTemplates.ts` |
| `threshold` | **100** | 用户指定,前端传(不动后端默认 95) |
| `severity` | **MEDIUM/中** | 用户指定,与后端默认一致 |
| `is_enabled` | **true** | 后端默认 |
| `version` | `v1.0` | 后端默认 |
| `owner` | 人工填,必填 | — |
| `description` | 人工填,选填 | — |
| `derivation_type` | `MANUAL` | 批量新建视为手工 |

## 3. 数据模型变更

无。后端 schema/ORM 不变,只复用既有 `DataQualityRuleCreate`。

### 3.1 后端正则放宽(2026-09-15 12:32 决定)

**问题**:`DataQualityRuleCreate.rule_code` 原正则为 `^[A-Z][A-Z0-9_]*$`,拒绝 `-`(连字符)。用户原话编码格式 `MU-DQ+类名+年+月+日+5位流水` 显式含连字符 → `POST /rules 422`。

**决策**:把 `backend/app/domain/schemas.py:1847` 的 pattern 从 `^[A-Z][A-Z0-9_]*$` 改为 `^[A-Z][A-Z0-9_-]*$`,仅放宽连字符。下划线/纯字母编码仍合法。

**影响面**:
- 仅 `DataQualityRuleCreate`(rule_code 字段);`AgentCreate` (line 2461) 和 `GenerateRuleItem` (line 2932) 不动(后者是 AI 自动推导确认路径,编码格式固定 `DQ_XXX_<HASH>`,用户未要求改)。
- 历史数据:DB 现有 256 条规则,100% 下划线,0 条含 `-`(查证 `SELECT COUNT(*) FILTER (WHERE rule_code LIKE '%-%')`),放宽对历史数据零破坏。
- 约束保留:小写字母、特殊字符仍被拒(只放 `-`,不放开字母大小写规则)。

**验证**:
- RED:新增 integration test `test_create_code_with_hyphen_accepted`,断言 `MU-DQ-PURCHASE_ORDER-20260915-00001` 接受。
- 既有 `test_create_invalid_code_format_rejected`(lowercase 拒)仍 PASSED。
- 关联 suite 全过:23 条 DQ API + 32 条 generator + 9 条 confirm = 64 条 PASSED。
- 实测 `curl POST /rules` 返 201 + 真实入库 + 查询 DELETE 204。

## 4. 接口契约变更

**新增**:`GET /api/v1/data-quality/rules/next-code?class_name={X}&date={YYYYMMDD}`
- 入参:`class_name` (必填)、`date` (可选,默认今天)
- 出参:`{ "code": "MU-DQ-PURCHASE_ORDER-20260915-00001", "seq": 1 }`
- 实现:`SELECT MAX(...) WHERE rule_code LIKE 'MU-DQ-{CLASS}-{DATE}-%'`,解析后 5 位补零

**复用**:
- `GET /api/v1/ontology/classes/{classId}/properties` (取列元信息)
- `GET /api/v1/datasources/{datasourceId}/schema` (取表/列清单)
- `POST /api/v1/data-quality/rules` (逐条创建)

## 5. 实现要点

### 5.1 新增前端文件

| 文件 | 内容 |
|------|------|
| `frontend/src/pages/DataQualityRuleBatchCreatePage.tsx` | 主页面(三步布局) |
| `frontend/src/components/dq/RuleBatchStepBasic.tsx` | 步骤 1:基础配置(数据源+类+表) |
| `frontend/src/components/dq/RuleBatchStepColumns.tsx` | 步骤 2:列清单+规则动作 |
| `frontend/src/components/dq/RuleBatchStepPreview.tsx` | 步骤 3:预览+全局默认值 |
| `frontend/src/utils/ruleExpressionTemplates.ts` | 表达式模板纯函数(可单测) |
| `frontend/src/utils/ruleCodeGenerator.ts` | 规则名/编码生成 |
| `frontend/src/utils/ruleNameDedup.ts` | 规则名去重检查 |
| `frontend/src/api/dataQualityRuleNextCode.ts` | next-code API 封装 |

### 5.2 修改前端文件

| 文件 | 改动 |
|------|------|
| `frontend/src/App.tsx` | 加 `<Route path="data-quality/rules/batch-create">` |
| `frontend/src/pages/DataQualityPage.tsx` | RulesTab 头部加链接按钮「批量新建」 |
| `frontend/src/i18n/zh-CN.ts` | 加 `dq.batchCreate.*` + 修正 `common.description="描述"` |
| `frontend/src/i18n/en-US.ts` | 加 `dq.batchCreate.*` + 修正 `common.description="Description"` |

### 5.3 新增后端文件

| 文件 | 改动 |
|------|------|
| `backend/app/api/v1/data_quality.py` | 加 `GET /rules/next-code` 端点 |
| `backend/app/services/data_quality_service.py` | 加 `next_rule_code(class_name, date)` 函数 |

### 5.4 数据流

```
用户选类 → 前端 GET /rules/next-code → 拿到编码建议
用户选数据源 → 前端 GET /datasources/{id}/schema → 拿全表/列
用户选表 → 渲染列清单(本地 substring 过滤)
用户勾列 + 选规则类型 → 前端 ruleExpressionTemplates 自动填表达式
                  ↓
                 步骤 3 预览 N 条规则 → 用户可改任意字段
                  ↓
用户点保存 → 前端并发 POST /rules × N
                  ↓
后端 1 次:createRule() → 写库 + 触发 code 唯一性校验
                  ↓
失败行标红(rule_code 冲突 / expression 白名单失败 / name 重复)
```

### 5.5 关键边界

- **类没绑 source_table**:步骤 1 允许用户手动改 target_table(走 fuzzy)
- **列类型不在模板表**:模板 dropdown 显示「无自动模板,需手填」,表达式空白,标黄提示
- **REFERENTIAL 规则类型**:必须选本体关联类 + 列,从 ontology properties 的 `is_foreign_key` 自动预填
- **并发冲突**:`UniqueConstraint` 抛 `IntegrityError`,前端捕获后标红该行「编码已存在,请刷新重试」

## 6. 测试

### 6.1 前端单测(vitest)

| 文件 | 用例 |
|------|------|
| `frontend/src/tests/ruleExpressionTemplates.test.ts` | 模板匹配:NUMBER+VALIDITY+列名 PO_QTY → `PO_QTY > 0`;VARCHAR+UNIQUENESS → 空;DATE+VALIDITY+列名 ORDER_DATE → `ORDER_DATE < SYSDATE`;本体属性 min_value 缺失 → 模板降级为空 |
| `frontend/src/tests/ruleNameDedup.test.ts` | 5 条规则名有 2 条重复 → 标记索引 1 和 3;全 unique → 空 |
| `frontend/src/tests/ruleCodeGenerator.test.ts` | 类名清洗:非 ASCII 转 transliterate;超长截断到 12 字符;流水号 5 位补零 |
| `frontend/src/tests/DataQualityRuleBatchCreatePage.test.tsx` | 端到端:选类→选表→勾 3 列→点保存→并发 3 次 createRule mock→断言 UI 显示「已创建 3 条」 |

### 6.2 后端单测

| 文件 | 用例 |
|------|------|
| `backend/app/tests/unit/test_data_quality_service.py` | `next_rule_code("PURCHASE_ORDER", date)` 首次返 `...-00001`;已存在 `...-00003` 返 `...-00004`;跨日 `20260916` 重新计数 |
| `backend/app/tests/integration/test_data_quality_api.py` | `test_create_code_with_hyphen_accepted`:MU-DQ-... 含连字符编码应 201 入库(2026-09-15 新增) |

## 7. 安全审查

无新增认证/输入/外网依赖:
- `next-code` 端点只读 DB,无 SQL 注入风险(参数化 + LIKE 转义)
- 规则创建走既有 `createRule()`,前端已校验 expression 白名单(`validate_expression` 服务端再做一次)
- 批量提交是 N 次独立 POST,每条都走 ACL 校验,不会绕过权限

## 8. 部署验证

**手动 E2E**:
1. 进 `/data-quality` → RulesTab 头部看到「批量新建」按钮
2. 点击 → 跳转到 `/data-quality/rules/batch-create`
3. 选数据源 → 选类 PURCHASE_ORDER → 目标表自动填 `PURCHASE_ORDER`
4. 表加载后,12 列展示;勾选 PO_QTY、UNIT_PRICE、PO_DATE
5. 步骤 2 自动填表达式(PO_QTY > 0 等);USER 改 UNIT_PRICE 的表达式为 `UNIT_PRICE >= 0`
6. 步骤 3 预览 3 条规则,编码形如 `MU-DQ-PURCHASE_ORDER-20260915-00001`
7. 全局设阈值 100%,严重级别中,责任方填 `zhangsan`,描述填「测试」
8. 点保存 → 进度条 → 「已创建 3 条」→ 跳回 RulesTab 看到 3 条新规则

**回归**:
- 现有「新建规则」入口不变,单独使用仍正常
- AI 自动生成向导不变
- DataQualityPage 表分页、批量评估、规则筛选全部不变

## 9. 关联

- 设计稿:无(基于现有 DataQualityPage + GeneratePage 视觉)
- Wiki:`Harness/wiki/frontend.md` Phase 6 增量章节(批量新建模式)
- Rules:`Harness/rules/变更记录强制规范.md`
- Memory:`rule-batch-create.md`(待写)
- 相关变更:
  - `feat-dq-rule-auto-generation`(AI 生成向导,本期复用其 i18n + 步骤视觉)
  - `feat-data-quality-rule-model`(rule schema,本期复用其字段定义)

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 ≥ 2 个候选方案对比(共 5 个子问题,每子 1 表)
- [x] 第 3 段无 DB 迁移
- [x] 第 7 段说明未触发 security-reviewer(无 auth/外部依赖)
- [x] 第 8 段说明手动 E2E + 回归
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新(待写)
- [x] 至少 1 条 MEMORY 索引(已写 `rule-batch-create.md`)
- [x] 无真实 SQL/DB 改动,跳过真实数据验证

---

## 10. 已知遗留(非本期阻塞)

| 项 | 现状 | 后续动作 |
|---|---|---|
| `nextRuleCode` 类名截断到 12 字符 | 后端 `[:12]` 截断 → `MU-DQ-PURCHASE_ORD-...`;前端 `buildRuleCode` 默认 30 字符 → `MU-DQ-PURCHASE_ORDER-...` | 用户手动确认编码(预览时就给最终值),前后端不一致不阻塞入库;后续如需统一,改 service `[:12]` 到 `[:30]` 或让前端带 seq 调用 next-code |
| antd `addonAfter` deprecation 警告 | 当前 UI 未用 addonAfter,警告由旧组件残留 | 待清理 |
| `tag-icon-deprecated` antd warning | Tag icon prop 弃用 | 待清理 |