# feat-merge-gate-required-check 变更记录

> 后续项 5（`Harness/changes/fix-ci-integration-hardening/summary.md` §后续项）落地批：
> 把「失败集合对差、净新增为空才合并」从人可用判据升级为**强制闸门**。
> 2026-11-05。零 DDL、零生产写。

## 需求

工具化的对差判据此前只靠人执行；main 无分支保护、无必需状态检查（实测 404），
CODEOWNERS 落地但不生效。本批把判据接进 GitHub 必需检查。

## 变更清单

| 文件 | 内容 |
|---|---|
| `.github/workflows/merge-gate.yml`（新增） | CI workflow 完成后（`workflow_run`）自动跑 `app.utils.ci_failure_set`，把结论落成 check **`merge-gate / failure-set-diff`**；退出码 `0` ⇒ success，`1/2/3/4/5/6` ⇒ failure |
| `Harness/rules/测试规范.md` | 钉死基线取法、闸门安全性质、job API 一致性延迟处置 |
| `main` 分支保护（仓库设置，非代码） | 必需 check = `merge-gate / failure-set-diff`；`enforce_admins`；无必需复核（solo 仓库）；bypass = `sunql`（显式越权口，人工仲裁净丢失 / CI 相关面变更时用） |

## 设计决策（与文档后续项 5 的差异）

1. **必需检查不是裸分片 job**：main 基线本身红（~140 条）⇒ 裸 `Backend / integration shard N`
   必失败 ⇒ 所有 PR 永远无法合并。必需的是「净新增=0」的门禁 check，与人工判据同构。
2. **闸门代码不受 PR 控制**：`workflow_run` 触发的工作流与其 checkout 到的工具代码
   **始终取自默认分支**，PR 篡改不了裁决者。
3. **不带 `--allow-ci-changes`**：改 CI 相关面（退出码 6）与净丢失待人工确认（退出码 5）
   一律 failure，人工仲裁后由 bypass 账号显式合并，用一次留痕一次。
4. **基线取法（定死）**：同一完成标记协议下 main 的最新完成 push run
   （`branch=main` + `event=push` + `status=completed` 最新一条）。

## 实现中抓到并修掉的三个坑

| 坑 | 根因 | 修法 |
|---|---|---|
| gate 首跑几乎必然误判 UNUSABLE | job 级 API 最终一致性延迟：run 级已 completed，job `conclusion` 仍 null **13–18 分钟**（PR #101/#102 两次实测） | 跑工具前轮询 job conclusion 全部收敛（上限 30 分钟；不收敛放行给工具 fail-closed）（PR #103） |
| 同名 check 竞态 | job 自带 check 与 API 创建的 check 同名 ⇒ 必需检查按名字匹配时撞最新一个是竞态 | 去掉 job 级 `name:`，API check 是该名字唯一来源（本 PR） |
| 滞后时长记录失真 | MEMORY 早前记「~4 分钟内收敛」 | 实测 13/18 分钟，已更正（MEMORY + 测试规范） |

## 验证证据（合并后回填）

- [ ] gate 拦截证明：必需 check 未完成时对验证 PR 调 merge API ⇒ GitHub 拒绝
- [ ] gate 通路证明：gate success 后合并放行
- [ ] gate 全链路首跑：验证 PR 的 check 由 merge-gate 工作流真实产出（非人工）
- [ ] 净新增拦截证明：暂缺（红基线上构造净新增需一个含真失败的 PR；由后续项 7 的白名单批次顺带取证）

## 威胁模型（不变，见测试规范）

工具从 PR 作者可控的 pytest stdout 读证据 ⇒ 挡偶然假绿、不挡蓄意伪造。
必需检查把「人的纪律」变成「机器拒绝」；但绕过手段（伪造 stdout / bypass 账号）仍在，
⇒ 后续项 3（不可伪造证据）与后续项 7（白名单只许 main 上 CODEOWNER 维护）依然必要。
