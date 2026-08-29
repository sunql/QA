---
created: 2026-08-14
updated: 2026-08-14
source: 用户需求「增加表 PPRICCONF 这个类和对应的属性」+ Oracle ZJTH 实测
tags: [qa-system, 本体, seed, milvus, 运维]
---

# 新增 PPRICCONF（供应商价格配置）本体类与属性

## 背景

用户需求：在 qa-system 本体中新增业务表 `PPRICCONF` 的类与属性，使其可被 NL2SQL
语义检索与选表覆盖。

`PPRICCONF` 为供应商价格配置表，定义价格清单的取价条件维度（供应商/物料等字段组合）、
取价优先级与价格类型，按 `PLI_0` 价格表号与 `PPRICFICH`（价格单）、`PPRICLIST`（价格明细）关联。

## 处置

1. **Oracle 内察**（`scripts/diag_ppricconf_schema.py`）：ZJTH.PPRICCONF 共 102 列、
   无声明主键、6 行（PLI_0=T10/T11/T20/T21/T30/T31）；`PPRICFICH`/`PPRICLIST` 各命中
   4 个 PLI_0，关联真实。抽样行确认条件维度实际填充：`ABB_0=BPS / FIL_0=BPSUPPLIER /
   FLD_0=BPSNUM / CRIDES_0=供应商`、`ABB_1=ITM / FIL_1=ITMMASTER / FLD_1=ITMREF /
   CRIDES_1=产品`、`LANDESSHO_0=CHI~含税价`。

2. **seed_ontology.py** 新增（幂等，可重复运行）：
   - CLASSES：`SupplierPriceConf`（alias=供应商价格配置，source_table=PPRICCONF，说明如上）。
   - PROPERTIES["PPRICCONF"]：22 个建模属性（价格表号 PLI_0 pk、价格清单说明 LANDESSHO_0、
     价格清单状态 PLISTC_0、取价优先级 PIO_0、启用标志 PLIENAFLG_0、价格清单类型 PLITYP_0、
     价格类型 PRITYP_0、价格字段 PRIFLD_0、货币 CUR_0、条件数量 CRINBR_0、条件1~简称/表/字段/描述
     ABB_0/FIL_0/FLD_0/CRIDES_0、货币/单位/物料/供应商条件数 CRICURNUM_0/CRIUOMNUM_0/
     CRIITMNUM_0/CRIBPSNUM_0、创建人/日期 CREUSR_0/CREDAT_0、更新人/日期 UPDUSR_0/UPDDAT_0）。
   - BUSINESS_JOINS：`PPRICCONF.PLI_0 → PPRICFICH.PLI_0`、`PPRICCONF.PLI_0 → PPRICLIST.PLI_0`。

3. **元数据清理**（`scripts/apply_ppricconf_metadata.py`，一次性）：seed 按 source_table
   复用了既有 id=30 类（历史 raw 名/别名 `PPRICCONF`、误导性说明「含税价格说明」、残留 1 条
   原始 `PLI_0 <- PLI_0` 重复属性）。经 `updateClass` 原地修正为友好名/别名/说明（id 稳定，
   Neo4j 按原 id 重同步），经 `deleteProperty` 删除残留重复属性（PG+Neo4j+Milvus 同步清理）。

4. **Milvus 向量**（`scripts/backfill_milvus_embeddings.py` 扩展）：脚本从「仅回填类」扩展到
   「类+属性」，新增 `--sources A,B` 过滤、`--skip-classes`、失败非零退出码。对 PPRICCONF
   生成了 1 类 + 22 属性 = 23 条向量（类文本 = 类名+别名+说明；属性文本 = 中文名+业务别名+
   说明，dim=1024）。随后全量属性回填（512 条属性，--skip-classes）。

5. **系统性 bug 修复：类/属性 id 碰撞导致 Milvus 向量互删**（回填暴露）
   - 根因：`ontology_class` 与 `ontology_property` 共用 id 序列，Milvus `ontology_id`
     非跨类型唯一（类 id=10 与 ItemMaster 属性「物料类型代码」id=10 并存）。原
     `deleteByOntologyId(oid)` 只按 ontology_id 删、不分 type，重同步某属性会把同 id 的
     类向量误删——本次全量属性回填触发，8 个类向量丢失、13 条属性向量重复。
   - 修复：`deleteByOntologyId(oid, type)` 删除表达式带 type 作用域（非法 type 抛
     ValueError）；`syncEmbedding`/`deleteClass`/`deleteProperty`/`deleteMetric` 及
     `collapse_ontology_versions.py` 全部传 type。运行时编辑碰撞属性不再误删类向量。
   - 回归：`TestDeleteByOntologyId`（expr 作用域、同 oid 类/属性不互删、非法 type 拒绝）+
     `TestSyncEmbeddingTypeScope`（透传 type）。全量 820 测试通过。

6. **Milvus 数据确定性收敛**（修复后全量回填仍暴露重复）
   - 现象：修复 id 碰撞后全量重跑报「532 成功 / 0 失败」，但 Milvus 实为 835 行 /
     299 重复；手动单条 `deleteByOntologyId` 可靠（删后 0 行）、PPRICCONF 小批量
     (23 同步) 干净——**Milvus 批量 delete-then-insert 在重负载下不可靠**（大量
     delete+flush+insert 混跑时 tombstone/分段封存时序导致重复行累积）。
   - 处置：放弃逐条增量删除，改确定性重建——`backfill_milvus_embeddings.py` 新增
     `--cleanup`：读全量 → 按 (ontology_id, type) 去重（保留 auto_id 最新）→ 对
     PG 有而 Milvus 无的实体补生成向量（本次缺失 oid=1「物料编号」，实证删除测试
     手删）→ 删集（`milvus_client.dropCollection`，仅本体集合，绝不触碰
     query_embeddings）→ 重建空集合 → 一次整批插入去重行 → 校验无缺失/无陈旧。
   - 新增 `milvus_client.listAllEmbeddings`（全量读取，query limit 上限 16384）。
   - 最终状态（诊断复核）：**532 = 20 类 + 512 属性，无重复、无缺失、无陈旧**。
   - 回归：`TestListAllEmbeddings` + `TestDropCollection`。

## 验证

- seed 重跑幂等：`properties: created=22 skipped=490`、`joins: created=2 skipped=40`。
- 清理脚本校验：`name=SupplierPriceConf alias=供应商价格配置 properties=22`。
- 修复后确定性收敛：20 类 + 512 属性 = 532 条向量，无重复、无缺失（class 20、property 512）。

## 生效说明

本体类/属性/向量从 DB 读取，新增即时生效，**无需重启** uvicorn 8000（--reload 已加载）。
`seed_ontology.py` 的代码改动仅影响未来重跑。
