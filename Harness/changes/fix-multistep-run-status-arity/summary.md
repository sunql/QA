# 变更：fix-multistep-run-status-arity

- **日期**：2026-10-30
- **作者**：启琳
- **Phase**：done
- **状态**：done（本地验证绿；生产待重新构建后端镜像后复测）

## 1. 需求

**现象**：对合并后的 main 跑核心集成套件，10 个用例红：

- `test_chat_multi_step.py` 8 个流式用例：事件序列末尾是 `error` 而不是 `done`
- `test_route_auth_guard.py` 2 个用例：只枚举到 **1 条**路由（下限 300）

第一个是**真回归**：main 上的多步 chat 一到收尾就 500。

**根因 A（真回归）**：`runStatusFor` 的签名与调用点对不上。

- `multi_step_persist_hooks.runStatusFor(completed, total, anyFailed)` —— 3 参，
  由 PR #59（删除步间依赖脚手架 P0-2）收敛：L1 线性计划没有「跳过步骤」语义，
  `anySkipped` 恒为 False ⇒ `partially_failed` 分支删除。
- `chat_stream_multistep.py` 两处调用仍传 4 参
  （`completedCount, len(steps), anyFailed, anySkipped`）——该文件由 PR #54
  （chat_stream 5-mixin 拆分）从 chat_stream.py 切出，切分时基于的是 #59 之前的
  main。#59 与 #54 都改到了「同一个函数的语义」，但落点是**不同文件** ⇒
  git 无冲突、CI 无感知，两边各自绿，合起来才炸。

运行期报错：`TypeError: runStatusFor() takes 3 positional arguments but 4 were given`
⇒ 多步 chat 收尾直接抛错 ⇒ SSE 只剩 `error` 帧。

**根因 B（陈旧测试）**：鉴权守卫测试的路由展开逻辑写于 FastAPI 0.136.1
（`app.routes` 是 flat 的 APIRoute 列表）。锁文件已升到 FastAPI **0.141.1**
（`include_router()` 的结果被包成 `_IncludedRouter` 包装层）⇒
`isinstance(route, APIRoute)` 只能命中 app 上直接声明的少数路由 ⇒
枚举结果 1 条 ⇒ 守卫「空集通过」，安全断言形同虚设。

## 2. 设计评审

| 问题 | 方案 | 结论 |
|---|---|---|
| A：`runStatusFor` 参数不一致 | ① 恢复 4 参 + `anySkipped` ② 调用点收敛到 3 参 | **②**。#59 已定 SSOT（无 skip 语义），`anySkipped` 在调用点恒为 False ⇒ 语义等价，且不复活已删的分叉 |
| B：路由枚举失效 | ① `isinstance(route, APIRoute)` + 私有 `_IncludedRouter` ② 公开 API `iter_route_contexts()` | **②**。公开 API 双版本兼容（0.141 有、0.136 无 → try/except 兜底），不依赖私有符号 |

**验收标准**：多步流式用例末尾事件恢复为 `done`；鉴权守卫枚举到 ≥300 条路由
且**非白名单路由全部带 `getCurrentUser`**（守卫本身仍然承重，不是空集通过）。

## 3. 实施清单

- `backend/app/services/chat_stream_multistep.py`：两处 `runStatusFor(...)` 去掉
  第 4 参；删除随之失效的 `anySkipped` 局部变量（附注释指向 #59 的语义变更）
- `backend/app/tests/integration/test_route_auth_guard.py`：
  - `iter_route_contexts` 公开 API 优先（0.141），flat 分支兜底（0.136）
  - 拆出 `_hasCurrentUserInDependant(dependant)`，两条分支共用同一依赖链遍历
  - 断言改用**有效** dependant（含 router 级 dependencies）与**拼好的最终路径**
- `frontend/src/api/client.ts`：`normalizeError` 的兜底链补 `status === 0 ⇒
  i18n.t("errors.networkError")`（见 §5 连带修复）
- `frontend/src/api/ontology.ts`：`runOntologyBatch` 的 300s 字面量提取为
  `ONTOLOGY_BATCH_TIMEOUT_MS` 并导出（测试不再硬编码数字）
- `frontend/src/tests/{kpiCatalogApi,ontologyApi,client}.test.ts`：陈旧断言更新
- 本文档

## 4. 验收结果

- 后端核心套件（16 个文件：路由守卫 / chat api / stream api / multi-step /
  历史 / KPI 目录 / KPI 契约注入 / KPI 分母 / 发布闸门 / 前置 FK / 多步持久化
  单测）**全绿**
- 前端：`tsc -b` 0 错；全量 vitest **198/198 文件、1898/1898 用例绿**
- 真机走查（生产容器，跑的是 #70 之前的旧代码，故未触发根因 A）：
  THBI Oracle 上「采购订单有多少条」→ 72384；「按供应商分组 Top-5」→ hbar 图正常

## 5. 连带修复（同一批，根因不同）

- **`client.ts` 网络异常文案丢失**：10-28 的「surface backend diagnostic」重写
  `normalizeError` 时，把 `status=0`（无 HTTP 响应）的兜底从「网络异常，请稍后重试」
  换成了 axios 兜底 / `Unknown error`。toast 用的仍是友好文案，但**页面 catch 到的
  Error.message 变成了 `Unknown error`** ⇒ 同一次失败两套文案。已补回
  `status === 0 ⇒ errors.networkError`（与 toast 同源）。
- **陈旧前端断言 7 处**（`kpiCatalogApi` 1 + `ontologyApi` 5 + `client` 1）：
  源文件在 10-18 / 10-23 / 10-25~28 加了参数（unbound 查询、per-call 超时、
  错误归一化），断言仍按老签名写 ⇒ 红。已按当前源更新。

## 6. 遗留

- `frontend/src/tests/AgentRegistryPage.test.tsx` 仍有 1 个 **unhandled rejection**
  （`validateFields` 未 await ⇒ onChange 裸调）。修复已在 **PR #71**
  （`chore/consolidate-unmerged-1030`，含 `frontend/src/pages/AgentRegistryPage.tsx`）
  ⇒ 本 PR 不重复实现，等 #71 合并后自然消失。
- 生产部署：main 上的多步 chat 回归**当前仍在线**（生产镜像跑的是合并前的代码，
  侥幸未触发）。本 PR 合并后需重建后端镜像（`docker compose build backend`
  && `up -d backend`）并复测多步 chat。
