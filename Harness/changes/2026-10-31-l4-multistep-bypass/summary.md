# 显式分步问题让路 L4 Agent Loop（PR #76）

- 日期：2026-10-31
- 分支：`fix/l4-explicit-multistep-bypass`（squash 合入 main `540e8ce2`）
- 类型：fix（后端路由）
- 部署：backend 镜像重建 + `up -d`，发版源 `/private/tmp/qa-deploy-1031-l4fix`

## 1. 症状

用户（2026-10-30 23:45 CST 起）：多步分析「直接出结果」，看不到 分析计划 → 查询计划 → 分步 SQL；单步问题反而正常。

## 2. 根因

流水线顺序 **L1 KPI → L4 Agent Loop → L2（单步/多步 NL2SQL）**。L4 拦截条件是朴素子串匹配 `_L4_EXPLORATORY_KEYWORDS`（chat_l4.py：为什么/怎么算/拆解/解释/如何/**是什么**…）。多步式问句「第一步…第二步…分别**是什么**」含「是什么」→ 整题被 L4 Agent Loop 截走：直接产出大段文本（无计划卡/无 SQL），单次 12~18 万输入 token（¥0.18–0.26），`max_iterations` 时答案中途截断。不含关键词的「分步分析：B019…」正常走 L2 拆步。

排查中排除的前端嫌疑（全部验证完好）：SSE 分发（chat.ts case multi_step_plan/step_plan/step_result）、chatStore 两组 handlers（首发内联 + resumeRun 共用 streamHandlers）、MessageItem 渲染条件、部署 bundle、真实 SSE 抓包（multi_step_plan payload 与前端过滤器逐字段匹配）。

## 3. 修复

`_handleNl2SqlAgent` 在关键词判定**之前**先过 `StepQueryPlanner.is_explicit_multi_step`（零 LLM 成本；与 L2 拆步入口同一把尺子）——命中即让路降级 L2。纯探索性问题行为不变。日志新增一行「显式分步问题让路 L4 Agent Loop，降级 L2 拆步」。

## 4. 测试

- 新增 `backend/app/tests/unit/test_chat_l4_multistep_bypass.py`（6 用例：3 让路 + 3 回归守卫），先 RED 后 GREEN
- `test_l4_agent_loop_metering` 4 passed（「为什么…」集成链路仍进 L4）
- `test_chat_stream_l1_l4` + `test_chat_multi_step` 40 passed（2 个 setup 错误为基线既有：BO_L1_STREAM seed NotNullViolation，stash 验证）
- `test_chat_service` 4 失败 / `test_chat_service_stream` 2 失败均为基线既有红（stash 逐个验证，净新增失败 0）

## 5. 生产冒烟

用被劫持的原始问法回放 `/api/v1/chat/stream`：
- 修复前：`meta/token/done`（L4 直接出结果）
- 修复后：`multi_step_plan` + `step_plan`×3 + `step_result`×2 + `token` + `done`
- 日志证据链：`显式分步问题让路…` → `拆步规则命中（第X步标号）` → 事件下发

## 6. 顺带发现（未修，待议）

1. **多步/单步历史回放不还原计划**：`ChatMessageRead` 无 steps/queryPlan 字段，multi_step 汇总消息落库只有汇总文本（routing_layer 还标成 L2，见 chat_stream_multistep.py:259）——刷新/重开会话后多步消息只剩文字。结构缺口，需立项。
2. **L4 agent loop 成本**：单次 12~18 万 token 来自「问题原文 + 工具结果全量回灌」，重复问同一分步问题 4 次 ≈ ¥1。让路修复后此类流量回多步管线（成本大幅下降），但纯探索问题的成本仍偏高。
3. **用户期望「单步也要分析计划」**：现契约单步只有 查询计划（QueryPlanCard，done 后显示）+ SQL。是否给单步加「分析计划」卡待与用户对齐。
