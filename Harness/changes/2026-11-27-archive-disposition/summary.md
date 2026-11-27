---
created: 2026-11-27
updated: 2026-11-27
---

# 归档分支清理：内容处置记录（2026-11-27）

## 1. 背景

采购 V0.3 落地计划（2026-11-18 ~ 11-26）那条线留下了 23 个本地分支与 11 个远端
分支。它们的内容**绝大多数已在 main**（本仓用 merge commit 合入，分支指针留在本地
只是历史残留），但有 4 条分支各自持有**只存在于本地**的内容。清理时按
「新增文件是否已在 main」逐条核对后，逐项处置如下。

⚠️ 核对口径说明：`git cherry` / `rev-list origin/main..branch` 在本仓**会误报**
（本仓合入方式与 squash 语义混用 ⇒ 原 commit 不是 main 的祖先）⇒ 一律用
「分支新增文件是否已在 main」+「main 在这些文件上的内容是否更新」两项判断。

## 2. 处置清单

| 归档来源 | 独有价值 | 处置 |
|---|---|---|
| `archive/feat-procurement-m0` | `Harness/changes/2026-11-19-milvus-ef-limit/summary.md`（PR #156 的变更记录：线上 `ef(64) < k(80)` ⇒ property/metric 注入**静默失效**；代码已在 main，记录没进） | ✅ **合入 main** |
| `archive/backup-evidence-sql-metric-docs` | ① `0095_id_mapping.py` + `test_alembic_0095.py` ② `2026-09-28-m0-id-mapping-a1/summary.md` ③ 2 份 plan 文档 ④ `2026-09-28-m0-id-mapping-integration.md` | ① **不合**（见 §3.1）② **不合**（见 §3.2）③ 之「实施计划」**合入 main**，之「迁移分叉合并方案」**不合**（见 §3.3）④ 见 §3.1。**另补**：`test_alembic_0097.py`（见 §4） |
| `archive/feat-m0-foundation` | 5 份业务问卷 A–E（`docs/update/1118/M0/业务问卷/`） | ❌ **不合**（见 §3.4） |
| `archive/feat-a4-0-approval-subject-key` | A4-0 整批：0159 迁移 + 单测 + 变更记录 + 审批面放开代码 | ↩️ **恢复为远端分支** `feat/a4-0-approval-subject-key`（见 §3.5） |

## 3. 不合入的理由（逐条）

### 3.1 `0095_id_mapping.py` 迁移 —— 它是 `0097` 的重复前身，合进去会断链

main 的迁移链是 `0094 → 0096 → 0097(id_mapping) → …`；`0095_id_mapping` 是
**同一个迁移改号前的版本**（同样建 `id_mapping` 表）。合进 main ⇒ 链上出现两个
建同一张表的版本 ⇒ 重复建表 + 迁移链断裂。**已确证**：0095 与 0097 的表结构、
列集合、索引完全同源，0097 才是正式版本。

配套的 `test_alembic_0095.py` **未按 0097 改号** ⇒ 原用例恒红；且它的第三条
「downgrade 0094 → 复原」在 0097 语境下要**退 75 个版本**（0098–0171 全撤）⇒
风险与原用例（只退 1 个版本）不可比。处置见 §4。

### 3.2 `2026-09-28-m0-id-mapping-a1/summary.md` —— 早期版本，main 已有正式版

main 上已有 `Harness/changes/m0-id-mapping-a1/summary.md`（2026-10-31 重写版，
200 行）。归档那份是 2026-09-28 的早期版本（501 行，含当时的实施流水）。两份同
主题并存会让读者分不清哪份是准 ⇒ **不合**。

### 3.3 `2026-09-28-m0-id-mapping-integration.md` —— 「挂起中」的决策已被后续落地方案取代

该文档状态是「**已审 · 挂起中**：决定等 B1 并入后再让号」。让号这件事后来已经
以 `0097` 落地 ⇒ 这份「还在等」的过程文档已过时，入库会误导 ⇒ **不合**。

### 3.4 5 份业务问卷 —— `.gitignore` 明令「只留在本地，绝不入库」

```
# 业务问卷 A–E 及其回收登记快照：**用户编写的工作副本**，只留在本地，绝不入库
# （答案的机器可读产物在 docs/update/1118/M0/answers/；同目录 README.md 是仓内说明，保留跟踪）
docs/update/1118/M0/业务问卷/[A-E]-*.md
```

合进 main = **违背用户自己定的规则**。仓内 `README.md` 也写明判据 4 的口径是
「答案要点在仓、**原文在本地**」。⇒ **不合**（文件仍在本地磁盘，未动）。

### 3.5 A4-0（0159 迁移 + 审批面放开）—— 是**二期特性**，不是遗留缺陷

main 的现状：`approval_task.instance_id` 仍 NOT NULL、唯一约束仍是
`uq_approval_task_matrix(instance_id, matrix_code)`；而 `0166_discovery_pipeline`
明确把 `discovery_review.approval_task_id` 设为**可空**并注明
「P5 审批任务**二期接入前为空**」⇒ **main 现在是自洽的，不缺 0159**。

0159 迁移要做的是「`instance_id` 放开可空 + 幂等键换成
`UNIQUE(subject_type, subject_id, matrix_code)`」。**为什么必须换**（0159 自己的
docstring 已写明）：旧唯一键含可空列 ⇒ PG 里 `NULL` 互不相等 ⇒
`UNIQUE(instance_id, matrix_code)` 在 `instance_id IS NULL` 时**静默失效** ⇒
同一提案的同一矩阵条目能落两行 ⇒ **四眼审核失效**。

⇒ 合并 0159 = 提前落地一个**业务上未决**的特性（WP-ONT-AI A4 二期），且需要
**迁移链手术**（main 的 0160 的 `down_revision` 指向 0158 ⇒ 要让 0159 进链得改
它）。这不是「清理」，是**做决策** ⇒ 不在本次清理里做。

**处置**：恢复为远端分支 `feat/a4-0-approval-subject-key`（内容完整，随时可开 PR
推进），并在本记录里留下它的状态与合并前提。

## 4. 顺带补上的缺口：`test_alembic_0097.py`

`id_mapping`（M0-P0.1，**已合入 main 的特性**）的迁移契约测试**从未进 main**
（0095 改号 0097 时用例没跟着改，两边都没进）。本次补上 `0097` 版：

- 保留：① `upgrade head` 后表存在 + 列集合与 ORM 一致；② 唯一约束
  `(business_object, external_id)` 由 DB 真实兜底；
- **不保留**原用例的「downgrade → 复原」那条（理由见 §3.1：0097 距 head 75 个
  版本，downgrade 会连带撤销 0098–0171 全部迁移，风险与原用例不可比）；
- 写库闸改用仓内 `assertTestDatabase`（库名 `qa_metadata_test*` + 回环主机），
  原用例手写的 `_REQUIRED_DB == "qa_metadata_test"` 写死库名 ⇒ 本地 SSOT 库
  `qa_metadata_test_mainline` 跑不了；改用仓内闸后可本地验证
  （本地实跑 `2 passed`）。

## 5. 遗留的已知缺口（本次不修，明确登记）

1. **`test_alembic_0097` 的降级方向未覆盖**（§4）—— 需要一个「只降 1 个版本」的
   安全写法（例如在链尾追加一个可回滚的探针迁移）才能补，不是简单照搬旧用例。
2. **`feat/a4-0-approval-subject-key` 未合入**（§3.5）—— 需要一个业务决策
   （A4 二期何时接）+ 迁移链手术。分支已在 origin 上。
3. **共享测试库 `qa_metadata_test` 卡在 0159**（main 没有该版本）⇒ main 线跑测试
   必须用 `qa_metadata_test_mainline`。这是**独立于本次清理**的基建债
   （见 `docs/update/1118/M0/A1-本地栈同构核对表.md`），本次未处理。
