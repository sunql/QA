# 治理缺口：供货量词条漂移 + Oracle 自然月模板 off-by-one

- 日期：2026-10-31
- 分支：`fix/term-dict-drift-date-template`
- 类型：fix（提示词模板 + 术语词典数据）
- 迁移版本：无（纯 prompt 文本与 term_dictionary 数据）

## 1. 症状与根因

### 1.1 「供货量」词条映射到已下线类（漂移）

生产 `term_dictionary.id = 5`（供货量）写作：

- `mapped_class_name = DWD_GOODS_RECEIPT_LINE`
- `mapped_property_name = RECEIVED_QTY`
- `formula_hint` 引用 `DWD_GOODS_RECEIPT_LINE.MATERIAL_CODE` / `.RECEIPT_DATE`

真实本体状态（2026-10-31 只读核对）：

| 类 | valid_to | 关键列 |
|---|---|---|
| `DWD_GOODS_RECEIPT_LINE` | 2026-09-19（已 tombstone） | 无 `INTER_COM_CODE` / `ITEM_CODE` / `RCV_QTY_PUU` |
| `DWD_GOODS_RECEIPT_DTL` | 存活 | `ITEM_CODE`、`RCV_DATE`、`RCV_QTY_PUU`、`INTER_COM_CODE`、`INTER_SITE_CODE` |

词条内容写于 LINE 被 tombstone 之前，之后没有随本体迁移更新。同一个问题
（如「3 月供货量 Top3 供应商」）会因召回/措辞不同落到不同表：已按 DTL 问出
一套结果，按词典提示又可能落到 LINE，产生第三套结果。`外部供应商贸易`
（id 6）已映射 DTL，只有 `供货量` 漂移。

### 1.2 Oracle 日期算术模板 off-by-one

`_DATE_ARITHMETIC_RULE_ORACLE` 的「最近 N 个月」示例是：

```
WHERE date_col >= ADD_MONTHS(TRUNC(CURRENT_DATE, 'MM'), -N)
```

按本月截断再回退 N 个月 ⇒ 实际覆盖 N+1 个自然月（含本月再往前数 N 个）。
LLM 直接照抄模板，任何「近 N 个月」都会多算一个月。

## 2. 修复

### 2.1 模板（已改）

`backend/app/services/nl2sql_dialects.py`：① 改为带上下界的自然月示例，
下界 `-(N-1)`、上界 `< 下月 1 号`，并给出 6 个自然月的完整写法（-5 → < +1）。
② 「近 N 天」保持原样（同类 off-by-one 家族，本次未改，见 §5）。

### 2.2 词条（代码侧）

`backend/app/tests/integration/test_term_dictionary_supply_qty.py` 夹具与生产对齐：
映射类改 `DWD_GOODS_RECEIPT_DTL`，属性改 `RCV_QTY_PUU`，JOIN/日期列改
`ITEM_CODE` / `RCV_DATE`；`外部供应商贸易` 夹具同步改 DTL；种子本体类改 DTL。
新增 `test_supply_qty_term_maps_to_live_dtl_class` 锁死映射类。

### 2.3 词条（生产数据，待执行）

```sql
UPDATE term_dictionary
   SET mapped_class_name   = 'DWD_GOODS_RECEIPT_DTL',
       mapped_property_name = 'RCV_QTY_PUU',
       formula_hint = '默认 JOIN DIM_IMATERIAL ON DIM_IMATERIAL.ITMREF_0 = DWD_GOODS_RECEIPT_DTL.ITEM_CODE WHERE DIM_IMATERIAL.TCLCOD_0 IN (''A02'',''A03'',''A04'',''A05'') AND DWD_GOODS_RECEIPT_DTL.RCV_DATE 在统计月份范围内,统计量用 SUM(DWD_GOODS_RECEIPT_DTL.RCV_QTY_PUU)。【例外】用户已明确指定供应商编码列表时,不应用 TCLCOD_0 过滤。'
 WHERE id = 5 AND term = '供货量';
```

全表核对：其余 3 条词条映射类均存活、属性列命中（`DIM_SUPPLIER.BPSNUM_0`、
`DIM_IMATERIAL.TCLCOD_0`、`DWD_GOODS_RECEIPT_DTL.INTER_COM_CODE`）。

## 3. 测试

- TDD：先加 `test_日期算术规则_Oracle_最近N个月_模板必须带上下界`（RED：旧模板命中
  断言），改模板后 GREEN。
- 先加 `test_supply_qty_term_maps_to_live_dtl_class`（RED：夹具仍指向 LINE），
  改夹具后 GREEN。
- 回归：`app/tests/prompts` + 四个术语词典套件 128 passed。
- 顺带修正 PR #43（L1 注入防御）后过期的断言：词典经 `wrapUntrusted` 后单引号
  转义为 `&#39;`；`ITMMASTER` 反向断言限定在词典段（演示本体类会进入本体召回段）。

### 基线说明

改动前同套件在本机为 4 failed / 4 passed：3 条是上述 `&#39;` 过期断言，
1 条是本地测试库残留演示本体（`ItemMaster`）+ embedding 不可用导致召回降级
（CI 的干净库不含演示数据）。与本批改动无关，已一并修正为可移植断言。

## 4. 部署与冒烟（待执行）

- backend 镜像重建（prompt 模板随镜像发布）；术语词条为纯数据，UPDATE 即生效。
- 冒烟：同一问题「3 月份供货量最多的三家供应商」确认计划 prompt 中
  `<term_dictionary>` 段映射为 `DWD_GOODS_RECEIPT_DTL` / `RCV_QTY_PUU`，
  且「近 6 个月」类问题 SQL 使用 -5 到 < 下月 1 号。

## 5. 未做 / 待议

1. **写入时校验缺口**：`TermDictionaryService.createTerm` 不校验
   mapped_class_name / mapped_property_name 是否存在于存活本体——这正是本次
   漂移能长期存在的原因。建议后续加「写入时校验 + 存活类约束」。
2. **「近 N 天」同族 off-by-one**：`>= CURRENT_DATE - N` 覆盖 N+1 天（含今天）。
   本次按用户范围未改。
3. **无实时词条审计**：可考虑把「词条映射是否存活」纳入真实数据门禁。
