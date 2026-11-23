# 值域建模：窄值域挂属性，宽值域建字典类

> 适用：物料主数据里 `TSICOD_0`（分类）、`PURFLG_0`（外购）、供货周期等
> 「码值 + 含义」型属性怎么接进本体，才能被 NL2SQL / Agent 正确使用。

## 一句话口径

**值域窄（几个值）→ 配在属性上；值域宽（几十上百、会增长）→ 建字典类 + join 边。**
两者都要求：**码本身在数据里，含义由人补** —— 平台不会凭空知道 `85` 是"标准件"。

## A. 窄值域：`ontology_property.allowed_values`

适合：外购/自制标志、状态、几个等级的枚举。

| 位置 | 内容 |
|---|---|
| `ontology_property.allowed_values` | 原始码列表，如 `["1", "2"]`（**只放码，不要塞 "1=外购"**，数据质量规则会拿它做 `IN (...)` 校验） |
| `ontology_property.description` | 码→义，如 `1=外购；2=自制` |

渲染：`backend/app/services/nl2sql_schema.py` 列行输出

```
    PURFLG_0: STRING 说明: 1=外购；2=自制 可选值: [1, 2]
```

- 硬上限：**30 条**、单值 24 字符（超限截断并标 `…`），防止 prompt 膨胀；
- 每个值都过 `_sanitizeSchemaField`（本体配置属外部输入，换行会注入裸指令行）。

> 历史：`allowed_values` 原先只被数据质量规则消费，**不进 NL2SQL prompt**，
> 导致模型看得到真实码值（值域示例）却不知道含义，只能反问用户要编码。
> 2026-11-23 补上渲染。

## B. 宽值域：字典类 + join 边

适合：物料分类（`TSICOD_0` 约 10 个一级码 + 45 个 `ITMCATEG` 品类码）、地区、组织等。

1. **字典表必须可读**：先用探针确认它在数据源只读账号覆盖范围内
   （实测：`ZJTH.ITMCATEG` 可读，`ZJTH.TSICOD` **不存在**）；
2. 建 `ontology_class`（`source_table` = 字典表），属性 = 码（PK）+ 名称；
3. 用 `ontology_join` 把业务类（如物料）的码列连到字典类的码列；
4. NL2SQL 会 join 出名称——`buildSchemaText` 的 `### JOIN 关系` 段是模型唯一线索，
   缺边就查不出名称。

## 两条路都要的第三件事：业务术语

码→名是字典能解决的；**业务口语 → 码** 不能：

```
「标准件」 → 哪几个 TCLCOD / TSICOD_0 ？
```

实测（2026-11-23，THBI 350,922 条物料）：名称含「标准件」的只有 **1 条**，
品类表 `ITMCATEG.TCLDES_0` **45 行全空** ⇒ 这个词不在主数据体系里。
必须由业务在 `term_dictionary` 维护映射（`term` → `mapped_class_name` / `mapped_property_name`），
否则工具层加 `statGroup` 参数也落不到任何码上。

## 工具面（Copilot Agent）不是自动获得

本体治理只影响 NL2SQL / 问答链路。Agent 工具是硬契约（`input_schema` +
`additionalProperties: false`）：`material_search` 不会因为你补了别名就有 `category` 参数。
要支持「按属性分析」，二选一：

- 给具体工具加参数（精准，但属性多了会爆炸）；
- 加一个受控的**本体分析工具**（本体驱动只读 SQL + SQL Guard + ACL）——推荐。

## 自检清单（接入一个新属性前）

- [ ] 业务给了「码 → 中文名」对照，且确认了业务口语映射（如「标准件」）
- [ ] 值域大小：≤ 30 且稳定 ⇒ A；否则 ⇒ B
- [ ] `property_alias` / `description` 已补（ITMMASTER 248 列当时 **0 个别名**）
- [ ] 若走 B：字典表在只读账号内、join 边已建、`_renderJoinLines` 能看到该边
- [ ] 若期望 Agent 能用：确认工具契约或分析工具已支持

## 相关

- 探针：`backend/scripts/probe_material_category.py`（只读，`--dump-categories` 导出品类码给业务标注）
- 渲染：`backend/app/services/nl2sql_schema.py`（`_formatAllowedValues`）
- 术语：`term_dictionary` 表 + 「业务配置 → 术语目录」
