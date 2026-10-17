# N4 — Qwen 多步知识保留 + 任务模式路由策略 设计稿

> **状态**：🟡 OPEN-SSOT-CLOSED-BY-SPEC（SSOT 收口，spec-only，不实施）
> **继承**：[2026-10-04 qwen-multistep-uplift](../plans/2026-10-04-qwen-multistep-uplift.md) 2026-10-05 回滚后的知识沉淀
> **记忆**：[qa-system-qwen-multistep-uplift](~/.claude/projects/.../memory/qa-system-qwen-multistep-uplift.md)
> **SSOT 入口**：[remaining-work-ssot.md §N4](../../Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md)

---

## 1. 目的

承接 2026-10-04 交付、2026-10-05 回滚的 `feat-qwen-multistep-uplift` 经验，将
qwen-uplift 的 5 条非显而易见教训 + 当前路由现状 + 未来扩展边界，固化为一份
**可被后人索引的决策记录**。本 spec **不实施任何代码改动**，仅沉淀知识、关闭
SSOT 显式 ⏳ 项；后续如需"按任务匹配模型"路由策略，新建独立 spec/plan。

## 2. 范围与非范围

### 2.1 In scope（本 spec 落地）

- 决策记录：qwen-uplift 的 5 条非显而易见教训（per memory §1-5）
- 现状冻结：当前 `ModelRouterService` 的能力 + 约束（per §3）
- 红线写入：禁止后端覆盖用户 `modelId`（per `qa-system-no-model-override`）
- 路线图：`taskPattern` 路由策略的扩展触发条件（per §4）
- 残留代码标注：`step_subquestion_rewriter.py` 是 2026-10-04 uplift 产物，
  当前 0 import 引用，列入 P3 backlog 显式清理目标（**不在本 spec 处理**）

### 2.2 Out of scope（本 spec 不做）

- 新增 `taskPattern` 字段（属 §4 路线图触发后的独立 plan）
- 新增 `LlmConfig.capability_tags` 字段（同上）
- RouterService 选点扩展（同上）
- 死代码 `step_subquestion_rewriter.py` + 2 个 unit tests 的清理（属 P3 backlog）
- N4 手动续跑功能（PR #48 已合 main `5f447349`，SSOT 范围外，仅 cross-ref）

## 3. 现状冻结（as of 2026-10-17 main `aef0124e`）

### 3.1 ModelRouterService（`backend/app/services/model_router_service.py`）

```python
class RoutingContext:                          # frozen dataclass
    sessionId: str
    sessionCost: float = 0.0
    sessionTurnCount: int = 0
    priorModelId: int | None = None

class ModelRouterService:
    def selectModel(
        self,
        configs: list[Any],
        prompt: str,
        ctx: RoutingContext,
        required_capabilities: set[str] | None = None,   # ← 唯一"任务硬过滤"点
    ) -> Any: ...
```

**已有能力**：
- 加权随机 + 成本阈值熔断 + 会话亲和 + 累计成本降级
- P1-2 起：基于 `required_capabilities: set[str]` 的**能力硬过滤**（chat_service 在
  preamble 阶段按 `routing_layer` L1/L2 传不同 cap set）
- 不识别"任务模式"——`prompt` 仅用于 token 计数 + 成本阈值评估，**不参与模式分类**

**chat_service 集成点**（`backend/app/services/chat_service.py`）：
- line 863：`if dto.modelId is not None` → 跳过 router 直接加载（用户显式优先）
- line 892：`forcedModel = dto.modelId is not None` → 标记 forced，下游走 forced 路径
- line 113: `from app.services.model_router_service import ModelRouterService, RoutingContext`

### 3.2 qwen-uplift 残留（2026-10-04 交付、2026-10-05 回滚）

| 文件 | 状态 | 说明 |
|---|---|---|
| `backend/app/services/step_subquestion_rewriter.py` | 🟡 孤儿 | 模块完整存在，0 import 引用 |
| `backend/app/tests/unit/test_chat_multistep_rewrite_hook.py` | 🟡 孤儿 | 引用 `StepSubquestionRewriter`，被 pytest collection 加载但无 prod 集成 |
| `backend/app/tests/unit/test_step_subquestion_rewriter.py` | 🟡 孤儿 | 同上 |
| `backend/app/services/chat_multistep.py` | ✅ 已回滚 | grep `StepSubquestionRewriter\|QueryPatternRouter` 0 hit |
| `backend/app/tests/integration/test_multistep_qwen_uplift.py` | ✅ 已删除 | 不存在 |

**判定**：孤儿 3 文件是回滚不彻底产物，**不在本 spec 处理**，列入 P3 清理 backlog。

### 3.3 N4 手动续跑（cross-ref，非本 spec 范围）

PR #48 `5f447349` 2026-10-17 merged：
- 17 commits / 24 files / +1775/-90 / 31 tests
- 用户级"Qwen 失败后手动选 deepseek 续跑"——**用户驱动**，**不违反"禁止覆盖 user modelId"红线**
- sawTerminal 收尾 commit `89d31119`（4 参 resumeMultiStepRun 加 EOF 守门）
- spec: `docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md`

## 4. qwen-uplift 5 条非显而易见教训（决策记录）

> 直接来自 memory `qa-system-qwen-multistep-uplift.md` §1-5。**本 spec 的核心交付物**。

### 4.1 `_PipelineContext` 是 `@dataclass(frozen=True)`

`chat_helpers.py:368` 定义 `_PipelineContext` 为 `frozen=True`。任何试图直接赋值
`pc.selected = new_cfg` 的代码在生产环境会 raise `FrozenInstanceError`。
Fix Round 2 犯了这个错——测试之所以绿，是因为 `MagicMock` 允许任意属性赋值，
把 frozen enforcement 静默绕过了。

**如何应用**：替换 `_PipelineContext` 字段必须用 `dataclasses.replace(pc, selected=new_cfg)`。
路由 hook 改动 pc 后通过 5-tuple return 传播给 caller，caller 再解包更新局部变量。

### 4.2 路由 hook 必须放在 `plan_explicit` 早返回之前

`_resolveExplicitMultiStep` 约 line 109 有 `plan_explicit` 的 early return。B019 这类
含"第一步…第二步…第三步…最后汇总"的题目会被该 fast path 直接返回，完全跳过放
在方法末尾的 hook。

**如何应用**：新增路由/改写逻辑应放在 `_resolveExplicitMultiStep` 方法入口
（约 line 92），在调用 `await self._stepPlanner.plan_explicit(dto.question)` 之前。

### 4.3 下游读 `pc.selected`，不读 `dto.modelId`

`_executeMultiStep` line 662 用 `pc.selected` 做 LLM call config。虽然路由 hook 改了
`dto.modelId`，但若不同时改 `pc.selected`，实际 LLM 调用仍会用原模型。Fix Round 1 修了
dto 但没修 pc，导致 routing hook 虽然执行了但没效果。

**如何应用**：路由 hook 必须同时替换 `pc.selected`（用 `dataclasses.replace`）并
通过 5-tuple 返回给 caller。

### 4.4 `MagicMock()` 对 frozen dataclass 给出假绿

`MagicMock(spec=_PipelineContext)` 仍然允许任意属性赋值——它只阻止未定义属性访问，
不执行 frozen 约束。用 bare `MagicMock()` 测试 frozen dataclass 交互是无效的。

**如何应用**：测试涉及 `_PipelineContext` 的代码时，用真实的 frozen 实例（字段可以
是 mock 的，但结构必须是真实的）。见 `TestPcSelectedSwapRealFrozen` 模式。

### 4.5 Qwen step-3 NL2SQL 固有能力边界

`compare_by_location` 改写规则把 sub-question 改成"按 RCV_SITE_CODE 分组…"后，Qwen
生成的 SQL 仍可能因 `SUPPLIER_CODE` 等属性不在本体 schema 而被 plan validator 拒绝。
这是 Qwen NL2SQL 生成能力的固有限制，不是改写逻辑的 bug。遇到此类失败应走
step isolation 降级而非继续调优改写模板。

**如何应用**：对比类题优先靠路由 hook 走 deepseek（它有更好的 NL2SQL 理解），
改写 hook 是补充手段而非主力。

## 5. 路线图：什么时候考虑"按任务匹配模型"路由策略

> **触发条件**（任一满足才开新 spec/plan，不要 YAGNI 提前开）：
>
> 1. **用户报障 ≥ 3 次**：同一类题（如"对比/聚合/分类"）在当前默认 model 上反复失败
>    且切换到另一 model 后成功率显著提升（≥ 30% 差）
> 2. **新模型接入 ≥ 2 个**：本地 oMLX / Qwen / Deepseek 等 ≥ 2 个并存，且能力差异显著
>    （NL2SQL 强 vs 通用对话强 vs 工具调用强等）
> 3. **routing_metrics 数据驱动**：chat_service 的 `routing_layer` 维度能区分
>    "失败率 × 任务模式" 且差值 ≥ 2x

### 5.1 扩展时需要的新组件（路线图，不实施）

| 组件 | 责任 | 大致接口 |
|---|---|---|
| `TaskPatternClassifier` | 单轮 prompt → `TaskPattern` 枚举 | `classify(prompt: str) -> TaskPattern` |
| `TaskPattern` 枚举 | 任务模式分类 | `COMPARE \| AGGREGATE \| CLASSIFY \| EXTRACT \| GENERIC` |
| `LlmConfig.capability_tags` | 模型能处理哪些 pattern（DB 列 + memory cache） | `set[str]` |
| `RoutingContext.taskPattern` | 当前轮任务模式（frozen ctx 字段） | `TaskPattern` |
| chat_service 选点 | preamble 之后、selectModel 之前判定 `taskPattern` | 扩展现有 preamble 阶段 |

### 5.2 红线（任何扩展都必须遵守）

**禁止后端覆盖用户 `modelId`**（per `qa-system-no-model-override`）：

- `dto.modelId is not None` → **永远走 user-chosen 路径**，router 不介入
- `dto.modelId is None` → 走 taskPattern 选 default（user 接受降级）
- **绝不能**："按内容自动改派 user 已选的 model"——这违反用户对 model 选择的
  显式 control，是 P0 信任崩塌级 bug

> N4 手动续跑（PR #48）的"用户失败后手动选"模式是合规的，因为是 user-driven。

## 6. SSOT 收口要点

收口 P2 SSOT 显式 ⏳ 的 N4 唯一项：

1. **SSOT 状态变更**：`⏳ 独立 plan` → `✅ spec 入口 / 路线图触发条件明确`
2. **cross-ref 链接**：本 spec 路径写入 SSOT N4 段
3. **N4 手动续跑区分**（per SSOT N4 段已有说明）：本 spec ≠ PR #48，PR #48 是
   user-driven 功能（已合），本 spec 是 routing 策略级（未实施、待触发）
4. **P3 清理 backlog 新增**：`step_subquestion_rewriter.py` 孤儿 3 文件清理
   （独立 task list，不在本 spec 范围）

## 7. 实施计划

**本 spec 不写实施计划**。当 §5 触发条件满足时，新建独立 spec + plan（per
`superpowers:brainstorming` + `superpowers:writing-plans` skill 流程）。当前 spec
到此为止，SSOT 收口走 §6。

## 8. 链接

- qwen-uplift plan（已回滚）：`docs/superpowers/plans/2026-10-04-qwen-multistep-uplift.md`
- qwen-uplift memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md`
- qwen-uplift 残留 summary：`Harness/changes/feat-qwen-multistep-uplift/summary.md`
- N4 手动续跑 spec：`docs/superpowers/specs/2026-10-17-n4-qwen-resume-design.md`
- N4 手动续跑 PR：https://github.com/sunql/QA/pull/48
- SSOT 入口：`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md`
- 记忆红线：[qa-system-no-model-override](~/.claude/projects/.../memory/qa-system-no-model-override.md)
- 记忆 chat 模型选择器：[qa-system-llm-model-selector](~/.claude/projects/.../memory/qa-system-llm-model-selector.md)
