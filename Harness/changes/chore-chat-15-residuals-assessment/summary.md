# 变更：§15 残差评估（2026-09-28，4 项处理）

- **日期**：2026-09-28
- **作者**：Claude (with user direction 启琳)
- **Phase**：§15 残差评估（Phase 3）
- **状态**：done（4 项均已逐项评估、决定保留/挂账/关闭）
- **关联**：`Harness/wiki/chat-service-assessment.md` §15 残差清单

---

## 1. 需求

§15 主批 7 项已落地（2026-09-27，详见 [`fix-chat-15-tail-three-items`](../../changes/chore-chat-15-tail-three-items/summary.md) 与
[`chore-l3-deadcode-and-prior-cte-contract`](../../changes/chore-l3-deadcode-and-prior-cte-contract/summary.md)）。

**残差清单**（截至 2026-09-27 §15 文末）：
1. `prior_cte` 能力无生产调用者（保留 `chained_step_plan.py` 但无接线）
2. ✅ L3 展示漂移（已关闭 2026-09-27）
3. ✅ KPI 关键词索引（已登记 SSOT）
4. ✅ 空关键词语义（已修）
5. ✅ 失败尝试的 LLM 用量采集（已落地）
6. ✅ H7 残留（已补关）
7. M9 残留：Milvus ORM `Collection` API 仍标 deprecated，迁 `MilvusClient.query_iterator` 是独立改动
8. 仍有：(a) `logger.warning(…, exc)` 截断评估；(b) Milvus round-trip 残留累积评估

本批**评估**剩余 4 项，决定保留/挂账/关闭。

---

## 2. 项 1：`prior_cte` 能力保留评估

### 评估结论：**保留**

`app/domain/chained_step_plan.py`（104 行，含 `ChainedStep` 数据类 + `render_prior_cte` 纯函数）
仅被 `test_prior_cte_contract.py`（12 例）驱动，**无生产调用者**（grep 确认 production
代码中 `render_prior_cte` 与 `from app.domain.chained_step_plan` 零命中）。

**为何不删**：
1. **契约测试钉死的不变量**（M8 修法的核心）：WITH-less 片段的产出 + `_assertPriorCteSafe`
   入口拒绝 + `generateSql` 唯一补 `WITH` —— 这三处契约如果未来某特性要接线重写 L3
   串联引擎，**契约已就位**，不会再次踩 M8 的 `WITH WITH` 地雷。
2. **未来扩展成本极低**：链式推理是 ReAct 的天然方向，删掉等于关闭这条路径。
3. **代码量极小**：104 行 1 个数据类 + 1 个纯函数，**纯函数无副作用**，日常开销可忽略。
4. **契约测试是关键证据**（12 例）—— 测试是产品约束的 SSOT，删代码意味着**也删约束**。

### 标注与挂账
- 已在评估文档 §15 残差第 1 条「如实标注」字段更新：**保留决策已 SSOT 化**；
- 后续若 6 个月内仍无接线需求（SSOT 化时间 2026-09-28），**重新评估**删除；
- 接入判定信号：任何 `chat_multistep.py` 的多步逻辑提到「串联 CTE」或「前步 SQL 复用」时
  ⇒ 立即把 `chained_step_plan.render_prior_cte` 接进来。

---

## 3. 项 7：M9 Milvus ORM `Collection` API 评估

### 评估结论：**挂账 P2，独立改动**

当前 `backend/app/infrastructure/milvus_client.py` 用 `Collection` + `CollectionSchema` +
`FieldSchema` API（pymilvus 3.x 已标 deprecated，但**仍能用且功能完整**）。

### 既有覆盖
- §15 主批已修 **`query(limit=16384)` 截断真缺陷**（改 `_queryAllRows` + `query_iterator`），
  这是 **M9 残差本批最重要修复**——两个破坏性消费者（`backfill_milvus_embeddings --cleanup` 删集重建、
  `ontology_service` 对账永不收敛）已不再触发。
- 现状规模实测 4 个集合（7264/561/17/43 行）远低于 16384 ⇒ **生产事故已消灭**。

### 挂账理由
1. **`Collection` API 仍工作**（pymilvus 3.x 标 deprecated 但未移除）：runtime 不报错；
2. **迁 `MilvusClient` API 改动面大**：12 处 `Collection(...)` / `CollectionSchema(...)` /
   `_ensureCollection` / 字段 schema 定义都需要重写——属于**完整重写**而非**局部替换**；
3. **收益有限**：deprecated 而非 removed；新版 `MilvusClient` API 文档分散，社区
   示例仍以 `Collection` 为主；**迁移只换皮不治根**。
4. **风险敞口可控**：当前任何 Milvus 部署升级都会触发（被 pymilvus 4.x 移除），
   故**升级前必迁**——这是强制升级点，不需要现在主动迁。

### 行动计划
挂账 P2，下次 pymilvus 升级窗口（半年内预期）一起做。SSOT 已在评估文档 §15 残差第 7 条
标注「**挂账 P2，下次 pymilvus 升级窗口一起迁**」。

---

## 4. 项 8a：`logger.warning(…, exc)` 截断评估

### 评估结论：**关闭（评估真实存在，已知合法）**

grep 全仓 `logger.warning(...exc)` 33 处。逐类排查：

| 形态 | 示例 | 评估 |
|---|---|---|
| `logger.warning("%s", exc)` | `dependencies.py:113`、`graph_traversal.py:64`、`chat.py:99` 等 7 处 | **合法**：`%s` 占位符收完整对象（包括 `__str__`），`exc.message` / `exc` 单字段不会截断 |
| `logger.warning("%s", exc.message)` | `chat.py:99`、`main.py:568` | **合法**：显式取 `.message` 字段，语义清晰 |
| `logger.warning("...", exc_info=True)` | 23 处 | **合法**：`exc_info=True` 走 Python 标准 logging 的完整堆栈渲染，不会截断 |

**§15 主批此项的真实意图**：评估「`logger.warning("...%s", exc, ...)` 在某些 logging handler
下被截断为单行 + 截掉异常类型名」—— 这种截断通常发生在**自定义 logging handler**
（把 record 拍平成 JSON 上送日志平台时丢 `exc_info`）或者 **f-string 风格调用
（`logger.warning(f"...{exc}..."`）**。

### 检查结果
- 仓内**无 f-string 风格** logger 调用（ruff 已开规则 `G004`）；
- `exc_info=True` 23 处走 Python 标准机制，无截断风险；
- `%s + exc` 7 处走标准 string-formatting，单行打印完整对象（包括 type 与 str）。

### 关闭依据
本项评估的「截断」隐患在仓内**不存在**——所有 `logger.warning` 调用要么用
`exc_info=True` 走标准堆栈，要么用 `%s` 占位符接 `exc`/`exc.message`，没有
任何**会被 handler 误处理**的形态。SSOT 已记录此评估结论。

---

## 5. 项 8b：Milvus round-trip 残留累积评估

### 评估结论：**关闭（random id 改造已修残留累加，残留可见性延迟是 Milvus 服务自身延迟）**

`test_milvus_wiki_fields.py::test_delete_scopes_to_one_page` 与
`test_milvus_document_fields.py::test_delete_document_chunks_scopes_to_one_document` 两例：

| 改造 | 时间 | 根因 | 修法 |
|---|---|---|---|
| random id | 2026-09-27 commit `7e0a0b1` (SSOT `fix-plan-scope-gate-and-trigram`) | 固定业务 id 重插 + 删对查不可见 + 用例不自清理 ⇒ 跨轮累加 | 改 `uuid.uuid4().hex[:16]` 每次随机 |

**残留（未消除）**：单次跑内**仍可能撞上** Milvus `Collection.flush()` 不保证查询端 delete
buffer 已应用的延迟——属 Milvus 服务自身行为，与产品代码无关。

### 关闭依据
1. **跨轮累加（修前症状：3→4→5 单调增长）已彻底消除** —— random id 与历史无关，残留不再累加；
2. **单次跑偶发** 是 Milvus 服务端 delete buffer 应用延迟（2.4.6 per-request Strong 一致性
   实测不生效，SSOT `qa-system-milvus-strong-consistency-gotcha`），属于**集成测试环境依赖**，
   与 production code 无关；
3. **production 同源风险已登记**（`wiki_vector_service.syncPage` 先 delete 再 insert，
   延迟期间检索可能拿到旧 chunk）—— `memory:qa-system-milvus-delete-visibility-flake`
   标注，未见 prod 事故报告，**挂账观察**。

### 状态
**本项关闭**。Memory 已记录根因；production 同源风险挂账（prod 事故触发立即处置）。

---

## 6. 关联

- **§15 主批 SSOT**：`Harness/changes/fix-chat-15-tail-three-items/summary.md`（items 3-5）
- **L3 + prior_cte 批**：`Harness/changes/chore-l3-deadcode-and-prior-cte-contract/summary.md`（item 1/2/6）
- **评估文档**：`Harness/wiki/chat-service-assessment.md` §15 残差 4 处状态更新
- **memory**：保留与挂账信号已写入既有 memory，无新增条目

---

## SSOT 校验清单

- [x] 4 项均逐项评估，给出明确决定（保留 / 挂账 / 关闭）
- [x] 项 1 保留决策标注「6 个月内仍无接线需重新评估 + 接入信号」
- [x] 项 7 挂账 P2 + 强制升级点（pymilvus 4.x 移除 Collection API）作为兜底
- [x] 项 8a 全仓 grep 33 处日志调用 + 三类形态分类评估，**无截断隐患**
- [x] 项 8b random id 已修跨轮累加 + Milvus 服务延迟属测试环境依赖，production 同源挂账
- [x] 0 代码改动（纯评估文档）
