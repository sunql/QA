# 修复：merge-gate 自指（闸门自己的 job 把闸门自己拦下）

- 日期：2026-11-16
- 分支：`fix/merge-gate-self-reference`
- 范围：`backend/app/utils/ci_{judge,failure_set,report}.py` + 两个既有 unit 文件 + `merge-gate.yml` 注释 + `Harness/rules/测试规范.md`
- 不部署：纯 CI 判据工具改动，与运行时服务无关

## 1. 问题

闸门 `merge-gate / failure-set-diff` 会在**每个 PR** 上判「不可合并」，退出码在这些形态之间跳：

| 现象 | 退出码 |
|---|---|
| 两次 run 的 job 集合不等价（PR 侧多出闸门自己的 job） | 4 `COVERAGE_MISMATCH` |
| 闸门自己跑红 ⇒ 被记成净新增失败 `job:merge-gate / failure-set-diff` | 1 |
| 闸门**正在跑**时拉 job 列表，自己的 job 结论还是 `null` ⇒ 判不可用 | 2 |

⇒ **闸门自己拦自己**：任何 PR 都无法通过必需检查，只能 owner bypass。

## 2. 实测证据

- PR #137 的 CI run `43800518364` 的 job 列表**有 10 个** job：9 个 CI 矩阵 job
  + `merge-gate / failure-set-diff`（job id `133113190867`，与它自己创建的
  check-run id 相同）。该 job 的 `workflow_name` 就是 `CI`、`run_id` 就是这轮 CI run。
- 同一 head 的 main push run（如 `43782038578`）job 列表**只有 9 个** ——
  闸门只在 `pull_request` run 上裁决，main 侧没有它。
- `GET /actions/runs/<PR run>/jobs` 是按 run 取的 ⇒ 这个 job 是 GitHub
  把 `workflow_run` 触发的那条 workflow 的 job **挂到了触发它的 run 上**。
- 5ab1 会话里观察到的 gate 输出：`PR 有而基线没有：['merge-gate / failure-set-diff']`。
- **挂载发生在闸门 job 完成之后**：`merge-gate.yml` 的 run 全部在 ~30 秒内完成
  （`43811205138`/`43809657501`/`43800550658`；其中「等 job 结论收敛」那步若看见
  自己的 job 是 `null` 就会空转 30 分钟 ⇒ 实际没有空转 ⇒ 拉 job 列表时它还不在列表里）。
  ⇒ **同一个 head 的第一次闸门裁决可能通过，任何复跑都会转红（退出码 4）** ——
  这解释了此前「有时绿有时红」的间歇性。
  ⇒ 「自己还在跑时结论为 null ⇒ 退出码 2」是**理论上**的通道，本次未实测到；
  过滤对三种形态一律生效（测试里四种结论都覆盖）。

## 3. 根因

`ci_judge._coverageMismatchReason` 用 **job 名集合**做覆盖等价判定，
`_unionUsableFailures` 把可用 job 的失败集合取并集 —— 两者都没有
「排除闸门自身的 job」这一步。而闸门的 job 天然只在 PR 侧
⇒ 覆盖必然不等价；它自己的结论又天然是「不可合并」
⇒ 失败集合里天然多一条自己。

## 4. 修复

1. `ci_judge.GATE_JOB_NAME = "merge-gate / failure-set-diff"`（`<workflow 名> / <job id>` 形态）。
2. `_withoutGateOwnJob(jobs)`：两侧**一律剔除**该 job；
   - `collectRunFailures`（本地/手工路径）在判定前剔除；
   - `collectRunFromGithub`（闸门路径）在**拉日志之前**剔除 —— 它的日志是闸门自己的输出、
     没有 pytest 完成标记，拉也判不出东西，而且实测那一步没有归档日志（`BlobNotFound`）。
3. `ci_failure_set._reportNotes` 增一行提示：「判据不含闸门自身的 job（…）」——
   剔除是**设计**，不是静默忽略；报告读者必须知道 PR 侧的 job 列表**被剔过一个**。
4. 常量与 workflow 文件**钉死**：`test_ci_merge_gate_workflow.py::test_gate_job_name_constant_matches_the_workflow`
   从 YAML 里取 `name` + 唯一的 job id 拼出期望值 ⇒ workflow/job 改名立刻转红
   （否则剔除静默失效 ⇒ 闸门对所有 PR 死锁）。`merge-gate.yml` 的注释里写明这条联动。
5. 顺带：`_mainTipRemedyNote`（纯呈现文本）移到 `ci_report.mainTipRemedyNote` ——
   `ci_failure_set.py` 原本正好 800 行（行数上限），本次改动需要头部空间；
   呈现文本归呈现模块也更内聚。

## 5. 测试（TDD，先红后绿）

新增 4 个用例函数（7 个收集项）：

| 用例 | 钉住的性质 |
|---|---|
| `test_collectRunFailures_excludes_the_gate_own_job` | 闸门 job 不进逐 job 判定、不进失败并集 |
| `test_verdict_is_mergeable_when_only_the_pr_side_has_the_gate_job[failure/''/success/cancelled]` | 四种结论下都仍可合并（覆盖不等价 / 净新增 / 不可用三条自拦通道全关） |
| `test_collectRunFromGithub_does_not_even_fetch_the_gate_job_log` | 连日志都不拉（不白等 404） |
| `test_gate_job_name_constant_matches_the_workflow` | 常量与 workflow 文件同源（workflow 名 / job id / **job 级 `name:` 必须不存在** / 脚本里的 `CHECK_NAME` 三处一起钉住），改名即红 |
| `test_main_discloses_that_the_gate_own_job_is_excluded` | 报告里那一行提示不得被删（剔除是设计，但不能静默） |

- RED：5 条失败（`ImportError: GATE_JOB_NAME` + 4 条参数化）+ 2 条判定为假（断言失败）
  —— 失败原因均为「闸门自己的 job 参与了判据」。
- GREEN：CI 判据全部 6 个套件 **219 passed**（`test_ci_failure_set` /
  `test_ci_failure_set_flakes` / `test_ci_merge_gate_workflow` / `test_ci_report` /
  `test_ci_surface` / `test_ci_gh`）。
- 未新增测试**文件**（只在既有文件里加用例）⇒ 不触碰 CI 分片轮转。

## 6. 影响面与合并方式

- ⚠️ 本 PR 改了 **CI 相关面**（`ci_*.py` + `merge-gate.yml`）⇒ 闸门对**本 PR 自己**
  会判退出码 6（`CI_SURFACE_CHANGED`）⇒ 按既定纪律走 **owner bypass**，并在此留痕。
- 修复生效后：闸门对**后续** PR 才不再自指。本 PR 的 check 仍是旧的（闸门代码取自默认分支）。

## 6.1 复查处置（code-reviewer）

审查结论：0 CRITICAL / 0 HIGH / 1 MEDIUM / 3 LOW，**APPROVE**（不含 fail-open 路径；
把生产侧过滤临时换成恒等函数后，新用例 6 项转红 ⇒ 守卫不是恒绿）。处置：

| 级别 | 问题 | 处置 |
|---|---|---|
| MEDIUM | 守卫只比派生值，若将来给 job 加 job 级 `name:` ⇒ 实际名字变了而守卫照样绿（假绿守卫） | 已加断言：job 级 `name:` 必须不存在；并把脚本里的 `CHECK_NAME` 字面量也钉到同一常量 |
| LOW | 报告里的披露提示没有测试 | 已加 `test_main_discloses_that_the_gate_own_job_is_excluded` |
| LOW | `测试规范` 之外的 `开发流程规范.md` 仍引用旧的 `_mainTipRemedyNote` | 已改为 `ci_report.mainTipRemedyNote` |
| LOW | 参数化用例单独看分辨不出「剔对了」与「剔光了」 | 已加 `prAssessments == ["Backend / unit shard 0"]` 局部断言 |
| 残留风险 | 闸门 workflow 的「等 job 结论收敛」步骤会不会被自己的 job 卡住 30 分钟 | **已用实测排除**：闸门 run 全部 ~30 秒完成 ⇒ 拉 job 列表时自己的 job 还没挂上（见 §2） |

## 7. 验证

- 定向套件全绿（见 §5）。
- 期望态（下一个 PR）：基线 run 的 9 个 job 与 PR run 剔除后的 9 个 job 等价 ⇒
  覆盖等价通过，报告多一行「判据不含闸门自身的 job」。

## 8. 回滚

单个 commit（`ci_judge` 常量 + 过滤 + 报告提示 + 常量守卫 + 文本搬移）。回滚 =
revert 该 commit ⇒ 恢复「闸门自拦」的旧行为（只能 owner bypass）。
`merge-gate.yml` 只改了注释，回滚不影响行为。

## 7.1 修复后的实测（PR #141）

PR #141（纯 docs）的闸门 run `133163851197`：

- **自指消失**：报告里没有任何「PR 有而基线没有：`merge-gate / failure-set-diff`」；
  逐 job 清单 PR 侧只有 9 个 CI job；并出现新提示「判据不含闸门自身的 job」。
- 结论仍是退出码 6（`CI_SURFACE_CHANGED`），但**原因不同**：基线 run 是
  main@`26059c95`（旧基线），而该 PR 的 CI 跑的是 merge(main@`3f691c60`, PR 头)
  —— 中间 main 连续合了 #139 与 **#140 自己**（改了 `ci_*.py`）⇒ 两次 run 的
  CI 相关面不同且**归因不到本 PR** ⇒ 工具按设计判 6，并给出处置：
  **推一个新 commit（新 CI 事件派发）**，复跑无效（合并 commit 已在派发时冻结）。
- ⇒ 这是**基线陈旧**的独立现象（main 在 PR 派发与闸门执行之间推进过），不是自指回归。
  正常节奏下「基线 = 派发前最后一条已完成的 main run」即自动对齐。

## 9. 遗留

- **基线陈旧 ⇒ 退出码 6**：main 在「PR 的 CI 派发」与「闸门执行」之间推进（含 CI 面变更）时，
  闸门对**老 PR** 会判 6。处置是推新 commit / rebase / 重开 PR（复跑无效）。
  这是既有行为，本次未改；若频繁发生，可考虑「基线自动取 PR run 的 main 侧底座」
  （工具已能算出 `merge-base` 侧底座并打印，但取 run 仍按「派发前最后完成」规则）。
- 这次按**名字**精确剔除一个 job，没有泛化成「凡不是 CI workflow 的 job 都剔除」：
  实测那个 job 的 `workflow_name` 就是 `CI`（GitHub 把它挂进触发 run 时丢掉了来源信息）
  ⇒ 无法用 `workflow_name` 区分。若将来再加 `workflow_run` 触发的 workflow，
  同样会污染覆盖等价 ⇒ 需要同样登记（守卫会因 workflow 文件变化而转红，形成提醒）。
- 闸门自身的 check-run 不在 job 列表里（它是 check，不是 job）⇒ 无需额外处理。
