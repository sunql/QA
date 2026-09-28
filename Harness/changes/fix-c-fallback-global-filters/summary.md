# 变更：fix-c-fallback-global-filters

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 Chat 服务（追问级联 × 多步全局过滤）
- **状态**：done
- **关联变更**：[feat-follow-up-cascade](../feat-follow-up-cascade/summary.md)、[feat-multistep-global-filter](../feat-multistep-global-filter/summary.md)、[fix-multistep-failure-isolation](../fix-multistep-failure-isolation/summary.md)
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §3 P0 第 5 项（当时为「P0 唯一剩余项」）
- **MEMORY**：[qa-system-follow-up-cascade.md](../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-follow-up-cascade.md)

---

## 1. 需求

C 兜底分支（短句不可回答 → 升级追问重试）命中后，经 `_prepareFollowUpMultiStep` 走多步重跑的路径
**没有传 `global_filters`**，而 B 分支（FOLLOW_UP 且上一轮多步）传了。非流式与流式**两侧同样缺失**
（对称缺口），于是同一段多步执行代码因入场点不同而行为不同。

用户可见后果：上一轮是「第一步…外购+内贸+境内；第二步…」这类带跨步口径约束的多步问题时，
一句省略式追问（"4月份呢？"）若先进了 C 兜底而不是 B，则改写后重跑的**每一步 SQL 都丢掉**
`[global_constraints]`（外购/内外贸/站点/物料类别等口径），答案口径与上一轮不一致且无任何报错。

验收标准：C 兜底经多步重跑时，与非流式 B / 流式 B **同口径**按**改写后的问题**抽取一次全局约束并
注入每一步 plan prompt；抽取 token 如实落台账（核心约束 #3）；抽取失败仍不阻断多步执行。

## 2. 设计评审

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 各调用方自取（现状） | B/C × 非流式/流式 四个入场点各自 `gf2 = await self._resolveGlobalFilters(session, dto2, pc)` 再传下去 | ❌ **这正是缺陷成因**：手传参数靠人记住，已经漏过两次（C1/C2 流式那次、本次 C 兜底两侧）。下一个入场点还会漏 |
| B 收敛进共享前置 `_prepareFollowUpMultiStep`（**选定**） | 该 helper 已负责「改写 → 拆多步」，全局约束与改写后问题同源，一并产出并随 5 元组返回；四个调用点改为只透传 | ✅ 抽取点从 4 个降为 1 个，结构上不可能再漏；顺带消除「B 分支不再需要在 `_prepareFollowUpMultiStep` 之外重复调用」的隐式契约。<br>⚠️ 代价：helper 职责从「改写+拆步」扩为「改写+拆步+抽约束」（3 次 LLM 调用），已在 docstring 写明 |
| C 让 `_prepareFollowUpMultiStep` 原地改写 `dto2.metadata` 携带约束 | 把约束塞进 dto 的某个字段随 dto 流动 | ❌ 契约变隐式（`ChatRequest` 承担了不属于它的语义），且要改 Pydantic 模型与前端契约面 |

选择 B 的理由是**消因不消症**：本缺陷的根因不是「漏写了一行」，而是「同一份多步前置知识被复制到 4 个
入场点、靠人逐个记得传参」。把知识收敛到 SSOT，才能防止第三个入场点再漏。

## 3. 数据模型变更

无。

## 4. 接口契约变更

对外 HTTP API 无变更。内部私有方法签名变更：

| 方法 | 变更 |
|---|---|
| `ChatService._prepareFollowUpMultiStep` | 返回元组由 4 元组 → **5 元组**：追加 `GlobalFilters \| None`（末位）；docstring 补「全局约束在此抽取」及原因 |
| 4 处调用点 | 非流式 B（`_handleGenericQuery`）/ 非流式 C / 流式 B / 流式 C（`_streamQuery`）统一解包 5 元组 + 透传 `global_filters=gf2` |

C 分支此前完全不传 → 现在传；B 分支此前在 helper 外单独抽 → 现在随 helper 一起拿到（**抽取次数不变，仍为 1 次**）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/chat_service.py` | `_prepareFollowUpMultiStep` 内新增 `globalFilters = await self._resolveGlobalFilters(session, dto2, pc)` 并随返回值交回；4 处调用点删除各自的 `_resolveGlobalFilters` 调用、改解包 5 元组、C 两处补 `global_filters=gf2` |

不变式（有意保持）：
- 抽取发生在**改写成功且已拆出多步计划之后** —— 前三种失败分支（上一轮非多步 / 改写失败 / 拆不出多步）
  返回 None 时不做无谓的 LLM 调用，行为与修复前一致。
- `_resolveGlobalFilters` 失败降级返回 `None`，多步照常执行（仅靠 A 层措辞兜底）——不新增失败面。
- 抽取 token 由 `_resolveGlobalFilters` 内部落台账（`purpose="multistep_global_filter"`），不进本步
  `initial_tokens` 累计（与 B 分支修复前口径一致，避免一次抽取被计两次）。

## 6. 测试

用例先行（RED → GREEN），三个新用例覆盖「两侧 × 两个套件」：

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/integration/test_chat_follow_up_cascade.py` | `TestCascadeEdges::test_c_multistep_rerun_inherits_global_filters` | C→B 重跑的两步 plan prompt 均含 `[global_constraints]` 与 `INTER_COM_CODE`；提取器只调 1 次且输入是**改写后的问题**；台账有 `multistep_global_filter` |
| `tests/integration/test_multistep_global_filter.py` | `TestMultistepGlobalFilterCascadeFallback::test_c_fallback_multistep_inherits_global_constraints` | 同上（非流式，用该文件既有 fake） |
| 同上 | `...::test_stream_c_fallback_multistep_inherits_global_constraints` | 流式侧：无 error 事件 + DONE + 重跑各步含 `[global_constraints]` + 台账 purpose |

两处细节是有意为之：

- 只断言 `planCalls[1:]`：第 1 次计划调用是 C 首轮单步（不可回答），**本就不该**有全局约束块 ——
  若连它也断言，用例会退化成「只要有块就算过」的假阳性。
- 断言提取器输入含**改写后的问题**（而非用户原话「火星人口」）：这正是「按 dto2 抽取」与
  「按 dto 抽取」的区别，也是本缺口与「忘记传参」的分界。

RED 证据（2026-09-26，修复前）：

```
test_multistep_global_filter.py 2 failed, 8 passed
  E  AssertionError: 流式 C 兜底重跑的第 1 步 plan prompt 缺少 [global_constraints] 块
test_chat_follow_up_cascade.py 1 failed, 9 passed
  E  AssertionError: C→B 重跑的第 1 步 plan prompt 缺少 [global_constraints] 块
```

（三条都停在 `planCalls` 断言之前，即「3 次计划调用」已成立 ⇒ 失败的确实是约束注入，不是链路没走到。）

GREEN 证据：

```
test_multistep_global_filter.py + test_chat_follow_up_cascade.py  →  20 passed
相邻套件（multi_step / service_state / l1_routing / stream_api / model_routing_fallback）
                                                                  →  54 passed
全部 chat 集成套件（app/tests/integration/test_chat_*.py）         →  130 passed
```

## 7. 安全审查

`code-reviewer` 已跑（触碰 prompt 构造、LLM 计量与降级路径），结论 **APPROVE**：
**0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW**。

| 级别 | 项 | 处置 |
|---|---|---|
| LOW | 三个新用例只断言「重跑各步**有**约束块」，未加负向对照「C 首轮单步**没有**块」⇒ 未来若约束泄漏进单步首轮（过度注入）不会被发现 | **当场补**：三处各加 `assert "[global_constraints]" not in planCalls[0][1][1]`，用例仍全绿 |
| LOW | 窄路径上有两次独立抽取：L1.5 复合问题启发式（`:1051`，`_resolveExplicitMultiStep` 返回 None 时其抽取结果被丢弃）与 C 兜底（`:1956`，针对改写后问题）可能在同一请求里各抽一次 | **预存行为、非本次引入**，且两次是**不同问题**、各自独立计量（无重复计一笔，也无丢账）。记入 backlog，不在本次范围 |

核验结论要点（reviewer 逐项查证，非抽样）：

- 四个调用点全部改为 5 元组解包并透传 `global_filters=gf2`；`grep` 确认全仓仅此四处引用
  `_prepareFollowUpMultiStep`，无残留 4 元组解包；`_executeMultiStep` / `_streamMultiStep` 均接受
  `GlobalFilters | None`（默认 None）⇒ C 路径此前吃默认值的两处现在真正拿到抽取结果。
- **计量无变化**：每次 `_resolveGlobalFilters` 恰好一次 `extract_global_filters` + 恰好一行
  `multistep_global_filter` 台账；B（FOLLOW_UP）与 C（NEW_QUERY/QUERY）意图互斥 ⇒ 每请求至多执行
  一次 `_prepareFollowUpMultiStep`，不存在重复抽取或漏记。
- **降级不变**：抽取仍吞异常返回 None；`_prepareFollowUpMultiStep` 返回 None 时仍退回单轮
  FOLLOW_UP 注入，与修复前逐行一致。
- 用例断言的是可观测行为（prompt 内容 + 真实列名 + 提取器调用次数/输入 + 台账 purpose），
  回退生产改动即全红；`planCalls == 3` 这一条额外证明用例确实落在 C 路径（B 路径只有 2 次）。

## 7.1 附带修正（reviewer 之外的 lint 发现）

`chat_service.py` 的 `GlobalFilters` 在 5 处（含本次新增的返回注解）用作类型注解却**从未 import** ——
文件有 `from __future__ import annotations`，注解是字符串，故运行时无感，但 `get_type_hints()` /
FastAPI 解析这类注解会 `NameError`。已补进既有 `from app.domain.multi_step_plan import (...)`（同模块，
无循环导入风险），ruff `F821 Undefined name 'GlobalFilters'` 5 处全部消除。

> 同类隐患仍在（`StepResultRead` / `AgentLoopResult` / `KpiCatalog` 共 6 处，预存，不在本次范围）。

## 8. 部署验证

**未部署**（本次只做提交，用户未要求上线）。后端改动生效方式：

```bash
./scripts/deploy_backend.sh          # 一次灌 app/ + scripts/ + alembic/ + 快照（含 --rollback）
```

真机冒烟（需上一轮多步会话）：

```bash
# 1) 先问一个多步问题建立 state；2) 再发一条短句触发 C 兜底
curl -s -X POST http://localhost/api/v1/chat -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  -d '{"sessionId":"<sid>","question":"火星人口","datasourceId":1}' | head -c 400
# 期望：intent=multi_step，且各步 SQL 含上一轮的跨步 WHERE（如 INTER_COM_CODE）
```

> 无前端改动 ⇒ 不需要重建前端镜像。

## 9. 关联

- Wiki：`Harness/wiki/chat-service-assessment.md`（§3 P0 第 5 项 ✅ + §10 修复记录）
- Wiki：`Harness/wiki/frontend.md`（无关；本批不含前端）
- 关联变更：`../feat-follow-up-cascade/summary.md`、`../feat-multistep-global-filter/summary.md`、`../fix-multistep-failure-isolation/summary.md`
- Memory：`qa-system-follow-up-cascade.md`（追问级联 A/B/C 的 SSOT）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C 三案）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：已跑 code-reviewer（APPROVE，0 C/H/M + 2 LOW，LOW#1 已当场修）
- [x] 第 8 段给出部署命令 + 端点冒烟命令（结果如实标注未执行）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki：`chat-service-assessment.md` §3 P0 第 5 项已勾选并链到本文件
- [x] 至少 1 条 MEMORY 索引已添加 —— 复用既有 `qa-system-follow-up-cascade`（本批为其次级修复，不新增条目）
