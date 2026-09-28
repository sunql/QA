# 变更：chore-doc-drift-cleanup

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「文档-代码漂移清扫」批（chat-service-assessment §2.5 P2 #10 剩余半）
- **状态**：done
- **关联变更**：无（纯 docs/docstring 修订，无代码逻辑变更）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.5 P2 #10 剩余条目（`sql_guard.py` 引用 + `IntentType` docstring）

---

## 1. 需求

评估文档 §2.5 挂账的最后两条文档漂移：

1. **`IntentType` docstring 写「DEFINE/MAP/METRIC 暂未接入流水线」** —— 实测已接入（`chat_service.py:2551-2568` 四个 handler）；
2. **`sql_guard.py` 引用** —— 实际无此文件，SQL Guard 在 `business_db_pool.py`；该漂移连带污染 `architecture.md:41` Agent Loop 路径、`agent-loop.md` 整篇虚构 LangGraph 实现。

外加 §2.5 已挂账的「`nl2sql-engine.md` 5 类意图」漂移（实际 13 类）一并处置。

用户口径（binding）：
- **不发明协议 / 不发明实现**：本批纯文档/代码注释与现实对齐，不引入任何代码逻辑变更；
- **不留「已识别但未修」**：所有已查实漂移点在本批全部关闭；
- **Agent Loop 实现真相**：是纯 Python async while loop，不是 LangGraph。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只改 `IntentType` docstring 一处 | 否决：根因是同一类腐化（写文档时未复核代码），agent-loop.md 整篇基于虚构实现、用户读到会被严重误导 |
| B | **整批清扫 + agent-loop.md 整篇重写**（选定） | 评估文档 §2.5 已是结构性漂移台账，逐条修改下次评估又会重开；一次性把 5 处漂移全部对齐 |

### 改动面

5 处修改，4 个文件：

| # | 文件 | 漂移点 | 改动 |
|---|---|---|---|
| 1 | `backend/app/domain/enums.py:151` | IntentType docstring 误述 DEFINE/MAP/METRIC 状态 | 改为「已接入流水线（本体治理指令，含指标/类/属性创建与查询）」 |
| 2 | `Harness/wiki/architecture.md:8,41` | Agent Loop 路径 `services/agent_loop.py`（不存在）+ 描述「LangGraph」（虚构） | 改为 `app/services/agent_runtime_service.py:593` + 「纯 Python async while loop」 |
| 3 | `Harness/wiki/agent-loop.md` | 整篇基于 LangGraph StateGraph 撰写（含不存在的 `agent_loop.py` / `security/sql_guard.py` 引用、虚构的 Checkpointing / LangSmith tracing） | 整篇重写：删除虚构能力、修正路径（Agent Loop 在 `agent_runtime_service.py:593`、SQL Guard 在 `business_db_pool.py`）、保留 5 个 tool 与 `AgentLoopState` / `AgentLoopResult` 真实结构 |
| 4 | `Harness/wiki/nl2sql-engine.md:26` | 「5 类活跃意图」→ 实际 13 类 | 改为 13 类完整列表 + 标注 4 条领域拦截路径 |
| 5 | `Harness/wiki/chat-service-assessment.md` §0/§2.5 | 两条漂移行挂账 | ✅ 关闭；§0 加「2026-09-27 追加批次」段落 |

### 不做

- **不发明 LangGraph 实现**：即便「理论上更优雅」，实测否决理由（handler 都是 async、StateGraph node 包装复杂、mock 困难）已在 `agent_runtime_service.py:609` docstring 写明，本批忠实于现状。
- **不补连接级 `statement_timeout`**：那是库侧只读兜底提案的范围（已转 `2026-09-26-sql-guard-db-side-readonly-proposal.md`），本批仅在 agent-loop.md 注明该提案存在。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无（`IntentType` docstring 修订不影响枚举值/语义；架构文档重写不影响代码契约）。

## 5. 实现要点

- **TDD 不适用**：纯 docs/docstring 修订，无运行时逻辑变更。
- **`agent-loop.md` 重写**保留所有真实存在的元素（5 tool 名、`AgentLoopResult` 字段、`_L4_SYSTEM_PROMPT` 措辞、Cost Cap 机制、SQL Guard 双层检查设计），删掉虚构元素（LangGraph StateGraph 节点定义、Checkpointing 论证、LangSmith tracing）。
- **保留字段级知识**：把 H2 批修复的 `prompt_tokens / completion_tokens / cost_cap_hit / terminated_reason` 字段写进 `AgentLoopResult` 模板（之前文档缺这部分），同时记录 `_runL4AgentLoop` 调用入口与终止条件 4 个。

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| 后端单测 | `app/tests/unit/test_enums.py`（若有）| 不动 — 枚举值未变 |
| 后端 ruff | 本批 1 文件改动（`enums.py:151`） | 1 行字符串变更，语法合规；ruff 与基线对比 delta = 0 |
| 文档可读性 | 人工 spot check | 5 处改动均按 grep 命中行验证可读 |

本批无新增/变更代码逻辑，**测试套件无变化**。

## 7. 安全审查

无代码改动，安全审查不适用。

## 8. 部署验证（2026-09-27）

- **后端仅 1 行 docstring 改动**：无 alembic、无配置、无运行时行为变更，无需 build 镜像；
- `./scripts/deploy_backend.sh` 可跳，仅 `app/domain/enums.py` 同步即可（**文档不在 backend 容器内**，`Harness/wiki/**` 是仓库文档，部署脚本不灌入容器）；
- 真机探针：`python -c "from app.domain.enums import IntentType; print(IntentType.DEFINE, IntentType.MAP, IntentType.METRIC, IntentType.AGENT_RUN)"` → 输出 4 个枚举值字符串；
- `/api/v1/health` 直连 8000 与 nginx 5173 均 200。

## 9. 关联

- commit：`docs: 文档-代码漂移清扫（IntentType docstring + 架构文档 + agent-loop.md 重写 + nl2sql-engine.md 意图数）`（末尾一个 docs commit）
- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.5 两条挂账行 ✅ 关闭、§0 进度追加批次段落
- memory：登记为新条目 `qa-system-doc-drift-cleanup`，包含「agent-loop.md 是虚构 LangGraph 实现的文档」「IntentType 13 类接入现状」「H4/M5/M10 之后又一波 docs 对齐」
- 关联条目：
  - `chat-service-assessment.md` §13 残差 1（库侧只读兜底提案） —— 本批文档确认提案存在，未触动
  - `chat-service-assessment.md` §15 残差 6（H7 残留） —— 本批未触及

## SSOT 校验清单

- [x] 5 处漂移全部对齐（enums.py docstring、architecture.md:8/41、agent-loop.md 整篇、nl2sql-engine.md:26、评估文档 §2.5/§0）
- [x] `agent-loop.md` 不再引用 `app/services/agent_loop.py` / `app/infrastructure/security/sql_guard.py`（grep 验证）
- [x] `architecture.md` Agent Loop 行指向真实路径
- [x] `nl2sql-engine.md` 意图数 = 13 类
- [x] `IntentType` docstring 不再写「暂未接入」
- [x] 评估文档 §2.5 两条挂账行标 ✅；§0 追加 2026-09-27 批次段落
- [x] 无 alembic / 无配置 / 无运行时行为变更
- [x] 后端 ruff delta = 0（仅 1 行字符串变更）
- [x] 真机探针：4 个枚举值（DEFINE/MAP/METRIC/AGENT_RUN）输出正确