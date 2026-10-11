# r3-p0-convergence — 研究分析 r3 收敛与 P0 修复

> 日期：2026-10-11 | 状态：done | 分支：`feat-research-report-r3`
> Plan: `docs/superpowers/plans/2026-10-11-r3-p0-convergence.md`（故意 untracked）

## 背景

`feat-research-report-r3` 上 3 例集成红灯 + 2 处 P0 缺口；r3 是报告质量层的超集。

分支谱系（本次实测，命令与结果均记录在案）：

```bash
git merge-base --is-ancestor feat/multi-step-persist HEAD; echo $?   # 0  → 是祖先
git merge-base --is-ancestor HEAD feat/multi-step-persist; echo $?   # 1
git merge-base --is-ancestor main HEAD; echo $?                      # 1  → main 不是祖先
git rev-list --count main..1523fd5                                   # 153（至 T4）
git rev-list --count main..HEAD                                      # 155（=153 + 本记录 2 提交）
git rev-list --count HEAD..main                                      # 1
git merge-base main HEAD                                             # c1857683dec4252f1a978738f6dc73f67d58fae5
git log --oneline -1 feat/multi-step-persist                         # 8ba43e5
```

- `feat/multi-step-persist`（tip `8ba43e5`）是 HEAD 的**祖先** ⇒ r3 是它的超集。
- **r3 的 tip**：至 T4 为 `1523fd5`；本变更记录自身贡献 **2 个提交**（`0608cfe` 记录初稿、
  `3fb6acc` 事实核对），故当前 `main..HEAD` = 155。把 r3 的 tip 说成 HEAD 的祖先不成立
  ——HEAD 就在 r3 上（本记录提交之后即为 HEAD）。
- `main` **不是** r3 的祖先：两者已分叉（r3 独有 155 个提交 = 至 T4 的 153 + 本记录 2，
  main 独有 1 个，共同祖先 = `c185768`）。

## 改动

- **T1** `research_agent_service.py`（`c42c182`）：补 `MAX_REPLAN_PER_TURN` 导入
  —— “改计划”路径此前命中 `NameError` 必崩；
  新增 AST 导入面守卫（`test_research_agent_ports.py`），**仅**钉住“被当作模块级名
  引用（`node.id.isupper()`，即全大写常量/伪常量）的名字必须可解析”——正是本次漏导入
  的形态；小写局部变量一类不在该守卫射程内（由审查人工兜住，测试未改）。
- **T2** `research_agent_phases._stageReport`（`c3029b8`）：计量落账移进 `finally`
  —— `compose` 逐块调 LLM，抛错时已消耗的 token 不再成为永久盲区（核心约束 #3）；
  失败子路径由同 commit 新增的
  `test_report_metering_survives_reporter_failure`（`test_research_agent_service.py:565`）
  覆盖：reporter 抛 `RuntimeError` 时异常仍上抛、报告不归档，但该轮 `research_report`
  消耗已入账（`promptTokens=120/completionTokens=40`）。
- **T3** `research_agent_ports.MAX_RESEARCH_DATA_STEPS = MAX_PLAN_DATA_STEPS`
  （= 4，派生而非第二个字面量）+ `research_agent_phases._capDataSteps`
  （`8cfbaa3`）：`normalizePlan` 截断超限**数据步**、原样保留 `aggregation_only` 汇总步；
  截断必须**有声**（`logger.warning` 留痕）+ 检查点展示截断后的计划。
- **T3 fix round**（`a7eecdb`，2026-10-11 用户裁定）：原计划逐字要求的反向守卫
  `test_normalize_plan_does_not_truncate_at_the_cap` 被审查证明**不可能失败** ——
  数据步 ≤ 上限时 keep-set 恒覆盖全部下标，截断版与未截断版结果逐字节相同，
  把 `<=` 改成 `<`（该测试本要抓的 off-by-one）仍然通过（非判别性假绿）。
  裁定改为**可判别**断言：`caplog` 断言“恰好等于上限 ⇒ 不得出现 `截断` 日志”，
  并加 **cap+1 正向对照**（`截断` 必须出现），证明捕获本身有效 —— 否则
  “无 warning”断言自身也是空的。
- **T4** `research_agent_phases._stagePlan`（`1523fd5`，2026-10-11 用户裁定）：
  `if replan or self._autoConfirm: return None` → `if self._autoConfirm: return None`
  —— replan 后**重新暂停**在计划点（用户看得到改后的计划）；
  迭代不再是死循环，因为 `resumeTurn` 的 `MAX_REPLAN_PER_TURN`（= 3）兜底：
  第 N+1 次 modify 按 confirm 推进。该常量由**死代码变为承重**。
- **T4 第三文件修复（裁定 D）**：`backend/app/services/research_agent_service.py`
  被加入 T4 允许文件，仅两处有界改动：
  - `:305` `nextPhaseForPhase(checkpoint.phase, options)` → `checkpoint.options or {}`
    —— 修掉 `90096f67`（2026-10-08）引入的**第二处死代码 `NameError`**：
    **T1** 让 `:299` 那条守卫可求值（此前 `MAX_REPLAN_PER_TURN` 未导入，守卫先抛
    `NameError`，`else` 永远不可达），**T4** 再让该 `else` 分支首次真正执行；
  - `:296` 陈旧括注改写（说明第 1..MAX 次 modify 会重新暂停）。
  - 已验证**未改动**：`rebuildState`（在 `research_agent_stages.py`，T1–T4 均未触碰该文件，
    见 `git show --name-only 1523fd5`）、`:299` 上限守卫
    （`if replan_count < MAX_REPLAN_PER_TURN:`）、`:302` 计数写入
    （`state["replanCount"] = replan_count + 1`）。

## 测试

- 集成 `app/tests/integration/test_research_agent_service.py`：3 red → 0 red
  （**55 passed / 21 warnings / 0 failed**），含 T2 新增的
  `test_report_metering_survives_reporter_failure`（reporter 抛错时计量仍落账）。
- 单元 `app/tests/unit/test_research_agent_ports.py`：24 passed（T1 导入面守卫 +
  T3 数据步上限含“恰好等于上限不截断”的 caplog 判别式反向用例 + cap+1 正向对照）。

## 真机走查（T5，2026-10-11）

重建共享 `qa-backend`（`docker compose build backend` + `up -d --force-recreate --no-deps backend`，
项目名 `qa-system`，镜像 `qa-system-backend:latest` @ `8c65b922f8d9`），启动干净
（`Application startup complete` + `Uvicorn running on http://0.0.0.0:8000`，
未命中 `uv run` 现装 `hatchling` 的崩溃坑）。走查全程走 **HTTP API**（未碰前端）。

会话 `99c8b606-3e58-4664-bb69-77b6d8357489`（`modelId=1`、`datasourceId=1`）：

1. **计划点 modify 可迭代（T1+T4）** —— 观察到的检查点序列：
   `intent(confirm)` → `planning rc=0 (modify)` → `planning rc=1 (modify)` →
   `planning rc=2 (modify)` → `planning rc=3 (modify)` →（无 planning，直接）
   `low_confidence_step(confirm)` → `hypothesis(confirm)` → `verify` → `done`。
   前 3 次 modify 每次都**重新暂停在 planning**（新 checkpoint id，`replanCount` 0→1→2→3），
   第 4 次 modify 命中上限分支并**自动推进**（后端日志：
   `replan 上限已达 3，按 confirm 推进: session=99c8b606-...` —— 即 `:305` 那条 T4 修复行）。
   新计划**反映了指令**：每次 replan 后计划步描述里带
   `[用户修改要求] {"instruction": "第 N 次修改要求：..."}` 原文。
2. **Token 计量有账（T2）** —— 报告落库后 `session_token_usage` 中
   `purpose='research_report'` 有 1 行：`prompt=1230 / completion=583 / total=1813`
   （报告 `renderedMd` 长 2783 字符，量级相称）。真机走查覆盖的是**成功路径**；
   报告装配中途抛错的**失败子路径**由
   `test_report_metering_survives_reporter_failure`（`c3029b8` 新增，本次 55 条全绿中）
   覆盖 —— 两者合起来才是 T2“消耗不再成为永久盲区”的完整口径。
3. **数据步被截住（T3）** —— 后端日志**未出现** `研究计划数据步超上限 4，截断 N 步`；
   本次会话可见计划恒为 **1 个数据步**（≤ 4）⇒ 与“守卫未触发”**一致**（非 4，故不是歧义态）。

## 遗留（未做，记录在案）

- 图表轴**维度取值**翻译（`C1`→基地 C1）另开任务，起点是给 categories 过 `resolveValueName`，
  **不要**碰 `SpecDimension.label`（渲染器从不读它）。
- 前端 `vitest.config.ts` 的 `coverage.include` 分母口径缺陷。
- Research 入口 A1 `selectedIndexes` 下标契约 bug。
- 数据步截断目前只有日志留痕，未走 SSE/options 告知用户。
- 走查观察（非 T1–T4 回归，仅记录）：`research_report` 行的 `model_name` 由 LLM **响应**回填
  （`MeteredClient.modelName` ← `response.modelName`），本次上游自报 `deepseek-flash`（不在
  `llm_config` 中）⇒ `cost` 落 0；`research_plan` 行则自报 `deepseek-chat`（`cost>0`）。
- 走查观察：`POST /checkpoints/{id}/answer` 响应里的 `nextPhase` 由 `nextPhaseForPhase`
  （`research_agent_ports.py:454`）产出——**优先**返回显式 `options["resumePhase"]`，
  未给时才回落 `NEXT_PHASE_BY_CHECKPOINT.get(phase) or PHASES[-1]`。planning 检查点的
  `options.resumePhase` 恒为 `"execute"`，故响应里的 `nextPhase` 也恒为 `"execute"`，
  **不反映** T4 的“modify 重新暂停在 planning”；实际相位以重新拉取的 `pendingCheckpoint`
  （它走 replan 分支重新生成）为准。
