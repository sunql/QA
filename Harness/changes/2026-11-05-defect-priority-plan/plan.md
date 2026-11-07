# 缺陷优先级处置计划（2026-11-05 今日待办）

> 用户 2026-11-05 指示：「统一给一个建议吧，优先解决问题，其次解决工程保障」→
> 「把这个建议写到 Harness 里面当成今天开始代办的一个计划，然后根据建议现在就起 ①②③ 的 TDD 实施」。
>
> 本文件是**今日待办计划**（不是变更 SSOT）。本批实施完成后另写
> `summary.md` 作为该批的 SSOT。
>
> **本批零 DDL、零生产写、零生产读。**

---

## 一、为什么是这个顺序

前面盘点出的问题分两类，混在一起会让「改代码」和「加固流程」互相挤压：

- **真缺陷**：会算错数、会丢数据、会静默破坏用户已录入的内容 ⇒ 影响业务正确性。
- **工程保障**：合并判据、分支保护、证据可伪造性 ⇒ 影响「缺陷能不能被拦住」。

真缺陷一旦上线，后面所有保障都只是在给 bug 装护栏。所以**先修缺陷，再补保障**。
但保障不是「以后再说」——它决定了下一个缺陷会不会重演，故排在本批之后、不砍掉。

## 二、第一梯队（真缺陷，本批起逐项消）

| # | 缺陷 | 影响面 | 状态 |
|---|---|---|---|
| ① | 「最近 N 天」时间窗口多算一天（Oracle 日期算术模板下界写成 `-N`，且缺上界） | NL2SQL 生成的 SQL 口径错 ⇒ 所有「近 N 天」类问句结果偏大 | **本批实施** |
| ② | MEASURE 类型指标编辑保存 ⇒ 历史分母（及 `class_id`/`class_alias`）被清空 | 前端 `buildMetricPayload` 显式提交 `denominatorMeasure: null` ⇒ 后端整块替换 JSONB ⇒ 用户已录入的度量定义丢失 | **本批实施** |
| ③ | L4 历史回放不还原 `steps` | 研究线回放看不到多步过程 | **已修**（PR #78 / main `37f72a76`，快照落库 `session_message.query_plan` / `steps_snapshot`，迁移 0138）⇒ 本批不做；MEMORY 索引行过期，需更正 |
| ④ | 研究入口 UX 7 项缺口 | 研究线可用性 | **已修**（2026-10-05，`feat-research-entry-ux-fixes`，`Harness/changes/feat-research-entry-ux-fixes/summary.md`）：7 项全部落地；其中 5 项是「能力已在、暴露缺失」（数据已到 store / 后端已就绪，只是没渲染/没回显）⇒ **本批不做**（⚠️ 该计划表原写「待立项」= **过期**，2026-11-07 更正） |
| ⑤ | THBI 业务图回填（404 根因=子图为空） | 业务图谱页面 | 待立项（**生产写，须用户先批准 SQL**） |
| ⑥ | 测试会删生产 Milvus / Neo4j 数据 | 一次误跑即掉线上数据 | **已修**：Milvus 侧（2026-09-30）独立逻辑库 `qa_test` + `_guardIsolatedDatabase()` 硬守卫；Neo4j 侧（2026-10-03）`TEST_NEO4J_URI` **fail-fast** + `installTestNeo4jEnv` + `assertAppNeo4jIsIsolated` 三层 ⇒ **本批不做**（⚠️ 该计划表原写「待立项」= **过期**，2026-11-07 更正） |
| ⑦ | Milvus delete-visibility | 向量删除不生效 | 待立项（**下一批第二优先**，2026-11-07 复核后细化）。范围要**拆两半**：<br>· **测试侧（可先做，零生产风险）**：`backfill_milvus_external_id.py` 的 delete 后缺 `release`+`load` ⇒ 3 个抖动用例（`test_backfill_is_idempotent` 等）⇒ 修好后 B7 白名单里那一条可以**退休**（止血 → 根治的闭环）；另两个「固定业务 id ⇒ 单调累积」的用例改随机 id。<br>· **产品侧（需口径，先别做）**：`wiki_vector_service.syncPage` 先 delete 再 insert ⇒ 删除可见性延迟期间**检索可能拿到旧 chunk** ⇒ 这是**用户可见的真缺陷**，但修法（delete 后 `release`+`load` / 轮询等待可见 / 一致性级别）各有延迟与语义代价 ⇒ 需产品口径确认 ⇒ 单独立项 |

### ① 的根因与范围

- 根因：方言日期算术规则（`backend/app/services/nl2sql_dialects.py`）里 Oracle 的
  「最近 N 天」模板写成 `WHERE date_col >= CURRENT_DATE - N` ⇒ 覆盖 N+1 天；
  且无上界 ⇒ 未来数据也会被算进来。
- 同族：自然月窗口 `-N` 多算一个月已在 PR #89 修好（`-(N-1)` + 上界）；
  PostgreSQL / MySQL 规则未给「最近 N 天」模板，同样存在多算一天的风险。
- 边界：`app/services/time_scope_resolver.py` **刻意不解析**「最近 N 天」这类滚动窗口
  （业务上「滚动 vs 自然边界」有歧义）⇒ 修在 prompt 模板层，不动解析器。

### ② 的根因与范围

- 根因：`MetricTechniqueForm` 只对 `RATIO` / `DIFFERENCE` 渲染分母行；
  `MEASURE` 下分母字段**从未注册到 antd 表单** ⇒ `values.denominatorAgg` 为 `undefined`
  ⇒ `toMeasureSpec` 按设计返回 `null` ⇒ `buildMetricPayload` 显式提交
  `denominatorMeasure: null` ⇒ 后端 `ontology_service.updateMetric`
  （`dto.denominator_measure.model_dump() if dto.denominator_measure else None`）
  整块替换 JSONB ⇒ 历史分母连同 `class_id` / `class_alias` 一起被抹掉。
- 修法：表单**未暴露**分母的行时，`existing.denominator` 非空 ⇒ **原样回传**；
  表单暴露了（RATIO/DIFFERENCE）⇒ 仍由表单值决定（含用户显式清空 ⇒ `null`）。

## 三、第二梯队（工程保障，本批之后按序排）

| # | 项 | 状态 |
|---|---|---|
| A1 | merge-gate「净新增拦截」证明（缺一个含真失败的 PR 取证） | **已取证完成**（2026-11-07，探针 PR #109 → **已关闭、绝不合并**、分支已删）：闸门 check `128027482407` **conclusion=failure**、title「退出码 1 ⇒ 不可合并」、summary 首行「结论：不可合并（NEW_FAILURES）」；基线 `42249797523`（main `06f56d48`）失败 **139** 条 / PR `42249940998` 失败 **140** 条 ⇒ **净新增 1 条 / 净丢失 0 条**，唯一净新增 = `app/tests/unit/test_a1_merge_gate_probe.py::test_a1_probe_intentional_new_failure`；PR `mergeable: MERGEABLE`（无冲突）+ `mergeStateStatus: BLOCKED`（被唯一必需检查挡下）⇒ **证据缺口补齐**：B7 批只证明「净新增 0 时怎么判」，本批补上「PR 真有失败时确实拦住」。证据见 `summary.md` §11.1 |
| B1 | integration 分片改哈希分组（当前按文件序 ⇒ 改文件即换分片归属） | 未启动 |
| B2 | 残余 36 条 alembic 差异（16 条零 DDL + 6 条需 DDL） | 已评估无运行时影响，排期 **2026-11-09** |
| B3 | 不可伪造证据（当前 stdout 可控 ⇒ 挡得住偶然假绿、挡不住蓄意伪造） | 未启动 |
| B4 | `_runGh` 调用加截止时间 | 未启动 |
| B7 | 抖动容忍（白名单类机制，只许 main 上 CODEOWNER 维护） | **已实施并合并**（2026-11-06，PR #108 → main `88599b19`，`Harness/changes/2026-11-06-merge-gate-flake-whitelist/summary.md`）：对称豁免 + 精确 node ID + 失败关闭 + 报告显式可见 + **④ 豁免条件化在「该用例所在文件本次未被改动」**；白名单文件 `.github/ci-known-flaky-tests.json` 属 CI 相关面且由 CODEOWNER 维护，闸门 workflow **显式** checkout 默认分支 ⇒ PR 不能自我豁免。⚠️ 落地**依赖 owner 越权口**（`enforce_admins: false`）⇒ **仍必须先于把 `enforce_admins` 收回去**，顺序不能反（见 `summary.md` §10.6 / N7） |
| C1 | PR #92（docs/deploy-prod-1031）收口 | **已合并**（2026-11-07 → main `81b865af`）：分支已 `merge origin/main` **重建**（tip `e9e87f0c`）⇒ `git diff origin/main HEAD` 恰 1 个文件（1031 生产发版记录，**原不在 main** ⇒ 必须合）；**未 force-push**（用 merge 保住 PR 历史）；闸门 check `128032047830` **conclusion=success**（「净新增 0 ⇒ 可合并」；基线 139 / PR 140，**计入对差 139** ⇒ 净新增 0 / 净丢失 0）；内容级仲裁：squash tree 与分支 tip tree **零差异**；main 净变更恰 **1 文件 +101 行**；分支已删 ⇒ **远端现只剩 `main`** |
| C2 | 5 个 diverged 旧分支盘点 | **已完成**（2026-11-07）：内容级仲裁后 **4 条删除**（`docs/deploy-prod-1101-zhlabels` / `feat/chat-output-sync` / `feat/chat-output-zh-labels` / `feat/l4-agent-trace`，全部内容已在 main）、**1 条转 C1**（`docs/deploy-prod-1031`，已合并并删分支）；**远端分支现只剩 `main`**。仲裁口径与 tip SHA 留档见 `summary.md` §11.2 |

## 四、三条执行纪律

1. **先红后绿**：每个缺陷先写一条**能复现该缺陷**的失败测试并亲眼看到它因该缺陷失败
   （不是因拼写/环境失败），再写最小实现；改完跑邻近套件确认**净新增失败 0**。
2. **合并判据不变**：失败集合对差、**净新增为空**才合并；不等 CI 变绿。
   红基线是既有事实（~139 条），不是本批造成的。
3. **每项完工即回填**：结论写进本目录 `summary.md`（SSOT）+ 对应 MEMORY 条目，
   过期记忆当场更正（③ 已证明记忆索引行会过期）。

## 五、验收口径（本批）

| 项 | 口径 |
|---|---|
| ① | Oracle 规则文本必须含 `-(N-1)` 下界与上界；不得出现裸 `CURRENT_DATE - N`；PG / MySQL 规则给出同口径的「最近 N 天」模板；回归测试断言模板文本 |
| ② | 「MEASURE + 非空历史分母」的指标，编辑只改名字后保存 ⇒ `denominatorMeasure` 与原值**逐字段相等**（含 `class_id` / `class_alias`）；RATIO 显式清空分母 ⇒ 仍为 `null`（回归不倒退） |
| ③ | 标注「已修（PR #78）」，MEMORY 索引行更正 |
| 本批整体 | 定向测试全绿；邻近套件**净新增失败 0**；零 DDL、零生产写；不部署 |

## 六、文件清单（完工后回填 —— 计划期快照与落地结果的漂移）

| 文件 | 动作 |
|---|---|
| `backend/app/services/nl2sql_dialects.py` | 改：Oracle「最近 N 天」模板；补 PG / MySQL 的「最近 N 天」模板 + 首句窗口警告；去掉 PG/MySQL 的孤号「②」 |
| `backend/app/tests/prompts/test_nl2sql_dialects.py` | 改：新增「最近 N 天」模板回归用例（断言**整段子句**）+ `_normalized()` 辅助 |
| `frontend/src/utils/metricTechnique.ts` | 改：新增 `needsDenominatorMeasure` / `resolveDenominatorMeasure` / `sanitizeMeasureSpec` |
| `frontend/src/components/ontology/MetricTechniqueForm.tsx` | 改：改用 `needsDenominatorMeasure`，删本地常量（计划期漏列） |
| `frontend/src/components/ontology/MetricTab.tsx` | 改：`buildMetricPayload` 调用新函数；`ExistingMeasures` 增加 `metricType`（库中原类型） |
| `frontend/src/tests/metricTechnique.test.ts` | 新建：纯函数契约（计划期漏列） |
| `frontend/src/tests/MetricTab.test.tsx` | 改：新增 2 条端到端（度量型带历史分母编辑保存 / 比率改度量型清空分母） |
| `Harness/wiki/nl2sql-engine.md` | 改：补「相对时间窗口契约」段（计划期漏列） |
| `Harness/wiki/kpi-metric-technical-form.md` | 改：补 §15 前端编辑契约（计划期漏列） |
| `Harness/changes/2026-11-05-defect-priority-plan/summary.md` | 新建：本批 SSOT（完工时写） |
| `Harness/changes/2026-11-05-defect-priority-plan/plan.md` | 新建：本文件（今日待办计划） |

## 七、明确不做

- 不动 `time_scope_resolver.py`（滚动窗口语义歧义是设计决策，不是缺陷）。
- 不做生产写 / 生产读（含 Milvus 19530、Neo4j 生产、Oracle 生产库）。
- 不跑全量后端 unit 套件做门禁（已知 27min / 28 红基线）⇒ 只跑定向套件。
- 不部署（零 DDL、零生产写；改动随下次常规发版生效）。
- **不改 PG / MySQL 的「近 N 个月」口径**（见下「下一批候选」）—— 那不是漏抄模板，是**语义变更**，
  需业务口径确认（今天 PG/MySQL 是滚动月，Oracle 是自然月）。

## 八、本批暴露的下一批候选（2026-11-05 记录）

| # | 项 | 依据 | 处置建议 |
|---|---|---|---|
| N1 | PG / MySQL 缺「近 N 个月」窗口模板 | 只给 PG/MySQL 问「近 6 个月」仍多算且与 Oracle 口径不一致 | 先做**业务口径确认**（PG/MySQL 要滚动月还是自然月），再决定是否统一到自然月 |
| N2 | UI 无法清理 MEASURE 的残留分母 | 表单不渲染分母行 ⇒ 无清空入口；改型已能清，但「本来就是 MEASURE + 残留」清不掉 | 待产品口径：是否禁止 MEASURE 携带分母 / 服务端做「类型 ⇔ 分母」归一 / 清理历史遗留行 |
| N3 | 指标写接口无 `requireAdmin` / 归属校验（`backend/app/api/v1/ontology.py:343-355`） | security-reviewer 指出，属**既有**问题 | 与本批无关，另立项（写入口的越权面） |
| N6 | 闸门的 CI 面检查用 `compare/<base>...<head>`（三点 diff）取改动文件 | 分支若用「重建 main 的 tree 的合成提交」同步，main 的 tip 就不是分支的祖先 ⇒ 三点 diff 把 main 自己的 CI 文件算成「PR 改的」⇒ 假 `CI_SURFACE_CHANGED`（本批实证，见 `summary.md` §10.5） | **已修（2026-11-07，第三版定稿 + 第四版 CRITICAL-1 修订）**：**判定集合 = `A ∪ P` ∩「CI 相关面清单」**，其中 `A` = main 在 `[M_base, M_T]` 之间的内容推进（树对树）、`P` = PR 自己改的文件（三点语义，分叉点 = `merge_base(M_T, PR 头)`）、`M_T` = **PR run 事件派发那一刻 main 的 tip**。实测口径差 14 倍（三点 158 vs 净变更 11）。 **⚠️ 为什么不是 PR 头**：`ci.yml` 的三个 `actions/checkout` **都不带 `with: ref:`** ⇒ PR 的 CI 实际检出 `refs/pull/N/merge` ⇒ 跑的是 **merge(main@run 时点, PR 头)**（实证 PR #111：run `head_sha`=`1d226997…`，job 日志 `HEAD is now at 23db713 Merge 1d226997… into 87f8e8e6…`）⇒ 拿 PR 头比 ⇒ ① main 改 CI 文件 ⇒ **系统性假拦**（PR #92/#94/#95/#107 的净变更 CI 命中 1/29/29/17，三点归因命中 **0**）；② 更糟 —— **fail-open**（PR 头树与基线树同样旧 ⇒ 差异为空 ⇒ 判「没改」）。 **⚠️ 为什么也不取「测试合并 commit 的树」**：`pulls/{n}.merge_commit_sha` 是 `refs/pull/N/merge` 的**当前值**（main 一推进就漂移；PR 合并后 404）⇒ **回溯历史 run 时根本取不到** ⇒ 改用 `A ∪ P` 在算法上合成（合并结果的树逐路径看 = 「PR 动过就取 PR 的版本，否则取 `M_T` 的版本」⇒ 路径级差异 = `A ∪ P`）。 **两边都不能少**：只算 `P` ⇒ fail-open；只算 `A` ⇒ PR 改了判据却看不见 ⇒ 假放行。 **`M_T` 怎么取**（工具侧**独立取**，不许闸门代传）：上界 = PR run 的 **`created_at`（事件派发时刻）** + 120 秒宽限 ⇒ 取 main 的 **push run 列表**，逐条过三条硬条件（`event=="push"` ∧ `head_branch=="main"` ∧ `head_repository==本仓库`，大小写不敏感）⇒ 再过时间窗 ⇒ 取最新一条；一条都不合格 ⇒ **判定不出来（退出码 6）**。 **⚠️⚠️ 上界锚 `created_at` 而不是 `run_started_at`（2026-11-07 代码评审 CRITICAL-1）**：`pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结** ⇒ 复跑**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit ⇒ 它的 main 侧底座是**派发那一刻**的 main（实测 PR #105 run `41908498247` attempt=2：`created_at=20:38:19Z`、`run_started_at=22:24:11Z`，合并 commit `57a267a9` 的 committer.date=`20:38:18Z`、第一父=`83581e10`，attempt-2 日志写着 `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`）⇒ 锚 `run_started_at` ⇒ `M_base` 与 `M_T` 双双落到更晚的 main ⇒ `A=∅` ⇒ 两次 run 实际跑的 CI 定义不同却判「可合并」⇒ **fail-open**。 ⇒ **工具侧（`ci_gh.mainTipCutoff`）与 workflow 侧（`PR_RUN_CREATED_AT`）必须同时换锚**；且 `mainTipCutoff` 的两个时间戳改成 **keyword-only** 传（这次缺陷的成因正是「位置传参 + 锚点写反」⇒ 传反了**不会报错** ⇒ 静默用错锚点）。 **基线取法同步收紧**：基线 = **PR CI 事件派发之前**完成的 main push run（workflow 侧 `created_at <=` 窗口，值取 `created_at`、为空则 fail-closed）⇒ 消除系统性假拦。 **为什么判定用 `A ∪ P`**：两次 run 跑的 CI 定义若本身就不同（main 改了 CI 而 PR 没跟上）⇒ 失败集合不可比 ⇒ 交集判定会 **fail-open 放过去**。 **为什么不用 compare 的 `files`**：最多 300 项 ⇒ 大 PR 静默截断 ⇒ fail-open ⇒ 只从 compare 取 `merge_base_commit`，差异自己算（`base..head` 实测 404 ⇒ REST 只支持三点）。 **基线可信性**：基线必须是本仓库 main 上的 **push run**（GitHub `runs?branch=` 不区分事件 ⇒ 叫 main 的 fork 分支会命中）⇒ 否则退出码 3。 **fail-closed**：`truncated=true`/缺失/非布尔、拉取失败、判不出 `M_T` ⇒ 判定不出来 ⇒ 退出码 6，**不退回三点 diff、不退回 PR 头**。 变异取证 **58 组全红、净漏 0**（脚本全量重跑；其中本批新锚/新守卫 = M37–M58）。 详见 `2026-11-07-merge-gate-net-diff-ci-surface/summary.md`。 **①（分支以 main 的 tip 为底座起）只写进 `Harness/rules/开发流程规范.md` 作为过程规则**——工具无法强制 |
| N7 | `merge-gate.yml` 预设的「bypass 账号」在**个人仓库**里不存在 | 安装分支保护时用了 `enforce_admins: true` 且无 bypass 名单 ⇒ exit 5 分支要求的「人工确认后显式合并」**无路可走**；个人仓库又不支持 `bypass_pull_request_allowances.users`（GitHub 报 `Only organization repositories can have users and team restrictions`）⇒ 只能靠 `enforce_admins`（本批实证，见 `summary.md` §10.6） | 已按 GitHub 原生表达装成 `enforce_admins: false`（owner 越权口），其余策略不变。**代价**：必需检查对 owner 变成软约束 ⇒ **必须先实现 B7 再考虑收紧**（B7 改的是 CI 面 ⇒ 落地本身要靠这条越权口 ⇒ 顺序不能反）。**2026-11-06 更新**：B7 已合并（main `88599b19`）⇒ 这条顺序约束**已解除**，但收紧方案见 N8 |
| N8 | 把 `enforce_admins` 收回去（恢复 owner 的硬约束） | B7 已落地 ⇒ 顺序约束解除；但直接改回 `true` ⇒ **任何**改 CI 相关面的 PR（闸门代码本身、白名单文件、CODEOWNERS、CI workflow）都判退出码 6 = failure ⇒ **谁都不能合并** ⇒ 死锁 | **不要直接翻开关**。先设计「涉及 CI 相关面的改动」的**明确人工合并通道**（候选：闸门在「改动面仅限 CI 相关面 且 改动者为仓库唯一 owner」时给出带说明的人工裁决出口；或引入 CODEOWNER 复核要求；或给白名单/闸门改动单独的、显式记录的放行机制）。通道落地并验证后，才用 `PUT` 改回 `enforce_admins: true`（**PATCH 一律 404**） |

## 九、执行记录（2026-11-05）

| 项 | 结果 |
|---|---|
| ①「最近 N 天」窗口 | **已修**：Oracle/PG/MySQL 三方言模板统一为「下界 -(N-1) + 上界 +1」；三方言各一条整段子句断言；变异取证 4 项全红（改前 PG 那条是假绿） |
| ②MEASURE 编辑清空历史分母 | **已修**：三条规则（暴露⇒表单值 / 原比率改型⇒清空 / 原来就不需要⇒原样回传）+ `sanitizeMeasureSpec`（只带 schema 允许的 5 键、返回新对象） |
| ③L4 回放还原 steps | **本批不做**（PR #78 已修）；MEMORY 索引行已更正 |
| 本批 diff | 11 个文件（后端 2 + 前端 4 + 文档 5）；相对 `origin/main` 的净变更已核对（`git diff origin/main HEAD`） |
| 测试证据 | 后端 153 passed；前端定向 36 passed；前端邻近套件 101 passed；`npx tsc --noEmit` exit 0 |
| 合并判据 | 失败集合对差、净新增为空才合并（不等 CI 变绿）；见 `summary.md` §10 的闸门实操记录 |
| 合并 | **已合并**（2026-11-06）：squash → main `6bef7b6a`；闸门最终判 `NEEDS_REVIEW`（净新增 0 / 净丢失 1），按 `merge-gate.yml` 自述的 exit 5 分支做人工仲裁后由 owner 越权口显式合并；内容级仲裁通过（合并 tree == 分支 tree，净变更恰 11 个文件）；详见 `summary.md` §10.6 |
| 部署 | **不部署**（零 DDL、零生产写、零生产读） |

### 9.2 B7 批合并回填（2026-11-06）

| 项 | 结果 |
|---|---|
| B7 抖动容忍白名单 | **已合并**（PR #108 → squash main `88599b19`）：对称豁免 + **四条硬约束**（含④「豁免条件化在用例所在文件本次未被改动」）；白名单 `.github/ci-known-flaky-tests.json` 属 CI 相关面 + CODEOWNER 维护 + workflow 显式 checkout 默认分支 ⇒ PR 不能自我豁免 |
| 闸门最终判定 | 退出码 **6**（`CI_SURFACE_CHANGED`，本批改 CI 相关面 ⇒ 预期路径）；基线 `42092597549`（attempt 2）失败 **139** 条 / PR `42239883087` 失败 **139** 条 ⇒ **净新增 0 / 净丢失 0** |
| 人工仲裁（退出码 6） | **分片归属未变**：9 个分片逐片条数两侧完全一致（unit 9/4/1/3、int 17/44/26/35、frontend 0）⇒ 失败集合可比 ⇒ 仲裁通过 ⇒ 由 owner 越权口显式合并 |
| 中途假净新增 | 基线 attempt 1 的 `integration shard 3` 结论 `cancelled`（撞 90 min 超时）⇒ 失败集合为空 ⇒ 35 条「净新增」全是假净新增 ⇒ **整轮** `gh run rerun`（不能只复跑单个 job）后恢复 |
| 内容级仲裁 | 合并 tree 与分支 tip tree **逐 blob 一致**（各 2566 个 blob，零差异）；相对 base `5a7ac461` 净变更恰 22 个文件（+4499 / −142）⇒ 可安全删分支 |
| 部署 | **不部署**（零 DDL、零生产写、零生产读；闸门在 GitHub 上跑 ⇒ 合并即生效） |
| 顺序约束 | B7 已落地 ⇒ 「把 `enforce_admins` 收回去」**现在才可排期**；⚠️ 收紧前必须先确认「涉及 CI 相关面的改动」有明确的人工合并通道，否则变成「谁都不能改闸门」的死锁（见 B7 `summary.md` §11.4） |

### 9.3 A1 取证 + 分支盘点收口（2026-11-07）

| 项 | 结果 |
|---|---|
| A1 探针 | **已取证**：探针 PR #109（head `6ba97fc7`）的闸门 check `128027482407` 判 **退出码 1 / `NEW_FAILURES`**（conclusion=failure）⇒ 基线 139 / PR 140 ⇒ **净新增 1、净丢失 0**，唯一净新增就是探针用例 ⇒ PR `mergeStateStatus: BLOCKED`。**取证后关闭 PR（未合并）+ 删分支**（探针内容不进 main） |
| 探针为何不叫 `test_ci_*.py` | `test_ci_*.py` 落「CI 相关面」⇒ 闸门先判 `CI_SURFACE_CHANGED`（退出码 6）⇒ 取不到 `NEW_FAILURES` 的证据 ⇒ 探针文件名必须避开 `test_ci_*` |
| C2 分支盘点 | 远端 5 条 diverged 分支：**4 条内容已在 main ⇒ 删除**；`docs/deploy-prod-1031` 的 1 个发版记录文件**不在 main** ⇒ 转 C1 合并 |
| C2 仲裁口径 | 逐分支「分支 tip 的 squash 提交 vs 分支 tip tree」逐 blob 比对 ⇒ 三条**零差异**；`feat/chat-output-sync` 仅 1 个文件有差，且方向是 **main 更新**（分支是旧版「待执行」，main 是新版「已执行」+ 实操细节）⇒ 分支无 main 缺失的内容 |
| 远端分支现状 | 只剩 `main` + `docs/deploy-prod-1031`（+ 探针分支已删） |
| 部署 | **不部署**（本批零 DDL、零生产写、零生产读） |

### 9.1 闸门实操结论（引 `summary.md` §10）

本批合并卡在 `merge-gate / failure-set-diff` 的 `UNUSABLE_JOBS` 上，根因是
`Backend / integration shard 3` 撞 `timeout-minutes: 90` 被杀（cancelled ⇒ 失败集合为空），
属 CI 基础设施 flake（基线同分片 23 min，且本批未碰任何 integration 测试文件）。
复跑过程中另暴露闸门链路的三个事实（API 滞后、闸门自己的 check run 留在 head sha 上、
已完成的 check run 删不掉）⇒ 已在 `summary.md` §10.2/§10.3 记成可复用流程，
并落 MEMORY（`qa-system-merge-gate-rerun-procedure`）。

⇒ 下一批候选 **N4**：`merge-gate` 工具应把「闸门自己创建的 check run」排除出 job 列表
（或闸门改为 UPDATE 已有 check run 而不是每次 POST 新的），否则同一个 sha 上闸门只有第一次判定可用。

⇒ 换 sha 后的第一次判定（head `7f5faac8`）是**干净的单次判定**：净新增 0 / 净丢失 1 ⇒ `NEEDS_REVIEW`。
净丢失的那一条是 Milvus 侧可见性竞态（基线红、PR 绿），仲裁记录见 `summary.md` §10.4。
⇒ 下一批候选 **N5**：闸门对「净丢失」目前只能给 `NEEDS_REVIEW`（failure），
但净丢失本质是**基线侧**的失败集合差（多半是 flake），把它做成人工仲裁项需要一条
可复用的「已知抖动白名单」通道（B7 的前置形态）——否则每次都要靠日志逐条仲裁。
