# 执行清单 — 把语义关系落到库里 + 入图

> 这些文件是**真正待执行**的输入。方式：系统的「批量关系引擎」（本体 → 批量关系 弹窗，或下方 API）。
> 用 `skip`（当前语义层仅 1 条，本清单 45 条全部新建，无冲突）。写库 + Neo4j 入图同一次执行完成。

## 待执行文件

| 文件 | 内容 | 在引擎里怎么用 |
|---|---|---|
| `relations.csv` | 45 条语义关系（推荐通道，按类名解析） | CSV 清单导入，kind = **语义关系** |
| `manifest.example.json` | 同 45 条（JSON，按真实 id） | 弹窗切「JSON」直接粘贴 |

> `joins.csv`（物理关联 279）**不在本目录**：库里已全部存在，无需执行；只留作还原/审计
> （`docs/batch-relation-engine/joins.csv`）。除非目标是空库重建，否则不要用它。

## 图形前置状态（已核实，2026-09-09）

- PG：67 类 / 3629 属性 / 279 join / **1 语义关系**
- Neo4j：Class 67 + Property 3629 节点已全量存在，`HAS_PROPERTY` 3629 / `JOIN` 200 / `SUPPLIES` 1

→ 节点已在图里，**无需先跑「本体入图」**。若在全新/空图环境，先执行一次 `syncGraph` 再应用本清单。

## 执行步骤（引擎弹窗）

1. 打开「本体 → **批量关系**」。
2. 勾选 **「应用关系清单」**（仅此项即可；节点已在图，`本体入图` 可留空）。
3. 清单来源切 **CSV**，类型选 **语义关系** → 上传 `to-apply/relations.csv`。解析应显示 **45 条、0 行错误**。
4. 冲突策略选 **跳过（skip）**。
5. **预览** → 应显示 relations 新建 45 / 跳过 0 / 覆盖 0。
6. **执行** → 成功后 toast 显示新建 45。库 + 图同时写入（PG 为准，Neo4j 失败不阻断）。

（也可切 JSON 粘贴 `manifest.example.json` 内容，效果相同。）

## 预期结果

- PG `ontology_relation`：1 → **46**
- Neo4j：新增 45 条关系边，按类型 = `CONTAINS` 10 + `GENERATES` 9 + `GENERATED` 12 + `SUPPLIES` 1 + `INSPECTED_BY` 1 + `RELATED_TO` 12
- Audit：45 条 `ONTOLOGY_RELATION` CREATE 记录

## API 直连（可选，等价）

```bash
# 1) 解析（只读）得到 manifest
curl -s -X POST "http://localhost:8000/api/v1/ontology/batch/parse-csv" \
  -F "kind=relations" -F "file=@docs/batch-relation-engine/to-apply/relations.csv"

# 2) 执行（把 manifest 原样放回请求）
curl -s -X POST "http://localhost:8000/api/v1/ontology/batch" \
  -H "Content-Type: application/json" \
  -d '{"syncGraph":false,"inferJoins":false,"applyManifest":true,"onConflict":"skip","manifest":{...上一步返回的 manifest...}}'
```

> 幂等：本清单重复执行在 `skip` 下全部计入 `skipped`、不产生重复。
> 任何一条方向/归属要改：编辑 `relations.csv` 那行 → 重新上传，只影响该行。
> SSOT：推导规则见 `Harness/changes/feat-batch-relation-engine/summary.md`。
