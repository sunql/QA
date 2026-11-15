# 分支审计 2026-11-15（删除凭据）

删除前的 tip SHA 留档；90 天内可经 reflog / 本表恢复。

## 已删除分支（本地 21 条）

| 分支 | tip SHA | 判据 |
|---|---|---|
| `fix/import-tombstone-name-guard` | `938b67f0c64a3d5864373e766069371d7349f09b` | PR head == tip（squash 合并） |
| `feat/kpi-p4b-p5-compiler` | `bca7a26033e3ea0b32e15ef44d4f51ec60ddbfae` | PR head == tip（squash 合并） |
| `feat/kpi-vector-graph-sync` | `379dbfc1d1557b6b656d825239fa78ceddc985d6` | PR head == tip（squash 合并） |
| `feat/kpi-p3-alias-resolver` | `085fdc89c49b756b2341d660a094bd9d18e4d710` | PR head == tip（squash 合并） |
| `feat/kpi-tier-a-executor` | `57b02f0690143289103c3223a69df59f66af61d8` | PR head == tip（squash 合并） |
| `feat/kpi-write-acl` | `279773646671203cf12dcfecefa05d1d6eeae507` | PR head == tip（squash 合并） |
| `feat/kpi-match-v2` | `b4fe9e5f1bd356da0fe6680bcfbff70fdca744eb` | PR head == tip（squash 合并） |
| `feat/kpi-definition-consumption` | `b0b1f144a73255cac4a974b862976ee9bb5ed55e` | PR head == tip（squash 合并） |
| `feat/term-dictionary-add-entry` | `ada3cef656adc315ac88bc97527f2e095283139d` | PR head == tip（squash 合并） |
| `feat/semantic-registry` | `32f4c65637960d71df459574cfd34e1dd415b771` | PR head == tip（squash 合并） |
| `feat/semantic-release-a` | `ab4cb513a062f8066e6849405e1a5f1bf15008f9` | PR head == tip（squash 合并） |
| `fix/alembic-residual-36` | `bdbecc9ab2aa4f5aac24de5e6265283cf4fc25e8` | PR head == tip（squash 合并） |
| `feat/kpi-governance-runtime` | `c811aa47e0b42faf21a05ea1649da8f97140fff4` | PR head == tip（本地副本，未推送） |
| `feat/ontology-sync-admin` | `64d801368dff260b446d351400dd4d33d1de3ce1` | PR head == tip（本地副本，未推送） |
| `fix/governance-write-gate` | `d1a7a84f00a7f513473594fc6de89219574b649b` | PR head == tip（本地副本，未推送） |
| `docs/2026-11-06-post-merge-backfill` | `10c79bb392b8c51b8576fc645ecf3c33b66d05fe` | 指向 main 本体 10c79bb3，零独有内容 |
| `docs/a1-backfill-20261107` | `10c79bb392b8c51b8576fc645ecf3c33b66d05fe` | 指向 main 本体 10c79bb3，零独有内容 |
| `docs/next-batch-priority-20261107` | `10c79bb392b8c51b8576fc645ecf3c33b66d05fe` | 指向 main 本体 10c79bb3，零独有内容 |
| `feat-research-report-r3` | `45b7793bde1a4c972d6c26994fdaa5c872415856` | main 祖先，main..branch 为空 |
| `sync/remote-main-b6e809a2` | `f62102018e18f729333b20e7a3cd15aab539bd57` | b6e809a2 是 main 祖先，老 main 快照 |
| `docs/ci-hardening-post-merge-100` | `252a14be973719464cc3367b92c1e9693d0c4d89` | PR #100 内容 + 2 个后续 doc 提交（标题逐字在 main 历史） |

## 保留

| 分支 | tip SHA | 理由 |
|---|---|---|
| `chore/sync-change-record-status` | `e207ecf277facf513a8bfd164bd34fc7c67e19c8` | PR #127 open；merge-tree 判定合并会产生变化（有未合内容） |
| `backup/evidence-sql-metric-docs` | `f218c46e1eded00330eeb44924078688452d179a` | 1030 盘点定为 D 类刻意存档（独有 11024 行证据文档） |

## 远端删除（13 条）

`fix/merge-gate-net-diff-ci-surface` 仅远端：
`0e38e35abaee14801665d48ab5de7e7c6d4991e3`

## 未跟踪文件保全

两个 worktree 的未跟踪内容已先复制到 `/tmp/branch-audit-1115-backup/`：
`2026-11-11-kpi-p3-alias-resolver/`（main 那份的子集）、`2026-10-11-r3-p0-convergence.md.draft.bak`。

## 执行结果（2026-11-15）

| 动作 | 结果 |
|---|---|
| worktree 移除 | 11 个（9 个 `/private/tmp/qa-*` + 2 个 `.worktrees/*`）；其中 `qa-governance-write-gate` 的 `.git` 已损坏，用 `git worktree prune` 清元数据 |
| 本地分支删除 | **21 条成功 / 0 失败** |
| 远端分支删除 | **13 条成功 / 0 失败** |
| 终态 —— origin | `main` + `chore/sync-change-record-status`（2 条） |
| 终态 —— 本地 | `main` + `chore/sync-change-record-status` + `backup/evidence-sql-metric-docs`（3 条） |
| 终态 —— worktree | **1 个（主工作树）**；4 个 detached 部署源（`qa-deploy-1113` / `qa-deploy-20261113` / `qa-deploy-20261114` / `qa-deploy-1114-tombguard`，各约 39–40M）同日清理，清理前均为 0 未提交 |

未跟踪文件保全：`/tmp/branch-audit-1115-backup/`（会话级，30 天后失效；内容均已确认 ⊆ main 或为草稿）。
