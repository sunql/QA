# Milvus HNSW ef/limit bug 修复（随本窗口发版）

## 1. 背景与目标

线上 2026-11-19：wiki 注入的 property/metric 召回每轮 chat 必现
`MilvusException(code=65535, ef(64) should be larger than k(80))`，异常被
chat_service 吞成 warning ⇒ **property/metric 注入静默失效**。

根因：HNSW 服务端硬校验 `ef >= limit`；全仓 5 个 `collection.search` 调用点把
`{"ef": 64}` 写死，而去重超取 `limit = topK × _DEDUP_OVERFETCH(8)` ⇒ topK > 8
必炸。wiki 注入 property 召回 topK=10 → limit=80 > ef=64。类召回能活是因为生产
`CLASS_FILTER_TOP_K ≤ 8` 恰好压线 —— 不是设计使然。

## 2. 决策

- **helper 单点**：`milvus_client.hnswSearchParams(limit)` 返回
  `{"metric_type": "L2", "params": {"ef": max(_HNSW_EF_FLOOR=64, limit)}}`。
  ef 大于 k 只是多探一些候选，命中口径不变 ⇒ 取 max(下限, limit)。
- **5 个调用点全部切换**，ef 源 = 各自的 limit 实参（逐一对齐核验）：
  | 调用点 | ef 源 | limit |
  |---|---|---|
  | `milvus_client.searchQueryEmbedding` | `topK` | `topK` |
  | `milvus_client.searchDocumentChunks` | `topK` | `topK` |
  | `milvus_client.searchWikiPageChunks` | `topK` | `topK` |
  | `milvus_search._searchCollection` | `topK * _DEDUP_OVERFETCH` | 同 |
  | `milvus_registry_v2._searchV2Collection` | `topK * _DEDUP_OVERFETCH` | 同 |
- **测试钉住不变量**（`test_milvus_search_dedup.py` 新增 2 类 4 用例）：
  服务端形态断言（fake collection 记录 `receivedParam`，ef ≥ limit）+
  helper 直接单测（大 limit 收敛 / 小 limit 保下限）。未来任何调用点重新写死
  `{"ef": 64}` 都会被抓到。
- `pyproject.toml` per-file-ignores：`test_milvus_search_dedup.py` 的 I001
  —— 测试 import 顺序是**刻意**的循环导入规避（milvus_client 文件尾反向 import
  milvus_search，必须先导 client），isort 恰好要破坏这个顺序。
- 顺手修 `milvus_search.py` 既有的 I001（纯 import 重排，HEAD 即有）。

## 3. 实证

- `test_milvus_search_dedup.py` 9 passed（5 既有去重 + 2 不变量 + 2 helper）。
- 邻居回归：`test_milvus_client.py` + `test_milvus_search_connection.py` +
  `test_milvus_query_projection.py` 18 passed。
- 全仓 grep：`"ef"` 只剩 helper 内部一处；`collection.search` 全部 5 个调用点
  都已走 helper。
- ruff：净新增 0（milvus_client 21 条既有不碰；milvus_search 既有 1 条已修）。

## 4. 评审发现与处置

code-reviewer 审查（含 5 调用点 ef/limit 实参逐一对齐核对、全仓 grep 漏网排查、裸解释器
实证循环导入方向、实跑 27 passed 复现、ruff 基线对账）：**0 CRITICAL / 0 HIGH / 0 MEDIUM /
3 LOW，APPROVE**。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | LOW | pyproject 注释称「收集期撞循环导入」夸大——HEAD 测试顺序在 pytest 下收集正常，失败仅在裸解释器可复现 | 措辞软化为「部分装载上下文会撞循环导入；钉死顺序在任意上下文都安全」 |
| 2 | LOW | 集成级不变量只覆盖 `_searchCollection` 一条路径；registry_v2 与 milvus_client 三调用点靠 helper 直测间接守护（registry_v2 侧重新写死 ef=64 不会有集成级红灯） | 接受（helper 是唯一 param 来源，风险已合理缓解）；记入遗留 |
| 3 | LOW | 提交卫生：工作树混入 M1-1.6 无关改动 | 非本批代码问题——M1-1.6 已先独立提交（`2222f643`） |

## 5. 遗留

- 生产镜像随本窗口发版一起带上本修复（用户指示：「发版的时候把 milvus 的 bug
  看是否可以一起发版修复」）。
- chat_service 把 MilvusException 吞成 warning 的静默失效面：本批不动，另立批次
  评估告警口径（异常应可见，而不是每轮静默重试失败）。
- 集成级 ef 不变量只覆盖 `_searchCollection` 一条路径（评审 LOW-2）：若未来在
  `milvus_registry_v2._searchV2Collection` 或 milvus_client 三调用点重新写死
  `{"ef": 64}`，不会有集成级红灯 —— 靠 `TestHnswSearchParamsHelper` 直测 +
  helper 单点约束兜底；要更硬可在 fakeSearch 夹具上对全部路径做参数断言。
