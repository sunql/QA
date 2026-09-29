# 变更：跨类属性引用校验补可操作 hint（属性归属 + schema 不存在）

- **日期**：2026-09-16
- **作者**：QA System
- **Phase**：bugfix（NL2SQL 计划校验）
- **状态**：done

## 1. 需求

用户报障：同一会话里问了 6 次的「近五个月供应商供货量最大」突然报
「无法生成通过校验的查询计划…选中的属性 供应商名称 不属于选定的任何类；
分组属性 供应商名称 不属于选定的任何类」。再问一次就又好了，时好时坏。

验收标准：

- 跨类属性引用（如「供应商名称」属于 Supplier/PurchaseOrder/PurchaseInvoice，
  但 selectedClasses 只选了 ReceiptDetail 等明细类）时，校验反馈必须**列出
  该属性在 schema 中实际归属的类**，引导 LLM 在下一次重试把对应类加入
  selectedClasses 并按 JOIN 目录关联
- 属性在本体 schema 中完全不存在（含别名/物理列口径）时，反馈必须如实说明，
  防止 LLM 在重试中继续幻觉同一属性名
- validatePlan 三个分支（selectedProperties / aggregations else / groupBy 兜底）
  同步修复，与排序属性、公式属性、时间粒度属性已修过的"可操作重试 hint"
  设计模式对齐
- 已有测试 + 新增 3 个测试守约，单测 + 关联下游测试全量通过、无回归

## 2. 设计评审

### 根因

1. **类召回无罪**：复现脚本打印出 27 类窗口含 Supplier/PurchaseOrder，
   schema 渲染正确（`backend/scripts/repro_supplier_top_supplier.py`）。
2. **低概率上下文相关 LLM 偏差**：失败轮紧跟「4月份有多少供应商下单」
   （PurchaseOrder COUNT）→ 有前轮状态 → 意图被判为 FOLLOW_UP（对非
   REFINE 词的追问一律 FOLLOW_UP）→ statePrompt 注入不相关的上轮计划，
   LLM 偶发写出「用 `供应商名称` 但 selectedClasses 只选 Receipt/ReceiptDetail」
   的计划（明细表只有供应商编码 BPSNUM_0，没有名称列）。
3. **重试反馈不可操作**：旧报错「不属于选定的任何类」仅说属性没在当前选
   中类里，没告诉 LLM 属性**在哪些类里**。重试两次 LLM 仍犯同错
   → `maxPlanAttempts=2` 耗尽 → `Nl2SqlError` → 整轮失败。这与
   validatePlan 其他分支（排序属性 2026-08-14、公式属性 2026-08-17、
   时间粒度词）已修过的"可操作 hint"模式完全一致，唯独这三个分支漏掉。

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 类召回窗口强制包含 Supplier | ❌ 不可行 | 「供货量」语境召回本身就不该带 Supplier（明细表更相关）；且强制拉宽会污染召回相关性排序 |
| (B) FOLLOW_UP 意图清洗/跳过上轮状态 | ❌ 风险大 | 影响正常追问场景；改意图分类属于另一个 issue |
| **(C) validatePlan 三分支补可操作 hint** | ✅ 推荐 | 与既有排序/公式/时间粒度 hint 同款；只动校验反馈文案，不动 schema、不动意图、不动召回 |

### 关键设计点

- `_propertyOwnerHint(prop, propsByClass)`：以 `propsByClass`（已
  经 `_classRefNames` 展开过的业务名 + 别名 + 物理列 + 表限定名 + 类限定名）
  为真源查归属类
- 有归属类 → 列出（截断到 `_OWNER_HINT_MAX_CLASSES=3`，避免 schema
  类多时提示过长挤占重试 token）并引导"加入 selectedClasses + JOIN
  目录关联后引用"
- 完全不存在 → 如实说明（"已比对全部类的业务名/别名/物理列"），防
  LLM 继续幻觉
- 三个分支中 groupBy 分支同时保留原有 `_timeBucketGroupHint` 兜底
  （粒度词优先命中粒度提示，命中不到才走归属提示）

### 复现方法论

纯新会话 6/6 复现不出；要复现须带 FOLLOW_UP 状态
（last_question + last_plan + last_sql + recent_rounds）。脚本
`backend/scripts/repro_supplier_top_supplier.py --followup` 提供
`--followup` 开关触发该路径；不带开关走 NEW_QUERY（多数情形即通过）。
烧 LLM 抓精确失败现场不划算，证据链闭合即可转修复。

## 3. 变更内容

### 3.1 代码（`backend/app/services/nl2sql_service.py`）

- 新增 `_propertyOwnerHint(prop, propsByClass) -> str`：返回归属
  类清单或"不存在"说明
- `validatePlan` 三分支追加 hint：
  - `selectedProperties`：`选中的属性 X 不属于选定的任何类；<hint>`
  - `aggregations` else 分支：`聚合属性 X 不属于选定的任何类；<hint>`
    （formula 分支保留 2026-08-14 的"property 应填真实属性 / 别名引用
    写在 formula 内" 风格，不重复插入，避免覆盖原有可操作指引）
  - `groupBy` 兜底：`f"分组属性 X 不属于选定的任何类" + (f"；{hint}"
    if hint else "")`，hint 优先取 `_timeBucketGroupHint`（粒度词命中），
    否则取 `_propertyOwnerHint`

### 3.2 测试（`backend/app/tests/unit/test_query_plan_validation.py`）

新增 3 个测试守约：

- `test_cross_class_property_hint_lists_owner_classes`：属性在 schema
  其他类里、selectedClasses 漏选 → 反馈含归属类名 + selectedClasses
  引导关键词；选中与分组两处都验证
- `test_property_nowhere_in_schema_says_so`：属性在本体 schema 中完
  全不存在 → 反馈如实说明含"本体 schema"字样
- `test_aggregation_cross_class_property_hint_lists_owner_classes`：
  聚合 property 跨类 → 反馈同样带归属类

## 4. 测试

| 套件 | 用例数 | 结果 |
|---|---|---|
| `test_query_plan_validation.py` | 48（含 3 新增） | ✅ |
| `test_nl2sql_service.py` + `test_query_plan_generation.py` | 161 | ✅ |
| `test_receiptdetail_qty_alias.py` + `test_chat_service.py` + `test_chat_multi_step.py` | 102 | ✅ |
| **合计** | **311** | ✅ |

## 5. 部署

`scripts/deploy_backend.sh` 部署并验证容器内 `_propertyOwnerHint` 出
现 4 次（def + 3 处调用）。`docker cp` 临时手段，容器重建前需
`docker compose build backend && docker compose up -d backend` 固化。

## 6. 沉淀

- wiki `Harness/wiki/nl2sql-engine.md` 追加"跨类属性引用可操作 hint"段落，
  与"派生指标 formula 必填""排序属性派生指标"段落并列，统一可操作反馈
  设计模式
- memory `qa-system-cross-class-property-hint` 沉淀根因链 + 复现方法论
  + "先查 prompt / 召回 / 重试反馈，别先怀疑模型或本体数据" 元教训
