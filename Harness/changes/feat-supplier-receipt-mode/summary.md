# 变更：供应商收货模式 YPTHFLGM_0 本体补录

- **日期**：2026-08-20
- **作者**：Claude (with user direction)
- **Phase**：本体治理
- **状态**：done

## 1. 需求

BPSUPPLIER（供应商主档）缺少 `YPTHFLGM_0`（收货模式）字段，导致：

1. 订单完成率无法按「零库存 / 非零库存」分组统计，混算结果失真。
2. NL2SQL 召回相关属性时无法命中列语义（「零库存」「收货模式」是用户经常使用的关键词）。
3. 知识库缺少「两条收货链路差异」的业务描述，后续维护易遗忘。

业务口径：

- `YPTHFLGM_0 = 1`：非零库存供应商，先建到货单 → 推质检 → 输入合格数量 → 建收货单入库。
- `YPTHFLGM_0 = 2`：零库存供应商，采购订单直接驱动收货单，无 YPRECEIPT，到车间后由领用系统自动入库出库。

验收：

- PG / Neo4j / Milvus 三处本体均含 `Supplier.收货模式 (YPTHFLGM_0, INT)`。
- Wiki 新增两条业务规则（`business-domain.md` 摘要 + `supplier-receipt-workflow.md` 详情）。
- orders completion rate 类查询必须按 `YPTHFLGM_0` 分组（Prompt 自动注入）。

## 2. 设计评审

候选路径：

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 在 seed_ontology.py 直接追加 `P(...)` 后跑 seed | 一致走 PG + Neo4j 同步路径（已有），Milvus 用 `--sources BPSUPPLIER` 增量补 | **选**：复用既有同步链路，不绕开 ontology_service。 |
| B. 写专用 migration alembic + 独立 service sync 脚本 | 显式但重复，已经有 seed 路径 | 重复且易漂移。 |
| C. 推迟到下个 Phase 治理迭代 | 业务先抢 | 拒：用户口径明确，本轮就要可被 NL2SQL 召回。 |

最终：**A**。

字段命名选择：「收货模式」做中文名（本意即收料 / 到货方式的语义大类），
`零库存标志 / 到货模式 / 收货管理方式` 做业务别名覆盖口语化问法。`desc` 写完整
取值说明 + 关键业务约束（订单完成率分组），确保 LLM 看到 schema 上下文即理解。

## 3. 数据模型变更

### 本体（seed_ontology.py）

- 表 `BPSUPPLIER` 新增属性（第 4 位，紧跟「供应商类型」）：
  - 中文：`收货模式`
  - 别名：`YPTHFLGM_0`
  - 类型：`INT`
  - `business_aliases`: `["零库存标志", "到货模式", "收货管理方式"]`
  - `description`：列出 1/2 取值与对应流程差异 + 「订单完成率须按此分组」强约束。

### PostgreSQL（ontology_property）

| id | class_id | property_name | property_alias | data_type | business_aliases |
|----|----------|---------------|----------------|-----------|------------------|
| 1036 | 4 (Supplier) | 收货模式 | YPTHFLGM_0 | INT | ["零库存标志","到货模式","收货管理方式"] |

无迁移脚本体改动：seed_ontology 走 ORM upsert 路径，幂等可重放。

### Neo4j

新增 Property 节点 pid=1036，HAS_PROPERTY 连接到 BPSUPPLIER 类的 Class 节点。

### Milvus

`ontology_embeddings` 新增 prop id=1036 向量（dim=1024），构造文本：
`"收货模式 零库存标志 到货模式 收货管理方式 供应商收货模式标志：1=非零库存供应商... 2=零库存供应商..."`。

## 4. 接口契约变更

无 API 变化。本体新增属性能立即被 `selectRelevantClasses` 命中、被 NL2SQL Prompt 注入。

## 5. 实现要点

关键文件：

- `backend/seed_ontology.py` - BPSUPPLIER 段（第 291 行起）插入新 `P(...)`。
- `Harness/wiki/business-domain.md` - 新增「供应商域」一节，含取值表 + 两个流程图 + 路由要点。
- `Harness/wiki/supplier-receipt-workflow.md` - 新文件，详尽记录两种模式的差异、订单完成率口径、NL2SQL 注意。
- `Harness/changes/feat-supplier-receipt-mode/summary.md` - 本文件（SSOT）。

关键命令（执行顺序）：

```bash
cd qa-system/backend
uv run python seed_ontology.py                                        # PG + Neo4j upsert
uv run python scripts/backfill_milvus_embeddings.py --sources BPSUPPLIER  # Milvus 增量补
```

## 6. 测试

- 手动验证 PG：通过 SQLAlchemy select 命中 `property_alias = YPTHFLGM_0`，id=1036。
- 手动验证 Neo4j：Cypher `MATCH (c:Class {sourceTable:"BPSUPPLIER"})-[:HAS_PROPERTY]->(p:Property {alias:"YPTHFLGM_0"})` 返回 1 行，pid=1036。
- 手动验证 Milvus：`backfill_milvus_embeddings.py --sources BPSUPPLIER` 输出含 `ok prop id=1036 Supplier.收货模式 dim=1024`。

未补 e2e：本次为本体增量，未触发新代码路径；下次涉及订单完成率 SQL 时再补端到端断言（确保 Prompt 注入分组约束）。

## 7. 安全审查

未涉及。改动仅本体元数据 + 静态文档，无运行时 SQL 注入、密钥、鉴权等敏感面。

## 8. 部署验证

dev：seed 已运行，输出 `properties: created=1 skipped=740`，新属性入 PG；Neo4j
同步完成，无相关 ERROR；Milvus 回填 24 / 0，命中 1036。三处一致 → 部署完成。

## 9. 关联

- 业务文档：`docs/20260814-PPRICCONF-ontology.md`（本体治理范式参考）
- Wiki：`Harness/wiki/business-domain.md`、`Harness/wiki/supplier-receipt-workflow.md`、`Harness/wiki/data-model.md`
- 规则：`Harness/rules/数据与AI治理规范.md`（本体版本与同步策略）
- 记忆：`memory/MEMORY.md` 新增「BPSUPPLIER.YPTHFLGM_0 收货模式分组语义」条目
