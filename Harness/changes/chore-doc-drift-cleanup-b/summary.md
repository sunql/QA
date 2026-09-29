# 变更：chore-doc-drift-cleanup-b

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「文档-代码漂移清扫」批第二拨（接续 `chore-doc-drift-cleanup` f064c31）
- **状态**：done
- **关联变更**：`chore-doc-drift-cleanup`（f064c31 上批，已修 `architecture.md` Agent Loop 路径 + `agent-loop.md` 整篇重写 + `nl2sql-engine.md` 意图数 + `IntentType` 核心 docstring）；`fix-routing-metrics-l3-truth`（ac94416，前端 L3 真话化）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.5 P2 #10 评估文档（剩余条目）

---

## 1. 需求

上批 `chore-doc-drift-cleanup`（f064c31）已修 5 处漂移中的 4 处；本批接续清查 3 处剩余漂移：

1. **`nl2sql-engine.md` L4 段仍写「LangGraph `StateGraph`」** —— 实际代码是纯 Python async while loop；上批只改了意图数与架构摘要行，L4 详细段、Decision Flow 图未触及
2. **`IntentType` docstring 不完整** —— 13 类枚举中上批只描述了 8 类，遗漏 5 类（CHITCHAT / SUPPLIER_360 / SUPPLIER_RISK / GRAPH_REASONING / AGENT_RUN）
3. **`Harness/agents/owner.md:11` 仍引用 `app/infrastructure/security/sql_guard.py`** —— 文件不存在，SQL Guard 在 `business_db_pool.py`

**用户口径**（binding，沿用上批）：
- **不发明协议 / 不发明实现**：忠实于现状
- **不留「已识别但未修」**：本批彻底关闭这三处

---

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只修一处 | 否决：根因是同一类腐化（写文档时未复核代码），逐条修改下次评估又会重开 |
| B | **整批清扫**（选定） | 与上批 `chore-doc-drift-cleanup` 一致 |

### 改动面

3 处修改，3 个文件：

| # | 文件 | 漂移点 | 改动 |
|---|---|---|---|
| 1 | `Harness/wiki/nl2sql-engine.md:85,139,142` | L4 段标题、Decision Flow 图、L4 bullet 三处仍写 LangGraph | 改为「Pure Python async while loop」+ 加 ⚠️ 更正注 + 补充 4 类终止条件 / cost cap 默认值 / `cost_per_1k_input` 默认 0 风险 |
| 2 | `backend/app/domain/enums.py:146` | IntentType docstring 遗漏 5 类 | 补全 13 类分四档（NL2SQL 主路径 5 / 不进 NL2SQL 1 / 本体治理 3 / 领域拦截 4） |
| 3 | `Harness/agents/owner.md:11` | 引用不存在的 `security/sql_guard.py` | 改为 `business_db_pool.py` + 加「该文件不存在」注 |

### 不做

- **不动 `changes/**` SSOT 里的 LangGraph 引用**：历史变更记录里的 LangGraph 引用是合法的（如 `feat-complex-metric-pipeline/summary.md` 记录「LangGraph 被否决」的决策过程；`chore-doc-drift-cleanup/summary.md` 描述上批清扫），保留为决策审计
- **不动 `agent-loop.md` 内的「不在 `security/sql_guard.py`」明示**：该段已是合法反例交叉引用

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

无（仅 docstring + 文档修订）。

---

## 5. 实现要点

- **`nl2sql-engine.md` L4 段重写**：与 `architecture.md` / `agent-loop.md` 一致——明示「纯 Python async while loop + AgentLoopState TypedDict 作为 LangGraph 升级占位」，补充 `agent_runtime_service.py:599` 注释里的成本低估风险
- **`IntentType` docstring 四档分述**：按 NL2SQL 主路径 / 不进 NL2SQL / 本体治理 / 领域拦截分档，每档附 Phase 编号与下游服务名（与代码 `// Phase X.Y` 注释对齐）
- **`owner.md` 引用校正**：与 `nl2sql-engine.md:44` 同口径，附「该文件不存在」明示，避免后续读者再误引用

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| 后端 ruff | `app/domain/enums.py` 字符串变更；35 条 StrEnum 迁移建议为预存，非本批引入 | delta = 0 |
| 文档可读性 | grep `LangGraph` / `sql_guard.py` / `暂未接入` 在用户面向 doc / owner.md 零命中（SSOT 历史记录除外） | 验证通过 |

### grep 验证

```bash
# 期望：用户面向 Harness/wiki 仅命中 agent-loop.md / nl2sql-engine.md 的「更正」段
grep -rn "LangGraph" Harness/wiki/ Harness/agents/
grep -rn "sql_guard\.py" Harness/wiki/ Harness/agents/
grep -rn "暂未接入" Harness/wiki/ backend/app/domain/enums.py
```

---

## 7. 安全审查

无代码改动，安全审查不适用。

---

## 8. 部署验证（2026-09-27）

- 后端仅 `enums.py` docstring 改动，无 alembic、无配置、无运行时行为变更；
- 文档变更不入容器（`Harness/wiki/`、`Harness/agents/` 是仓库文档），无需 deploy_backend.sh；
- 真机探针：`python -c "from app.domain.enums import IntentType; print(list(IntentType))"` → 输出 13 个枚举值字符串；
- `/api/v1/health` 双通道 200（与上批共用同套部署）。

---

## 9. 关联

- commit（待提交）：
  - `docs: 文档-代码漂移清扫第二拨（nl2sql-engine L4 段 + IntentType docstring 补全 + owner.md SQL Guard 引用）`
- 上批 commit `f064c31`：上批 `chore-doc-drift-cleanup` 改 4 处
- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.5 P2 #10 评估（剩余条目全部关闭）
- memory：既有 `qa-system-doc-drift-cleanup` 已有，本批作「接续清扫」追加备注

---

## SSOT 校验清单

- [x] `nl2sql-engine.md` L4 段不再写 LangGraph StateGraph
- [x] `nl2sql-engine.md` Decision Flow 图 L4 标注「Pure Python async while loop」
- [x] `IntentType` docstring 完整列出 13 类
- [x] `owner.md:11` 引用 `business_db_pool.py` 而非不存在的 `sql_guard.py`
- [x] 无 alembic / 无配置 / 无运行时行为变更
- [x] 后端 ruff delta = 0（仅 docstring 字符串变更）
- [x] 真机探针：13 个 IntentType 枚举值输出正确