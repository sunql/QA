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
| `backend/app/utils/ci_failure_set.py` | **新增**：失败集合抽取、完成标记解析、job 可用性判定、集合对差、结论判定、CLI | 775 行 |
| `backend/app/utils/ci_text.py` | **新增**：日志**净化**（剔控制字符 / ANSI / OSC，防 CSI 擦行与 OSC 52 改剪贴板）+ **脱敏**（token / 密码 / DSN / SAS 签名） | 109 行 |
| `backend/app/utils/ci_gh.py` | **新增**：`gh` 适配层（拉 run 的 job 列表与日志，丢弃原始日志只留失败 ID 与判定） | 170 行 |
| `backend/app/utils/ci_surface.py` | **新增**：CI 相关面判定（PR 改了 workflow / conftest / pytest 配置 ⇒ 失败集合不可比） | 111 行 |
| `backend/app/tests/unit/test_ci_failure_set.py` | **新增**：57 条用例 | 1058 行 |
| `backend/app/tests/unit/test_ci_text.py` | **新增**：10 条用例（净化 / 脱敏） | 151 行 |
| `backend/app/tests/unit/test_ci_gh.py` | **新增**：16 条用例（`gh` 适配层） | 165 行 |
| `backend/app/tests/unit/test_ci_surface.py` | **新增**：6 条用例（CI 相关面判定） | 103 行 |
| `backend/app/tests/unit/test_ci_integration_step.py` | **新增**：24 条用例（14 条静态结构断言 + 10 条**行为**断言，把 CI 步骤钉在仓库里） | 550 行 |
| `.github/workflows/ci.yml` | 改 `backend-integration`：`timeout-minutes` 60→90；清单改 **NUL 分隔临时文件**；空清单/空分片/分片号非法统一走 `fail_shard`（**先打标记再退出**）；加 `SHARD_COUNT` env；加 `INTEGRATION_SHARD_DONE` 标记；`${{ matrix.shard }}` 改走 env。**并给 `backend-unit` 补同款硬化**（`fail_unit` 统一出口 + 标记 + 分片号校验） | +81/-18 行（integration）+ unit 侧加固 |
| `Harness/rules/开发流程规范.md` | **新增**「CI 合并判据（失败集合对差）」一节（8 条规则 + 退出码表含 `6` + 8 行事故表） | +42 行 |
| `Harness/rules/测试规范.md` | 补 integration 防护说明 + unit 同款硬化 + 工具化说明（111 条用例 / 12 组变异取证 / 退出码 `0`–`6`） | +39 行 |
| **不新增任何 Alembic 迁移** | 本批零 DDL、零生产写 ⇒ 合并后**不部署** | — |

**未改动**：`backend-unit` job 的分片归属（PR #99 已合，本批只补失败路径硬化）。

## 5. 实现要点

### 5.1 工具的 API（拆成 4 个模块，单文件 ≤ 800 行）

**`ci_failure_set.py`（判据主逻辑，775 行）**

```python
extractFailureIds(logText: str) -> frozenset[str]
    # FAILED <nodeid> + ERROR <file>.py（收集期错误）；正则**非锚定**
    # （GH Actions 每行带 `2026-11-02T07:10:13.4960640Z ` 时间戳前缀）

parseShardDoneMarkers(logText: str) -> list[ShardDoneMarker]
    # 匹配 <FAMILY>_SHARD_DONE shard=N status=<int>；通用（unit / integration 同族）

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

renderReport(...) -> str
    # 一切外部字符串过 sanitizeAndRedact（净化 + 脱敏）
```

**`ci_text.py`（净化 + 脱敏，109 行）**

```python
stripControlChars(text: str) -> str      # 剔 \x00-\x1f\x7f-\x9f（防 CSI 擦行 / OSC 52 改剪贴板）
sanitizeForReport(text: str) -> str      # 净化 + 截断（单字段上限 200 字符）
redactSecrets(text: str) -> str          # token / 密码 / DSN / SAS 签名
sanitizeAndRedact(text: str) -> str      # 先净化后脱敏（顺序不能反）
```

⚠️ **净化先于脱敏**：反过来的话，攻击者可用控制字符把「token」二字拆开绕过关键字匹配。

**`ci_gh.py`（`gh` 适配层，170 行）**

```python
validateRepo(repo) / validateRunId(runId) / validateSha(sha)   # 入参白名单，防注入
fetchJobPayloads(repo, runId)      # job 列表（--paginate --slurp 后在 Python 侧合并）
fetchJobLog(repo, jobId)           # 拉日志（必须 --allow-escape-sequences）
fetchRunHeadSha(repo, runId)
fetchChangedFiles(repo, baseSha, headSha)
```

**`ci_surface.py`（CI 相关面判定，111 行）**

```python
detectCiSurfaceChanges(changedFiles) -> tuple[str, ...]
    # 命中 .github/workflows/、backend/conftest.py、backend/app/tests/**/conftest.py、
    # backend/app/tests/{_pg_support,milvus_isolation}.py、
    # backend/{pyproject.toml,pytest.ini,setup.cfg,tox.ini}、
    # backend/app/utils/ci_{shard,failure_set,text,gh,surface}.py
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

**合并后的总体结果**（本地 macOS，5 个守卫文件）：

```
$ pytest app/tests/unit/test_ci_failure_set.py app/tests/unit/test_ci_text.py \
         app/tests/unit/test_ci_gh.py app/tests/unit/test_ci_surface.py \
         app/tests/unit/test_ci_integration_step.py -q
103 passed, 10 skipped in 39.06s
```

（10 条 skip = 行为部分需要 bash ≥ 4.4，macOS 自带 bash 3.2 ⇒ 本机跳过；**CI（ubuntu-latest，bash 5）必跑**。）
用例分布：`test_ci_failure_set.py` 57 + `test_ci_text.py` 10 + `test_ci_gh.py` 16 +
`test_ci_surface.py` 6 + `test_ci_integration_step.py` 24 = **113 条**。

### 6.2 「守卫不是假绿」的变异取证（12 组）

在 `/tmp/cifs_mut/backend` 这份**副本**上施加单点变异（工作区不受影响），每个变异都必须让守卫转红。
脚本：`/tmp/run_mutations.py`（跑完自动还原，并做 **md5 逐字节校验**）。

| 变异 | 含义 | 实测 |
|---|---|---|
| **M1** 放宽「跑完了」判据 `_COMPLETED_STATUSES = {0,1}` → `{0,1,2,3,4,5,137}` | 被 SIGKILL（137）杀死的 pytest 也会被当成「跑完了」 | ✅ **2 条转红** |
| **M2** 删掉「两次 run 的 job 集合必须等价」检查 | PR 少跑分片 ⇒ 假的「净新增 0」 | ✅ **2 条转红** |
| **M3** 删掉「净丢失失败要人工确认」 | 把失败藏起来也算可合并 | ✅ **2 条转红** |
| **M4** 非分片 job 真红时**不再**给不透明失败 ID | 红 frontend 以**空集合**参与对差 ⇒ 假绿 | ✅ **1 条转红** |
| **M5** 关掉「标记族名/分片号与 job 名交叉校验」 | 取错分片的标记也算数 | ✅ **2 条转红** |
| **M6** 去掉「`status=1` 必须有 pytest 收尾汇总行」 | 中途夭折的 pytest 也能贡献「真红」集合 | ✅ **1 条转红** |
| **M7** 多条完成标记不再判伪造（阈值抬到 99） | `print` 伪造标记即可洗白 | ✅ **1 条转红** |
| **M8** 允许自比自（同一 run id 填两次） | 假「净新增 0」 | ✅ **1 条转红** |
| **M9** 允许一侧 0 个 job | 空集对差恒成立 | ✅ **1 条转红** |
| **M10** CI 相关面守卫短路 | PR 改 conftest 跳过失败用例也判可合并 | ✅ **2 条转红** |
| **M11** 删掉 SAS 签名脱敏 | 签名 URL 落进报告 | ✅ **3 条转红** |
| **M12** workflow 文件移出 CI 相关面清单 | 改 `ci.yml` 不再触发不可比判定 | ✅ **4 条转红** |

```
M1-completed-statuses:   转红 2 条 | 2 failed, 71 passed
M2-coverage-parity:      转红 2 条 | 2 failed, 71 passed
M3-removed-needs-review: 转红 2 条 | 2 failed, 71 passed
M4-non-shard-opaque-id:  转红 1 条 | 1 failed, 72 passed
M5-marker-cross-check:   转红 2 条 | 2 failed, 71 passed
M6-pytest-summary-required: 转红 1 条 | 1 failed, 72 passed
M7-multi-marker:         转红 1 条 | 1 failed, 72 passed
M8-same-run-id:          转红 1 条 | 1 failed, 72 passed
M9-zero-jobs:            转红 1 条 | 1 failed, 72 passed
M10-ci-surface-guard:    转红 2 条 | 2 failed, 71 passed
M11-redact-sas-sig:      转红 3 条 | 3 failed, 70 passed
M12-surface-workflow-pattern: 转红 4 条 | 4 failed, 69 passed
完整性校验: 一致
还原后: 73 passed
```

⇒ 守卫**不是恒真**：每条判据都有对应的、会在实现被破坏时转红的用例。
⇒ 还原后**逐字节一致**（md5 校验）⇒ 「还原后那一跑的绿」建立在未被污染的代码上。

⚠️ **取证过程中发现并修掉的两处「假绿守卫」**（这本身就是取证的收益）：

| 现象 | 根因 | 处置 |
|---|---|---|
| M7 最初**未转红** | 该守卫用例造的日志缺 pytest 收尾汇总行 ⇒ 被 M6 那条规则（`status=1` 无汇总行 ⇒ 不可用）**提前遮蔽** ⇒ 断言「不可用」照样成立 | 给日志补上 `1 failed, 1 passed in 12.34s` ⇒ 遮蔽消失，M7 转红 |
| M8 最初**未转红** | 该用例没 stub `collectRunFromGithub` ⇒ 守卫被变异后真的去调 `gh` ⇒ 调用失败 ⇒ 退出码 3 ⇒ 断言**恰好**通过 ⇒ **断言空转** | 补 `monkeypatch` stub ⇒ M8 转红 |

⇒ 「守卫转红」这件事本身也会假绿；取证脚本必须做 md5 还原校验，且**守卫用例不能被别的规则遮蔽**。

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

### 6.4 静态检查

`ruff check --config backend/pyproject.toml`（line-length 110，select E/F/I/UP/B/SIM）
对三个新增/改动文件：**All checks passed**。

## 7. 评审

（待填：`code-reviewer` / `security-reviewer` 结论）

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
