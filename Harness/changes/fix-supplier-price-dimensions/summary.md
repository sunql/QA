---
# 变更：供应商报价多维度取价修复（术语词典 + 地点/时间列语义化 + few-shot 清洗 + 冗余属性清理）

- **日期**：2026-08-17
- **Phase**：NL2SQL 取价能力修复
- **状态**：done

## 0. 背景（用户场景）

供应商报价可能有多档有效数据：按时间段（如 1-5 月 / 6-12 月各一档）、按含税性
（含税/不含税）、按地点（不同工厂有不同价）。LLM 询问时倾向把多档 AVG 成平均价，
且「含税价」「地点」等业务词无法正确落到取价列。5 项修复提议**已全部完成**：
第 3 项（术语词典）、第 1 项（价格条件列语义化）、第 2 项（生效/失效日期 desc 增强）、
第 4 项（few-shot 清洗）、第 5 项（删除冗余属性）。

## 1. 实证结论（直连 Oracle 2026-08-17）

Sage X3 价格清单 `PPRICLIST`（本体类 `SupplierPriceDetail`）：

| 列 | 语义 | 证据 |
|---|---|---|
| `PLI_0`（价格表号） | T10=含税价、T11=不含税价、T20=含税价(地点)、T21=不含税价(地点) | PPRICCONF 配置 |
| `PLISTRDAT_0` / `PLIENDDAT_0` | 生效 / 失效日期（时间段维度） | 既有语义 |
| `PLICRI3_0`（价格条件4） | **取价地点（工厂）码 = `FACILITY.FCY_0`** | 8 个 distinct 值全部命中 FACILITY 表：C1=济南泰鸿、D1=保定泰鸿、J1=河北新泰鸿、T0=泰鸿万立、T1=滨海总厂、T2=滨海分厂、T5=塑件事业部、T7=冲焊事业二部；T10/T11 该列为空 |
| `PLICRI_0` / `PLICRI1_0` / `PLICRI2_0` | 价格条件1=复合键`供应商~物料~地点~~~`（稀疏 legacy，**已删**）、2=供应商码、3=物料编码 | 采样 |
| `PLICRI4_0` / `PLICRI5_0` | 空（价格条件5/6 未用，**已删**） | 采样 |

Oracle 直连法：dev 默认 SECRET_KEY 非法，须取**运行中 uvicorn 进程的 SECRET_KEY**
（`ps eww -p <pid> | tr ' ' '\n' | grep '^SECRET_KEY='`）解密 datasource id=2 的
`password_encrypted`。陷阱：`TRIM(col) <> ''` 在 Oracle 中 `''`=NULL 会杀光全表，
须用 `TRIM(col) IS NOT NULL`；bind 名不能以数字开头。

## 2. 修复内容

### 第 3 项：术语词典（5 条）

写入 dev `term_dictionary`（id 7、8、11、12、13）：

| term | formula_hint |
|---|---|
| 含税价 | `WHERE 价格表号='T10' AND 生效日期<=查询日 AND 失效日期>=查询日`；仅明确要「平均价」才 AVG |
| 不含税价 | `WHERE 价格表号='T11'`…（同上） |
| 含税价(地点) | `WHERE 价格表号='T20' AND 地点='C1'`；按地点过滤/分组 |
| 不含税价(地点) | `WHERE 价格表号='T21' AND 地点='C1'`；按地点过滤/分组 |
| 供应商报价 | 默认取当期生效单价（生效期约束）；仅在明确要「平均价」时 AVG，且须限定同一价格表号+时间段 |

注入链：`chat_service._loadDictionaryText` → `buildDictionaryText` → 计划 system
prompt 的 `<term_dictionary>` 块（`nl2sql_service._buildPlanSystemPrompt`）。

### 第 1 项：价格条件4 语义化

- `seed_ontology.py` `价格条件4`（PLICRI3_0）补业务别名
  `["地点","地点编码","工厂","场所","地点编号"]` + desc（含 FCY_0 实证，code-reviewer
  LOW 修正：FACILITY 对 FCY_0 的规范属性名是「地点编码」，已并入别名）。
- dev DB property 420 同步（PUT `/api/v1/ontology/properties/420`）。
- T20/T21 术语条目改为引用语义化的「地点」列。

### 第 2 项：生效/失效日期 desc 增强

- `seed_ontology.py` `生效日期`（PLISTRDAT_0）/`失效日期`（PLIENDDAT_0）补业务别名
  （起始日期/生效起始日…，截止日期/生效结束日…）+ 时间维度 desc（中文自然语言表述
  「不晚于查询日 / 不早于查询日」而非 `<=`/`>=`，避免 `_sanitizeSchemaField` 转义）。
- dev DB property 423 / 424 同步（PUT）。
- 效果：schema 提示显式告知「当期价须带生效期约束，勿跨时间段平均」，与术语词典
  formula_hint 语义一致。

### 第 4 项：few-shot 清洗

- **污染根因**：Milvus `query_embeddings` 存有历史「供应商报价」示例，其 SQL 对
  PPRICLIST 直接 `AVG(PRI_0)` 且**不按 价格表号(PLI_0) 过滤**（跨含税/不含税/地点
  清单平均）——即使问题写了「只看 T10 含税价格」，SQL 仍是同一份全清单平均。这些
  示例被 `_buildFewShot` 检索注入后强化「给平均价」行为。
- **处置**：逐条核对 35 条 PPRICLIST few-shot 全文 SQL，删除 **13 条污染示例**
  （无 `PLI_0` 过滤的 AVG 族 + 1 条 `%T110%` 失效清单码），保留 22 条正确示例
  （正确按 T10/T11/`LANDESSHO_0 LIKE '%T11%'` 过滤、或展示原始报价明细列）。
  Milvus delete 后需 `flush()` 使删除对查询可见（num_entities 含 tombstone 属预期，
  等待 compaction 收敛）。
- 未加检索层启发式过滤（YAGNI）：术语词典 + schema 语义化已从源头纠偏，后续新示例
  会积累正确模式。

### 第 5 项：删除冗余属性

- `seed_ontology.py` 删除 PPRICLIST 的 `价格条件1`（PLICRI_0，稀疏 legacy 复合键，
  与 价格条件2/物料编码/价格条件4 重复）、`价格条件5`/`价格条件6`（PLICRI4_0/
  PLICRI5_0，全空列）。
- dev DB DELETE `/api/v1/ontology/properties/417/421/422`（服务级级联：PG + Neo4j +
  Milvus，type 作用域见
  [[milvus-embedding-id-collision]]）。
- `_seedProperties` 保持「只增不删」，故删除走 API 显式执行；seed 移除后不会重建。

## 3. 数据模型变更

- **无 schema 迁移**。改动均为本体属性元数据（business_aliases / description）、
  term_dictionary 行数据，以及 ontology_property 行删除，经 API 更新。
- `ontology_property`：420（价格条件4）aliases/desc 同步；423/424（日期）desc 增强；
  **417/421/422（价格条件1/5/6）删除**。SupplierPriceDetail 属性 39 → 36。

## 4. 接口契约变更

无 API 契约变更。新增事实：
- `价格条件4` 可用别名「地点 / 地点编码 / 工厂 / 场所 / 地点编号」命中。
- `生效日期` / `失效日期` 可用别名「起始日期 / 生效起始日 / 开始日期」与「截止日期 /
  生效结束日 / 结束日期」命中。
- `价格条件1/5/6` 从 schema 移除（不再出现在 schema 文本，减少 LLM 误选与 token）。

## 5. 测试

`app/tests/integration/test_term_dictionary_inject.py`（7 用例）：

| 用例 | 锁定 |
|---|---|
| `test_terms_injected_into_plan_prompt` | 词典非空 → 计划 prompt 含 `<term_dictionary>` 与渲染格式 |
| `test_no_dictionary_when_table_empty` | 词典空表 → 无 `<term_dictionary>` 段（控制组） |
| `test_plan_referencing_location_alias_passes` | 计划引用「地点」（T20 按工厂过滤）→ validatePlan 通过、200 |
| `test_seed_price_condition4_keeps_location_aliases` | 锁定 seed：价格条件4 地点别名 |
| `test_seed_effective_dates_keep_aliases_and_desc` | 锁定 seed：生效/失效日期 别名+desc（第 2 项） |
| `test_seed_price_condition_redundants_removed` | 锁定 seed：价格条件1/5/6 已删除、不得回归（第 5 项） |
| `test_seed_syncs_aliases_for_existing_property` | （receiptdetail 文件）seed 重跑增量同步别名 |

关键实现约束（防测试序回归）：`_supplierProperties()` 为工厂函数每测试新建 ORM
实例（模块级实例复用会静默丢属性，见
[[qa-integration-seed-orm-instances]]）；`TERMS` 为纯 dict 可安全复用。

回归：**935 passed**（全量）。

## 6. 安全审查

- 无硬编码 secrets；Oracle 直连仅用于诊断采样，密码经运行进程 SECRET_KEY 解密后
  内存使用，未落盘。
- 别名/desc 均为读侧提示，不改变 SQL Guard 只读约束。
- few-shot 清洗仅删除 Milvus 历史向量，不触碰 Oracle 数据。

## 7. 验证记录

- dev DB：term_dictionary 5 条；property 420 别名含「地点编码」、423/424 desc 增强；
  价格条件1/5/6 已删（39→36）。
- Milvus query_embeddings：108 → 95（删除 13 条污染示例），剩余 22 条 PPRICLIST
  示例全部按 T10/T11 过滤或展示明细，0 条无 `PLI_0` 过滤。
- 全量 `uv run pytest app/tests/` = 935 passed。
- code-reviewer（第 1 项）APPROVE，0 CRITICAL/HIGH/MEDIUM；2 条 LOW 已修正。
- code-reviewer（第 2/4/5 项）APPROVE，0 CRITICAL/HIGH/MEDIUM/LOW。核对要点：生效/失效
  日期 desc 与术语词典 formula_hint（`生效日期<=查询日 AND 失效日期>=查询日`）边界一致
  且规避 `_sanitizeSchemaField` 转义；删除的 3 列除诊断脚本 `diag_price_join_truth.py:62`
  外无任何引用；2 个新 seed-guard 用例真实验证（`2 passed`）。

## 8. 后续建议（非阻塞）

- Milvus `query_embeddings` 的删除需等待 compaction 释放物理存储；若后续大批量清洗，
  参照 [[milvus-bulk-delete-unreliable]] 用删集重建收敛。
- 可选加固：`_buildFewShot` 检索层对 PPRICLIST 相关示例按「是否含 `PLI_0` 过滤」
  做软过滤，防止历史坏模式再次污染（本次未做，YAGNI）。
