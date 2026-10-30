---
slug: prod-audit-2026-10-16
created: 2026-10-16
status: 完成（无漂移）
type: audit
scope: prod alembic vs origin/main
---

# prod 数据库 alembic 状态审计（2026-10-16）

## 结论（一句话）

**prod 与 origin/main 在迁移层完全对齐，无漂移，无需任何写操作。**

| 项 | 值 |
|---|---|
| `origin/main` SHA | `d5c8a4295f2cc8951f5b1a6cce6082832f2d1741` |
| origin/main 迁移文件数 | 127 |
| origin/main head(s) | `['0127']`（**单 head，线性链**） |
| prod 5433 `alembic_version.version_num` | `0127` |
| prod 关键表齐备 | `multi_step_run` / `multi_step_step` / `research_session` / `research_event_outbox` / `research_task` 全部存在 |

⇒ `alembic upgrade head` 在 prod 跑会是 no-op。

> **后续状态（2026-10-30 补记）**：本文是 **2026-10-16 的快照**，结论只对当时的
> `d5c8a429` 成立。此后 `main` 又合入 `0136_metric_structured` / `0137_metric_source_unique`
> （KPI 指标技术形态治理），**prod 已随部署迁移到 `0137`**（`alembic_version = 0137`），
> `main` 的 head 同样是 `0137` ⇒ 当前仍对齐，但**不要再用本文的 `0127` 判断今天的 prod 状态**。
> 审计方法（静态 head + prod version + 关键表 + `alembic check`）依然有效，照跑即可。

## 验证方法

1. 静态扫描 origin/main 所有迁移文件，提取 `(revision, down_revision)` 配对：
   ```bash
   git ls-tree -r origin/main --name-only -- backend/alembic/versions/ | grep '\.py$' | sort
   # 每文件取：
   git show origin/main:$f | grep -E '^revision' | grep '=' | head -1 | sed -E 's/.*= *"([^"]+)".*/\1/'
   git show origin/main:$f | grep -E '^down_revision' | grep '=' | head -1 | sed -E 's/.*= *"([^"]+)".*/\1/'
   ```
2. 计算 heads：`comm -23 <revs 集合> <parents 集合>`，得所有不被任何文件 down_revision 引用的 revision。
3. 跨校 prod：`psql -h localhost -p 5433 -U qa_user -d qa_metadata -tAc "SELECT version_num FROM alembic_version;"`。
4. 关键表存在性回查（防「stamp 但 DDL 未跑」假象）。

## 重要历史背景（本次审计暴露的链子结构事实）

迁移链 127 条，非典型线性，几个值得记录的特征：

### 1. 编号 0095 缺位
链子在 0094 → 0096 之间跳过了 0095。`git log --all --diff-filter=D -- 'backend/alembic/versions/0095*'` 也搜不到任何曾经存在的痕迹。
⇒ 不是被删，是**从未被分配**（0094 之后直接接 0096 是 renumbering 期间手抖漏号）。对运行无影响，但容易在脚本里被误判。

### 2. `b1ea6cb32588` 反插在 0117 与 0118 之间
正常链子 0117 → 0118，但 origin/main 上 0118 的 `down_revision = "b1ea6cb32588"`，而 `b1ea6cb32588` 的 `down_revision = "0117"`。
⇒ 历史上是 `b1ea6cb32588_add_research_run_state.py` 改名 + 编号收紧到 `b1ea6cb32588_run_state.py`（对齐文件名 ≤ 32 字符模板），插在 0117 之后形成「研究线专有路径」与「主线」的合流点。
⇒ 文件头 docstring 显式说明：「对已应用的库无影响」（alembic 按 `revision` 字符串发现，不按文件名）。

### 3. 命名风格二分裂
- 多数文件：`revision: str = "0091_wiki_ontology_link"`（**长名 = 文件名主体**）
- 少数文件：`revision: str = "0096"`（**短名 = 数字**）
两种混用，未统一。alembic 不在乎（按字符串比较），但下游脚本/审计工具需要按字符串严格匹配时容易踩。

### 4. 派生工作树（本地 backend/）的 head 是 0125
当 `cwd=backend/` 时跑 `alembic script` 报 `heads=['0125']`、`current=0125`——因为本地 `backend/alembic/versions/` **没有** 0126/0127 这两个文件。
⇒ 这是正常的：当前分支 `fix/aichat-gap-f5-intent-threshold` 的 working tree 还没拉 origin/main 的最新迁移。
⇒ 不会撞 prod drift——prod 用的是 origin/main 的迁移集（已经 stamp 到 0127），不是当前分支的。

## 踩坑记录（以后再做类似审计要避开）

**第一次 awk 解析炸了两次。** 两类陷阱：

### 坑 A：行前缀匹配被注释行短路
`0092_fix_uq_wol_active_for_null.py` 的文件头有 3 行匹配 `^down_revision`：
```
down_revision: 0091_wiki_ontology_link              ← 注释行（无引号无 =）
revision: str = "0092_fix_uq_wol_active"            ← 中间行
down_revision: str = "0091_wiki_ontology_link"      ← 真正的声明
```
`awk -F'"' '/^down_revision/ {print $2; exit}'` 命中第一行，$2 为空，**整个 down 链断在这里**——0092 看起来无 parent，0091 看起来是孤立 head。
**修法：** 在 `grep` 上加 `grep '='` 限定（声明行必有 `=`，注释行没有）：
```bash
grep -E '^down_revision' | grep '=' | head -1 | sed -E 's/.*= *"([^"]+)".*/\1/'
```

### 坑 B：cwd 在 backend/ 时 `origin/main:alembic/versions/X.py` 能解析；回到 qa-system/ 必须带 `backend/` 前缀
git 解析 `rev:path` 总是相对**仓库根**，不相对 shell cwd。混用会随机炸。
**修法：** 永远带 `backend/`，且 `git rev-parse --show-toplevel` 验一次再批量跑。

### 坑 C：行内多个等号 / 嵌套引号
sed `s/.*= *"([^"]+)".*/\1/` 假设每行只有一个 `=` 后跟一个 `"..."`。如果以后有 inline 注释或链式赋值会乱。
**更稳的替代：** 直接调 alembic script API 解析单文件：
```python
from alembic.script import ScriptDirectory
from alembic.config import Config
cfg = Config('alembic.ini')
cfg.set_main_option('script_location', 'alembic')
# 复制目标 origin/main 的 alembic/versions/ 到一个临时目录，set_main_option 指过去
script = ScriptDirectory.from_config(cfg)
print(script.get_heads())
```
本次审计最后一段的最终正确结果就是用这个 API 在本地 backend/ 验的，**与手解析 100% 一致**：`heads=['0125']`（本地）、`heads=['0127']`（origin/main 通过 `git show` 取到的链子解析）。

## 行动项

| 项 | 决定 |
|---|---|
| 跑 `alembic stamp head` | **不做**（已对齐） |
| 跑 `alembic upgrade head` | **不做**（已对齐，再跑 no-op） |
| 修 0095 缺号 | **不做**（无功能影响，纯美学；改了反而制造新漂移） |
| 解 b1ea6 反插 | **不做**（同上） |
| 统一 revision 命名风格 | **不做**（不在本次范围） |
| 后续定期审计 | **建议** 每次 origin/main 有新迁移合入后跑一次 4 步验证（静态 head + prod version + 关键表 + 实际跑 `alembic check`），15 分钟内可完成 |

## 与既有记忆/规则的关系

- [[qa-system-never-fake-alembic-version-number]] 的「stamp 承认事实」精神保留：本次 audit 确认了**当时为什么需要 stamp**（0116 落后 0118/0120/0121）以及 stamp 后的目标态（0127）。
- [[qa-system-alembic-targets-prod]] 的「env.py:52 默认走 prod 5433」警示依然有效：未来任何 `alembic upgrade` 命令都**必须显式 `-x` 或环境变量覆盖**——裸跑会撞 prod。
- [[qa-system-alembic-merge-number-collision]] 的「合并撞号改号到本线 head 之后」依然适用；本次审计未发现新撞号。
