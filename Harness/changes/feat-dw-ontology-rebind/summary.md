# 变更：feat-dw-ontology-rebind

- **日期**：2026-08-30
- **Phase**：数仓分层（THBI 建仓后的本体重建，NL2SQL 切换到 DWD 标准命名）
- **状态**：✅ implemented (2026-08-30，rebind + Neo4j + Milvus + schema_cache + NL2SQL 验证全过)

## 1. 需求

THBI 数仓建仓后，本体仍绑定 ZJTH X3 源表（大写列名）。本 change 把 **27 个本体类从 ZJTH X3 直连表 rebind 到 THBI 的 DWD/DIM 层标准表**（英文 snake_case 列名），使 NL2SQL 的 AI 查询跑在数仓标准命名上。

验收标准：
1. 27 个类 source_table 全部指向 DWD（25 类）/ ODS（2 类，关联键待业务确认）
2. 属性 source_column 全部翻译为 DWD 英文列；data_type 按 Oracle 类型映射
3. Join 按 DWD 列重绑，X3 列在 DWD 缺失的 join 删除
4. Metric formula 改写为 DWD 列
5. THBI 注册为默认数据源，ZJTH 降为非默认
6. Neo4j / Milvus / schema_cache 三处同步
7. NL2SQL 端到端生成 `THBI.DWD_*` SQL 并可执行返回数据

## 2. 设计评审

| 决策点 | 选择 | 理由 |
|---|---|---|
| 数据源默认 | **THBI 设为默认，ZJTH 保留非默认**（用户确认） | 前端 ChatPanel 自动选中 `isDefault` 数据源，NL2SQL `schemaPrefix = ds.username`，默认即切到 THBI |
| source_table 命名 | **裸 DWD 表名**（如 `DWD_MATERIAL`），不加 THBI. 前缀 | `nl2sql_service._bakeTableName` 会自动补 `THBI.` 前缀（schemaPrefix 已含），加前缀会重复 |
| 2 类价格表 | SupplierPriceList / SupplierPriceConf 保留 ODS（`ODS_PPRICFICH`/`ODS_PPRICCONF`） | 关联键 PLI_0 语义待业务确认（SSOT feat-dw-layering-thbi §9），暂不切 DWD |
| 属性删除 | DWD 精选 25 表瘦身后，旧 X3 列不在 DWD 的属性删除（连带 Neo4j 删除） | 避免 ontology 引用不存在列 |
| 新列命名 | DWD 新列需 `COLUMN_CN` 提供中文名，否则告警跳过 | 保证每条属性有可读中文名 |
| 同名冲突 | 新列沿用已删除属性名（如 需求日期: RETRCPDAT_0 → DEMRCPDAT_0 喂的 requested_receipt_date）需先 flush 删除再 INSERT | (class_id, property_name) 唯一约束；删旧建新须释放名 |

## 3. 数据模型变更（qa_metadata PG）

本体（ontology_class / ontology_property / ontology_join / ontology_metric）：

- **27 个类**：25 个 source_table 切到 `DWD_*`，2 个保留 `ODS_PPRICFICH` / `ODS_PPRICCONF`（价格表）
- **属性**：~313 个保留（source_column → DWD 英文列，data_type 按 Oracle 类型映射），**575 个删除**（X3 列不在 DWD 精选），**~25 个新建**（DWD 新列，含区分维度 zero_stock_flag / material_category）
- **Join**：55 个重绑保留（源/目标列翻译为 DWD 列，join_key 重算），31 个删除（列在 DWD 缺失）
- **Metric**：9 个 formula 全部改写为 DWD 列（`SUM(t.order_qty)` 等）

数据源（data_source）：

- 新增 **THBI-Oracle**（id=4）：oracle / 192.168.205.70:1521/X3V71ORA / username THBI / **is_default=True** / oracle_version 11g
- **ZJTH-Oracle**（id=2）is_default 降为 False（保留 is_active）
- 密码经 `encryptApiKey` 加密存储，明文只走 `THBI_PASSWORD` 环境变量

## 4. 接口契约变更

无 API/DTO 变更。行为变化：

- Chat 默认数据源自动选中 THBI → NL2SQL prompt 的 `schemaPrefix` 由 ZJTH 变为 **THBI**
- schema_cache 为 THBI 新 introspect（74 表 = 27 ODS + 25 DWD + 5 DIM + 5 DWS + 2 ADS，另有 10 张 Oracle 回收站 `BIN$` 残留表，仅存缓存不注入 prompt）

## 5. 实现要点

新增脚本（backend）：

- **`scripts/dwd_spec.py`**：纯函数 SQL 解析器，从 `dw/02_dwd_master.sql` + `03_dwd_facts.sql` 提取 `spec[table] = {col: {type, pk, x3}}`。X3 列解析取表达式**最后一个** X3 匹配（清洗规则 `CASE WHEN QTYUOM_0 > 1e9 THEN NULL ELSE LINAMT_0 END` 的守卫列是 QTYUOM_0，取值列是 LINAMT_0）
- **`scripts/rebind_ontology_thbi.py`**：编排脚本（幂等可重跑）
  - `CLASS_TABLE`：27 类 → DWD/ODS 表映射
  - `COLUMN_CN`：DWD 列 → 中文名（含区分维度、纠偏旧误译如 `payment_term_type ← BPTNUM_0`）
  - `_TYPE_MAP`：VARCHAR2→STRING / NUMBER→DECIMAL / INTEGER→INT / DATE→DATETIME
  - `_registerDataSource`：注册 THBI 默认数据源，密码只从 `THBI_PASSWORD` 读取（写死密码即报错）
  - `planProperties` / `planJoins`：纯函数规划（预览不写库）
  - `_syncNeo4j`：类/属性/指标节点 upsert + HAS_PROPERTY / REFERENCES / DERIVED_FROM 关系（best-effort，单点失败仅告警）
  - **同名冲突修复**：删旧属性后 `await session.flush()` 再建新属性，避免 (class_id, property_name) 唯一约束冲突
- 执行：`THBI_PASSWORD=*** DATABASE_URL=... ./.venv/bin/python scripts/rebind_ontology_thbi.py`

## 6. 测试

- `--dump-spec` / `--dry-run`：25 张 DWD 表全部解析，所有列有中文名（无 unmatched_new），55 join 保留 / 31 删除，9 metric 公式映射正确
- 实际执行后 PG 校验：
  - 27 类全部绑定 DWD/ODS；DWD 绑定类无残留 X3 大写列
  - 残留大写 source_column 恰好 37 = 两个 ODS 类属性数（身份保留，预期）
  - 55 join 全部翻译到 DWD 列，关键链路完整（PO→GR→INV→PAY、Requisition→PO、Quotation、ODS 价格表对）
  - 区分维度：`Supplier.零库存供应商标志→zero_stock_flag`、`ItemMaster.物料类别→material_category` ✓
- Neo4j 同步无报错
- Milvus：`backfill_milvus_embeddings.py --cleanup` 收敛 374 行（27 类 + 347 属性），无重复/缺失/陈旧
- schema_cache：`POST /datasources/4/introspect` 成功，74 表入库
- **NL2SQL 端到端**（真实 LLM deepseek-chat + THBI）：问「查询采购订单的总数量」→ `SELECT SUM(d.order_qty) FROM THBI.DWD_PURCHASE_ORDER_LINE d`，执行返回 1 行 `TOTAL_QTY=4373106735.863` ✓（THBI 前缀 + DWD 英文列 + 真实数据）

## 7. 安全审查

- **密码**：THBI 密码仅走 `THBI_PASSWORD` 环境变量，脚本 `_thbiPassword()` 缺失即抛错；不再有硬编码（初始版本曾有 `encryptApiKey("thbi123")` 已修正）
- **ZJTH**：降为非默认但保留 is_active（可手动切回）；密码未改动
- 只读约束不变：业务查询仍走 SQL Guard 只读校验
- 无新 API、无用户输入面；脚本为一次性 DDL/DML，无注入面

## 8. 部署验证

```bash
cd backend
# 1. 本体重建（幂等可重跑）
THBI_PASSWORD=*** DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  ./.venv/bin/python scripts/rebind_ontology_thbi.py

# 2. Milvus 重同步（PG 为真源）
DATABASE_URL=... ./.venv/bin/python scripts/backfill_milvus_embeddings.py --cleanup
# → Milvus ontology_embeddings 已收敛: 374 行 = 27 类 + 347 属性

# 3. schema_cache（THBI id=4）
curl -X POST http://localhost:8000/api/v1/datasources/4/introspect

# 4. NL2SQL 端到端
curl -X POST http://localhost:8000/api/v1/chat -H 'Content-Type: application/json' \
  -d '{"sessionId":"verify","question":"查询采购订单的总数量","datasourceId":4}'
# → SQL: SELECT SUM(d.order_qty) FROM THBI.DWD_PURCHASE_ORDER_LINE d
```

## 9. 已知限制与后续工作

| 事项 | 说明 | 处理 |
|---|---|---|
| 2 类价格表留 ODS | SupplierPriceList/SupplierPriceConf 关联键 PLI_0 语义待业务确认 | 确认后补 DWD 并再 rebind |
| **join 语义存疑** | `Supplier.payment_term_type == BusinessPartner.partner_code`（由旧 X3 `BPTNUM_0→BPRNUM_0` 翻译而来，DWD 把 BPTNUM_0 纠偏为付款条件）现读作「付款条件==伙伴编码」，语义可能错 | 待用户确认后删除或改绑；当前保留以忠实翻译 |
| BIN$ 回收站表 | schema_cache 含 10 张 Oracle 回收站残留表 | 不影响 NL2SQL（不注入 prompt）；后续可在 introspection 过滤 `BIN$` |
| 全量快照无增量 | 同 feat-dw-layering-thbi §9 | 后续 change 加 MERGE 增量 |
| 漂移校验 | 本体引用 DWD 表已全部存在于 schema_cache，无漂移告警 | — |

## 10. 关联

- 前置：`Harness/changes/feat-dw-layering-thbi/summary.md`（建仓 + 区分维度补充）
- 变更：本 change 落地后，feat-dw-layering-thbi §9「qa-system 未接入」已关闭
- 工具：`backend/scripts/dwd_spec.py`、`backend/scripts/rebind_ontology_thbi.py`
- Wiki：`Harness/wiki/data-model.md`（数仓分层）、`Harness/wiki/ontology.md`（本体结构）
