# 变更：fix-milvus-list-all-pagination

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（M9）
- **状态**：done
- **关联变更**：同批 [fix-chat-disconnect-persistence](../fix-chat-disconnect-persistence/summary.md)、[fix-llm-transient-retry](../fix-llm-transient-retry/summary.md)、[chore-l3-deadcode-and-prior-cte-contract](../chore-l3-deadcode-and-prior-cte-contract/summary.md)、[fix-embedding-provider-type-guard](../fix-embedding-provider-type-guard/summary.md)、[test-kpi-match-cache-ordering](../test-kpi-match-cache-ordering/summary.md)
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M9**（评估日期 2026-09-25）
- **commit**：`785a6ff`

---

## 1. 需求

`listAllEmbeddings()` 此前以 `collection.query(limit=16384)` 取「全量」，而 Milvus 对**超过 limit 的结果集静默截断**（不报错、不警告）—— 一旦集合行数超过 16384，**窗口外的向量在调用方眼里根本不存在**。

评估原文把这条写成「性能/正确性隐患」，但**破坏力被低估**（本批探查确认）：这个「全量读」有两个**破坏性消费者**：

1. `scripts/backfill_milvus_embeddings.py --cleanup` 走 **删集重建** —— 从截断结果重建 ⇒ 窗口外的向量**永久丢失**（不是「少读一次」，是「被删掉且不再写入」）；
2. `ontology_service` 的对账反复把「实际仍存在但在窗口外」的向量判为**缺失**并重复插入 ⇒ **永不收敛**（越跑越脏）。

**验收标准**：① 全量读不再截断（与行数无关）；② 返回值与字段集**不变**（调用方零改动）；③ 16384 字面量收敛为具名常量，且**代码里只剩常量定义一处**；④ 分页行为有单测覆盖（不依赖真 Milvus）；⑤ 累计行数恰为批大小整数倍时**留痕**（提示可能仍在边界收尾，不静默）。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 保留 `query()` 但把 limit 调大到「足够大」（如 100 万） | **否决**：同一个静默截断语义，只是把悬崖推远 —— 且 Milvus 对超大 limit 有响应体上限问题，把「静默少行」换成「查询失败」并不更好 |
| B | 自己写 `offset` 翻页（`query(limit=N, offset=k*N)`） | 否决：offset 翻页在**并发写入**下会漏行/重行；且需要额外的一次 count 查询才能判断结束 |
| C | **`Collection.query_iterator`**（选定） | pymilvus 3.0.1 已具备（`orm/collection.py:1174`），`limit` 默认 UNLIMITED，取到 `next()` 返回空为止；游标由服务端维护，语义即「取尽」；`close()` 释放迭代器 cache 与游标 checkpoint 文件 |
| D | 改用 `MilvusClient.query_iterator`（新 API） | 暂缓（非否决）：ORM 风格 API 在 pymilvus 3.x 被标 deprecated，但**本模块其余部分**（`connections` / `utility` / ORM 集合）都是 ORM 风格 —— 只换一个函数会造成两套风格并存。迁移属**独立改动**（已写进契约与后续登记） |

### 可注入的迭代器工厂（本项唯一设计接缝）

`_queryAllRows(collection, *, expr, outputFields, iteratorFactory=None)` 的 `iteratorFactory` 让单测能喂**假迭代器**（跨 2 个批次、含顺序与边界），从而**不依赖真 Milvus** 验证「跨批不丢行/不乱序」；默认值是 `collection.query_iterator` ⇒ 生产路径零变化。

### 常量收敛的口径

`_MILVUS_QUERY_PAGE = 16384` 同时用于「迭代批大小」与两个**expr 受限**的兄弟读取（`:570`、`:716` —— 它们的 `expr` 已把结果集限定在小范围，`limit=` 是**合法**的护栏，不是截断缺陷）。收敛常量保证「批大小」与「这些护栏」不会再各写各的字面量。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本；**不动 Milvus 集合结构与数据**（本项只改读取方式，探针全程只读）。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `listAllEmbeddings()`（`milvus_client.py:250`） | **签名与返回值不变**（`list[dict]`，字段仍是 `id/ontology_id/type/name/alias/description/embedding`）—— 调用方（`--cleanup`、`ontology_service` 对账）**零改动**自动受益 |
| `_queryAllRows(...)`（新增内部 helper） | 迭代取全量；`close()` 放 `finally`；返回值拼接为单个列表 |
| `_MILVUS_QUERY_PAGE`（新增常量，`:40`） | 批大小单源；注释写明「超过该值的 `query(limit=…)` 会被服务端静默截断」 |
| 留痕契约 | 累计行数 **恰为批大小整数倍**时 `logger.warning`（提示疑似在分页边界提前收尾，要求核对集合实际行数）—— 不静默 |
| 已知限制 | 仍用 ORM 风格 `Collection` API（pymilvus 3.x 已标 deprecated）；迁 `MilvusClient` 属独立改动 |

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/infrastructure/milvus_client.py:250` `listAllEmbeddings` | docstring 写明「必须走分批迭代」的**理由链**（截断 → 删集重建永久丢失 / 对账永不收敛）；实现改调 `_queryAllRows` |
| 同文件新增 `_queryAllRows` | `query_iterator(batch_size=_MILVUS_QUERY_PAGE, expr=…, output_fields=…)` + `while True: batch = iterator.next(); if not batch: break` + `finally: iterator.close()` + 整数倍 warning；`iteratorFactory` 为可注入接缝 |
| 同文件 `:40` | 16384 字面量收敛为 `_MILVUS_QUERY_PAGE`（含为何不能用作「取全量」的注释） |
| `app/tests/unit/test_milvus_client.py`（+137 行） | 假迭代器跨批用例（不漏行/不乱序/批次边界）、`close()` 被调用、整数倍 warning、`listAllEmbeddings` 走迭代器而非 `query(limit=…)` |

**探查确认的破坏力（写入 docstring 的依据）**：删集重建的**写**来自同一份截断结果 ⇒ 窗口外向量丢失不可恢复；对账的**判缺**来自同一份截断结果 ⇒ 反复插入、永不收敛。

## 6. 测试

- **单元 `test_milvus_client.py` 18 passed**（含本项新增的分页用例：假迭代器跨 2 批、顺序保持、`close()` 在 `finally`）。
- **容器内真机差分探针（`/tmp/probe_m9_diff.py`，4 项全 PASS，2026-09-27）** —— 同 expr、同 output_fields，两条独立路径必须逐行等集**等序**：

  ```
  PASS  M9 批大小是具名常量  | 16384
  PASS  M9 listAllEmbeddings 代码骨架内不再用 query(limit=…)
        （兄弟读取仍用 limit= 的行号 = 570,716 —— expr 受限，合法）
        listAllEmbeddings 行数 = 7261
        batch_size=7 扫出  行数 = 7261
  PASS  M9 两条路径行数一致  | 7261 vs 7261
  PASS  M9 两条路径 id 顺序一致（无漏、无重、不乱序）
        集合 num_entities = 7264（含未 flush 的删除标记，仅作参考）
  ```

- **规模实测（同一探针，2026-09-27）**：`ontology_embeddings` 7264（`num_entities`）/ 7261 行可读、`query_embeddings` 561、`document_embeddings` 17、`wiki_page_embeddings` 43 ⇒ **四个集合当前都远低于 16384**。诚实结论：**本项修复的是「潜伏的悬崖」，不是正在发生的事故** —— 但 7264 距 16384 已不远，且修复成本低、破坏力大，故按真缺陷处理。
- **探针自身的两处断言缺陷（首跑 1 FAIL，如实记录）**：
  1. `src.count("16384") == 0` **恒不可能通过** —— 它把常量定义与解释性注释一起算进违规；改为**剥离字符串/注释后的代码骨架**计数，得「代码骨架 1 处，全文（含常量定义 + 文档注释）2 处」；
  2. 改为「`listAllEmbeddings` 函数体内无 `limit=`」后**仍 FAIL**：函数体内的 **docstring** 正是在解释这条改动（「原实现 `query(limit=16384)` 会被静默截断」）—— 按原文匹配等于**把自己的文档判成违规**。
  两次是同一根因：**「文档提到旧写法」≠「代码还在用旧写法」**（同批 M4 探针也踩过一次，见 [fix-llm-transient-retry](../fix-llm-transient-retry/summary.md) §6）。修正后 4/4 PASS，两条断言均改为对**代码骨架**断言。

## 7. 安全审查

**未触发**：不涉及认证、密钥、SQL 构造（Milvus 表达式的 `expr` 字面量未变）、用户输入、加解密、支付。两点说明：

- 本项**减少**了一类数据完整性风险（截断结果驱动删集重建 ⇒ 向量永久丢失）；
- 探针与验证全程**只读**（不 insert/delete/flush；未触碰 `query_embeddings`）。

## 8. 部署验证（2026-09-27）

- **部署**：`./scripts/deploy_backend.sh`（含 `alembic/`；本项无迁移）；仓库 ↔ 容器 md5 `app/infrastructure/milvus_client.py` **MATCH**（本批 22/22 现存文件 MATCH）；
- **容器内真机探针 4 项全 PASS**（输出见 §6），其中「整页迭代 vs `batch_size=7` 微批次」是**独立第二条路径**的差分 —— 不是把实现的输出抄一遍当期望；
- **测试**：全量 unit+services **2 failed, 2522 passed, 1 skipped**（两条为**预存**失败，`git worktree add --detach` 在基线复现判别，delta = 0）；集成切片 120 passed + 3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；
- **静态检查**：本批 22 文件 ruff 与基线逐行一致；唯一告警是与本项无关的 **预存 F841**（`test_milvus_client.py::TestDropCollection::test_drops_ontology_collection` 未用绑定，基线 `:174` → HEAD `:309`，因本项新增 135 行用例下移）—— **本项未引入新告警，也未顺手修它**（属独立改动；若要清零可单开一条）；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M9（标 ✅；**含「潜伏而非活跃」的精确说明**）、§15 批次记录
- 代码契约：`app/infrastructure/milvus_client.py`（`_MILVUS_QUERY_PAGE` / `_queryAllRows` / `listAllEmbeddings`）、`app/tests/unit/test_milvus_client.py`
- 下游受益（零改动）：`scripts/backfill_milvus_embeddings.py --cleanup`、`app/services/ontology_service.py`（对账路径）
- 后续登记：`MilvusClient.query_iterator` 迁移（两套 API 风格并存的统一）、`test_milvus_client.py` 的预存 F841
- 规则：`Harness/rules/数据与AI治理*.md`、根 `CLAUDE.md` 核心约束 #6（不静默）
- Memory：`qa-system-*` 新增 M9 条目（分页 + 常量收敛 + 「潜伏悬崖而非活跃事故」的规模实测）
- 同批：`../fix-chat-disconnect-persistence/`、`../fix-llm-transient-retry/`、`../chore-l3-deadcode-and-prior-cte-contract/`、`../fix-embedding-provider-type-guard/`、`../test-kpi-match-cache-ordering/`

## SSOT 校验清单

- [x] 第 1 段 需求：静默截断 + **两个破坏性消费者**（删集重建永久丢失 / 对账永不收敛）+ 5 条验收标准
- [x] 第 2 段 4 个候选方案（含否决「调大 limit」「自己 offset 翻页」）+ 可注入迭代器接缝 + 常量收敛口径（含兄弟函数的**合法** `limit=`）
- [x] 第 3 段 无迁移、不动 Milvus 数据（探针只读）
- [x] 第 4 段 接口契约：签名/返回值不变（下游零改动）+ 留痕契约 + 已知限制（ORM API deprecated，迁移另开）
- [x] 第 5 段 实现要点逐位置表 + 破坏力依据写入 docstring
- [x] 第 6 段 测试：18 例 + **4 项真机差分探针实录** + 四集合规模实测（**潜伏而非活跃**的诚实结论）+ 两处探针断言缺陷及同一根因
- [x] 第 7 段 安全审查：未触发 + 减少数据完整性风险 + 全程只读
- [x] 第 8 段 部署验证：md5 MATCH + 探针 + 全量测试与预存失败判别 + ruff delta 0（含**预存 F841 未顺手修**的说明）+ 网关 200
- [x] 第 9 段 跨文件链接（评估文档 / 代码契约 / 下游受益 / 后续登记 / 规则 / Memory / 同批）
