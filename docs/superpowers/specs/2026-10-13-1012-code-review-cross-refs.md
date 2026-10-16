# 1012 三份代码审查交叉引用与统一 P0 清单

> 状态: Draft（aichat-priority plan N2 留口子落地）
> 编写: 2026-10-16
> 关联 plan: `docs/superpowers/plans/2026-10-13-aichat-service-priority.md` N2

## 背景

2026-10-12 前后 1 周内并行产出 3 份代码审查，每份单独跑出 P0/P1 清单但**互不交叉引用**。
本 spec 把三份审查放在一起比对：

| 报告 | 路径 | 范围 | commit 基线 | 状态 |
|---|---|---|---|---|
| QA-1012-01 | `docs/QA-1012-01.md` | 认证/模型/数据源/文档 | `fabd487d` | 已合 main |
| AIChat Service | `docs/AIChat Service 功能、兼容性与完整性分析.md` | chat_service + 9 mixin + 意图 + NL2SQL + 流式 | `fabd487d` | 已合 main（aichat-priority plan） |
| 1012 研究会话 | `docs/1012研究会话--代码分析与优化建议.md` | `/research` 独立研究状态机 | `bf0e0bd` (feat/multi-step-persist) | **未合 main**（独立分支） |

## 三份报告的 issue 互相对照

### 已合并报告（main）

#### QA-1012-01 (6 issues, 全部针对 `dependencies.py / factory.py / openai_client.py / auth.py`)

| # | 标题 | 严重度 | 修了吗？ |
|---|---|---|---|
| 1 | 普通用户可管理全局模型配置 + 凭据外发 | 高 | ⚠️ 部分（rateLimitDep 公开化 PR #28 `ec0570b0`） |
| 2 | 生产鉴权误配置只告警不阻止 | 高 | ❌ 未修（config.py 启动检查仍只 log） |
| 3 | must_change_password 无服务端限制 | 高 | ❌ 未修（依赖 utils.dependencies 没改） |
| 4 | 登录失效返 403 前端保留 token | 中 | ❌ 未修（client.ts 拦截器未区分 401/403） |
| 5 | 模型配置更新后缓存客户端用旧端点+key | 中 | ❌ 未修（factory.py 缓存键只按 id） |
| 6 | 密码长度校验与 bcrypt 5 不兼容 | 中 | ❌ 未修（auth.py:17 schema + password_policy.py:24） |

#### AIChat Service (12 issues, 全部针对 chat_service / mixin / 流式)

| # | 标题 | 严重度 | 修了吗？ | aichat-priority 计划中？ |
|---|---|---|---|---|
| 1 | 普通 vs 流式入口不对齐 | P0 | ✅ P0-1 | ✅ |
| 2 | 短追问/问候误分类 | P0 | ✅ P0-2 | ✅ |
| 3 | SessionQueryState 不绑定数据源/用户 | P0 | ✅ P0-3 | ✅ |
| 4 | 会话归属守卫 | P3 | ✅ 3759d85 (pre-existing) | ✅ |
| 5 | 快捷 SQL 改写 quote-unaware | P1 | ✅ P1-1 | ✅ |
| 6 | KPI 用固化 FeatureDefinition | P0 | ✅ P0-1 (合并项) | ✅ |
| 7 | 模型能力路由与流式 usage | P1 | ✅ P1-2 | ✅ |
| 8 | JSON/数组/Decimal/date 序列化 | P0 | ✅ P0-4 | ✅ |
| 9 | 流式绑定会话/轮次 | P1 | ✅ P1-3 | ✅ |
| 10 | 多步依赖不显式 | P1 | ✅ P1-4 | ✅ |
| 11 | 假设自动验证 / 证据回放 | P1 | ✅ P1-5 | ✅ |
| 12 | 预算/错误分类不闭环 | P2 | ✅ P2-1 | ✅ |

**12/12 全部合入 main**。已合验证时间 2026-10-14 ~ 2026-10-16。

### 未合并报告（独立分支）

#### 1012 研究会话 (12 issues + 建议落地顺序)

| # | 标题 | 严重度 | 修了吗？ |
|---|---|---|---|
| 1 | 检查点恢复会丢失本轮研究问题 | 高 | ❌ 未合 main（仅在 feat/multi-step-persist） |
| 2 | 假设生成和报告解读缺少实际业务数值 | 高 | ❌ 同上 |
| 3 | "SQL 无错误"被标为"假设已验证" | 高 | ⚠️ 部分（main 上 P1-5 Hypothesis.verify 修了） |
| 4 | 计划修改分支存在运行时 NameError | 高 | ❌ 同上 |
| 5 | 会话未限制同时运行 | 高 | ⚠️ 部分（main 上 `qa-system-research-claim-invariant-create-only` 修 claim 但 resume 不修） |
| 6 | 后续轮次 SSE 生命周期不完整 | 高 | ❌ 同上 |
| 7 | 多进程和重启兼容 | 中 | ❌ 同上 |
| 8 | 数据类型和数据源兼容需边界统一 | 中 | ⚠️ 部分（main P0-4 _jsonSafe + Decimal/date） |
| 9 | 语义确认应改变执行 | 中 | ❌ 同上 |
| 10 | 报告保留基础查询 + 证据回放 | 中 | ⚠️ 部分（main P1-5） |
| 11 | 研究模式和跨会话比较 | 低 | ❌ 同上 |
| 12 | 模型协议/成本/恢复策略 | 低 | ⚠️ 部分（main P1-2 模型能力 + P2-1 错误分类） |

## 跨报告重叠

研究会话和 AIChat 在以下 issue 重复（两边都看出来的同一根因）：

| 主题 | AIChat # | 研究 # | 根因 |
|---|---|---|---|
| 假设"已验证"误判 | 11 | 3 | `outcome.error is None == verified` 是 1012 时刻的 main 行为；两边都指出 |
| 数据类型边界（Decimal/date/UUID） | 8 | 8 | 跨两边都要在序列化层处理 |
| SSE 生命周期 | 9 | 6 | 流式收尾、CRLF 跨块、EOF 守门 |
| 会话状态归属 | 3 | 5 | 跨数据源/用户的 state 串号 |

**根因分析**：
1. **两边都缺"会话互斥"** — AIChat `assertSessionOwnership` (✅) 但研究 `running ⇒ 持 claim` 只在 createTurn（❌）
2. **两边 SSE 都不完整** — chat 流式 + research 流式是两个独立实现，bug 模式重叠但修法不互通
3. **两边序列化都漏 NaN/Decimal** — 1012 报告同时点出

## 统一 P0 清单（跨三份）

按 ROI 排序，**1 个 P0 项对应 1 个 cross-cutting 修复**：

### P0-X1: 模型配置管理权限 + 凭据保护（来自 QA-1012-01 #1）
- 现状：rateLimitDep PR #28 把限流解了，但**写入接口未要求 model_admin 角色**（factory.py:104 + model_config_service.py:69）
- 关联：AIChat #7 模型路由 / P1-2 `ModelCapability` 过滤 — 但**admin 鉴权**未在 service 层加
- 修法：在 `model_config_service.create/update` 加 `requires("model:admin")` 装饰器；端点变化时清缓存（QA-1012-01 #5）

### P0-X2: must_change_password 服务端强制（来自 QA-1012-01 #3）
- 现状：dependencies.py:202 只查 `is_active`，不查 `must_change_password`
- 修法：`getCurrentUser` 短路为 403 if `must_change_password and not _isPasswordChangePath(request.url.path)`
- 关联前端：RequireAuth.tsx 路径匹配有 bug（前端 5 红即修）

### P0-X3: 检查点恢复保留本轮研究问题（来自研究 #1）
- 现状：研究分支 `bf0e0bd`；main 无研究平台（`qa-system-research-line-not-merged-to-main`）
- 修法：在 `ResearchCheckpoint` schema 加 `originalQuestion: str`；恢复时优先用 checkpoint 而非 turn.content
- 关联：研究 +5 / +6 都在同一持久层

### P0-X4: 流式 CRLF + EOF 守门（chat + research 共修）
- 现状：chat 流式 P1-3 已修（chatStore.ts）；research 流式未修
- 修法：抽 `stream_chunk_parser.ts`（chat 已有）到共享库 `frontend/src/stores/streamChunkParser.ts`；research 也用

### P0-X5: 会话归属 + 状态互斥（chat + research 联合）
- 现状：chat `assertSessionOwnership` (✅)；research claim 互斥只在 createTurn (❌)
- 修法：抽 `claim_invariant` 装饰器到 `app/services/session_claim.py`；chat + research 共用

## 统一 P1 清单（节选）

| 主题 | 来源 | 优先级 |
|---|---|---|
| 401 vs 403 区分（前端拦截器） | QA-1012-01 #4 | P1 |
| 模型缓存键纳入版本 | QA-1012-01 #5 | P1 |
| 密码长度校验与 bcrypt 5 | QA-1012-01 #6 | P1 |
| 研究模式跨会话比较 | 研究 #11 | P2 |

## 落地建议（不在本 spec 范围）

1. 短期：开 5 个独立 branch（`fix/qa-1012-p0-x1 ~ x5`）独立 PR
2. 中期：建 `app/services/session_claim.py` 抽 chat + research 共用会话互斥
3. 长期：把 3 份报告的所有 issue 一次性归到 `Harness/changes/cross-1012/SSOT.md` 用 SSOT 模式跟踪（参考 `qa-system-phase3-reconciled-main`）

## 链接

- 计划源：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md` N2
- [[qa-system-research-line-not-merged-to-main]] — 研究线不在 main
- [[qa-system-rate-limit-dep-implicit-auth]] — PR #28 已改 rateLimitDep
- [[qa-system-router-auth-mandatory]] — read-only 必须 `Depends(getCurrentUser)`
- [[qa-system-research-claim-invariant-create-only]] — 研究互斥只覆盖 create
- [[qa-system-stream-chunk-parser]] — 共享 CRLF 解析（chat 已有）
- 上一轮：[[qa-system-phase3-reconciled-main]] — 6 PR cherry-pick 落地

## 待办

- [ ] 跑 security-reviewer 跨 9 文件（已派 agent，结果写入 `Harness/changes/aichat-priority-remaining-2026-10-16/security-review-report.md`）
- [ ] 修 chatStore.test.ts 8 红（plan P0-3/P1-3 行为漂移）
- [ ] 修 chatKpiWarning / chatApi / ChatPage 共 9 红
- [ ] 把上述 P0-X1 ~ X5 排进 `Harness/changes/` 准备开 5 个独立 branch
