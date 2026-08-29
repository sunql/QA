# 变更：按 Excel `不需要=1` 列裁剪 ontology_property

- **日期**：2026-08-17
- **作者**：Claude (with user approval)
- **Phase**：本体治理
- **状态**：done

## 1. 需求

`docs/excel/*.xlsx` 新增一列 `不需要`，列值=1 表示该属性不在本体范围内。
需据此将系统中已有但被标注的属性从 PG / Neo4j / Milvus 三处一致删除，并保证
`seed_ontology.PROPERTIES` 不再回填这些属性（防止 reseed 后回潮）。

验收：删除后 PG ontology_property 总数 = Milvus ontology_embeddings property 数
= Neo4j Property 节点数；`verify_ontology_consistency.py` 不再报告 missing property。

## 2. 设计评审

候选路径：

| 方案 | 描述 | 取舍 |
|---|---|---|
| A. 逐条 `OntologyService.deleteProperty` | service 自带 PG cascade + Neo4j DETACH DELETE + Milvus best-effort delete | 简单；但 Milvus 增删在高负载下残留（见 memory [[milvus-bulk-delete-unreliable]]） |
| B. 直接 SQL DELETE + Neo4j Cypher + Milvus | 绕过 service | 失去 cascade 与事件一致性 |
| C. 删 + Milvus `--cleanup` 删集重建 | A 之后跑 `backfill_milvus_embeddings.py --cleanup`，PG 为唯一真源 | **选**：A 的 Neo4j 逐条删除 + B 不可逆的 Milvus 删集重建 |

最终：**A + C**。Neo4j 节点由 `deleteProperty` 内置的 `deleteNode` 逐条
`DETACH DELETE`，无残留风险；Milvus 由 `--cleanup` 删集重建兜底。

逐条 `deleteProperty` + commit 的设计遵循 memory [[autoflush-delete-ordering]]，
防止 autoflush=False 下批量删除按 DB 旧值误删刚改动的行（历史上曾丢 43 条）。

## 3. 数据模型变更

### PG

`ontology_property` 删除 236 行（按类分布）：

| source_table | 删除 | 删除前 | 删除后 |
|---|---|---|---|
| PORDER  | 31 | 116 | 85 |
| PORDERQ | 61 | 150 | 89 |
| PREQUISD| 51 | 96  | 45 |
| PRECEIPT| 33 | 101 | 68 |
| PRECEIPTD| 35 | 154 | 119 |
| YPRECEIPTD| 6 | 45 | 39 |
| PPRICLIST| 18 | 36 | 18 |
| PPRICFICH| 1 | 15 | 14 |
| **合计** | **236** | — | — |

未匹配的 2 个 Excel 列 `PLICRI4` / `PLICRI5` 在 DB 不存在，留作后续新增（已记录在
delete_plan.json `not_matched` 段，未本次处理）。

### Neo4j

- `deleteProperty` 内置 `deleteNode("Property", id)` 处理 236 个 Property 节点；
- 顺带清理了 45 个历史遗留 Property 孤儿 + 10 个历史遗留 Class 孤儿
  （ZZTEST_* / BPARTNER v1 / Customer v1 / PRECEIPT 旧版等 — 之前测试或版本切换残留）
- 最终 Neo4j：Class=21 Property=745 = PG 当前值

### Milvus

`backfill_milvus_embeddings.py --cleanup` 删集重建：21 类 + 745 属性 = 766 行，
无重复、无缺失、无陈旧。

### seed_ontology.py

PROPERTIES dict 同步移除 235 个 `P(...)` 调用（235/236 — 1 个 `PPRICLIST.组件`
原本就不在 seed，不动）。修改后 P() 总数 975 → 740。AST 节点删除，按行号倒序
处理；删除后语法校验通过，PROPERTIES 重新 import 验证 OK。

## 4. 接口契约变更

无（只动数据，不改 API）。

`DELETE /api/v1/ontology/properties/{id}` 内部行为不变；批量删除走脚本
`scripts/delete_marked_properties.py`。

## 5. 实现要点

### 新增脚本

- `backend/scripts/delete_marked_properties.py`
  读 `/tmp/delete_plan.json`，串行 `OntologyService.deleteProperty`，每条
  独立 commit。`--dry-run` 仅打印；非零失败 exit 2。

### 辅助步骤（一次性，本目录未保留）

- 用 raw XML 解析 xlsx 绕过 openpyxl 解析样式表的错误（8 个文件 stylesheet
  corrupt，只有 sheet2 的数据可读，sheet1 实际为空）
- AST 定位 `seed_ontology.PROPERTIES["<table>"][...]` 中的 P() 节点，
  按 lineno/end_lineno 倒序删除整段
- Neo4j 孤儿清理：`listNodesByLabel` + 比 PG ID 集 + `deleteNode`

### 关键决策点

- **不用 SQL DELETE 跳过 service**：service 内置的 Neo4j 同步 + best-effort
  Milvus 删除必须走，否则图谱会立即出现孤儿
- **Milvus 不逐条同步**：Phase A 跑完后 Milvus 残留/缺失由 `--cleanup` 统一兜底
- **Neo4j 孤儿顺带清**：用户要求"重新构建图、向量库信息"，历史 cruft 一起清

## 6. 测试

未新增自动化测试（一次性数据修复）。验证手段：

- `uv run python -c "...COUNT(*)..."` 三处数量一致：
  - PG class=21 property=745
  - Milvus ontology_embeddings=766 行（21 + 745）
  - Neo4j Class=21 Property=745
- `verify_ontology_consistency.py` 报告：
  - property_missing=0, property_mismatch=0
  - property_extra=5（pre-existing：seed 没列但 PG 有的属性，例如
    PPRICLIST.组件 — 之前就在，与本次删除无关）
  - join_extra=60 / join_mismatch=9（pre-existing：seed 的 BUSINESS_JOINS
    列表与 PG 不全一致，与本次删除无关）

## 7. 安全审查

未触发（无用户输入、无认证路径变更、无 schema 变更）。

## 8. 部署验证

- `seed_models.py` 仍可幂等导入（不动 llm_config）
- `seed_ontology.py` 重新 import 通过、AST 校验通过
- Neo4j / Milvus / PG 三处总数对齐

## 9. 关联

- 原始素材：`docs/excel/*.xlsx`（10 个文件，新增 `不需要` 列）
- 旧对比：`docs/excel-old/`（无 `不需要` 列）
- 脚本：`backend/scripts/delete_marked_properties.py`
- 输出：`/tmp/delete_plan.json`（236 matched + 2 not_matched）
- 记忆：[[milvus-bulk-delete-unreliable]] [[autoflush-delete-ordering]]
