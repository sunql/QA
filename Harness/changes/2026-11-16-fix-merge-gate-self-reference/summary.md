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
| `test_gate_job_name_constant_matches_the_workflow` | 常量与 workflow 文件同源，改名即红 |

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

## 7. 验证

- 定向套件全绿（见 §5）。
- 期望态（下一个 PR）：基线 run 的 9 个 job 与 PR run 剔除后的 9 个 job 等价 ⇒
  覆盖等价通过，报告多一行「判据不含闸门自身的 job」。

## 8. 回滚

单个 commit（`ci_judge` 常量 + 过滤 + 报告提示 + 常量守卫 + 文本搬移）。回滚 =
revert 该 commit ⇒ 恢复「闸门自拦」的旧行为（只能 owner bypass）。
`merge-gate.yml` 只改了注释，回滚不影响行为。

## 9. 遗留

- 这次按**名字**精确剔除一个 job，没有泛化成「凡不是 CI workflow 的 job 都剔除」：
  实测那个 job 的 `workflow_name` 就是 `CI`（GitHub 把它挂进触发 run 时丢掉了来源信息）
  ⇒ 无法用 `workflow_name` 区分。若将来再加 `workflow_run` 触发的 workflow，
  同样会污染覆盖等价 ⇒ 需要同样登记（守卫会因 workflow 文件变化而转红，形成提醒）。
- 闸门自身的 check-run 不在 job 列表里（它是 check，不是 job）⇒ 无需额外处理。
