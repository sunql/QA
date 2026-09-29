# 变更：chore-doc-drift-cleanup-c

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「文档-代码漂移清扫」批第三拨（接续 `chore-doc-drift-cleanup` + `-b`）
- **状态**：done
- **关联变更**：
  - `chore-doc-drift-cleanup`（f064c31）修 `architecture.md` Agent Loop 路径 + `agent-loop.md` 整篇重写 + `nl2sql-engine.md:26` 意图数 5→13 + `IntentType` docstring DEFINE/MAP/METRIC
  - `chore-doc-drift-cleanup-b` 修 `nl2sql-engine.md:85,139,142` L4 LangGraph + `enums.py:146` IntentType docstring 补全 + `owner.md:11` SQL Guard 引用
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.5 P2 #10 评估文档（剩余表格行状态）

---

## 1. 需求

上批 `chore-doc-drift-cleanup-b` 后 §2.5 表格仍剩 2 行未打 ✅（虽 doc 早已修）：
1. **`nl2sql-engine.md` 写「5 类活跃意图」行** —— doc `nl2sql-engine.md:26` 已被 f064c31 改为「13 类意图」，但 §2.5 表里该行无 ✅ 标记
2. **`nl2sql-engine.md` 的「L2 可选 CTE 增强：`plan.requiresCte=True`」行** —— doc `nl2sql-engine.md:120` 已被 -b 批加 ⚠️ 更正注替换原 bullet，但 §2.5 表里该行无 ✅ 标记

同批主动巡检发现 2 处 doc 漂移尚未被任何批次覆盖：
3. **`metric-pipeline.md:166` 迁移文件名漂移** —— 写 `0051_add_routing_fields.py`，实测为 `0051_add_routing_metrics_fields.py`（缺 `_metrics`）
4. **`metric-pipeline.md:168` LangGraph 残留** —— 写「L4 LangGraph Agent Loop detail」，但 `agent-loop.md` 整篇已被重写为 async while loop，不再是 LangGraph

**用户口径**（沿用上批 binding）：
- **不留「已识别但未修」**：本批彻底关闭 §2.5 全部 7 行表格
- **同源漂移一次清**：发现 2 处 `metric-pipeline.md` 漂移同批处理

---

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只补 §2.5 表格 ✅ 标记，不动 doc | 否决：metric-pipeline.md 漂移不补会留下次评估重开 |
| B | **整批清扫**（选定） | 与前两批一致；本批统一关闭 |

### 改动面

4 处修改，3 个文件：

| # | 文件 | 漂移点 | 改动 |
|---|---|---|---|
| 1 | `Harness/wiki/chat-service-assessment.md` §2.5 第 2 行 | 「5 类活跃意图」无 ✅ 标记 | 加 ✅ + 引用 chore-doc-drift-cleanup |
| 2 | `Harness/wiki/chat-service-assessment.md` §2.5 第 7 行 | 「L2 可选 CTE 增强」无 ✅ 标记 | 加 ✅ + 引用 chore-doc-drift-cleanup-b + 列出同批顺带修正 |
| 3 | `Harness/wiki/metric-pipeline.md:166` | 迁移文件名 `0051_add_routing_fields.py` | 改为 `0051_add_routing_metrics_fields.py` + 加「原文...已更正」注 |
| 4 | `Harness/wiki/metric-pipeline.md:168` | `agent-loop.md` 描述「L4 LangGraph」 | 改为「L4 纯 Python async while loop Agent」+ 加更正注 |

### 不做

- **不动 `changes/**` SSOT 历史**：LangGraph 历史引用在 `chore-doc-drift-cleanup/summary.md` 等 SSOT 里是决策审计，保留
- **不动 `chat-service-assessment.md` §2.5 已 ✅ 行**：本批仅补两行未 ✅ 状态

---

## 3. 数据模型变更

无。

---

## 4. 接口契约变更

无（仅文档修订）。

---

## 5. 实现要点

- **§2.5 表 7 行 ✅ 闭合**：剩余 2 行均为「doc 早已修、表格未打 ✅」状态，本批仅做表格状态对齐
- **`metric-pipeline.md` 主动巡检**：搜「迁移文件名」+「LangGraph」在 wiki/ 残留，发现两处未被任何批次覆盖
- **每处改动加更正注**：与前两批风格一致——保留原引用作为历史审计，同时标注正确路径

---

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| 文档可读性 | grep `LangGraph Agent Loop` / `0051_add_routing_fields.py` / `sql_guard.py` 在用户面向 wiki 零命中（SSOT 历史 / 更正注除外） | 验证通过 |

### grep 验证

```bash
# 期望：用户面向 wiki 中除更正注/历史 SSOT 外零命中
grep -rn "0051_add_routing_fields\.py" Harness/wiki/ Harness/agents/   # 仅 metric-pipeline.md:166 已修
grep -rn "L4 LangGraph" Harness/wiki/ Harness/agents/                   # 仅 agent-loop.md / nl2sql-engine.md 的 ⚠️ 注
```

---

## 7. 安全审查

无代码改动，安全审查不适用。

---

## 8. 部署验证（2026-09-27）

- 仅 wiki 文档改动，无 alembic、无配置、无运行时行为变更；
- 文档变更不入容器（`Harness/wiki/` 是仓库文档），无需 deploy_backend.sh；
- 无需真机探针（无后端代码变更）。

---

## 9. 关联

- 上批 commit `chore-doc-drift-cleanup-b`（df29de5）：补 L4 段 / IntentType 13 类 / owner.md 引用
- 上上批 commit `chore-doc-drift-cleanup`（f064c31）：architecture.md / agent-loop.md / nl2sql-engine.md:26 / IntentType docstring
- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.5 P2 #10（**本批后 7 行表格全部 ✅ 关闭**）
- memory：既有 `qa-system-doc-drift-cleanup` 已有，本批作「第三拨接续」追加备注

---

## SSOT 校验清单

- [x] `chat-service-assessment.md` §2.5 第 2 行「5 类活跃意图」加 ✅
- [x] `chat-service-assessment.md` §2.5 第 7 行「L2 可选 CTE 增强」加 ✅
- [x] §2.5 表格 7 行全部 ✅ 关闭
- [x] §0 状态头追加「第三拨补」段
- [x] `metric-pipeline.md:166` 迁移文件名更正为 `0051_add_routing_metrics_fields.py`
- [x] `metric-pipeline.md:168` LangGraph 描述更正为 async while loop
- [x] 无 alembic / 无配置 / 无运行时行为变更
- [x] 无需 deploy_backend.sh / 无需真机探针
- [x] grep 验证：用户面向 wiki 零命中残留