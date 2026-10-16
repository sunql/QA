# chat_service.py / chat_multistep.py 行数审计 (2026-10-16)

**触发**：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md` N1
（"建议 chat_service.py ≤2000 行硬上限，超出 2000 行必须拆"）+ 全局记忆
`chat_multistep-line-cap-exemption`（≥1000 行暂不拆）。

## 当前行数（2026-10-16，main tip `ec0570b0`）

| 文件 | 行数 | 阈值 | 状态 | 触发拆分？ |
|---|---|---|---|---|
| `backend/app/services/chat_service.py` | **1986** | 2000 上限 | ✅ under | **否** |
| `backend/app/services/chat_multistep.py` | **991** | 1000 豁免 | ✅ under | **否** |

## 与计划估算的对比

- 计划 PR 描述估算："chat_service.py 行数从 1969 增至 ~2050（仍在 2000 豁免内）"
- 实际：**1986 行**（含 L1L4PreambleMixin 在 line 295 加入）
- 评估偏差：-64 行（计划估高，可能因为 mixin 抽取时没引入预期行数；或 N5 stream_events `default=str` 等改动没回流到 chat_service.py）

## N1 拆分决策

**不触发拆分**。理由：

1. 1986 < 2000（硬上限内）
2. 12 个 mixin 仍集中声明在 `class ChatService(RecallMixin, MultiStepMixin, ..., L1L4PreambleMixin):`（line 295），mixin 组合稳定
3. `chat_multistep.py` 991 < 1000（豁免内）
4. 后续如果加新 mixin（接 P1-2 / P1-5 等），应优先：
   - 抽 mixin 到独立模块（`chat_service_mixins.py` 或按 mixin 拆 5 文件）
   - chat_service.py 只留编排（`processMessage / processMessageStream`）
5. **触发拆分的硬条件**：chat_service.py ≥ 2001 行 OR chat_multistep.py ≥ 1001 行 OR 任意 mixin 超过 400 行

## Why（为何不轻拆）

- 文件 200-400 行典型 800 上限是用户全局规则；chat_service.py 1986 远超大值
- 但 N1 写的是"≤2000 行硬上限"，**与全局规则差异是项目级特批**（既符合项目硬约束，又在 ±50 行范围内）
- 现在 1986 ≈ 阈值 = 安全边缘。新加任何方法（特别是 P1 任务 mixin）会立刻触线
- **未来加 mixin 必须先在 plan 里 commit 行数预算**（不要让 chat_service.py 在 PR 中静默过 2000）

## How to apply

1. 任何 chat_service.py 改动前先 `wc -l backend/app/services/chat_service.py` 确认
2. 加新 mixin 时若 chat_service.py > 1950 ⇒ 必须同时给一个 mixin 抽文件
3. 计划中提到 chat_service.py 行数时**实测优先**，别信 plan 描述里的估算
4. P1/P2 任务里 chat_service.py 的改动必须给出 +/- 行数 delta

## 链接

- [[qa-system-chat-multistep-line-cap-exemption]] — 853→991 行豁免
- [[qa-system-chat-service-file-split]] — 既有拆分计划
- 计划源：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md` N1
