# 变更：feat-dw-ontology-rebind

- **日期**：2026-08-30
- **Phase**：数仓分层（THBI 建仓后的本体重建，NL2SQL 切换到 DWD 标准命名）
- **状态**：✅ implemented (2026-08-30，rebind + Neo4j + Milvus + schema_cache + NL2SQL 验证全过)

## 1. 需求

THBI 数仓建仓后，本体仍绑定 ZJTH X3 源表（大写列名）。本 change 把 **27 个本体类从 ZJTH X3 直连表 rebind 到 THBI 的 DWD/DIM 层标准表**（英文 snake_case 列名），使 NL2SQL 的 AI 查询跑在数仓标准命名上。

验收标准：
1. 27 个类 source_table 全部指向 DWD（2026-08-30 补齐后 27/27；初期 25 DWD + 2 ODS）
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
| 2 类价格表 | SupplierPriceList → `DWD_SUPPLIER_PRICE_LIST_HEADER`、SupplierPriceConf → `DWD_SUPPLIER_PRICE_LIST_CONFIG`（明细补版本键） | 初期暂留 ODS 等关联键确认；2026-08-30 真实数据确认 (PLI_0, PLICRD_0) / PLI_0 后补 DWD（见 §9） |
| 属性删除 | DWD 精选 25 表瘦身后，旧 X3 列不在 DWD 的属性删除（连带 Neo4j 删除） | 避免 ontology 引用不存在列 |
| 新列命名 | DWD 新列需 `COLUMN_CN` 提供中文名，否则告警跳过 | 保证每条属性有可读中文名 |
| 同名冲突 | 新列沿用已删除属性名（如 需求日期: RETRCPDAT_0 → DEMRCPDAT_0 喂的 requested_receipt_date）需先 flush 删除再 INSERT | (class_id, property_name) 唯一约束；删旧建新须释放名 |

## 3. 数据模型变更（qa_metadata PG）

本体（ontology_class / ontology_property / ontology_join / ontology_metric）：

- **27 个类**：全部切到 `DWD_*`（初期 25 DWD + 2 ODS，2026-08-30 价格表补齐后 27/27）
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
- **价格表 NL2SQL**（2026-08-30 补 DWD 后）：问「T10 价目表里单价最高的价格」→ `SELECT MAX(d.unit_price) FROM THBI.DWD_SUPPLIER_PRICE_LIST d WHERE d.price_list_code='T10'` → 40251.73 ✓；问「T11 单价>100 的记录数」→ `COUNT(price_list_line)` 筛选 `price_list_code='T11' AND unit_price>100` → 3327 ✓（新增 price_list_code 列查询生效）

## 7. 安全审查

- **密码**：THBI 密码仅走 `THBI_PASSWORD` 环境变量，脚本 `_thbiPassword()` 缺失即抛错；不再有硬编码（初始版本曾有 `encryptApiKey("<密码>")` 硬编码已修正）
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
| 2 类价格表留 ODS | SupplierPriceList/SupplierPriceConf 关联键 PLI_0 语义待业务确认 | ✅ 已补 DWD（2026-08-30，见下） |
| ~~join 语义存疑~~ | ~~`Supplier.payment_term_type == BusinessPartner.partner_code`~~ | ✅ 已删除（2026-08-30 用户确认：付款条件≠伙伴编码，joins 55→54） |
| BIN$ 回收站表 | schema_cache 含 10 张 Oracle 回收站残留表 | 不影响 NL2SQL（不注入 prompt）；DWD 重建 DROP 后回收站自清 3 张，剩 7 张；后续可在 introspection 过滤 `BIN$` |
| 全量快照无增量 | 同 feat-dw-layering-thbi §9 | 后续 change 加 MERGE 增量 |
| 漂移校验 | 本体引用 DWD 表已全部存在于 schema_cache，无漂移告警 | — |

### 价格表 DWD 补齐（2026-08-30，joins 54→56）

**关联键确认（THBI ODS 真实数据）**：

- **表头↔明细**：`(PLI_0, PLICRD_0)` 版本键。明细 17,755 版本键 / 表头 17,749 命中（99.97%），5 个孤儿（表头版本被删、明细保留，共 54 行）
- **配置↔明细 / 配置↔表头**：`PLI_0` 价目表号。配置 6 行（T10/T11/T20/T21 + T30/T31 无表头无明细，疑似模板价目表）
- 表头 21 行 PLI_0 空白垃圾版本已在 DWD 层过滤

**DWD 三件套**（`dw/02_dwd_master.sql`）：

- `DWD_SUPPLIER_PRICE_LIST`（明细）：**补 price_list_code/price_list_record 两键列**，PK (price_list_code, price_list_record, price_list_line)
- `DWD_SUPPLIER_PRICE_LIST_HEADER`（新，←ODS_PPRICFICH）：版本主数据 10 列，PK (price_list_code, price_list_record)
- `DWD_SUPPLIER_PRICE_LIST_CONFIG`（新，←ODS_PPRICCONF）：取价配置精选 22 列，PK price_list_code

**本体 rebind**：SupplierPriceList→HEADER（保留 10 / 删 4 审计冗余列），SupplierPriceConf→CONFIG（保留 22 / 删 1 脏属性「没有用处」同列重复映射），SupplierPriceDetail 新建 2 属性（价格表号/价格表记录）；新增 2 join（明细↔表头版本键、配置↔明细）

**验证**：NL2SQL `SELECT MAX(d.unit_price) FROM THBI.DWD_SUPPLIER_PRICE_LIST d WHERE d.price_list_code='T10'` → 40251.73 ✓；`COUNT(...) WHERE price_list_code='T11' AND unit_price>100` → 3327 ✓；Milvus 收敛 371 = 27 类 + 344 属性；schema_cache 66 业务表 + 7 BIN$

**数据事实（不影响本次改造，供后续）**：价格明细 `material_code`（CPNITMREF_0）全空——X3 价格明细用条件列定位，不走物料直连，join 48（明细↔ItemMaster）产不出数据

## 10. 关联

- 前置：`Harness/changes/feat-dw-layering-thbi/summary.md`（建仓 + 区分维度补充）
- 变更：本 change 落地后，feat-dw-layering-thbi §9「qa-system 未接入」已关闭
- 工具：`backend/scripts/dwd_spec.py`、`backend/scripts/rebind_ontology_thbi.py`
- Wiki：`Harness/wiki/data-model.md`（数仓分层）、`Harness/wiki/ontology.md`（本体结构）
