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
| ④ | 研究入口 UX 7 项缺口 | 研究线可用性 | 待立项 |
| ⑤ | THBI 业务图回填（404 根因=子图为空） | 业务图谱页面 | 待立项（**生产写，须用户先批准 SQL**） |
| ⑥ | 测试会删生产 Milvus / Neo4j 数据 | 一次误跑即掉线上数据 | 待立项（需先做环境隔离） |
| ⑦ | Milvus delete-visibility | 向量删除不生效 | 待立项（**需单独授权碰生产 19530**） |

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
| A1 | merge-gate「净新增拦截」证明（缺一个含真失败的 PR 取证） | 未完成（后续项 7 顺带取证） |
| B1 | integration 分片改哈希分组（当前按文件序 ⇒ 改文件即换分片归属） | 未启动 |
| B2 | 残余 36 条 alembic 差异（16 条零 DDL + 6 条需 DDL） | 已评估无运行时影响，排期 **2026-11-09** |
| B3 | 不可伪造证据（当前 stdout 可控 ⇒ 挡得住偶然假绿、挡不住蓄意伪造） | 未启动 |
| B4 | `_runGh` 调用加截止时间 | 未启动 |
| B7 | 抖动容忍（白名单类机制，只许 main 上 CODEOWNER 维护） | **已实施**（2026-11-06，`Harness/changes/2026-11-06-merge-gate-flake-whitelist/summary.md`）：对称豁免 + 精确 node ID + 失败关闭 + 报告显式可见；白名单文件 `.github/ci-known-flaky-tests.json` 属 CI 相关面且由 CODEOWNER 维护，闸门 workflow **显式** checkout 默认分支 ⇒ PR 不能自我豁免。⚠️ 落地**依赖 owner 越权口**（`enforce_admins: false`）⇒ **仍必须先于把 `enforce_admins` 收回去**，顺序不能反（见 `summary.md` §10.6 / N7） |
| C1 | PR #92（docs/deploy-prod-1031）收口 | 仍 open / blocked |
| C2 | 5 个 diverged 旧分支盘点 | 未启动 |

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
| N6 | 闸门的 CI 面检查用 `compare/<base>...<head>`（三点 diff）取改动文件 | 分支若用「重建 main 的 tree 的合成提交」同步，main 的 tip 就不是分支的祖先 ⇒ 三点 diff 把 main 自己的 CI 文件算成「PR 改的」⇒ 假 `CI_SURFACE_CHANGED`（本批实证，见 `summary.md` §10.5） | 两条：① 分支一律以 main 的 tip 为底座起，不用合成提交同步；② 工具侧改为按**净变更**（两点）取文件，或显式用 merge-base 语义并排除「与 base 同 tree 的合成提交」 |
| N7 | `merge-gate.yml` 预设的「bypass 账号」在**个人仓库**里不存在 | 安装分支保护时用了 `enforce_admins: true` 且无 bypass 名单 ⇒ exit 5 分支要求的「人工确认后显式合并」**无路可走**；个人仓库又不支持 `bypass_pull_request_allowances.users`（GitHub 报 `Only organization repositories can have users and team restrictions`）⇒ 只能靠 `enforce_admins`（本批实证，见 `summary.md` §10.6） | 已按 GitHub 原生表达装成 `enforce_admins: false`（owner 越权口），其余策略不变。**代价**：必需检查对 owner 变成软约束 ⇒ **必须先实现 B7 再考虑收紧**（B7 改的是 CI 面 ⇒ 落地本身要靠这条越权口 ⇒ 顺序不能反） |

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
