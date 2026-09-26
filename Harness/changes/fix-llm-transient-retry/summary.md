# 变更：fix-llm-transient-retry

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：评估剩余项批次 **Phase B**（M4）
- **状态**：done
- **关联变更**：同批 [fix-chat-disconnect-persistence](../fix-chat-disconnect-persistence/summary.md)（同属「失败路径的计量诚实性」）、[fix-chat-retry-failure-details](../fix-chat-retry-failure-details/summary.md)（M7 二次失败详情+用量，消费本项复用的用量通道）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M4** + §3（评估日期 2026-09-25；行号更正见 §5）
- **commit**：`dfd8932`

---

## 1. 需求

两件事，第二件比评估原文更严重：

1. **瞬态故障（429 / 503 / timeout / connection reset）直接烧掉一次模型降级**：`generateQueryPlan` / `generateSql` 的 `for attempt` 循环里，一次可恢复的抖动就要付「换模型」的代价（新的 prompt 形状、可能更低的质量、额外的两套用量计费）。
2. **逃逸异常静默丢弃已累加的用量**：`NL2SQL_MAX_RETRIES=2` 下的反例 —— 第 1 轮 `complete()` 成功、`(3000, 500)` 已累加到局部变量 → 解析失败 `continue`；第 2 轮 `complete()` 抛 `LlmClientError` ⇒ 循环内无人捕获 ⇒ 逃出 `generateQueryPlan`，末尾本该抛出的 `Nl2SqlError(..., tokens=(totalPrompt, totalCompletion))` **根本到不了** ⇒ **3500 token 凭空消失**，既不进计量台账也不进审计行。这是「已测得的数据被丢」，与 H4 的「本来就没有」不同。

用户口径（binding）：**同模型瞬态重试放在 `generateQueryPlan` / `generateSql` 的现有 `for attempt` 循环内**。

**验收标准**：① 首轮瞬态失败 → 在同一模型上多试一次，成功即不换模型；② 非首轮不额外重试（预算）；③ 永久错误（401/403/400）不重试、立即上抛；④ **逃逸异常必须带上此前已累加的 `(totalPrompt, totalCompletion)`**（不是 0）；⑤ 判定/退避/用量通道只有**一套**实现（SSOT），不新增第二套。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 在 `chat_service` 的降级层加重试 | 否决：降级层已经要换模型，重试语义不同（用户明确要求放在 nl2sql 的循环内）；且会与 `_callWithRetryBackoff` 的既有契约（**任何** `LlmClientError` 都重试，由调用方过滤）纠缠 |
| B | **新增叶子模块 `llm_retry_policy.py` 承载判定/退避/用量通道，nl2sql 循环内调用 `completeWithTransientRetry`**（选定） | 一根 SSOT 同时被三处消费（chat 降级 / nl2sql 瞬态重试 / 逃逸用量），且**无循环依赖**：叶子模块只 import domain 层 |
| C | 独立装饰器包在 `llmClient.complete` 上 | 否决：装饰器藏掉了「重试几次、花多少钱、为什么重试」，与本项要修的可观测性相悖；且无法把「已累加用量」交给调用方 |
| D | 照原计划「失败尝试的 token 也累加」 | **否决（压测）**：`LlmClientError` 只带 `message/provider/detail`，**不带 token**；`openai_client` 也只把用量挂成功响应，三处失败抛出都不带用量 ⇒ **失败尝试的 token 在源头就不存在**，写进去就是假账 |

### 成本边界（按压测改为「仅首轮允许额外一次」）

原设计「每轮 2 次 × (maxRetries+1)=3 轮 = 最坏 6 次」在 429 风暴下**正撞节流窗口**。改为 `allowRetry = (attempt == 0)`：

- 最坏调用数 = `(maxRetries + 1) + 1 = 4`（默认），`maxRetries=0` 的两处恒为 2；
- **沿用同一套 backoff**（`RETRY_WAIT_EXPONENTIAL` 1s~4s），不手搓第二条指数序列 —— **一个库两条重试策略才是 M4 的真风险**；
- 依据：瞬态错误本就会由模型回退层再试（`chat_service` 对回退模型调 `callWithRetryBackoff`），内层重试的**新增价值**只是「先在同一模型上再试一次，再付切换代价」，而首轮（最便宜的 prompt）收益最大；
- `generateSql` 的截断扩容（`maxTokens` 翻倍）是每轮调用前算一次，内层重试不会打乱它。

### 判定口径（保持保守，不收紧既有行为）

`isRetryableLlmError` **默认 True**（兼容既有 fallback 行为：任何 `LlmClientError` 都触发降级），仅当 `__cause__` 携带**确定性** 4xx `status_code`（401/403/400，**429 除外** —— 它是 rate limit，属临时）时返回 False。`Nl2SqlError` 一律可重试。

**既有契约原样保留**：`callWithRetryBackoff` 仍重试**任何** `LlmClientError`，可重试性由调用方过滤 —— 这条有测试钉死（`test_fallback_backoff.py::TestCallWithRetryBackoff`），搬移时**不顺手改成策略感知**。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `app/services/llm_retry_policy.py`（新增叶子模块，243 行） | `isRetryableLlmError` / `consumedTokens` / `attachRetryGenTokens` / `attachRetryGenTokensIfAbsent` / `retryGenTokens` / `callWithRetryBackoff` / `completeWithTransientRetry` / `RETRY_MAX_ATTEMPTS` 等常量 |
| **依赖方向** | 该模块**不得** import `chat_service` / `nl2sql_service`（`chat_service → nl2sql_service`，反向即成环）；只允许 import domain 层。探针以 AST 复核实际 import 集合 = `{__future__, app.domain.exceptions, collections.abc, logging, tenacity, typing}` |
| `chat_service.py` | 判定/退避/用量通道改为**委托**，保留同名薄壳（`_isRetryableLlmError` 有 11 处测试引用；`_consumedTokens` 是 `@staticmethod`，被多处 `self.` 调用）；`_callWithRetryBackoff` 整体搬进新模块，`chat_service` 反向 import |
| `nl2sql_service.py` | 两个 LLM 调用点（plan / sql）包成 `completeWithTransientRetry(...)`，`allowRetry=attempt == 0` |
| 逃逸用量通道 | **复用**既有 `_RETRY_GEN_TOKENS_ATTR="…"` 属性通道（`attachRetryGenTokensIfAbsent(exc, consumedTokenCounts)`），**不发明第二套** |

**重试不写入 `errors`**：否则外层 prompt 被重试噪声撑大（本项只改重试，不改 prompt 构造）。

## 5. 实现要点

| 位置 | 改动 |
|---|---|
| `app/services/llm_retry_policy.py`（新增） | 常量 + 三组纯函数 + `_retryWithBackoff`（tenacity 包装，两入口共用）+ `callWithRetryBackoff`（旧契约）+ `completeWithTransientRetry`（M4 入口；`except` 里 `attachRetryGenTokensIfAbsent` 后再 `raise`）+ `_retryableAndLog`（**真要重试时**记 warning —— 重试成功与否都留痕，否则「为什么多花一次钱/一次延迟」在日志里无从查证） |
| `chat_service.py` | 净 **-129 行**（本地实现改为委托 + 文件头注释指向 SSOT） |
| `nl2sql_service.py` | 两个调用点（`:1695`、`:2157`）+ import；`allowRetry=attempt == 0` |
| `app/tests/unit/test_llm_retry_policy.py`（新增 18 例） | 判定口径 / 退避参数单源 / 用量通道 / 两入口语义 |
| `app/tests/unit/test_nl2sql_transient_retry.py`（新增 11 例） | 循环内行为：首轮重试成功、非首轮不重试、永久错误不重试、逃逸异常带用量 |

**行号更正**（评估文档原文 `nl2sql_service.py:1608`、`:2039` 已过期）：实为 plan 调用点 `:1695`、sql 调用点 `:2157`（本批已写回评估文档 §2.3）。

## 6. 测试

- **单元 18 + 11 = 29 passed**（`test_llm_retry_policy.py` 18、`test_nl2sql_transient_retry.py` 11）。
- **关键 RED 用例（实现前必失败）**：第 1 轮成功累加 `(3000, 500)` + 第 2 轮抛 `LlmClientError` ⇒ 逃逸异常上的 `retryGenTokens` 必须是 `(3000, 500)` 而非 `(0, 0)`。
- **容器内真机探针（`/tmp/probe_m4.py`，12 项全 PASS，2026-09-27）**：用假 client 控制失败序列，验证**部署物里的真实代码路径**（不是单测替身）：

  ```
  PASS  M4 叶子模块无反向 import（AST）  | imports=['__future__', 'app.domain.exceptions', 'collections.abc', 'logging', 'tenacity', 'typing']
  PASS  M4 公开面齐备 / 退避参数单源（RETRY_MAX_ATTEMPTS=2）
  PASS  M4 nl2sql 两个调用点都走同一入口 / 重试预算 = 仅首轮（allowRetry=attempt == 0 ×2）
  PASS  M4 chat_service._consumedTokens 委托 SSOT（读到逃逸异常上的累计用量）  | (3000, 500)
  PASS  M4 瞬态失败 → 同模型重试一次成功  | calls=2
  PASS  M4 allowRetry=False → 不重试、立即上抛  | calls=1
  PASS  M4 逃逸异常携带已累加用量（3000/500 不丢）  | calls=2 tokens=(3000, 500)
  PASS  M4 401 判定为不可重试 / 永久错误不重试（calls=1）
  PASS  M4 429 仍判定可重试
  ```

- **探针自身的三处断言缺陷（首跑 1 FAIL，属探针 bug 而非产品缺陷，如实记录）**：`"chat_service" not in src` 是**子串**匹配，把「本模块**不得** import chat_service」这条文档注释判成了违规 ⇒ 改为 **AST 取真实 import 目标**后 PASS。同一根因（把「文档提到」当成「代码在用」）在 M9 探针里也出现了一次（见该 SSOT §6）。
- **不做的事**：失败尝试的 token 累加（源头不存在）；重试写入 `errors`；自建第二条退避序列。

## 7. 安全审查

**未触发**：不涉及认证、密钥、SQL 构造、用户输入处理、加解密、支付。本项**减少**了两类风险：① 逃逸异常丢用量（计量完整性）；② 429 风暴下的调用放大（资源耗尽面）。重试上限有明确常量与预算口径，不存在无界重试。

## 8. 部署验证（2026-09-27）

- **部署**：`./scripts/deploy_backend.sh`（含 `alembic/`；本项无迁移）；仓库 ↔ 容器 md5 `app/services/llm_retry_policy.py`、`app/services/nl2sql_service.py`、`app/services/chat_service.py` **MATCH**（本批 22/22 现存文件 MATCH）；
- **容器内真机探针 12 项全 PASS**（输出见 §6），其中包括对**部署物源码**的 AST 复核与真实模块调用；
- **诚实边界**：**没有**「在生产环境故意注入 429」的探针（不可能也不该做）—— 瞬态重试的端到端验证靠单测（29 例）+ 容器探针的假 client 失败序列 + 真实模块装载；未做的是「真实供应商 429 的线上表现」。
- **测试**：全量 unit+services **2 failed, 2522 passed, 1 skipped**（两条为**预存**失败，`git worktree add --detach` 在 `HEAD~1` 复现同法判别，delta = 0）；集成切片 120 passed + 3 例环境耦合（导出 `DATABASE_URL` 后 3/3 通过）；
- **静态检查**：本批 22 文件 ruff 与基线逐行一致（唯一 F841 为基线既有）；新增 2 个测试文件 `All checks passed`；
- **网关**：`/api/v1/health` 直连 8000 与经 nginx 5173 均 200。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M4（标 ✅，含行号更正）、§15 批次记录
- 代码契约：`app/services/llm_retry_policy.py`（判定/退避/用量通道 SSOT）、`app/tests/unit/test_llm_retry_policy.py`、`app/tests/unit/test_nl2sql_transient_retry.py`
- 既有契约（不得回退）：`app/tests/unit/test_fallback_backoff.py`（`callWithRetryBackoff` 重试任何 `LlmClientError`，由调用方过滤）
- 规则：根 `CLAUDE.md` 核心约束 #3（Token 计量）、#5（小文件/单一职责）、`Harness/rules/编码规范.md`
- Memory：`qa-system-*` 新增 M4 条目（重试预算口径 + 「逃逸异常丢已累加用量」+ 叶子模块防成环）
- 同批：`../fix-chat-disconnect-persistence/summary.md`（失败路径计量诚实性同题）、`../fix-chat-retry-failure-details/summary.md`（M7 消费同一用量通道）

## SSOT 校验清单

- [x] 第 1 段 需求：两个缺陷（瞬态抖动烧掉降级 + 逃逸异常丢 3500 token 的反例）+ 用户口径 + 5 条验收标准
- [x] 第 2 段 4 个候选方案（含被压测**否决**的 D「失败尝试 token 累加」）+ 成本边界推导 + 判定口径与既有契约保留
- [x] 第 3 段 无迁移
- [x] 第 4 段 接口契约：新模块公开面 + **依赖方向禁则**（AST 复核）+ 薄壳保留 + 复用既有用量通道（不发明第二套）
- [x] 第 5 段 实现要点逐位置表（含 `chat_service` -129 行）+ 评估文档行号更正
- [x] 第 6 段 测试：29 例 + 关键 RED 反例 + **容器真机探针 12 项实录** + 探针自身断言缺陷的如实记录
- [x] 第 7 段 安全审查：未触发 + 本项减少的两类风险
- [x] 第 8 段 部署验证：md5 MATCH + 探针 + **诚实边界**（无线上 429 注入探针）+ 全量测试与预存失败判别 + ruff delta 0 + 网关 200
- [x] 第 9 段 跨文件链接（评估文档 / SSOT 模块 / 既有契约测试 / 规则 / Memory / 同批）
