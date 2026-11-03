# 变更：CI `backend-integration` 补「假绿」防护 + 失败集合对差工具化

> 分支 `feat/ci-integration-sharding`，worktree `/private/tmp/qa-schema-ssot`，base = `origin/main`（`d24c462e`）。
> **本批零 DDL、零生产写、不新增 Alembic 迁移 ⇒ 合并后不部署。**

## 1. 需求

PR #99（`backend-unit` 改 4 分片）收尾时留下三个后续项：

| # | 后续项 | 本批处置 |
|---|---|---|
| 1 | `backend-integration` 仍是「下标 mod 4」的不稳定分片，且基线 shard2 实测 49:48 / 上限 60 分钟，余量只有 **1.21×** | **拆两半**：1a（纯 CI 层防护）本批做；1b（换哈希分片）另立项（见文末「后续项」1） |
| 2 | 「失败集合对差」仍是人工流程 | 本批工具化（`app/utils/ci_failure_set.py`） |
| 3 | 合并后记结果沿用惯例（docs 直接 push main） | **不是缺陷，是惯例**；但惯例没落进规则文件 ⇒ 本批补进 [开发流程规范.md](../../rules/开发流程规范.md) |

## 2. 根因（实测，非推测）

### 2.1 `backend-integration` 与 `backend-unit` 的防护不对称

PR #99 只加固了 `backend-unit`，`backend-integration` 原封不动：

| 项 | `backend-unit`（PR #99 已加固） | `backend-integration`（= `main`，本批之前） |
|---|---|---|
| 完成标记 | `UNIT_SHARD_DONE shard=N status=` | **无** |
| 空分片硬失败 | 有 | **无** |
| 清单生成退出码 | 命令替换（可捕获） | **进程替换**（被 `mapfile` 吞掉） |
| `timeout-minutes` | 40（最重 18:57 ⇒ 余量 **2.1×**） | 60（最重 49:48 ⇒ 余量 **1.21×**） |

⇒ **integration 侧仍有完整的一条「假绿」通道**：

- **没有完成标记** ⇒ job 一旦被取消（超时 / 被抢占），它的失败集合是**空的**
  ⇒ 拿它对差会得出「净新增 0」的**假结论** ⇒ 把回归合进 main。
  这正是 PR #98 首轮 unit job 在 82% 处被取消时发生过的坑（`Harness/changes/fix-ci-unit-sharding/summary.md` §2）。
- **没有空分片硬失败** ⇒ 清单生成失败（进程替换吞退出码）静默变成「空数组」
  ⇒ pytest 不带路径参数 ⇒ 回落到 `testpaths=["app/tests"]`
  ⇒ 把 **unit+integration+e2e 整个套件**收进来，而本 job 只有 postgres/neo4j/milvus
  ⇒ 报错 + 超时 ⇒ job 被取消 ⇒ 又是假绿。
- **超时余量 1.21×** ⇒ runner 抖动 12% 就判 `cancelled`。

### 2.2 「失败集合对差」是纯人工流程，且已踩过六个坑

仓里**零实现**（`backend/scripts/` 118 个文件、`backend/app/utils/` 3 个模块都没有对差逻辑），
判据只活在变更记录和人的记忆里。人工做这件事在 PR #98/#99 批次连续踩了六个坑：

| 坑 | 实测现象 | 后果 |
|---|---|---|
| `cancelled` 的 job 失败集合是**空的** | GH Actions 把超时 job 标成 `cancelled`（**不是** `failure`） | 对差得出「净新增 0」的**假结论** |
| 缺完成标记 = 没跑完 | 同样贡献空失败集合 | 同上 |
| 日志行带时间戳前缀 | GH Actions 每行形如 `2026-11-02T07:10:13.4960640Z FAILED ...` ⇒ `grep -c "^FAILED"` 返回 **0** | 抽不出失败集合 ⇒ 对差形同虚设 |
| `comm` 要求两侧同序同 collation | python `sorted()`（码位序）与 `sort -u`（locale 序）不同序 | `comm -23` 与 `comm -13` **都非空** ⇒ 假差异 |
| **pytest 被信号杀死也会打出完成标记** | CI 脚本用 `\|\| status=$?` 捕获退出码 ⇒ OOM killer 杀掉 pytest 时 bash 报 **137**，标记照样打印 | 旧实现判「跑完了、真红、失败集合为空」⇒ **结论「可合并」** ⇒ 假绿通道 |
| **两次 run 跑的 job 不一样** | PR 把 `matrix.shard` 改窄、或给某个 shard 加 `if: false` | PR 失败集合只会**变小** ⇒「净新增 0」是假的，且 GitHub 上 CI **全绿** ⇒ 连二次防线都没有 |

### 2.3 合并判据没落进规则文件

`Harness/rules/开发流程规范.md`（合并前 178 行）里**完全没有合并判据章节**
⇒ 「失败集合对差、净新增为空才合并、不等 CI 变绿」是部落知识，换人接手就会退回「等绿灯」。

## 3. 设计

### 3.1 为什么**不改** integration 的分片归属

integration/ **确实**有跨文件副作用依赖（`Harness/rules/测试规范.md` 已记录并因此爆过两次）：

- **Milvus `qa_ci` 逻辑库惰性创建**：全仓唯一的 `create_database` 在
  `backend/app/tests/milvus_isolation.py:70`，只由 `dropOntologyCollectionsForTest`（`:83`）调，
  只被 `backend/app/tests/integration/conftest.py:256-264` 的 `milvusCleanClient` 触发
  ⇒ 全仓**只有 3 个文件能建库**，**5 个文件在消费**。
- **Neo4j `neo4jCleanDriver`**（`integration/conftest.py:87-109`）**只在 teardown 清图**，
  setup 不清 ⇒ 同分片更早文件留下的 `:Class` 残留会进到首个使用者。
- **6 个模块在导入期**调 `isNeo4jAvailable()`（早于 session 级 `isolateNeo4jEnv`）
  ⇒ CI 里恒 False ⇒ 整个模块被 `skipif` 掉
  ⇒ 唯一带 before+after 清图的 `test_ontology_relation_graph_integration.py` **从不执行**。

实测：314 个文件从「下标 mod 4」换成路径哈希 ⇒ **241/314 个文件换片**
（79/79/78/78 → 75/83/81/75）；若不先根治副作用，**净新增 ≥ 4 条**
（`test_rag_api.py` 的 4 条用例炸 `database not found[database=qa_ci]`）。

⇒ **本批不动分片归属**（保持失败集合逐条不变 ⇒ 合并判据仍是「净新增 0」）。
换哈希的前提见 §7 后续项。

### 3.2 「完成标记」把「cancelled ⇒ 空失败集」变成可机器判定

语义与 `UNIT_SHARD_DONE` 完全一致：

| 日志里 | 含义 | 失败集合 | 对差时怎么办 |
|---|---|---|---|
| 有标记，`status=0` | 跑完了，全绿 | 空 | 正常参与 |
| 有标记，`status=1` | 跑完了，真红 | **有**且可用 | 正常参与 |
| 有标记，`status ∉ {0,1}` | **pytest 没跑完**（中断/内部错误/用法错误/未收集到用例/被信号杀死） | 空或不可信 | **必须复跑该 job** |
| **无标记** | 没跑完（被取消/被抢占） | **空**（不可用） | **必须复跑该 job** |

⚠️ 「有标记」**不等于**「跑完了」——`status ∉ {0,1}` 同样不可用（实测的 `137` 假绿通道就在这里）。

⇒ 「假绿」通道从「靠人记得」变成「机器拒收」。

### 3.3 对差工具：把「不可用」变成一等的结论

`backend/app/utils/ci_failure_set.py` 的核心契约：

> **只要有任何一个 job 不可用，结论就不是「可合并」** —— 即使净新增为空。

这正是「假净新增 0」的根因所在，工具必须把它做成**硬拒绝**而不是警告。

## 4. 变更清单

| 文件 | 动作 | 体量 |
|---|---|---|
| `backend/app/utils/ci_failure_set.py` | **新增**：CLI 编排层（参数校验、两次 run 采集、CI 相关面守卫、run 新鲜度守卫、报告打印、退出码映射） | 433 行 |
| `backend/app/utils/ci_judge.py` | **新增**：**判定逻辑**（完成标记解析、job 可用性判定、集合对差、结论判定） | 473 行 |
| `backend/app/utils/ci_report.py` | **新增**：报告渲染（行数上界、失败 ID 指纹、fork 提示） | 176 行 |
| `backend/app/utils/ci_pytest_log.py` | **新增**：pytest 日志解析（失败 ID 抽取、完成标记解析、收尾汇总行识别与计数交叉核对） | 132 行 |
| `backend/app/utils/ci_head.py` | **新增**：run 新鲜度守卫（PR run 的 `head_sha` 必须等于分支当前 head） | 100 行 |
| `backend/app/utils/ci_shard.py` | **新增**：路径 sha256 哈希分片工具（unit 侧消费，带 `--nul` 输出） | 129 行 |
| `backend/app/utils/ci_text.py` | **新增**：日志**净化**（剔控制字符 / ANSI / OSC，防 CSI 擦行与 OSC 52 改剪贴板）+ **脱敏**（token / 密码 / DSN / SAS 签名） | 106 行 |
| `backend/app/utils/ci_gh.py` | **新增**：`gh` 适配层（拉 run 的 job 列表与日志，丢弃原始日志只留失败 ID 与判定） | 244 行 |
| `backend/app/utils/ci_surface.py` | **新增**：CI 相关面判定（PR 改了 workflow / conftest / pytest 配置 ⇒ 失败集合不可比） | 128 行 |
| `backend/app/tests/unit/test_ci_failure_set.py` | **新增**：66 条用例（判定 + CLI + 新鲜度守卫） | 1319 行 |
| `backend/app/tests/unit/test_ci_integration_step.py` | **新增**：38 条用例（**静态结构断言 + 行为断言**，把 CI 步骤钉在仓库里） | 910 行 |
| `backend/app/tests/unit/test_ci_shard.py` | **新增**：11 条用例（哈希分片 + `--nul` 输出） | 289 行 |
| `backend/app/tests/unit/test_ci_surface.py` | **新增**：11 条用例（CI 相关面判定 + 「覆盖工具每个模块」反向守卫） | 197 行 |
| `backend/app/tests/unit/test_ci_text.py` | **新增**：12 条用例（净化 / 脱敏） | 176 行 |
| `backend/app/tests/unit/test_ci_gh.py` | **新增**：16 条用例（`gh` 适配层） | 207 行 |
| `backend/app/tests/unit/test_ci_report.py` | **新增**：6 条用例（报告渲染） | 144 行 |
| `backend/app/tests/unit/test_ci_head.py` | **新增**：5 条用例（run 新鲜度守卫） | 105 行 |
| `.github/CODEOWNERS` | **新增**：CI 相关面（workflow + 工具 9 个模块 + 守卫 6 个文件）的复核落点 | 37 行 |
| `.github/workflows/ci.yml` | 改 `backend-integration`：`timeout-minutes` 60→90；清单改 **NUL 分隔临时文件**；空清单/空分片/分片号非法统一走 `fail_shard`（**先打标记再退出**）；加 `SHARD_COUNT` env；加 `INTEGRATION_SHARD_DONE` 标记；`${{ matrix.shard }}` 改走 env。**并给 `backend-unit` 补同款硬化**（`fail_unit` 统一出口 + 标记 + 分片号校验 + `SHARD_COUNT` 校验 + 清单改走 `app.utils.ci_shard --nul` + `trap` 清理临时文件 + `shard=-1` 非法哨兵） | +81/-18 行（integration）+ unit 侧加固 |
| `Harness/rules/开发流程规范.md` | **新增**「CI 合并判据（失败集合对差）」一节（9 条规则 + 退出码表含 `6` + 事故表 + **威胁模型**） | +42 行（评审轮再扩） |
| `Harness/rules/测试规范.md` | 补 integration 防护说明 + unit 同款硬化 + 工具化说明（**169 条用例 / 28 组变异取证** / 退出码 `0`–`6`） | +39 行 |
| **不新增任何 Alembic 迁移** | 本批零 DDL、零生产写 ⇒ 合并后**不部署** | — |

**未改动**：`backend-unit` job 的分片归属（PR #99 已合，本批只补失败路径硬化）。

## 5. 实现要点

### 5.1 工具的 API（拆成 9 个模块，单文件 ≤ 800 行，函数 < 50 行）

> **为什么要拆**：评审前 `ci_failure_set.py` 是 775 行单文件、含 4 类职责，
> `main()` 79 行 ⇒ 触发仓库的「函数 < 50 行 / 文件 ≤ 800 行」硬约束（MEDIUM-7）。
> 现按**职责**拆成 9 个模块，每个模块职责单一、可用文件名直接反查。

**`ci_pytest_log.py`（pytest 日志解析，132 行）**

```python
extractFailureIds(logText: str) -> frozenset[str]
    # FAILED <nodeid> + ERROR <file>.py（收集期错误）；正则**非锚定**
    # （GH Actions 每行带 `2026-11-02T07:10:13.4960640Z ` 时间戳前缀）

parseShardDoneMarkers(logText: str) -> list[ShardDoneMarker]
    # 匹配 <FAMILY>_SHARD_DONE shard=N status=<int>；通用（unit / integration 同族）

hasPytestSummary(logText: str) -> bool
lastPytestSummaryLine(logText: str) -> str | None
failedIdCount(logText: str) -> int
summaryCountMismatchReason(logText: str) -> str | None
    # 汇总行里的失败数 vs 抽出来的失败 ID 数 ⇒ 不一致就是「汇总行被伪造/截断」
```

**`ci_judge.py`（判定逻辑，473 行）**

```python
assessJob(job: CiJob, logText: str, logFetchError: str | None = None) -> JobAssessment
    # 判定该 job 的失败集合**是否可用**；`logFetchError` 非空 ⇒ 日志没拉到 ⇒ 不可用

collectRunFailures(jobs, logs) -> tuple[frozenset[str], tuple[JobAssessment, ...]]
    # 并集**只取可用 job** 的失败集合

diffFailureSets(base, pr) -> FailureDiff
    # Python 集合差 ⇒ **不依赖 shell `comm`**（避开 collation 不一致）

verdictFor(diff, baseAssessments, prAssessments) -> Verdict
    # 判定顺序：不可用 job → 覆盖不等价 → 净新增 → 净丢失需人工确认 → 可合并

collectRunFromGithub(repo, runId) -> tuple[frozenset[str], tuple[JobAssessment, ...]]
    # 逐 job 拉日志（丢弃原始日志，只保留失败 ID 与判定）
```

**`ci_failure_set.py`（CLI 编排层，433 行）**

```python
main(argv) -> int
    # 参数校验 → CI 相关面守卫 → run 新鲜度守卫 → 采集两次 run → 对差 → 报告 → 退出码
```

**`ci_report.py`（报告渲染，176 行）**

```python
renderReport(...) -> str
    # 一切外部字符串过 sanitizeForReport（净化 + 脱敏，顺序不可反）
    # 行数有上界（_MAX_REPORT_LINES）⇒ 攻击者不能靠海量失败 ID 埋掉报告
```

**`ci_head.py`（run 新鲜度守卫，100 行）**

```python
resolveHeadFreshness(repo, runId, runSha) -> HeadFreshness
    # PR run 的 head_sha 必须等于分支当前 head ⇒ 防止「拿旧绿 run 对差」
```

**`ci_shard.py`（哈希分片工具，129 行）**

```python
scanTestFiles(root) / assignShard(path, shardCount) / assignShards(...) / listShardFiles(...)
main(argv) -> int
    # `--nul` 输出 NUL 分隔清单（只能走 stdout 重定向，命令替换会剥掉 NUL）
```

**`ci_text.py`（净化 + 脱敏，106 行）**

```python
stripControlChars(text: str) -> str      # 剔 \x00-\x1f\x7f-\x9f（防 CSI 擦行 / OSC 52 改剪贴板）
redactSecrets(text: str) -> str          # token / 密码 / DSN / SAS 签名
sanitizeForReport(text: str) -> str      # 先净化、再脱敏、**最后**截断（单字段上限 200 字符）
```

⚠️ **净化先于脱敏**：反过来的话，攻击者可用控制字符把「token」二字拆开绕过关键字匹配。
⚠️ **脱敏先于截断**：反过来的话，跨越 200 字符边界的凭据会被截成半截而**逃过脱敏**（评审 M-2）。
（评审前的 `sanitizeAndRedact` 是**净化→脱敏→截断**，截断在最后 ⇒ 已按此顺序重写为
`sanitizeForReport`；`sanitizeAndRedact` 随之**删除**——它没有任何生产调用点，是死代码，评审 L-8。）

**`ci_gh.py`（`gh` 适配层，244 行）**

```python
validateRepo(repo) / validateRunId(runId)              # 入参白名单，防注入
fetchJobPayloads(repo, runId)      # job 列表（--paginate --slurp 后在 Python 侧合并）
fetchJobLog(repo, jobId)           # 拉日志（必须 --allow-escape-sequences）；jobId 必须正整数
fetchRunHeadSha(repo, runId) / fetchRunHeadInfo(repo, runId) / fetchBranchHeadSha(repo, branch)
fetchChangedFiles(repo, baseSha, headSha)
    # compare API 最多返回 300 个文件 ⇒ 达到上限直接**抛错**（⇒ 不可比判定）而不是静默截断
```

**`ci_surface.py`（CI 相关面判定，128 行）**

```python
detectCiSurfaceChanges(changedFiles) -> tuple[str, ...]
    # 命中 .github/workflows/、**任意深度的** conftest.py / pytest.ini / setup.cfg / tox.ini、
    # backend/app/tests/{_pg_support,milvus_isolation}.py、
    # backend/{pyproject.toml,uv.lock}、
    # backend/app/utils/ci_*.py（**工具的每一个模块**，由文件系统反查守卫）
resolveCiSurface(repo, baseRunId, prRunId) -> CiSurfaceInfo
```

CLI：

```bash
cd backend && uv run python -m app.utils.ci_failure_set \
    --repo sunql/QA --base-run <基线 runId> --pr-run <PR runId> \
    [--allow-ci-changes]
```

退出码：

| 码 | 含义 |
|---|---|
| `0` | 净新增 0、无待确认项、全部 job 可用且覆盖等价 ⇒ **可合并** |
| `1` | 净新增失败非空 ⇒ 不可合并 |
| `2` | 存在不可用 job ⇒ 必须复跑后再对差 |
| `3` | 运行错误（`gh` 调用失败、参数非法、两次 run 填了同一个 id 等） |
| `4` | 两次 run 的 job 集合不等价 ⇒ 对差不成立 ⇒ 人工确认 |
| `5` | 净丢失失败用例非空 ⇒ 须人工确认不是「把失败藏起来了」 |
| `6` | **PR 改了 CI 相关面（或判定不出）⇒ 失败集合不可比** ⇒ 须人工确认 |

### 5.2 「不可用 job」的完整判定矩阵（本批的核心）

| 情形 | 判定 | 理由 |
|---|---|---|
| 结论 `cancelled`（超时 / 被抢占） | **不可用** | GH Actions 把超时标成 `cancelled` 而非 `failure` ⇒ 失败集合是**空的** |
| 结论 `skipped` | **不可用** | 没跑 ⇒ 失败集合是空的 |
| 日志**缺完成标记** | **不可用** | 没跑完 ⇒ 失败集合是空的 |
| 结论与标记矛盾（`success` 但 `status≠0`；`failure` 但 `status=0`） | **不可用** | 两边自相矛盾 ⇒ 不可信 |
| 标记**族名/分片号**与 job 名对不上 | **不可用** | `Backend / unit shard 0` 的日志里出现 `INTEGRATION_SHARD_DONE shard=1` ⇒ 取错了分片的标记 |
| **pytest 退出码 ∉ {0,1}** | **不可用** | `2`=中断 / `3`=内部错误 / `4`=用法错误 / `5`=未收集到用例 / `128+N`=被信号杀死（`137`=SIGKILL、`143`=SIGTERM）⇒ **没跑完** |
| 真红（`status=1`）但**零失败明细** | **不可用** | 失败集合是空的 ⇒ 同样会得出假的「净新增 0」 |
| 真红（`status=1`）但**没有 pytest 收尾汇总行** | **不可用** | 收尾汇总行（形如 `2 failed, 118 passed in 812.34s`）只在 pytest 跑完整个 session 时才打印 ⇒ 缺它说明 pytest 可能只覆盖了前半截 ⇒ 失败集合可能不完整 |
| 日志里**多于一条**完成标记 | **不可用** | pytest 的 stdout 由 PR 作者可控（测试里可以 `print`）⇒ 可以**伪造**标记洗白 ⇒ 出现多条必须人工确认（**不是**「取最后一条」） |
| **job 名含 unit/integration/shard 却不匹配分片协议** | **不可用** | 协议被改过（job 名/标记族名/分片号三者对不上）⇒ 不可信 |
| **非分片 job**（如 `Frontend / typecheck + coverage`） | **按结论判定** | 无标记协议 ⇒ 只看结论；真红但抽不出明细（vitest 格式本工具不解析）⇒ 用**跨 run 稳定的不透明 ID** `job:<job 名>` 顶替 ⇒ 两次 run 可比，且「PR 把它弄红了」会作为净新增被看见 |
| 两次 run 的 **job 名集合不等价** | **判「对差不成立」**（退出码 4） | PR 少跑一个分片 ⇒ 失败集合只会**变小** ⇒「净新增 0」是假的（且 GitHub 上 CI 还会**全绿** ⇒ 连二次防线都没有） |
| **PR 改了 CI 相关面**（workflow / conftest / pytest 配置 / 本工具自身） | **判「不可比」**（退出码 6） | 改了这些 ⇒ 两次 run 的**测试语义不同** ⇒ 失败集合压根不可比 ⇒ 只能人工确认（`--allow-ci-changes` 是显式豁免口） |

⇒ **只要有任何一个 job 不可用，结论就不是「可合并」** —— 即使净新增为空。
这正是「假净新增 0」的根因所在，工具必须把它做成**硬拒绝**而不是警告。

### 5.3 `gh` 适配层的两个坑

1. `gh api …/logs` **必须**加 `--allow-escape-sequences`，否则 `gh` 直接报错：
   `the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway`。
2. 日志拉不到时（未归档）**不能**静默当成「空失败集合」⇒ 记为空日志 + `logFetchError`
   ⇒ 判定为「缺完成标记 ⇒ 不可用」⇒ 工具结论变成「必须复跑」。

另：`gh api --paginate` 在**对象形**端点上会把多页**拼接**成一个非法 JSON ⇒
改用 `--paginate --slurp`（返回页数组）再在 Python 侧合并。

### 5.4 CI 脚本：清单为什么落临时文件（而不是命令替换，也不是进程替换）

```bash
list_file=$(mktemp)
find app/tests/integration -name 'test_*.py' -print0 | sort -z > "$list_file" || {
  rm -f "$list_file"
  fail_shard "integration 测试文件清单生成失败"
}
if [ ! -s "$list_file" ]; then
  rm -f "$list_file"
  fail_shard "未找到任何 integration 测试文件"
fi
mapfile -d '' -t all_test_files < "$list_file"
rm -f "$list_file"
```

三条各自的坑：

| 写法 | 坑 |
|---|---|
| **进程替换** `mapfile -t all < <(find ...)` | 退出码被 `mapfile` **吞掉**（`mapfile` 恒返回 0）⇒ 生成器一失败就静默变成「空数组」 |
| **命令替换** `all=$(find ... \| sort)` | **命令替换会剥掉 NUL 字节** ⇒ 拿不到 NUL 分隔的列表；且换行分隔时文件名里含换行/空格就会被拆开 ⇒ 静默丢文件 |
| **落临时文件**（本批采用） | ① 退出码经 **pipefail** 传播（GH Actions 的 `shell: bash` 默认 `-eo pipefail`）；② **NUL 分隔** ⇒ 文件名带空格/换行也不被拆坏；③ 空清单 / 空分片一律硬失败 |

`${{ matrix.shard }}` 改走 `env: SHARD:`，理由与 `backend-unit` 一致：
GH Actions 会先把 `${{ }}` 展开成**脚本文本**再交给 bash 解析，直接插值就是注入面。
且 `SHARD` 在用于算术展开**之前**先做 `^[0-9]+$` 校验 + `10#$SHARD` 强制十进制
（否则 `08` 是八进制语法错误；将来矩阵改成动态来源时更是注入面）。

### 5.5 CI 脚本：统一失败出口 `fail_shard`（integration）与 `fail_unit`（unit）

```bash
fail_shard() {
  local msg=${1//'%'/'%25'}
  msg=${msg//$'\r'/'%0D'}
  msg=${msg//$'\n'/'%0A'}
  echo "::error::$msg"
  echo "INTEGRATION_SHARD_DONE shard=$SHARD status=4"
  exit 4
}
```

**所有跑不到 pytest 的失败路径都先打完成标记再退出**（`status=4` = 「pytest 没能正常跑起来」）。

理由：工具把「有标记且 `status ∉ {0,1}`」判为「跑完了但 pytest 没跑成」（**重跑即可**），
把「无标记」判为「被取消」（**要先解决超时/抢占**）。两者都要复跑，但**原因不同**
⇒ 标记让这两种情况在日志里可区分，而不是都退化成一句「没跑完」。

⚠️ **`::error::` 消息体必须转义**：`%` → `%25`、`\r` → `%0D`、`\n` → `%0A`
（GH 的 workflow command 协议会吞掉这些字符 ⇒ 消息里带它们时日志里看不到真实原因）。

**`backend-unit` 补同款硬化**（LOW-4）：它原来的失败路径是**裸退出**
（`echo "::error::…"; exit 1`）⇒ **不打完成标记** ⇒ 工具判「被取消」而不是「跑完了但没跑成」
⇒ 原因失真。现已补 `fail_unit`（打 `UNIT_SHARD_DONE shard=$SHARD status=4` 再退出）
+ 消息体转义 + 分片号校验。

守卫 `test_ci_integration_step.py` 用「`exit` 语句集合必须恰好是 `{exit 4, exit "$status"}`」
把这条纪律钉死 —— 任何人加第三个 `exit`（绕过统一出口）都会让守卫转红；
另有 4 条针对 unit job 的静态守卫（失败路径打标记 / 分片号位数校验 / 无游离 `exit` /
**完成标记必须用已校验的分片号**）。

⚠️ **完成标记里的分片号必须用已校验的数值**（`$shard_index`，不是 `$SHARD`）：
`SHARD` 来自 `matrix`，而 **workflow 文件本身 PR 可改** ⇒ `SHARD` 可能是任意文本
（含换行 ⇒ 可在日志里伪造一行完成标记）；而工具的标记正则是 `shard=(\d+)`
⇒ 非数字会让标记**解析不出来**（⇒ 判「缺完成标记」⇒ 不可用 —— fail-closed，
但真实原因「分片号非法」在日志里却像「被取消」）。
⇒ 脚本里 `shard_index=0` 占位，**校验通过后**才覆盖；
守卫 `test_marker_shard_number_is_validated` / `test_unit_marker_shard_number_is_validated` 钉死这条。

## 6. 验收

### 6.1 TDD 证据（RED → GREEN）

本分支分两轮落地：**第 1 轮**（`c9fc9581` 工具 + `a39f2771` integration 首次加固 +
`5de071cd` 文档）与**第 2 轮**（评审后的加固：清单改 NUL 分隔、统一 `fail_shard`、
分片号校验、`SHARD_COUNT`、覆盖等价检查、退出码 5）。

**第 1 轮 —— 工具本体**（`ci_failure_set.py`）：

```
RED  ：ModuleNotFoundError: No module named 'app.utils.ci_failure_set'（全红）
GREEN：21 passed
```

**第 1 轮 —— integration 步骤**：HEAD（= `main` 的 `d24c462e`）上的 run 块是

```bash
mapfile -t all_tests < <(find app/tests/integration -name 'test_*.py' -print | sort)
shard_tests=()
for ((i=${{ matrix.shard }}; i<${#all_tests[@]}; i+=4)); do … done
uv run pytest "${shard_tests[@]}" -q -p no:randomly
```

⇒ 进程替换吞退出码、无空清单/空分片硬失败、`${{ }}` 插值进脚本文本、**无完成标记**。
第 1 轮把它改成命令替换 + 硬失败 + 完成标记 + `timeout-minutes` 60→90。

**第 2 轮 —— 守卫的 RED 取的是本分支 HEAD（= 第 1 轮之后）的 `ci.yml`**
（把守卫文件复制到 `/tmp/cifs_red/` 并喂旧 workflow，复现命令见下）：

```
4 failed, 5 passed, 8 skipped
```

失败的 4 条恰好是第 1 轮之后仍缺的四件事：

```
FAILED test_shard_count_matches_matrix_length      # 没有 SHARD_COUNT
FAILED test_run_uses_nul_separated_file_list        # 用的是命令替换 + 换行分隔
FAILED test_run_validates_shard_is_decimal         # 分片号没校验
FAILED test_every_failure_path_emits_the_marker    # 失败路径 exit 1 且不打标记
```

复现（不碰工作区）：

```bash
mkdir -p /tmp/cifs_red/backend/app/tests/unit /tmp/cifs_red/.github/workflows
cp backend/app/tests/unit/test_ci_integration_step.py /tmp/cifs_red/backend/app/tests/unit/
git show HEAD:.github/workflows/ci.yml > /tmp/cifs_red/.github/workflows/ci.yml
cd backend && uv run pytest /tmp/cifs_red/backend/app/tests/unit/test_ci_integration_step.py -q
```

⇒ **4 failed, 5 passed, 8 skipped**；把同一份 workflow 换成加固版后 **9 passed, 8 skipped**。

**第 3 轮 —— 评审修复轮**（`80ede696` + 本轮）：两份评审（security-reviewer **BLOCK**、
code-reviewer **WARNING**）提出的问题按 TDD 逐条修：新增「汇总行对**所有** status 都要在」
「汇总行失败数 ≠ 失败 ID 数 ⇒ 不可用」「run 新鲜度守卫」「报告行数上界」「失败 ID 指纹」
「fork 提示」「`shard=-1` 非法哨兵」「unit 侧 `SHARD_COUNT` 校验」「unit 侧 NUL 清单」
「`trap` 清理临时文件」，并修掉两条**恒绿守卫**（详见 §6.2 与 §7）。

**合并后的总体结果**（本地 macOS，8 个守卫文件）：

```
$ pytest app/tests/unit/test_ci_failure_set.py app/tests/unit/test_ci_text.py \
         app/tests/unit/test_ci_gh.py app/tests/unit/test_ci_surface.py \
         app/tests/unit/test_ci_shard.py app/tests/unit/test_ci_head.py \
         app/tests/unit/test_ci_report.py app/tests/unit/test_ci_integration_step.py -q
150 passed, 19 skipped in 57.06s
```

（19 条 skip = 行为部分需要 bash ≥ 4.4，macOS 自带 bash 3.2 ⇒ 本机跳过；**CI（ubuntu-latest，bash 5）必跑**。）
用例分布：`test_ci_failure_set.py` 66 + `test_ci_integration_step.py` 38 + `test_ci_text.py` 12 +
`test_ci_gh.py` 16 + `test_ci_shard.py` 11 + `test_ci_surface.py` 11 + `test_ci_report.py` 6 +
`test_ci_head.py` 5 = **165 个用例函数**（含 parametrize 后**收集 169 条**）。

**同一批守卫在 bash 5.2.37 容器里的实测**（`qa-backend` 镜像）：

```
38 passed, 0 failed, 0 skipped
```

⇒ 19 条本机 skip 的行为断言在 CI 同款环境下**全部真跑且全绿**。

### 6.2 「守卫不是假绿」的变异取证（28 组）

脚本：`/tmp/ci_mutation_evidence.py` —— 对**单点**做变异，跑对应守卫，断言它**转红**，
然后**逐字节还原**该文件（还原后 `git diff` 必须为空 ⇒ 建立在未被污染的代码上）。

复现：

```bash
cd /private/tmp/qa-schema-ssot
python3 /tmp/ci_mutation_evidence.py            # 全部 28 组
python3 /tmp/ci_mutation_evidence.py M5 M6      # 只跑指定组
```

**M1–M12（继承自已合入的 12 组；模块拆分后锚点跟着搬家）**

| 变异 | 含义 | 作用域 |
|---|---|---|
| **M1** 放宽「跑完了」判据 `_COMPLETED_STATUSES = {0,1}` → `{0,1,2,3,4,5,137}` | 被 SIGKILL（137）杀死的 pytest 也会被当成「跑完了」 | `ci_judge.py` |
| **M2** 删掉「两次 run 的 job 集合必须等价」检查 | PR 少跑分片 ⇒ 假的「净新增 0」 | `ci_judge.py` |
| **M3** 删掉「净丢失失败要人工确认」 | 把失败藏起来也算可合并 | `ci_judge.py` |
| **M4** 非分片 job 真红时**不再**给不透明失败 ID | 红 frontend 以**空集合**参与对差 ⇒ 假绿 | `ci_judge.py` |
| **M5** 关掉「标记族名/分片号与 job 名交叉校验」 | 取错分片的标记也算数 | `ci_judge.py` |
| **M6** 去掉「真红必须有失败明细」 | 中途夭折的 pytest 也能贡献「真红」集合 | `ci_judge.py` |
| **M7** 多条完成标记不再判伪造（阈值抬到 99） | `print` 伪造标记即可洗白 | `ci_judge.py` |
| **M8** 允许自比自（同一 run id 填两次） | 假「净新增 0」 | `ci_failure_set.py` |
| **M9** 允许一侧 0 个 job | 空集对差恒成立 | `ci_failure_set.py` |
| **M10** CI 相关面守卫短路 | PR 改 conftest 跳过失败用例也判可合并 | `ci_failure_set.py` |
| **M11** 删掉 SAS 签名脱敏 | 签名 URL 落进报告 | `ci_text.py` |
| **M12** workflow 文件移出 CI 相关面清单 | 改 `ci.yml` 不再触发不可比判定 | `ci_surface.py` |

**M13–M20（模块拆分后新增的 8 条）**

| 变异 | 含义 |
|---|---|
| **M13** 去掉「日志里必须有 pytest 收尾汇总行」（对**所有** status） | `os._exit(0)` 的测试能让 job 带着空失败集合被当成「跑完了」 |
| **M14** 去掉「汇总行失败数 vs 失败 ID 数」交叉核对 | 伪造汇总行即可 |
| **M15** 去掉「`shard=-1` 非法哨兵」判定 | 分片号非法时原因失真 |
| **M16** 短路「run 新鲜度」检查 | 拿旧绿 run 对差 ⇒ 假「净新增 0」 |
| **M17** 去掉报告**行数上界** | 攻击者用海量失败 ID 埋掉报告 |
| **M18** 去掉失败 ID 的 **sha256 指纹** | 两个不同 ID 渲染成同一串 ⇒ 人读报告时分不清 |
| **M19** 去掉 fork 提示 | 「PR 落后于基线」这条重要提示从报告里消失 |
| **M20** run 不新鲜时不再退出码 3 | 陈旧 run 被当成有效结论 |

**M21–M28（workflow 侧，本轮新增）**

| 变异 | 含义 | 作用域 |
|---|---|---|
| **M21** 删掉 `fail_unit` 里的完成标记 | unit 侧「所有失败路径都打标记」的纪律失守 | `.github/workflows/ci.yml` |
| **M22** `shard_index=-1` 哨兵改回 `shard_index=0` | 非法分片号伪装成合法分片 0 | `.github/workflows/ci.yml` |
| **M23** 删掉 integration 侧 `trap` 清理 | 临时文件泄漏（不判据，但纪律） | `.github/workflows/ci.yml` |
| **M24** `--shards "$SHARD_COUNT"` 写死成 `4` | `SHARD_COUNT` 守卫失效 | `.github/workflows/ci.yml` |
| **M25** 去掉 `--nul` | NUL 清单退化成换行分隔 ⇒ 文件名含换行时被拆坏 | `.github/workflows/ci.yml` |
| **M26** unit 的 `SHARD_COUNT: "4"` 改成 `"5"` | unit 分片数与矩阵不一致 | `.github/workflows/ci.yml` |
| **M27** `ci_surface.py` 的清单漏掉 `judge`/`report` | 改判定逻辑的模块不再触发不可比判定 | `ci_surface.py` |
| **M28** `ci_shard.py` 的 `--nul` 输出改成换行 | NUL 清单失效 | `ci_shard.py` |

**实测结果（2026-11-03，全量重跑）**：

```
✓ M1 … ✓ M28   全部「转红（守卫有效）」
合计 28 组变异，净漏 0
```

⇒ 守卫**不是恒真**：每条判据都有对应的、会在实现被破坏时转红的用例。
⇒ 还原后 `git status` 与变异前**逐字节一致** ⇒ 「还原后那一跑的绿」建立在未被污染的代码上。

⚠️ **取证过程中发现并修掉的四处「假绿守卫」**（这本身就是取证的收益）：

| 现象 | 根因 | 处置 |
|---|---|---|
| M7 最初**未转红** | 该守卫用例造的日志缺 pytest 收尾汇总行 ⇒ 被 M6 那条规则（`status=1` 无汇总行 ⇒ 不可用）**提前遮蔽** ⇒ 断言「不可用」照样成立 | 给日志补上 `1 failed, 1 passed in 12.34s` ⇒ 遮蔽消失，M7 转红 |
| M8 最初**未转红** | 该用例没 stub `collectRunFromGithub` ⇒ 守卫被变异后真的去调 `gh` ⇒ 调用失败 ⇒ 退出码 3 ⇒ 断言**恰好**通过 ⇒ **断言空转** | 补 `monkeypatch` stub ⇒ M8 转红 |
| **M25 最初未转红** | 断言写成 `"--nul" in body`，而 `ci.yml` 的**注释**里就有 `--nul` 字面量 ⇒ 把真实调用里的 `--nul` 删掉照样绿 | 改成正则锚定**整条调用形态** ⇒ M25 转红 |
| **M5 / M6 最初未转红** | 两条守卫用例的 fixture **被别的规则遮蔽**：M5 的日志缺汇总行 ⇒ 被「缺汇总行」规则抢先判成不可用；M6 的汇总行写 `1 failed, 0 passed` ⇒ 被「汇总行失败数 ≠ 失败 ID 数」规则抢先判成不可用 | M5 的 fixture 补汇总行；M6 的汇总行改成 `1 error in 1.00s`（error 数 > 0 ⇒ 交叉核对跳过）⇒ 两条都转红 |

⇒ 「守卫转红」这件事本身也会假绿；取证脚本必须做还原校验，且
**守卫用例不能被别的规则遮蔽**（**同因掩盖**），**断言必须锚在真实代码上而不是注释上**。

### 6.3 CI 步骤语义**十连测**（真 bash 5）

用假 `find` / 假 `sort` / 假 `uv` 驱动 `backend-integration` 的 run 块
（在 `qa-system-backend` 镜像的 **bash 5.2.37** 里跑 —— macOS 自带 bash 3.2 没有 `mapfile -d ''`）：

```
OK  清单生成失败 ⇒ exit 4 + 标记 status=4 + pytest 未跑
OK  清单为空 ⇒ exit 4 + 标记 status=4 + pytest 未跑
OK  空分片 ⇒ exit 4 + 标记 status=4 + pytest 未跑
OK  分片号非数字 ⇒ exit 4 + pytest 未跑
OK  分片号越界 ⇒ exit 4 + pytest 未跑
OK  pytest 真红 ⇒ exit 1 + 标记 status=1
OK  pytest 全绿 ⇒ exit 0 + 标记 status=0
OK  分片切分：shard 0 = 下标 0 与 4
OK  带空格文件名原样传递（shard 3）
OK  带换行文件名作为一个实参传递（shard 1）

10/10 符合预期
```

⇒ **空分片绝不会退化成「跑全套件」**；**真红与被取消可凭标记区分**；
**五条失败路径全部先打标记再退出，且绝不调用 pytest**；
**文件名里的空格/换行不会把清单拆坏**。

**本轮补充：`backend-unit` 的 run 块同样有行为断言**（评审 MEDIUM-4 指出
「unit 侧的失败路径纪律**原本一条测试都没钉**」，静态守卫还因 YAML 块标量去缩进而恒绿）。

在 `qa-backend` 容器（**bash 5.2.37**）里用 mini-pytest 替身跑**真用例**：

```
$ python3 /tmp/qa_pytest_shim/run_ci_tests.py \
      /repo/backend/app/tests/unit/test_ci_integration_step.py
…（38 行 PASS）
38 passed, 0 failed, 0 skipped
```

⇒ unit 侧新增的行为断言覆盖：清单生成失败 / 清单为空 / 空分片 / 分片号非数字 /
分片号越界 / pytest 真红 / pytest 全绿 / `SHARD_COUNT` 传给分片工具 /
**带空格文件名** / **带换行文件名**（10 条），加上 13 条 integration 侧行为断言与
15 条静态结构断言 ⇒ 合计 38 条。

⇒ 本机（bash 3.2）skip 的 19 条行为断言在 CI 同款环境下**全部真跑且全绿**。

### 6.4 静态检查

`ruff check --config pyproject.toml`（line-length 110，select E/F/I/UP/B/SIM）
对本批改动的 6 个文件（`ci_shard.py` / `ci_surface.py` + 4 个守卫文件）：**All checks passed**。

函数体量：9 个工具模块里**最长 49 行**（`ci_head.resolveHeadFreshness`）⇒ 满足
「函数 < 50 行」；最大文件 473 行 ⇒ 满足「文件 ≤ 800 行」。

## 7. 评审

两份评审对同一批代码给出**互相印证**的结论：

| 评审 | 结论 | 要点 |
|---|---|---|
| `security-reviewer` | **BLOCK** | C-1（`status=0` 没有任何「session 跑完了」的证据 ⇒ 完整绕过）、H-1（标记可被**替换**而非只是重复）、H-2（失败 ID 集直接取自攻击者可控的 stdout） |
| `code-reviewer` | **WARNING** | HIGH-1（pytest 收尾汇总行可伪造 ⇒ **可演示的假绿**）、MEDIUM-2…7、LOW-8…12 |

⇒ 两边**独立地**指向同一个根因：**「证据」与「被判定的东西」来自同一条攻击者可控的流**
（pytest 的 stdout）。本轮把这条根因**显式写成威胁模型**（见 §7.3），
而不是假装能用日志解析把它堵死。

### 7.1 security-reviewer 结论的逐条处置

| 编号 | 结论 | 处置 | 落点 |
|---|---|---|---|
| **C-1** | `status=0` 时**没有任何**「pytest session 跑完了」的证据 ⇒ 判定逻辑缺一条规则 | ✅ **已修** | `ci_judge.py`：`if not hasPytestSummary(logText):` 对**所有** status 生效（原先只管 `status == 1`）⇒ 守卫 `test_assessJob_rejects_green_run_without_pytest_summary`（变异 M13） |
| **H-1** | 「恰好一条标记」只挡得住**重复**，挡不住**替换**（杀掉 CI shell 再打一条假标记） | ⏸️ **显式延后**（威胁模型 §7.3）——需要一条 pytest 进程**读不到**的信道 | 候选设计见 §7.4 |
| **H-2** | 被比较的失败 ID 集逐字取自攻击者可控的 stdout ⇒ 可伪造成「与基线一致」 | ⏸️ **显式延后**（与 H-1 同一个根因、同一份解法） | 候选设计见 §7.4 |
| **M-1** | 从不校验 `--pr-run` 对应 PR 的**当前** head ⇒ 拿旧绿 run 对差 | ✅ **已修** | 新模块 `ci_head.resolveHeadFreshness` ⇒ 退出码 3；守卫 `test_main_returns_three_when_pr_run_is_stale`（变异 M16/M20） |
| **M-2** | `redactSecrets` 跑在 200 字符**截断之后** ⇒ 跨边界的凭据只泄一半 | ✅ **已修** | `ci_text.sanitizeForReport` 改为「净化 → 脱敏 → **截断**」 |
| **M-3** | `ci_surface.py` 把脱敏跑在净化**之前**（正好是模块自己警告的顺序） | ✅ **已修** | 三个调用点统一走 `ci_text.sanitizeForReport`（净化先于脱敏） |
| **L-1** | 错误打印只脱敏、不剔控制字符 | ✅ **已修** | 错误路径统一走 `sanitizeForReport` |
| **L-2** | 临时文件没有 `trap` 清理 | ✅ **已修** | 两个 job 都加 `trap 'rm -f "$list_file"' EXIT`（变异 M23） |
| **L-3** | unit 的清单仍是换行分隔 | ✅ **已修** | unit 改走 `app.utils.ci_shard --nul`（变异 M25/M28） |
| **L-4** | 报告行数无上界 | ✅ **已修** | `ci_report.py` 的 `_MAX_REPORT_LINES`（变异 M17） |
| **L-5** | 脱敏可能让两个不同的失败 ID 渲染成同一串 | ✅ **已修** | `ci_report._reportFailureId` 在**脱敏也改动过**时补 sha256 指纹（变异 M18） |
| **L-6** | CI 相关面清单不完备 | ✅ **已修** | `ci_surface.py` 改成「任意深度的 `conftest.py` / pytest 配置」+「工具的**每一个**模块」（由文件系统反查的守卫，变异 M27） |

### 7.2 code-reviewer 结论的逐条处置

| 编号 | 结论 | 处置 | 落点 |
|---|---|---|---|
| **HIGH-1** | pytest 收尾汇总行可伪造 ⇒ **可演示的假绿** | ✅ **已修（廉价部分）** + ⏸️ **显式延后（彻底部分）**：① 「汇总行对所有 status 都要在」（堵 `os._exit(0)` 这一支）② 「汇总行失败数 ≠ 失败 ID 数 ⇒ 不可用」（堵伪造得不自洽的那一支）③ 彻底解法（同 H-1/H-2）见 §7.4 | `ci_judge.py` + `ci_pytest_log.summaryCountMismatchReason`（变异 M13/M14） |
| **MEDIUM-2** | `ci_surface` 枚举今天有哪几个 conftest ⇒ 换个层级就绕过去 | ✅ **已修** | 改成 `(?:^|.*/)conftest\.py$`（任意深度） |
| **MEDIUM-3** | compare API 最多返回 300 个文件且**静默截断** | ✅ **已修** | `_parseChangedFiles` 在达到上限时**抛错** ⇒ 走「不可比」判定（退出码 6） |
| **MEDIUM-4** | 两条静态守卫**恒绿**（YAML 块标量去缩进 ⇒ `split("\n          }")` 永不命中） | ✅ **已修** | 改为正则匹配去缩进后的形态；并**补齐 unit 侧的行为断言**（本轮新增 10 条）⇒ 变异 M21 转红 |
| **MEDIUM-5** | 「净化先于脱敏」的纪律在三个调用点被违反 | ✅ **已修** | 三个调用点统一 `sanitizeForReport` |
| **MEDIUM-6** | `fetchJobLog` 不校验 `jobId` | ✅ **已修** | `ci_gh.fetchJobLog` 校验必须为正整数 |
| **MEDIUM-7** | `main()` 79 行、`renderReport()` 54 行 ⇒ 超「函数 < 50 行」 | ✅ **已修** | 拆成 9 个模块；**最长函数 49 行** |
| **LOW-8** | `sanitizeAndRedact` 是死代码 | ✅ **已修** | **删除**，语义并入 `sanitizeForReport`（净化→脱敏→截断） |
| **LOW-9** | 失败路径用 `shard_index=0` 占位 ⇒ 真实原因失真 | ✅ **已修** | 改成 `shard_index=-1` 非法哨兵（`ci_pytest_log.INVALID_SHARD`），工具据此判「分片号非法」（变异 M15/M22） |
| **LOW-10** | 文档漂移（111 vs 113 条、550 vs 559 行） | ✅ **已修** | 本节 §4 / §6 与 `Harness/rules/测试规范.md` 的计数已按实测重写 |
| **LOW-11** | unit 侧没有 `SHARD_COUNT` 守卫 | ✅ **已修** | unit job 加 `SHARD_COUNT` env + 范围校验 + 守卫（变异 M24/M26） |
| **LOW-12** | `_runGh` 每次调用 300 s 超时、无整体截止 | ⏸️ **显式延后** | 见 §7.4 第 2 条 |

### 7.3 威胁模型（**必须写进规则文件**，否则「工具说可合并」会被误读成「可以合并」）

本工具从 **PR 作者可控的 pytest stdout** 里读「跑完了」的证据（完成标记、收尾汇总行、`FAILED` 行）。
因此：

- ✅ 它能可靠挡住**偶然**的假绿：job 被取消 / 超时 / 被抢占 / OOM 杀死 / pytest 中途夭折 /
  PR 改了 conftest 或 pytest 配置 / 两次 run 跑的 job 集合不一样 / 拿旧 run 对差。
- ❌ 它**挡不住**「作者主动伪造 stdout」：打印假汇总行、打印假 `FAILED` 行、
  杀掉 CI shell 后补一条假完成标记（安全评审 H-1/H-2、代码评审 HIGH-1 都指向这一点）。

⇒ **「工具说可合并」≠「可以合并」**。合并授权必须落在**人**身上：

1. **保护分支**（main 禁止直推）+ **必需状态检查**；
2. `.github/CODEOWNERS` 对 `.github/workflows/`、工具的**每一个**模块、守卫文件指定复核人
   （本批新增 `.github/CODEOWNERS`）。

这条已写进 `Harness/rules/开发流程规范.md`「CI 合并判据」一节的第 9 条规则。

### 7.4 显式延后项（**带理由与候选设计**，不是「忘了」）

1. **H-1 / H-2 / HIGH-1 的彻底解法 —— 让「跑完了」的证据来自 pytest 进程读不到的信道。**
   候选设计（三选一，需另行立项）：
   - **nonce**：CI 脚本在跑 pytest **之前**生成随机 nonce，作为**未 export** 的 shell 变量
     （不进 `/proc/<pid>/environ`）持有，完成标记里带上它 ⇒ 测试进程**无法**伪造。
     ⚠️ 但「未 export 的 shell 变量」在 GitHub runner（`yama ptrace_scope=1`）上是否真的读不到，
     **需要单独取证**，不能假设。
   - **junitxml**：CI 让 pytest 写 `--junitxml`，脚本再把 XML 里的计数写进标记
     （`status=1 failures=3 xml=1`）⇒ `os._exit` 不会产生 XML ⇒ 工具据此判不可用。
     ⚠️ XML 路径由 CI 脚本指定，但**文件内容仍可被测试进程改写** ⇒ 强度弱于 nonce。
   - **check-run annotation**：从 GH API 侧读结论，而不是从 job 日志读 ⇒ 需要额外 API 面。
   ⇒ 三个方案都要**先取证再落地**，本批不臆造「已经安全了」。
2. **L-12 —— `_runGh` 每次调用 300 s 超时、无整体截止。**
   一轮 run 有 8+ 个 job 时可能挂住 ~40 分钟。本批未改，因为：
   ① 改超时策略会改变「工具跑不完」的失败模式（超时 ⇒ 退出码 3 ⇒ 人工复跑），
   属于**判据语义**变更，必须走独立立项 + 变异取证；
   ② 当前最坏情况是「挂久一点」，**不会产生假绿**（fail-closed）。

### 7.5 复审前必须重新拿到的证据

- 变异取证：**28 组全部转红，净漏 0**（§6.2，含本轮新发现的 M25 / M5 / M6 三处恒绿守卫修复）。
- 守卫全量：**150 passed, 19 skipped**（本机 macOS bash 3.2）。
- 同一批守卫在 **bash 5.2.37** 容器：**38 passed, 0 failed, 0 skipped**。
- CI 步骤语义十连测：**10/10**。
- 静态检查：**All checks passed**。

## 8. 合并与发布记录

（待填：合并判据实测、CI 墙钟、完成标记纪律、合并与发布）

## 后续项

1. **integration 换哈希分片**（本批**未做**）——前置条件（按顺序）：
   ① `integration/conftest.py` 加 session 级 autouse 夹具预建 Milvus `qa_ci` 逻辑库
   （`ensureIsolatedDatabase` 幂等，见 `milvus_isolation.py:58-72`）；
   ② `neo4jCleanDriver` 在 `yield` **之前**也清一次 `:Class`/`:Property`/`:Metric`；
   ③ 6 个模块的 `isNeo4jAvailable()` 从**导入期**改到**夹具期**判定
   （否则 CI 里这些模块整体被 skip，是另一层「假绿」）；
   ④ 之后才把 `ci.yml` 的 `backend-integration` 换成 `app.utils.ci_shard`。
   ⇒ 这三条根治方向仓库自己已写下，见
   `Harness/changes/fix-schema-ssot-alignment/summary.md` §8.9。
2. **残余 36 条 `alembic check` 差异** —— 已评估为「对运行时无影响」，
   排期提醒见 MEMORY `qa-system-schema-check-constraints.md`（2026-11-09 触发）。
3. **「跑完了」证据的不可伪造化**（本批**未做**）—— nonce / junitxml / check-run annotation
   三个候选见 §7.4 第 1 条；落地前**必须先取证**（例如验证「未 export 的 shell 变量」
   在 GitHub runner 上确实读不到），不能假设。⇒ 在此之前，合并授权必须依赖
   保护分支 + 必需状态检查 + `.github/CODEOWNERS`，**不能只信工具的退出码**。
4. **`_runGh` 的整体截止**（L-12）—— 见 §7.4 第 2 条。
