# 变更：CI `backend-unit` 改分片 job（消除超时取消导致的「假净新增 0」）

- **日期**：2026-11-02
- **作者**：Claude
- **Phase**：Phase 10 工程治理（CI 可靠性）
- **状态**：评审通过（code-reviewer + security-reviewer 两条 HIGH 均已修），待 PR
- **关联变更**：[fix-schema-check-constraints](../fix-schema-check-constraints/summary.md) §10.2 与「后续项」第 7 条（本批是那条遗留项的根治）
- **部署影响**：无（本批只改 CI 工作流与新增一个纯函数工具模块，不触碰运行时、不改 DB、不新增迁移）
- **MEMORY**：[../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-no-ci-main-can-go-red.md](../../../../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-no-ci-main-can-go-red.md)

---

## 1. 需求

`Harness/changes/fix-schema-check-constraints/summary.md`「后续项」第 7 条：

> **CI 的 `backend-unit` job 已贴着超时上限**：`timeout-minutes: 45`，而该套件基线实测 **42 分钟**
> （余量 ~7%）⇒ runner 抖动 10% 就会把 job 打成 **`cancelled`**（不是 `failure`），
> 失败集合为空 ⇒ 失败集合对差会误判成「净新增 0」。
> ⇒ ① **对差时凡 `cancelled` 的 job 必须复跑**再取集合；
> ② 根治方向：把 unit 套件按目录拆成多个 job（与 integration 的分片同理），
> 或把 `backend-unit` 的 `timeout-minutes` 放宽 —— 属独立立项，本批只登记不实施。

本批做 ②（分片），并顺带把 ① 从「靠人记」变成**有机器可查的完成标记**。

**做完是什么样**：`backend-unit` 从 1 个 job 变成 4 个分片 job，单片实测最坏 **14.1 分钟**
（本地 5 进程并行抢同一个 PG 的**保守**值），上限 40 分钟（**余量 2.2 倍**）；
整个 workflow 的后端等待时间从 ~45 分钟降到 ~15 分钟（典型值）。

---

## 2. 根因（实测，非推测）

| 事实 | 取证 |
|---|---|
| `backend-unit` 原本是**单个** job，`timeout-minutes: 45` | `.github/workflows/ci.yml`（改前 38–83 行） |
| unit 套件基线要跑 **42 分钟** | PR #98 的 CI run `41387329353`：unit job `5418 passed (42:22)` |
| PR #98 首轮 unit job 在 82% 处被判 **`cancelled`** | 同 run 的首轮 attempt；`gh api …/jobs` 的 `conclusion=cancelled` |
| 该次 run 的**变慢是全局的**（不是本批引入的回归） | 进度标记流逐字符对齐：base 的 17 个 `F` 位置与 PR 的**前 15 个完全一致**，后 3 个整体 **+3 偏移**（= 本批新增的 3 条用例）；每 72 用例 32s → 41s，**从第 1% 就慢** |
| `cancelled` 的 job **失败集合是空的** | 该 job 无 `FAILED <testid>` 行 ⇒ 「失败集合对差」得出「净新增 0」的**假结论** |

⇒ 根因是**余量不足**（42 / 45 = 93.3%），不是回归。**放宽 timeout 只是治标**：
套件还会继续变大，余量还会被吃光。**分片**同时解决三件事：

1. 单片运行时降到 ~1/4 ⇒ 余量从 1.07 倍变成 ~2.2 倍；
2. workflow 墙钟时间从 ~45 分钟降到 ~15 分钟；
3. 一个分片被取消时，其余 3 个分片的失败集合仍然可用（`fail-fast: false`）。

---

## 3. 设计

### 3.1 为什么分片归属用「路径稳定哈希」，而不是「排序后下标 mod N」

仓里 `backend-integration` 现有分片用的是：

```bash
mapfile -t all_tests < <(find app/tests/integration -name 'test_*.py' -print | sort)
for ((i=${{ matrix.shard }}; i<${#all_tests[@]}; i+=4)); do … done
```

它**不稳定**：分片归属依赖文件在排序列表里的**下标** ⇒ 增删/重命名任何一个文件，
其后所有文件的归属整体轮转一位。`Harness/rules/测试规范.md` 记录了因此爆过的两类实例
（Milvus 逻辑库 `qa_ci` 惰性创建、Neo4j 残留 Class 节点）。

⇒ 本批**不复制这个模式**。新分片按 **路径 sha256 哈希** 分配：

- 只依赖**相对扫描根的路径** ⇒ 任何增删改都**不会移动其它文件**的归属
  （把 `a/x.py` 移到 `b/x.py` 确实会改变它的分片 —— 那是「文件自己换了位置」，
  不是「别的文件被连带移动」，后者才是要防的）；
- 哈希输入是**相对扫描根**的路径 ⇒ 换个 checkout 位置分片结果不变；
- 用 `hashlib` 而非内置 `hash()`：后者受 `PYTHONHASHSEED` 随机化影响 ⇒ 跨进程不可复现。

### 3.2 `unit/` 是否存在跨文件依赖？（分片可行性的前提）

结论：**不存在**（实测 + 代码结构），依据三条：

1. `app/tests/unit/conftest.py` 的 autouse 夹具 `warmBusinessObjectRegistry(dbSession)` 依赖
   `dbSession` → `seedEngine` → `_pg_support._truncateAll` ⇒ **每个用例前全库 TRUNCATE**
   ⇒ 文件 A 造的数据不可能被文件 B 看到。
2. `unit/` 下**没有任何**建表/建索引的 DDL（`rg -i "create table|drop table|alter table"` 命中的
   全是 SQL 文本 fixture；`test_orm_check_constraints.py` 只建 `pg_temp.` 会话级临时表并 `finally` 删除）。
3. `unit/` 下的 module 级 fixture 全部是**纯文件/compose** 的
   （`test_agent_orchestration_menu_seed.py`、`test_compose_centos_overlay.py`），不产生 DB 状态。

跨文件**代码**依赖存在但无害：`test_chat_service_stream.py` /
`test_chat_domain_actor_contract.py` 从 `test_chat_service.py` import 辅助类 ——
import 走包路径 `app.tests.unit.test_chat_service`（`app/tests/unit/__init__.py` 存在），
**与该文件在哪个分片无关**。

### 3.3 CI 里每个分片有独立数据库

`backend-unit` 的每个 matrix job 各自起一个 `postgres:16-alpine` service ⇒ **各自独立的库**
⇒ 分片之间不会互相 TRUNCATE（本地复现时用 4 个库 `qa_metadata_test_s0..s3` 模拟，见 §6）。

### 3.4 `fail-fast: false` 是硬要求

GitHub Actions 的 matrix 默认 `fail-fast: true` ⇒ **一个分片失败会取消其余分片** ⇒
被取消分片的失败集合**同样是空的** ⇒ 又一次「假净新增 0」。与 `backend-integration` 一致，必须显式关掉。

### 3.5 完成标记：把「cancelled ⇒ 空失败集」变成可机器判定

每个分片跑完打印 **`UNIT_SHARD_DONE shard=N status=<pytest 退出码>`**。

⚠️ 标记必须在 **pytest 失败时也打印**（用 `|| status=$?` 捕获退出码，最后 `exit "$status"`）。
若用「pytest 成功才往下走」的写法，标记就会变成「本分片全绿」的同义词，
于是「真红」和「被取消」两种 job 都缺标记 ⇒ 又分不出来 ⇒ 判据失效。

判据（可 grep，可脚本化）：

| 日志里 | 含义 | 失败集合 | 对差时怎么办 |
|---|---|---|---|
| 有标记，`status=0` | 跑完了，全绿 | 空（本来就该空） | 正常参与 |
| 有标记，`status!=0` | 跑完了，真红 | **有**且可用 | 正常参与 |
| **无标记** | 没跑完（被取消 / 被抢占 / 前置步骤就挂了） | **空**（不可用） | **必须复跑该 job** |

这把「后续项 7 的 ①」从口头纪律变成一条可 grep 的判据。

### 3.6 「空分片」必须硬失败，否则 pytest 会去收集整个 `app/tests` 树

`pytest` 不带任何路径参数时，会回落到 `pyproject.toml` 的
`testpaths = ["app/tests"]` ⇒ **unit + integration + e2e 全部被收进来**。

而 `backend-unit` 这个 job **只起 postgres 服务**，没有 neo4j / milvus ⇒ 集成用例大面积报错，
再叠加超时 ⇒ job 被判 `cancelled` ⇒ 失败集合为空 ⇒ 又一次假「净新增 0」。

两个必须堵住的入口：

1. **分片生成器的退出码**：用 `mapfile … < <(cmd)`（进程替换）时，`cmd` 的退出码被
   `mapfile` 吞掉（`mapfile` 恒返回 0）⇒ 生成器一失败就静默变成「空数组」。
   ⇒ 改用**命令替换** `shard_list=$(cmd)`（退出码可捕获）。
2. **数组长度**：`[ -z "$shard_list" ]` ⇒ 硬失败退出 1。

⇒ **空分片必须硬失败，绝不能退化成「跑全套件」**。

---

## 4. 变更清单

| 文件 | 动作 | 说明 |
|---|---|---|
| `.github/workflows/ci.yml` | 修改 | `backend-unit` 改为 4 分片 matrix job；`fail-fast: false`；`timeout-minutes: 45 → 40`；pytest 步骤改用分片清单 + **空分片硬失败** + **带退出码的完成标记**；`matrix.shard` 经 `env:` 传递 |
| `backend/app/utils/ci_shard.py` | 新增 | 稳定哈希分片分配器（纯函数，无 IO 副作用）+ CLI（一行式错误，不吐 traceback） |
| `backend/app/tests/unit/test_ci_shard.py` | 新增 | 10 条守卫用例（含 3 条变异取证 + 覆盖 CI 真正消费的 `listShardFiles`） |

**不变的部分**：

- ❌ **不改** `backend-integration` 的分片方式（仍是下标 mod 4）—— 改它会动既有分片归属，
  而 integration/ **确实**有跨文件副作用依赖（见 §3.1），必须另案评估；
  已登记为后续项。
- ❌ **不做 deselect**：既有红基线仍由 CI 如实暴露（防假绿）。
- ❌ **不碰**运行时、DB、迁移、生产。

---

## 5. 实现要点

### 5.1 分片清单怎么算

`app/utils/ci_shard.py`：

```python
def assignShard(rootRelativePath: str, shardCount: int) -> int:
    digest = hashlib.new(_HASH_ALGORITHM, rootRelativePath.encode("utf-8")).hexdigest()
    return int(digest, 16) % shardCount
```

`listShardFiles(root, shardCount, shardIndex)` 返回**相对当前工作目录**的 POSIX 路径，
可直接喂给 pytest；CLI 形式：

```bash
uv run python -m app.utils.ci_shard --root app/tests/unit --shards 4 --shard 2
```

CI 侧的消费方式（**注意不是进程替换**，理由见 §3.6）：

```bash
shard_list=$(uv run python -m app.utils.ci_shard \
  --root app/tests/unit --shards 4 --shard "$SHARD") || {
    echo "::error::分片清单生成失败（shard=$SHARD）"; exit 1; }
[ -z "$shard_list" ] && { echo "::error::空分片"; exit 1; }
mapfile -t shard_tests <<< "$shard_list"
status=0
uv run pytest "${shard_tests[@]}" -q -p no:randomly || status=$?
echo "UNIT_SHARD_DONE shard=$SHARD status=$status"
exit "$status"
```

### 5.2 分片均衡与耗时（实测，sha256 / 4 片）

unit/ 共 **320** 个测试文件、**5446** 个用例（本地 4 分片并行实跑，**当时只有 7 条新守卫**）：

| 分片 | 文件数 | 用例数 | 占比 | 墙钟（4 片并行抢同一 PG） | 墙钟（**独占**） |
|---|---|---|---|---|---|
| 0 | 65 | 1460 | 26.8% | 838 s（13:58） | — |
| 1 | 84 | 1219 | 22.4% | 703 s（11:43） | — |
| 2 | 84 | 1211 | 22.2% | 693 s（11:33） | — |
| 3 | 87 | 1556 | 28.6% | **846 s（14:06）** | **417 s（6:57）** |

> 复审又补了 3 条守卫 ⇒ 全量变成 **5449** 个用例（5439 + 10）。
> 真实 CI 首跑（§8.2）逐片用例数 = **1460 / 1222 / 1211 / 1556** = 5449，
> 与本地逐片数字**逐片吻合**，差额 +3 全部落在 shard 1（新守卫都在该片）⇒ 分片归属稳定可复现。

> 「4 片并行抢同一 PG」那一列是**保守值**：4 个分片 + 1 个未分片基线套件共 5 个 pytest
> 进程**同时抢同一个 PostgreSQL 实例**跑出来的。CI 里 4 个分片各占一台 runner、
> 各用一个**独立库** ⇒ 不会有这种互相拖慢。
> 「独占」一列 = 最重的 shard 3 **单独跑**（自己的库、无竞争）实测 **6:57**，
> 失败集合与并行跑时**逐条相同**。

⇒ 上限取值：

- 保守口径（14.1 min，含竞争）+ CI setup ~4 min ⇒ 最坏 ~18 分钟 ⇒ 40 分钟上限 = **2.2 倍余量**；
- 真实口径（6.96 min，无竞争）+ setup ⇒ ~11 分钟 ⇒ **3.6 倍余量**。

`timeout-minutes: 40` ⇒ **余量 2.2 倍起**（旧配置是 1.07 倍）。

> 为什么不给更紧的上限：本批要消灭的正是「超时 ⇒ cancelled ⇒ 失败集合为空 ⇒ 假绿」。
> 上限给紧的代价（偶发假绿）远大于给松的代价（极端慢天多等几分钟）⇒ **宁松勿紧**。
> 但也不能无限松：40 分钟仍比旧配置「整个套件 45 分钟」少，且典型墙钟 ~11-15 分钟。

> 5 片反而更不均（sha256 的运气），故取 4 片。

**真实 CI 首跑回填（2026-11-02，run `41402551747`）**——上表是本地口径，实测值见 §8.2。

---

## 6. 验收

### 6.1 本地复现：4 分片并行（各跑各的库）+ 未分片基线对照

复现方式：4 个分片**同时**跑，每个分片用**自己独立的库**
（`qa_metadata_test_s0..s3`，模拟 CI 里「每个 matrix job 一个 postgres service」）；
另跑一次**未分片的完整 unit 套件**作基线。

**关键判据 = 两个失败集合逐条比对**（分片并集 vs 未分片基线）：

| | 失败数 | 失败用例集合 |
|---|---|---|
| 未分片基线（5439 用例） | **15** | 见下 |
| 4 分片并集（5446 用例 = 5439 + 当时新增的 7 条守卫；复审补 3 条后为 5449） | **15** | **完全相同** |

`comm -23`（只在基线里 = 分片漏掉的失败）= **空**
`comm -13`（只在分片里 = 分片新引入的失败）= **空**
⇒ **分片既没让任何失败消失，也没让任何失败新增**。

15 条既有失败（与 `.github/workflows/ci.yml` 里已登记的 14 条一致，另加
`test_compose_centos_overlay.py::test_overlay_does_not_touch_other_services` 一条）：

```
test_chat_service.py            4   test_datasource_pool.py     2
test_chat_service_stream.py     2   test_dependencies.py        1
test_compose_centos_overlay.py  1   test_intent_classify_order.py 1
test_no_sqlite_fallback.py      1   test_rate_limit.py          3
```

⇒ 本批**没有**修这些既有红（属独立批次），也**没有**引入新的红。

补充：最重的 shard 3 **单独跑**（独占、无竞争）⇒ `3 failed, 1553 passed in 417.22s (0:06:57)`，
失败集合与并行跑时**逐条相同**（`test_compose_centos_overlay` / `test_dependencies` /
`test_no_sqlite_fallback`）。

### 6.2 TDD 证据（RED → GREEN）

- RED（首次）：`ModuleNotFoundError: No module named 'app.utils.ci_shard'`
  ⇒ **收集期 1 个 error**（模块级 import 失败，不是 7 条独立红）。
- GREEN（实现后）：**7 passed**。
- 复审后补 3 条守卫（覆盖 CI 真正消费的 `listShardFiles`、扫描器与 pytest 配置的耦合、
  跨 `PYTHONHASHSEED` 确定性）⇒ **10 passed**。

### 6.3 「守卫不是假绿」的变异取证

| 变异 | 转红的用例 | 实测 |
|---|---|---|
| `listShardFiles` 改成「排序后下标 mod N」 | `test_listShardFiles_membership_is_stable_when_a_file_is_added`、`test_assignment_is_stable_across_processes` | ✅ **2 条转红** |
| `assignShard` 改用内置 `hash()`（PYTHONHASHSEED 随机化） | `test_assignment_is_stable_across_processes` | ✅ 转红 |
| 扫描器 glob 与 pytest 的 `python_files` 漂移 | `test_scanner_pattern_matches_pytest_python_files`（另带红 3 条） | ✅ **4 条转红** |

⇒ 三条性质**都不是恒绿**。

> 第一条变异是**复审新增**的取证：原先只测 `assignShards`（一行字典推导，性质几乎白送），
> 若有人把「下标 mod N」搬进 CI 真正调用的 `listShardFiles`，旧守卫会**全绿**而 CI
> 已悄悄退回不稳定分片。现在这条路径被钉死。

### 6.4 CI 脚本语义核对（不经真实 CI 的那部分）

| 项 | 核对方式 | 结果 |
|---|---|---|
| YAML 合法、`strategy` / `timeout` / `shell` 结构正确 | YAML 解析 | ✅ |
| `mapfile` 需要 bash ≥ 4（**macOS 系统 bash 是 3.2 ⇒ `mapfile: command not found`**） | 本地实跑 | CI 用 ubuntu-latest（bash 5）⇒ 没问题；**本地复现脚本已改用 Python CLI + `xargs`，不用 `mapfile`** |
| 进程替换会吞掉生成器退出码 | 本地实跑 | ✅ 已证实 ⇒ 改用命令替换 |
| 空数组喂 pytest 会收集整个 `app/tests` 树 | 读 `pyproject.toml` 的 `testpaths` | ✅ 已堵（`[ -z "$shard_list" ]` ⇒ `exit 1`） |
| pytest 失败时完成标记仍要打印 | `cmd \|\| status=$?`（`set -e` 下不触发 errexit） | ✅ |
| `matrix.shard` 走 `env:` 而非直接插进脚本文本 | 复审建议 | ✅ 已改（防将来矩阵动态化后的注入面） |
| 文件名 → argv 的引用链（无词分割、无 glob、无命令替换） | 复审实测（含 `$(touch /tmp/PWNED).py` 等恶意名） | ✅ 全部作为**单个字面 argv** 传入，`shell=True` / `os.system` / `eval` 全无命中 |

**CI 步骤语义四连测**（用假 `uv` / 假 `pytest` 驱动 run 块，验证退出码与标记）：

```
生成器失败              OK      (exit=1, 无标记, pytest 未跑)
空分片                  OK      (exit=1, 无标记, pytest 未跑)
pytest 失败             OK      (exit=1, 有标记 status=1, pytest 跑了)
pytest 全绿             OK      (exit=0, 有标记 status=0, pytest 跑了)
```

⇒ **空分片绝不会退化成「跑全套件」**；**真红与被取消可凭标记区分**。

---

## 7. 评审

- **code-reviewer**：CRITICAL 0、HIGH 2、MEDIUM 4、LOW 10。
  两条 HIGH（**完成标记只在成功时打印**、**空分片不退化成跑全套件**）与
  security-reviewer 的结论一致，**均已修**（§3.5 / §3.6）。
  MEDIUM 里「守卫没覆盖 CI 真正消费的 `listShardFiles`」「`PYTHONHASHSEED` 继承」
  「扫描器 glob 与 pytest `python_files` 脱钩」「按文件数而非用例数衡量均衡」
  **前三项已修**，第四项的处理见下。
- **security-reviewer**：CRITICAL 0、HIGH 1、MEDIUM 1、LOW 6。
  HIGH 与 MEDIUM 即上述两条，**均已修**。LOW 里「生成器退出码要可捕获」
  「CLI 要有一行式错误路径」「`subprocess` 加 `timeout`」「`split()` 改 `splitlines()`」
  「`_UNIT_ROOT` 用绝对路径锚定」**均已修**。
- **关于 MEDIUM「均衡按文件数而非用例数」**：文件数确实是弱代理
  （实测 shard 0 = 22.5 用例/文件，shard 1 = 14.5 用例/文件）。
  本批的处置是**把上限放宽到 40 分钟**（§5.2）而不是加一套「用例数均衡」机制 ——
  后者需要跑 `pytest --collect-only` 才能拿到用例数，成本高、收益低；
  而上限放宽直接消灭了「慢天超时 ⇒ 假绿」这个真风险。
  用例数基线（1460/1219/1211/1556）已记入 §5.2 备查。

---

## 8. 合并与发布记录

### 8.1 合并判据：失败集合对差（**净新增 0**）

判据不是「CI 变绿」，而是**失败集合对差**（PR run vs main 基线 run）：

| | run | head SHA | job 数 | 结论 |
|---|---|---|---|---|
| 基线（main） | `41396973529` | `1fa7f031` | 1 unit + 4 integration + 1 frontend | unit `failure` / 4×integration `failure` / frontend `success` |
| 本 PR | `41402551747` | `50003b2f` | **4 unit** + 4 integration + 1 frontend | 4×unit `failure` / 4×integration `failure` / frontend `success` |

**没有任何 job 是 `cancelled`** ⇒ 所有 job 的失败集合都可用（这一条本身就是本批要消灭的假绿来源）。

失败集合对差（按 job 类别逐片比对，`LC_ALL=C comm`）：

| 类别 | 基线失败数 | PR 失败数 | `comm -23`（基线有、PR 没有） | `comm -13`（PR 新增） |
|---|---|---|---|---|
| unit | **17** | **17** | 空 | 空 |
| integration shard 0 | 17 | 17 | 空 | 空 |
| integration shard 1 | 37 | 37 | 空 | 空 |
| integration shard 2 | 22 | 22 | 空 | 空 |
| integration shard 3 | 33 | 33 | 空 | 空 |
| **全 run 并集** | **126** | **126** | **空** | **空** |

交集 = **126**（= 两边全集）⇒ **失败集合逐条相同，净新增失败 = 0，净丢失失败 = 0**。

### 8.2 真实 CI 墙钟（4 个 unit 分片）

| 分片 | 文件数 | 用例结果 | pytest 墙钟 | job 墙钟（含 setup） | 对 40 分钟上限的余量 |
|---|---|---|---|---|---|
| 0 | 65 | 9 failed, 1450 passed, 1 skipped | 18:09 | **18:57** | 2.1× |
| 1 | 84 | 4 failed, 1218 passed | 11:14 | 12:00 | 3.3× |
| 2 | 84 | 1 failed, 1210 passed | 13:49 | 14:33 | 2.7× |
| 3 | 87 | 3 failed, 1553 passed | 15:11 | 15:53 | 2.6× |

合计：**17 failed / 5431 passed / 1 skipped = 5449 用例**
（= 本地 5439 + 本批新增 **10** 条守卫，与 §6.2 一致）。

- 最重分片 **18:57**，对 40 分钟上限余量 **2.1 倍** —— 与本地保守口径（2.2×）几乎一致，
  说明「4 片并行抢同一 PG」的本地口径确实是**保守值**。
- 对比旧配置：**整个套件**塞一个 job、基线就要 **44 分 01 秒**、上限 45 分钟 ⇒ 余量 **1.07 倍**
  （实测只差 59 秒就会被判 `cancelled`）。
- 后续项 #4（用真实 CI 墙钟回填并按需收紧）⇒ **已回填**（见 §8.2）；**本批不收紧上限**，
  理由同 §5.2 的「宁松勿紧」。

### 8.3 完成标记纪律实测

4 个 unit 分片的日志**全部**含 `UNIT_SHARD_DONE shard=N status=1`（`status=1` = 真红，失败集合可用）：

```
UNIT_SHARD_DONE shard=0 status=1
UNIT_SHARD_DONE shard=1 status=1
UNIT_SHARD_DONE shard=2 status=1
UNIT_SHARD_DONE shard=3 status=1
```

⇒ 本批设计的「**有标记 = 跑完了（失败集合可用）；无标记 = 没跑完（必须复跑）**」判据
在真实 CI 上**成立**：4 个 job 全部跑完并如实暴露 17 条失败。

### 8.4 合并与发布

- **合并**：`fix/ci-unit-sharding` → `main`，**squash merge**（PR #99）
  ⇒ 落地 **`ede1f6c8`**（`fix: backend-unit 改 4 分片 job（消除超时取消导致的「假净新增 0」）`）。
  内容级仲裁：`git diff 50003b2f ede1f6c8` = **空** ⇒ 合并结果与被验证的分支 tip **逐字节一致**。
- **不部署**：本批零 DDL、零生产写、不新增 Alembic 迁移 ⇒ **合并后不部署**。
  CI 配置与工具函数随下次常规发版生效即可（CI 本身即刻生效）。

---

## 后续项

1. **`backend-integration` 仍是「下标 mod 4」的不稳定分片** ⇒ 加删一个 integration 测试文件
   会让其后所有文件轮转一位（已爆过两次）⇒ 应改用同一套 `app.utils.ci_shard`；
   但 integration/ **确实**有跨文件副作用依赖（Milvus 惰性建库、Neo4j 只在 teardown 清图），
   ⇒ 换算法**之前**必须先消除这两处副作用，否则会引入新的偶发失败。**本批不动。**
2. **「失败集合对差」仍是人工流程**（本批只加了带退出码的完成标记，未做自动收集）⇒
   可考虑把「取每个 job 的 `FAILED <id>` 集合 + 检测缺完成标记的 job」做成脚本。
   本批的标记已经让「哪些 job 不能参与对差」变成可 grep 的判据（§3.5）。
3. ✅ **`alembic check` 的 36 条残余差异**（承接上一批「后续项」第 1 条）⇒
   **运行时影响已评估完毕（2026-11-02）：无影响** ⇒ 可安全排期。
   评估详情见 [`fix-schema-check-constraints/summary.md`](../fix-schema-check-constraints/summary.md) §11
   （含「16 条零 DDL / 6 条需 DDL」的拆分与两个写迁移的陷阱）。
4. ✅ **本 job 的 `timeout-minutes: 40` 是按「本地 5 进程抢同一个 PG」的保守墙钟定的**
   ⇒ **真实 CI 墙钟已回填**（§8.2）：最重分片 18:57，余量 2.1 倍，与保守口径一致。
   **本批不收紧上限**（「宁松勿紧」，见 §5.2）；若后续想收紧，须重新跑一轮真实 CI 墙钟取证。
