# fix-research-carryover — 研究分析 r3 收尾债：SSOT 消歧 + 计量真名 + 恢复决策单一化 + 接缝抽离

> 日期：2026-10-11 | 状态：done | 分支：`fix/research-carryover`
> Base：`feat-research-report-r3` @ `61986a9`（r3 冻结；**未**合并，决定权在人）
> Plan: `docs/superpowers/plans/2026-10-11-research-carryover-fixes.md`

## 背景

r3（`r3-p0-convergence`）的收尾审查留下了 4 条「已定位、未修」的遗留：M1 `replanCount` 双拼写、M2 计量记的是模型自报名而非配置名、M3 端点的 `nextPhase` 与实际恢复相位不一致、M4 `ports.py` 逼近 800 行上限。本支一次性清掉这 4 条，外加一件用户裁定「做真修」的图表维度标签改造（r3 里以 `xfail(strict=True)` 停放的用例）。

**这 4 条在 r3 的记录里都能对上原文**：r3 遗留写「`research_report` 行的 `model_name` 由 LLM **响应**回填……上游自报 `deepseek-flash`（不在 `llm_config` 中）⇒ `cost` 落 0」（= M2），以及「planning 检查点的 `options.resumePhase` 恒为 `"execute"`，故响应里的 `nextPhase` 也恒为 `"execute"`，**不反映** modify 重新暂停在 planning」（= M3）。

**共同形态：三条都是「消灭第二份真相」**——M1 读写两侧走同一个常量、M2 五个计量站点写同一个名字来源、M3 端点与后台状态机共用同一个纯决策函数。没有引入新模式。

分支谱系（本次实测）：

```bash
git rev-list --count 61986a9..HEAD                  # 本支独有提交
git merge-base --is-ancestor d9a78e5 61986a9; echo $?   # 0 → checkpoint_conflict 早于本支
git merge-base --is-ancestor faf0007 61986a9; echo $?   # 0 → enhancedClaim 早于本支
git rev-list --count 61986a9..HEAD -- backend/app/tests/unit/test_research_event_bus.py   # 0
git rev-list --count 61986a9..HEAD -- backend/app/tests/integration/test_research_hypothesis_rewrite_wiring.py  # 0
git rev-list --count 61986a9..HEAD -- backend/app/tests/integration/test_research_session_service.py           # 0
```

## 改动

### M1 `replanCount` 单一事实源（`f180de9`）

- **改前症状**：写侧用**裸字面量** `"replanCount"`（`phases.py` 的 `buildOptions(replanCount=...)` 经 `**payload` 进 options、`service.py` 的 `state[...] =`），读侧用常量 `OPT_REPLAN_COUNT`。两者**只靠拼写巧合**一致——将来谁改了常量值，写侧仍写老字符串 ⇒ 计数不再往返 ⇒ **3 次 replan 上限静默永不生效**，planning 点可无限 modify（每轮一次 planner LLM 调用），正是 T4 刚封掉的循环。`research_agent_phases.py` 已 `import` 了 `OPT_REPLAN_COUNT` 却只字未用（ruff **F401** 在报），是这条缺陷的化石证据。
- **改法**：读写两侧一律走 `OPT_REPLAN_COUNT`。计划比初稿多找到**第 5 处**：`research_agent_stages.py:64` 的 `rebuildState` 把 state 的**键**也直写成了字面量（读已走常量、键还直写；键与 options 键本就是同一个字符串）。共 5 处（`phases.py` 读+写、`stages.py` 键、`service.py` 读+写）+ 测试断言 2 处改用常量。`OPT_REPLAN_COUNT` 那条 F401 随之消失。
- **判别测试**：本任务**是行为中性重构，其判别是结构守卫**（不假装有行为型 RED）——`test_research_replan_count_ssot.py` 扫描 4 个消费者模块源码，任何引号包住的 `replanCount` 字面量即红（常量自己的定义在 `ports.py`，那里字面量是必须的，故选了白名单式的消费者清单）。行为护栏是既有的 `test_planning_modify_reruns_planner_with_choice` / `test_planning_modify_iterates_then_caps_at_max_replan`（上限在第 4 次 modify 生效、`replanCount` 随 checkpoint 持久化）。RED 命中 `research_agent_phases.py`，GREEN 1 passed；`-k "replan or modify"` 2 passed。

### M2 计量落**配置名**而非响应自报名（`22d707b`）

- **改前症状**：`MeteredClient.modelName` 会被 LLM **响应**里的自报别名覆盖，而定价表 `llm_config` 认的是**配置名** ⇒ 自报名查不到价 ⇒ `cost` 落 0（r3 真机走查已实测到）。
- **改法**：新增 `meteringModelName(client, metered=None)` = `clientModelName(client) or getattr(metered, "modelName", None)` —— 配置名优先，取不到才回落自报名（保留旧行为作兜底）。5 个计量站点统一改走它（plan 相位原本就正确，一并统一口径）。
- **判别测试**：测试用的 `AliasedLlmClient` 令 `_modelName="fake-model"` 而响应自报 `"gateway-alias"`，两者分叉。RED `assert 'gateway-alias' == 'fake-model'`（正是预期的失败因），GREEN 定向 5 passed。
- **注意（已入档）**：计划**刻意**要求「断言交给 recorder 的名字，不去 seed `llm_config`」——这样测的是「传了哪个名字」而不是「定价表里有没有」。审查独立复核确认该设计**非空转**：旧代码在这条测试上必红。但 5 处里只有 1 处有判别性覆盖，另 3 处见「遗留」。

### M3 端点与状态机共用纯恢复决策（`430c670`）

- **改前症状**：研究状态机跑在 FastAPI BackgroundTask 里，而 `POST /research/checkpoints/{id}/answer` **在后台任务之前**同步算 `nextPhase` ⇒ 两条路径各有一份「接下来去哪」的判断，planning + modify 时端点报 `execute`、真实机器却重新暂停在 `planning`（r3 遗留原文）。前端拿到的是**说不通的** `nextPhase`。
- **改法**：把判断抽成 `decideResume(checkpoint, action, choice) -> ResumeDecision(phase, isReplan)`（frozen dataclass）放进 `phases.py`，端点（`api/v1/research.py`）与 `resumeTurn`（`service.py`）共用；`rewrittenQuestion(choice)` 一并抽出。判序：`rewrittenQuestion` 优先（改写压过拒绝），reject/abort 映射交给既有 `nextPhase`，其余走 `nextPhaseForPhase(checkpoint.phase, checkpoint.options or {})`。**对非 reject 动作，`nextPhase` 与被删掉的 `else` 分支逐字等价**——这正是「共用同一个纯函数」是**精确**而非近似的理由。`replanCount` 的读取源从 `state` 换成 `checkpoint.options`，两者经 `rebuildState`（`stages.py:64`）+ `_stagePlan` 持久化（`phases.py:204`）可证同一。
- **判别测试**：单元 `test_research_resume_decision.py` 6 例（覆盖 6 个分支）；端到端判别在 `test_research_api.py` 新增一例，镜像既有 `..._double_submit_409` 的装配，`phase="planning"` + `options={"signal":"fixed_plan","resumePhase":"execute","replanCount":0}` + `POST {"action":"modify","choice":{"instruction":"只看华东"}}` —— RED 旧代码报 `nextPhase: "execute"`，GREEN 报 `"plan"`。反向护栏保留 `..._invalid_action_422` 与 `..._double_submit_409`，确认既有语义没被决策函数改写。

### M4 抽出模型解析接缝，`ports.py` 回到 600 行内（`7573220`）

- **改前症状**：`research_agent_ports.py` 794/800 行，逼近本仓硬上限（800），任何后续小改都可能越线。
- **改法**：把模型解析接缝（`buildClient` → `resolveClient`，原 `ports.py:541-734`，194 行）**逐字搬**到新模块 `app/services/research_agent_model_resolution.py`。**不留再导出 shim**——被搬走的 `markLlmUnavailable` 调 `ports.emitEvent` / `ports.errorPayload`，所以新模块必须 import `ports`；`ports` 再反向 import 新模块即为双向环，只在「shim 恰好在文件末尾」时靠执行顺序侥幸成立。依赖**单向**：`research_agent_model_resolution → research_agent_ports`。`recordUsageQuietly` / `selectClassesForTables` 留在 `ports.py`。引用面 **4 个文件 / 7 处** import 全部改指新家。
- **判别测试**（纯搬移，判别是**单一老家**而非行为）：`test_research_agent_ports.py` 新增守卫，对 6 个被搬名**双向**断言——新家有、老家没有。旧家为真而非恰好：`ports.py` 内被搬的 6 个名字 0 命中。RED 是模块不存在时的 import 失败（实测报 `ImportError`，计划预测的是 `ModuleNotFoundError`；`from pkg import submodule` 缺模块时两者语义等价，判定为计划措辞不准而非缺陷）。审查独立复核：把 diff 里的 `-` 块与 `+` 块各自重建后 `diff` **空输出**（194 行逐字节相同）；`ports.py` 在 diff 里 **0 insertions**（无 shim）；`grep -n research_agent_model_resolution app/services/research_agent_ports.py` 空 ⇒ 无环。落点 `ports.py` **596 行**，新模块 227 行（33 行头 + 194 行接缝）。

### 图表维度标签真修（`d667aed` + 修轮 `7b7ee90`，用户裁定）

- **改前症状**：`chart_renderer` 只读 `dimensions[].field`，从不读 `.label`，所以报告里轴上装的是维度**取值**而非维度名；给维度注入显示名是空转。r3 把这条以 `xfail(strict=True)` 停放在 `test_report_planner.py`。
- **口径裁定（用户 2026-10-11「做真修」）**：在**共享**渲染器上开一个可选注入点，而不是在报告侧另写一遍。
  - `ChartSpec.axisLabels: Mapping[str, str] = field(default_factory=dict)`（新字段）；`buildSpec(..., *, aliasOverrides=None, axisLabels=None)`。
  - `chart_renderer._axisNameOf` / `_namedAxis`：`_axisNameOf` 带 `return label if label and label != field else None`——**恒等映射必须屏蔽**（老 bug 的形态正是「`resolveColumnName` 未命中原样返参 ⇒ 真值 ⇒ `aliases.get(f) or _prettify(f)` 不再兜底」）；`_namedAxis` 无名字时 `return axis`（**返回同一个对象**，不新建）。
  - 报告路径在 `chart_integration.py:50` 注入。
- **反向护栏**：**chat 渲染逐字节不变** —— `test_chat_shape_renders_no_axis_name` 断言 chat 形状下不出现轴名；**TABLE 契约冻结** —— `test_chart_table_payload_keeps_raw_columns_as_keys` 保证表格块仍以原始列名为键。这是「改了共享件但不许外溢」的反向断言。
- **xfail 退役（本支第二次、也是最关键的一次纠偏）**：原计划把恒等映射的样例条目挂在 `RCV_QTY_PUU`（一个**度量**列）上，而 `_buildAxisOption` 只看向维度列（`catField = _dimensionOf(spec, 0)`）⇒ 计划硬性要求的「恒等映射必须屏蔽」**零判别性覆盖**，实施者的报告声称了一个不成立的证明。审查定级 Important（标注 plan-mandated），修轮**只做加法**：新增 `test_identity_axis_label_is_not_rendered`（恒等条目改挂**维度**列），计划钉住的两条断言**逐字节保留**，`chart_renderer.py` 在修轮里**未被改动**。新增/重贴的用例：`test_chart_dimension_label_is_translated`、`test_chart_table_payload_keeps_raw_columns_as_keys`（取代被删的 `xfail` 块），断言一条不放松。
- **仍开放**：维度**取值**翻译（`C1` → 基地 C1）另开任务，**不要**碰 `SpecDimension.label`（渲染器从不读它）。

## 测试

### 分进程全套（收尾 Step 1，禁同进程混跑）

**单元 17 个文件**：`220 passed, 1 failed`，**无 xfailed**（原为 `1 xfailed`——parked xfail 退役的验收信号）。

- 唯一红：`test_research_event_bus.py::test_error_specs_covers_exactly_the_ruled_codes` —— 陈旧 frozen code-list（集合里少 `checkpoint_conflict`）。**继承债**。

**集成 7 个文件**：`118 passed, 3 failed`。

- 3 红全在 `test_research_hypothesis_rewrite_wiring.py` —— 陈旧 fake double（`_FakeSessionsRepo.saveFinding()` 不接受 `enhancedClaim`）。**继承债**。

### 继承红灯清单（4 处 / 2 个文件，**非本支引入**）

| 文件 | 处数 | 红因 | 引入提交（**均早于 base**） |
|---|---|---|---|
| `app/tests/unit/test_research_event_bus.py` | 1 | frozen `ERROR_SPECS` 集合陈旧，缺 `checkpoint_conflict` | `d9a78e5`（`61986a9` 的祖先） |
| `app/tests/integration/test_research_hypothesis_rewrite_wiring.py` | 3 | fake double 陈旧，`saveFinding()` 无 `enhancedClaim` | `faf0007`（`61986a9` 的祖先） |

**判别**：两个测试文件在本支均 0 次改动；两个红因的引入提交都是 base `61986a9` 的祖先（`git merge-base --is-ancestor` 退出码 0）。

> 修正：开工时的债单写的是「3 个文件 4 处红」，实测 `test_research_session_service.py` **全绿**。真红灯集是 **2 个文件 4 处**。以实测为准。

### 每任务 TDD 证据（摘要）

| 任务 | RED | GREEN |
|---|---|---|
| M1 | 守卫命中 `research_agent_phases.py` | 守卫 1 passed；`-k "replan or modify"` 2 passed；F401 消失 |
| M2 | `assert 'gateway-alias' == 'fake-model'` | 定向 5 passed；`test_research_agent_service.py` 56 passed |
| M3 | 端点报 `nextPhase: "execute"`（planning+modify） | 端点报 `"plan"`；单元 6 passed；`test_research_api.py` 31 passed |
| M4 | 新模块缺失 ⇒ import 失败 | 守卫 passed；ports 相关单元 35 passed；集成 56 passed |
| 图表 | builder `TypeError: buildSpec() got an unexpected keyword argument 'axisLabels'`；修前 option 恰为 `{"xAxis":{"type":"category","data":["C1","C2"]}}`（无轴名） | chart 单元 160 → **216** passed；`test_report_planner.py` 28 passed / **0 xfailed** |

## 遗留（未做，记录在案）

- **仓库级 `ruff check --select F821` 闸**：基线 30 条（全是 `from __future__ import annotations` 模块的注解位名字），且基线**含本支禁改的 chat 侧文件** ⇒ 上闸必须先钉基线，是独立议题，不进本计划。
- **维度取值翻译**（`C1` → 基地 C1）：起点是给 categories 过 `resolveValueName`，不是 `SpecDimension.label`。
- **`r3 → feat-research-report-analysis` 的合并决定**：r3 冻结在 `61986a9`，决定权在人，本支不动。
- **`llm_config` 测试库结构漂移**（缺 `disable_thinking`）：修它要 alembic，本仓禁用裸 alembic（打的是 prod）。
- **上面 4 处继承红灯**（2 个文件）：不在本支修复面。
- **各任务 deferred Minors**（已逐条记在 `.superpowers/sdd/2026-10-11-research-carryover-fixes/progress.md`，交由整支终评分级）：
  - M2：5 个计量站点里 3 处（`phases.py:384` 假设改写 / `phases.py:420` `_stageReport` / `execution.py:536` 图表）**无判别性测试** —— 其覆盖测试驱动的 client 没有 `_modelName`，把那几行改回 `metered.modelName` 仍然全绿。**回归护栏缺口**（非现症）。建议复用 `AliasedLlmClient` fork 补一条报告相位用例。
  - M3：`decideResume` 契约上说 IO-free 却会 `logger.info`；两个调用方共享后，一次用户决策会打**两遍**「replan 上限已达」；`checkpoint.options is None` 与 replan 路径上的 `choice=None` 未被单元覆盖；`research_agent_service.py:425` 注释仍说恢复 SSOT 是 `checkpoint.options["resumePhase"]`（对 planning+modify 已不成立，显示字段、无行为影响）；`test_research_resume_decision.py:8` **新增** I001（import 未排序，与实施者「未新增任何 ruff 告警」的说法相抵，一行可修）。
  - 图表：`_buildHeatmap` / `_buildWaterfall` 的轴名接线（`chart_renderer.py:377/381/429`）**无断言**，只有实施者的一次性脚本覆盖（共享渲染器分支，可静默回归）；`ChartSpec` 现为 `frozen=True` 且带 dict 默认字段 ⇒ `hash(spec)` 会 `TypeError`（今日无调用方 hash，潜伏脚枪）；`test_chat_shape_renders_no_axis_name` 用的默认 label 是中文而非 chat 真实的 `_prettify(field)` 英文回落（仍可判别，只是不够拟真）；`ChartSpec.axisLabels` 是 dict，`frozen=True` 下仅为名义不可变。
  - M4：被搬走的段头注释「无状态构件：LLM 客户端兜底 / 用量记账兜底」现在只说对一半（记账那半留在 ports），且 `ports.py` 的 `recordUsageQuietly` 因此失去段头（属「纯搬移」范围，是真实的小幅文档损失）；新模块 docstring 说 ports import 覆盖「这四个」而实际是六个（方向正确、枚举不全）；三处指向旧家的陈旧文字指针（`research_agent_service.py:518`、`test_research_model_selection.py:174/205`）。
- **计划自身缺陷类（写计划时埋的弱测试）**，供合并时回看：图表 Step 6 把恒等样例挂在度量列上（零判别性）；M4 Step 1 的 grep 判据对多行括号 import 块**完全失明**（实跑输出为空）且**漏掉 ports 自己的单测文件**（按「不留再导出」实施必然 `ImportError`）；M4 Step 3 的 `-k reexported` 与真实测试名对不上（`-k` 选 0 条 ⇒ 退出码 5「no tests ran」，不是有效 RED）；图表 Step 2 的 `-k axis_labels` 大小写不符（`pytest -k` 区分大小写）选 0 条。这四条都在开工前/派活前由控制者按符号名复核抓出并改进了计划（`0f41279`）。
