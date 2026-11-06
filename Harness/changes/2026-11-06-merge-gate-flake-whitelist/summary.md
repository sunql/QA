# 变更：2026-11-06-merge-gate-flake-whitelist（B7 · merge-gate 抖动容忍白名单）

- **日期**：2026-11-06
- **作者**：Claude
- **Phase**：Phase 5 工程保障（CI 合并判据工具化）
- **状态**：in-review（待合并；本批落地**依赖 owner 越权口** `enforce_admins: false`）
- **关联变更**：[2026-11-05-defect-priority-plan](../2026-11-05-defect-priority-plan/summary.md)（上一批；§10.6 记录了 PR #107 的闸门实操与越权口安装缺口 N7）
- **迁移版本**：无（本批**零 DDL、零生产写、零生产读**）
- **计划**：[plan.md](./plan.md)（本批实施计划：方案对比 + TDD 任务清单 T1–T6）
- **MEMORY**：[qa-system-merge-gate-flake-whitelist](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-flake-whitelist.md)

---

## 1. 需求

上一批（PR #107）合并时，闸门第四次取样才判出「净新增 0 / 净丢失 1」，
而那 1 条净丢失是 `app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`
—— 因 Milvus 可见性竞态**随机红**（PR #107 实证：同一份代码、两次 run 给出不同结论：基线红 / PR 绿）。

**用户视角的问题**：合并判据是「**失败集合对差，净新增为空才合并**」，
⇒ **任何一个用例在两侧的「失败与否」不同就一律拦下** ⇒ 一个**随机红**的用例会随机挡人。
按「两侧独立同分布、每侧失败概率 p」估算，闸门因这一个用例误拦的概率是 `2p(1-p)`；
实测 p≈0.5 ⇒ **约一半的 PR 被随机挡下**，每次都要人工做三件套证伪
（断言未触发 + 同代码不同结论 + diff 碰不到）。

**验收标准（可测量）**：

1. 已知抖动的用例**不再**因为「两侧结论不同」而挡下 PR ⇒ 闸门退出码从 5 变 0；
2. **不是**白名单的用例，行为**一字不变**（净丢失仍退出码 5）；
3. 白名单**读不到 / 非法** ⇒ 闸门**退出码 3**（绝不退化成「空白名单」）；
4. **PR 不能给自己开豁免**（白名单改动只在 main 上生效）；
5. **每一次豁免都出现在报告里**（不是静默放行）。

## 2. 设计评审

三个候选方案（完整对比见 [plan.md](./plan.md) §二）：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 白名单对称豁免**（**采用**） | 白名单里的 node ID 从**两侧**失败集合里都剔除后再对差 | 语义单一（「这个用例的结论不是证据」）；彻底消除该用例造成的随机拦截；新增用例只需加一行 | PR 侧若**确定性**弄红该用例也会被豁免 ⇒ 靠「报告显式提示 + CODEOWNER 门禁 + 失败关闭」兜底 |
| B. 只豁免「净丢失」方向 | 只剔除「基线红 / PR 绿」那侧 | 保住「PR 侧新红 ⇒ 拦下」的不变式 | 只解决一半场景（base 绿 / PR 红 仍随机拦，概率只从 `2p(1-p)` 降到 `p(1-p)`）；语义别扭（「结论是噪声，但只在我占便宜时」） |
| C. 在测试侧隔离（`xfail` / rerun 插件） | 让抖动用例**根本不产生 FAILED 行** | 最彻底，对差天然无感 | 改的是**被测代码与测试依赖**；该用例恰好是「裸跑会动生产 Milvus 19530」的高危用例（记忆：`裸 pytest 会删线上 Milvus 数据`）⇒ 隔离它反而制造「它被覆盖了」的错觉；且解决不了未来的新抖动 |

**决定 A**。理由与代价（写清楚，别后来人误读）：

1. **语义单一优先于「多挡一点」。** 白名单条目断言的是「**这个用例的结论不是证据**」，
   而不是「这个用例的失败只在某个方向上可忽略」。B 的方向性会让白名单语义随方向漂移，
   将来没人说得清「这条豁免到底豁免了什么」。
2. **A 的代价可控**，靠四条硬约束兜住：精确到**单个 node ID**（爆炸半径 = 1 个用例）；
   条目只在 **main** 上由 **CODEOWNER** 维护（PR 不能给自己开豁免）；
   **每一次豁免都必须出现在报告里**（不是静默放行）；
   **豁免条件化在「该用例所在文件本次未被改动」**（第四条，实施期由安全评审补上，见 §5① / §7.1 SR-H-2）。
3. **B 留作降级路径**：若将来报告里「PR 侧豁免」条目多到让人无法逐条看，再把语义收成 B。**现在不做**（YAGNI）。

**设计评审的第二个决策点：白名单文件读不到时怎么办？**

| 选项 | 行为 | 评价 |
|---|---|---|
| **失败关闭（采用）** | 退出码 3 | 白名单是**判据的一部分** ⇒ 判据读不出来 ⇒「不知道」不等于「没有」⇒ 保守拦下；与既有 `ci_surface` 的失败关闭同款纪律 |
| 视作空白名单 | 退出码照常 | 静默降级 ⇒ 一个半写坏的文件就能把豁免通道**悄悄关掉**，且无人察觉 |

**为什么失败关闭不会造成「全员死锁」**（本批专门验过）：

- 闸门由 `workflow_run` 触发 ⇒ 闸门**永远跑默认分支的代码** ⇒
  合并前，main 上既没有 `ci_flakes.py` 也没有新 CLI ⇒ **闸门行为与今天一字不差**；
- 闸门代码与白名单文件在**同一个 squash 提交**里 ⇒ 合并后 main 要么两个都有、要么两个都没有
  ⇒ 不存在「代码在、文件不在 ⇒ 每次跑闸门都退出码 3 ⇒ 全员死锁」的窗口。

## 3. 数据模型变更

**无。** 本批零 DDL、零迁移。

「数据」只有一个**纯配置文件**（不是数据库对象）：

| 文件 | 内容 | 校验时机 |
|---|---|---|
| `.github/ci-known-flaky-tests.json` | `{"entries": [{"nodeId": "...", "reason": "...", "since": "YYYY-MM-DD"}]}` | **每次闸门运行时**由 `ci_flakes.loadFlakeWhitelist` 逐条校验；任一条不合规 ⇒ 退出码 3 |

字段约束：

| 字段 | 约束 | 为什么 |
|---|---|---|
| `nodeId` | 非空；**必须是精确 node ID**（含 `::`）；**不接受**通配 / 前缀 / 文件级 | 爆炸半径 = 1 个用例 |
| `reason` | 非空字符串 | 审计者必须能读到「为什么信它」；空理由 = 无授权 |
| `since` | **必填**；必须是 `YYYY-MM-DD`，且是**真实存在的日历日期**、**不得在未来** | 老化审查的锚点；**可选 ⇒ 没有锚点 ⇒ 条目永久有效 ⇒ 白名单只增不减**；未来日期 ⇒ 该条在老化上限内**永不老化** ⇒ 老化机制对它彻底失效 |

首条 = PR #107 实证的 Milvus 可见性竞态用例（`reason` 里写清了竞态机理与实证日期）。

## 4. 接口契约变更

**无 HTTP API 变更。** 变的是一个**本地 CLI 契约**：

| 契约 | 变化 |
|---|---|
| `python -m app.utils.ci_failure_set` | 新增可选参数 `--flake-whitelist <path>`（默认 = 仓库根 `.github/ci-known-flaky-tests.json`）与 `--gate-title-file <path>`（把 check 标题写进**独立文件**） |
| 退出码 | `3` 的含义**扩大**：原先 = 「gh 调用失败 / 参数非法 / 两次 run 填了同一个 id / 某侧 0 个 job」；**新增** = 「白名单读不到 / JSON 非法 / 条目不合规 / 标题写盘失败」 |
| 报告文本 | 头部新增「（计入对差 N 条）」后缀（仅当有豁免时出现）；正文新增「已知抖动豁免 N 条」段（含**签发日期** / **老化 ⚠** / **PR 侧新增红 ⚠**）+「未命中 N 条」+「**豁免未生效**」提示；逐 job 行追加「（含已知抖动豁免 N 条）」；整体有**字节上限** `MAX_REPORT_BYTES = 20000`，截断提示在**头部** |
| **机器可读输出** | 标题走 `--gate-title-file` 指定的**独立文件**（不靠从人读报告正文里 grep）⇒ workflow 覆盖 GitHub check 的 `title` ⇒ **豁免条数出现在人第一眼看到的地方** |

> ⚠️ **退出码 3 的扩大是本批唯一一处「语义变更」**：`开发流程规范.md` 的退出码表已同步更新。
> 其余退出码（0/1/2/4/5/6）语义**一字未变**。

## 5. 实现要点

### ① `backend/app/utils/ci_flakes.py`（**新建**）

| 名字 | 职责 |
|---|---|
| `FlakeEntry` | 白名单条目（`nodeId` / `reason` / `since`），frozen dataclass；**`since` 必填** |
| `FlakeExemption` | 一次**实际发生**的豁免（`nodeId` / `reason` / `inBase` / `inPr` / `since`），frozen dataclass |
| `ExemptionOutcome` | 豁免后的两侧失败集合 + 豁免明细 + **原始条数**（`rawBaseCount` / `rawPrCount`），frozen dataclass |
| `defaultWhitelistPath()` | 仓库根 `.github/ci-known-flaky-tests.json`（由 `__file__` 上溯 3 级 ⇒ **不依赖 cwd** ⇒ CI（`cd backend` 后运行）与本地行为一致） |
| `loadFlakeWhitelist(path)` | 读 + 校验；**任何异常形态都抛 `FlakeWhitelistError`**（失败关闭） |
| `applyFlakeExemptions(base, pr, entries)` | **对称豁免**：从**两侧**剔除命中项 ⇒ 产出参与对差的集合与豁免明细；**不改动入参** |
| `isEntryStale(since, referenceDate=None)` | 老化判定（`MAX_ENTRY_AGE_DAYS = 180`）⇒ 超龄 ⇒ 报告 ⚠「老化」（**不自动失效**） |

**四条硬约束**（前三条是初始设计，第四条由 SR-H-2 追加）：

1. **精确 node ID**：只豁免清单里那**一个**用例（爆炸半径 = 1 个用例）；
2. **失败关闭**：白名单读不到 / 非法 / 条目不合规 ⇒ `FlakeWhitelistError` ⇒ 退出码 3；
3. **只许 main 上 CODEOWNER 维护**：PR 不能给自己开豁免；
4. **豁免条件化在「该用例所在文件本次未被改动」**：PR 改了那个文件 ⇒ **不豁免**
   ⇒ 该用例照常计入判据 ⇒ 闸门拦下 ⇒ 并明示「豁免未生效」（不静默）。
   ⚠️ 该判据只依赖「改了哪些文件」（服务端事实）⇒ **不引入随机阻塞**。

关键实现点：

- **精确 node ID 的正则**：`[\w./-]+\.py::[\w:.\[\]-]+` + **`fullmatch`**
  ⇒ 拒绝通配（`*`）与文件级 ID（无 `::`）。
  文件级 ID 形如 `app/tests/x.py`（收集期错误的 ID）⇒ **不豁免** ⇒ 宁可漏豁免，不可错豁免。
  ⚠️ **必须用 `fullmatch` 而不是 `^...$`**：Python 的 `$` 会匹配「末尾换行**之前**」
  ⇒ `"a.py::t\n"` 能绕过 `^...$` ⇒ 白名单文件里的 node ID 带个换行就能逃逸校验。
- **匹配语义是「精确成员判定」**：`nodeId in failures`（**不是**前缀 / 子串 / 文件级）。
  ⚠️ 这一条原先**只有注释、没有可执行断言** ⇒ 属**净漏** ⇒ 本批已补断言 + 变异组 M1。
- **失败关闭的错误面**（全部抛同一个异常类型，调用方只需 catch 一类）：
  文件不存在 / 读不到（`OSError`）/ **文件不是 UTF-8**（`UnicodeDecodeError` ⇒ **它是
  `ValueError` 子类、不是 `OSError`** ⇒ 只捕 `OSError` 会让它逃出加载器 ⇒ CLI 退出码 1
  ⇒ 语义变成「净新增失败」，方向错了）/ JSON 非法 / 顶层不是对象 / `entries` 不是数组 /
  条目不是对象 / 缺 `nodeId` / `nodeId` 不合规 / `nodeId` 重复 / `reason` 空 /
  `since` 缺失或格式错。
- **对称豁免的实现**：按 `nodeId` 排序遍历白名单（**确定性** ⇒ 报告顺序稳定），
  命中任一侧即记录一条豁免并从两侧同时 `discard`。
- **原始条数不靠「加法还原」**：`applyFlakeExemptions` **显式**产出 `rawBaseCount` /
  `rawPrCount` ⇒ 渲染层只负责显示 ⇒ 将来谁把「豁免后的集合」传进来也不会算错。

### ② `backend/app/utils/ci_failure_set.py`（接线）

```
原始失败集合（base/pr）
   └─ applyFlakeExemptions(白名单)  ──►  豁免后的两侧集合
          └─ diffFailureSets(...)   ──►  对差（added / removed 已不含被豁免用例）
                 └─ verdictFor(...)
```

- `main()` 里**先**加载白名单（加载失败 ⇒ 打印错误 + **返回 3**），**后**拉 run
  ⇒ 判据读不出来就别去拉数据（fail fast）。
- 用 `dataclasses.replace` 造出**新的** `RunComparison`（不可变数据纪律 ⇒ 不原地改）。
- **豁免只影响「对差」**：`verdictFor` 的 job 可用性判定、覆盖等价判定、
  CI 相关面判定、run 新鲜度判定**都不看**白名单
  ⇒ 白名单**不能**用来掩盖「job 没跑完」「少跑了分片」「改了 conftest」。

### ③ `backend/app/utils/ci_report.py`（报告）

- 头部：原始失败条数 + （若有豁免）「计入对差 N 条」（**原始条数显式传参** ⇒ 渲染层不做加法还原）。
- 新增「已知抖动豁免 N 条（该用例的结论不作为证据 ⇒ 不计入对差）」段：
  逐条列 node ID（走 `_reportFailureId` ⇒ 净化 + 指纹）/ 出现侧（基线侧 / PR 侧 / 两侧）/ 理由 /
  **签发日期 `since`**；**PR 侧命中**额外 ⚠ 明示（对称豁免的代价是「PR 侧确定性红也不拦」
  ⇒ 人必须看得见）；**超过 180 天**额外 ⚠「老化」（逼人回头看它 ⇒ 抖动修好了就该删掉）。
- **豁免段单独封顶 20 条**（`_MAX_LISTED_EXEMPTIONS`）：豁免段每条最多 3 行 ⇒ 白名单越大，
  越先把后面的**失败证据**挤出 200 行窗口 ⇒ 方向反了 ⇒ 必须单独封顶。
- **「净新增失败用例」清单上移到 job 明细之前**：报告截断是**从尾部砍**的
  ⇒ 清单排最后就会被先砍掉 ⇒ 结论看似完整、实际看不到新增了什么。
- 所有插入报告的外部字符串**逐字段**过 `sanitizeForReport`（与既有三条渲染纪律一致）。

### ④ `backend/app/utils/ci_surface.py`（授权面）

`_CI_SURFACE_PATTERNS` 本批**补六条**：

- `ci_(?:...|flakes)\.py` ⇒ 改白名单**加载/校验逻辑** = 改判据；
- `^\.github/ci-known-flaky-tests\.json$` ⇒ 改白名单**数据** = 改判据；
- `^backend/app/tests/(?:.*/)?test_ci_.*\.py$` ⇒ 改**守卫用例本身** = **解除守卫**
  （把「拒绝文件级 ID」那条断言删掉 ⇒ 白名单就能塞文件级 ID ⇒ 而守卫被改软后
  **它自己不会再红** ⇒ CI 全绿 ⇒ 没有二次防线）。
  ⚠️ 判定必须**任意深度**（SR-M-2）：旧写法只认 `unit/` ⇒ 把守卫挪到 `integration/`
  就同时逃出「CI 相关面」判定与 CODEOWNERS ⇒ 改守卫 = 解除守卫且无人复核；
- `^backend/app/tests/(?:.*/)?_ci_.*_support\.py$` ⇒ 守卫用例的**共享替身**同族
  （改替身就能让全部对差守卫恒绿 ⇒ 例如把 `redLogWith` 改成永远返回空失败集合）；
- `^\.github/CODEOWNERS$` ⇒ 改**授权面** = **换复核人**
  （个人仓库的 CODEOWNERS 装不了强制力 ⇒ 它是这条链上**仅剩的人工检查点**）。

配套：`ci_gh._parseChangedFiles` 同时收 `filename` **与** `previous_filename`（SR-H-1）
⇒ 改名绕过被堵住。

### ⑤ `.github/workflows/merge-gate.yml`（契约化）

- `actions/checkout@v4` 补 `ref: ${{ github.event.repository.default_branch }}`
  ⇒ 把「白名单只认 main 上的版本」从 **action 的隐含默认**变成 **workflow 里写明的契约**。
  守卫断言**收紧为整串等于** `${{ github.event.repository.default_branch }}`
  ⇒ 只判「含 `default_branch`」会让 `refs/heads/${{ github.event.pull_request.head.ref }}`
  这类混写蒙混过关。
- 闸门**故意不传** `--flake-whitelist` ⇒ 白名单路径只能是 CLI 的**默认值**
  ⇒ 传了就等于允许闸门读**任意路径**的白名单（只要那个路径在 PR 分支上 ⇒ PR 就能自我豁免）。
- 闸门**故意不传** `--allow-ci-changes`（既有纪律）⇒ CI 相关面改动 ⇒ 退出码 6 ⇒ 人工仲裁。
  ⚠️ 这两条「故意不传」由**扫全部 job 的 steps** 的守卫盯着（M-4）。
- 触发器集合必须**恰好**是 `{workflow_run}`（M-3）⇒ 多一条 `pull_request` 就会让 PR
  用自己分支上的闸门文件造**同名** success check ⇒ 闸门可被绕过。
- check 标题：工具把标题写进**独立文件**（`--gate-title-file /tmp/gate_title.txt`）⇒ workflow 读它
  ⇒ 覆盖 GitHub check 的 `title` ⇒ **豁免条数出现在人第一眼看到的地方**。
  ⚠️ **不从报告正文 grep**（SR-L-2）：报告是**给人读**的文本 ⇒ 改一下渲染就静默失效。
  标题写盘失败 ⇒ 工具**返回 3**（WF-2）⇒ 不静默降级。
- 摘要窗口 `head -c 30000`：**必须大于**工具自己的 `MAX_REPORT_BYTES = 20000`
  ⇒ 截断只由工具做（它会**明示**）⇒ workflow 只搬运（M-1）。

### ⑥ `.github/CODEOWNERS`（授权面）

补 `ci_flakes.py`、白名单数据文件、新增的守卫用例文件
（`test_ci_failure_set_flakes.py`）、**共享替身**（`_ci_gate_support.py`）；
并**补上了既有缺口** `test_ci_head.py` / `test_ci_report.py`。
另补 `/.github/CODEOWNERS` **自己**（改授权面 = 换授权人 ⇒ 必须人工复核）。

⚠️ CODEOWNERS 是**枚举式**且 GitHub「**最后匹配者生效**」⇒ 守卫从**文件系统反查**
且算**生效归属**（不只是「路径出现过」）（M-2 / M-5）。

## 6. 测试

### RED 证据（改实现前亲眼看到失败）

| Task | RED 现象 |
|---|---|
| T1 | 13 条守卫全部因 `FlakeWhitelistError` 不存在 / 校验缺失而失败 |
| T2 | `ImportError: cannot import name 'applyFlakeExemptions'`（先删掉实现再写测试，**不允许**先写实现） |
| T3 | 6 条 CLI 守卫 ⇒ `SystemExit: 2`（argparse 不认 `--flake-whitelist`）；3 条报告守卫 ⇒ `renderReport` 无 `exemptions` 参数 |
| T4 | `test_every_ci_tool_module_is_in_the_surface_list` ⇒ `Extra items in the right set: 'backend/app/utils/ci_flakes.py'`（**文件系统反查守卫自己抓到了**） |
| T4' | `test_ci_codeowners.py` 3 条 ⇒ 列出漏掉的 6 个路径（`ci_flakes.py` / 4 个守卫用例 / 白名单数据文件） |
| T5 | `test_ci_merge_gate_workflow.py` ⇒ `checkout 没有显式 ref` |
| **评审修复轮** | 见 §7：H1（精确 node ID 的**匹配语义**无断言）+ M1/M2/M3/M4/M5/M6/M7/M8/M9/M10/M11/M12/M13/M14/M15/M16 —— 每条都先有「该守卫不存在 / 断言不成立」的 RED，再补实现 |

### GREEN 证据

```
$ python3 -m pytest app/tests/unit/test_ci_failure_set.py \
    app/tests/unit/test_ci_failure_set_flakes.py \
    app/tests/unit/test_ci_report.py app/tests/unit/test_ci_flakes.py \
    app/tests/unit/test_ci_surface.py app/tests/unit/test_ci_codeowners.py \
    app/tests/unit/test_ci_merge_gate_workflow.py \
    app/tests/unit/test_ci_text.py app/tests/unit/test_ci_gh.py \
    app/tests/unit/test_ci_head.py app/tests/unit/test_ci_shard.py \
    app/tests/unit/test_ci_integration_step.py -q
250 passed, 19 skipped in 86.08s
```

（19 条 skip = `test_ci_integration_step.py` 里需要 bash ≥ 4.4 的行为部分，本机 macOS 自带 bash 3.2 ⇒ 跳过，CI 必跑。）
⇒ **12 个守卫文件共收集 269 条**（250 绿 + 19 跳）／**265 个用例函数**。

### 覆盖率

```
$ pytest <12 个守卫文件> --cov=app.utils.ci_flakes --cov=app.utils.ci_report \
    --cov=app.utils.ci_surface --cov=app.utils.ci_failure_set --cov=app.utils.ci_gh \
    --cov-report=term-missing
app/utils/ci_failure_set.py     171      1    99%   303
app/utils/ci_flakes.py          109      0   100%
app/utils/ci_gh.py              123     37    70%   74-75, 111, 115, 128-131, 149, 160, 166,
                                                175-186, 195-205, 212, 216-219, 246, 249
app/utils/ci_report.py          107      3    97%   131, 133, 151
app/utils/ci_surface.py          28      0   100%
TOTAL                           538     41    92%
```

⇒ 总覆盖率 **92.38%**（门槛 80%）。

- `ci_flakes.py`（**本批新写**）**100%**；`ci_surface.py` **100%**；整体 **92%**（门槛 80%）。
- 残余行是**既有代码**的分支（`ci_failure_set.py:303` = PR run 的 head sha 取不到；
  `ci_report.py:131/133` = job 真红但无 FAILED 行 / 多条完成标记；
  `ci_report.py:151` = 「两侧均未命中」分支，**当前实现不可达**（`applyFlakeExemptions`
  只记录真的命中的条目）⇒ 显式保留以防将来构造出这种豁免时被误标成「PR 侧」）。

### 变异取证（证明守卫不是假绿）

⚠️ **这是本批最重要的取证**：守卫断言若写错，会「看着有守卫、其实没有」。
驱动器 `/tmp/qa-mutate.py`（备份 → 定向破坏 → 跑对应守卫 → 还原），
**16 组全部实测转红，漏网 0**（收尾重跑时其中 4 组因实现演进而**锚点失配** ⇒ 已重锚补验，见 §7.6；
第三轮另有 1 组**新增** M15b；第四轮另有 3 组**新增** M5/M6/M7，见 §7.7）：

| 组 | 变异 | 期望转红的用例 |
|---|---|---|
| M1 | 精确成员判定 ⇒ 前缀匹配（**H1 净漏的根因**） | `test_applyFlakeExemptions_only_exempts_the_exact_node_id` |
| M2 | `since` 由必填变成可选 | `test_loadFlakeWhitelist_rejects_entry_without_since` |
| M3 | node ID 校验用 `^...$` 取代 `fullmatch`（末尾换行逃逸） | `test_loadFlakeWhitelist_rejects_node_id_with_trailing_newline` |
| M4 | 加载器只捕 `OSError`（`UnicodeDecodeError` 逃逸） | `test_loadFlakeWhitelist_rejects_non_utf8_file` |
| M5 | 豁免命中 ⇒ 直接判「可合并」（绕过不可用 job / 覆盖等价） | `test_main_returns_two_when_a_job_is_unusable_even_with_whitelist` |
| M6 | 豁免命中 ⇒ 跳过「CI 相关面」守卫 | `test_main_returns_six_when_ci_surface_changed_even_with_whitelist` |
| M7 | 豁免段不封顶（把「净新增失败用例」清单挤出 200 行窗口） | `test_report_caps_the_exemption_list` |
| M8 | 「净新增失败用例」清单排到 job 明细之后（被尾部截断砍掉） | `test_report_lists_new_failures_before_job_details` |
| M9 | 渲染层忽略原始条数（靠「豁免后集合」⇒ 头部条数虚高/虚低） | `test_report_shows_raw_and_counted_failure_counts` |
| M10 | 豁免老化检查被移除（条目永久有效） | `test_report_warns_when_exemption_is_stale` |
| M11 | 豁免条目不显示签发日期 `since` | `test_report_shows_exemption_since_date` |
| M12 | 白名单未命中条目完全静默 | `test_main_notes_unmatched_whitelist_entries` |
| M13 | 工具不打印 `gate-title:`（豁免不出现在 check 标题里） | `test_main_prints_gate_title_when_exemptions_happened` |
| M14 | 闸门传 `--flake-whitelist`（允许读任意路径的白名单） | `test_merge_gate_never_passes_flake_whitelist` |
| M15 | 守卫用例文件与 CODEOWNERS 移出「CI 相关面」（改守卫 = 解除守卫） | `test_detectCiSurfaceChanges_flags_guard_test_files` |
| M16 | checkout 的 ref 改指 PR 分支（PR 能给自己开豁免） | `test_checkout_targets_the_default_branch_explicitly` |

另有 **6 组配置面变异**（随实现同步验证）：

| 变异 | 期望 | 实测 |
|---|---|---|
| 从 `_CI_SURFACE_PATTERNS` 删掉白名单数据文件那条 | `test_detectCiSurfaceChanges_flags_flake_whitelist_data_file` 转红 | ✅ 实测转红 |
| 从 CODEOWNERS 删掉 `ci_flakes.py` | `test_every_ci_tool_module_has_a_codeowner` 转红 | ✅ 实测转红（列出了漏掉的路径） |
| 从 CODEOWNERS 删掉 `test_ci_head.py` / `test_ci_report.py` | `test_every_ci_guard_test_has_a_codeowner` 转红 | ✅ 实测转红（列出了 4 个漏掉的路径） |
| 从 `merge-gate.yml` 删掉 checkout 的 `ref:` | `test_checkout_targets_the_default_branch_explicitly` 转红 | ✅ 实测转红 |
| 白名单文件损坏（`{ not json`） | 退出码 3 | ✅ 实测（`test_main_returns_three_when_whitelist_is_broken`） |
| 白名单文件不存在 | 退出码 3 | ✅ 实测（`test_main_returns_three_when_whitelist_file_is_missing`） |

### 第二轮变异取证（评审修复之后重跑）

驱动器 `/tmp/b7_mutation_evidence.py`（备份 → 定向破坏 → 跑对应守卫 → 还原 → 校验复原）。
**11 组全部实测转红，漏网 0**：

| 组 | 变异 | 期望转红的用例 |
|---|---|---|
| M-3 | 触发器集合多一条 `pull_request` | `test_merge_gate_is_triggered_by_workflow_run_only` |
| M-4 | 加第二个 job 传 `--allow-ci-changes` | `test_merge_gate_never_passes_dangerous_flags_in_any_job` |
| M-1a | 摘要窗口 `30000` → `8000`（小于报告上限） | `test_merge_gate_summary_window_is_wider_than_the_report_cap` |
| M-1b | 去掉报告的**字节上限** | `test_report_is_capped_in_bytes_and_marks_the_truncation_in_the_header` |
| M-1c | 豁免段排到差异清单**之前** | `test_report_keeps_the_whole_new_failure_list_next_to_a_long_exemption_section` |
| M-5 | CODEOWNERS 末尾追加 `* @someone-else` | `test_the_effective_owner_of_every_controlled_path_is_the_expected_one` |
| L-1a | 「PR 侧命中」不排除「基线也红」 | `test_report_warns_only_when_a_flake_is_newly_red_on_the_pr_side` |
| L-2 | 逐 job 行不标「含已知抖动豁免 N 条」 | `test_report_reconciles_the_per_job_failure_count_with_the_exemptions` |
| L-4 | 允许 `since` 是未来日期 | `test_loadFlakeWhitelist_rejects_a_since_date_in_the_future` |
| L-1b | gate 标题把「两侧都红」算成「PR 侧命中」 | `test_main_gate_title_does_not_flag_a_both_sides_hit_as_a_pr_side_hit` |
| S-1 | 从 CI 相关面清单删掉 `_ci_*_support.py` | `test_detectCiSurfaceChanges_flags_the_shared_guard_support_module` |

⚠️ **M-1b 第一次跑时是绿的** ⇒ 抓出了一条**假绿守卫**（旧 fixture 让「行数上限」与「字节上限」
几乎同时触发 ⇒ 断言分辨不出是哪一个）⇒ 已换专用 fixture（详见 §7.5）。
⇒ 这也是「变异取证」的价值：**它验的不只是「守卫会红」，而是「守卫在测你想测的东西」**。

### 新增/修改测试清单

| 文件 | 条数 | 覆盖点 |
|---|---|---|
| `backend/app/tests/unit/test_ci_flakes.py`（**新建**） | 39 | T1 加载与校验（非 UTF-8 / 末尾换行 / 缺 `since` / 重复 node ID / 空理由 / **未来日期** / **非法日历日**…）+ T2 豁免计算（**精确成员判定** / 原始条数 / `since` 透传 / 老化判定）+ 仓库真实白名单文件必须合法 |
| `backend/app/tests/unit/test_ci_codeowners.py`（**新建**） | 6 | CODEOWNERS 反查：工具模块 / 守卫用例（**任意深度** + 共享替身）/ 白名单数据文件 + **生效归属** + **反向守卫**（证明匹配器不是假绿） |
| `backend/app/tests/unit/test_ci_merge_gate_workflow.py`（**新建**） | 10 | checkout **整串**等于 default_branch / 不传 `--allow-ci-changes`（**扫全部 job**）/ 触发器集合**恰为** `{workflow_run}` / **不传 `--flake-whitelist`** / 标题走**独立文件** / **标题真的流进 check payload**（M5）/ 摘要窗口 > 报告上限 |
| `backend/app/tests/unit/test_ci_failure_set_flakes.py`（**新建**，CR-L-5 拆分） | 22 | 白名单 CLI 接线：命中⇒0 / 未命中⇒5 / PR 侧命中⇒0 / 缺失⇒3 / 损坏⇒3 / **任何异常都⇒3** + **不可用 job 仍⇒2** + **覆盖不等价仍⇒4** + **CI 相关面仍⇒6** + 未命中提示 + **未生效条目不得被建议清理**（M6）+ gate 标题三条 + **豁免未生效** + **文件被改 ⇒ 不豁免** |
| `backend/app/tests/unit/_ci_gate_support.py`（**新建**，共享替身） | — | 两个套件共用的替身与固定数据（**只替换外部依赖**，失败集合抽取走**真实实现**） |
| `backend/app/tests/unit/test_ci_failure_set.py`（改，**回落到 1243 行**） | +2 | 原有基础对差用例（白名单段已迁出） |
| `backend/app/tests/unit/test_ci_report.py`（改） | **+14** | 豁免段 / PR 侧**新增红** ⚠ / 原始与计入对差两条计数 / 两侧命中标注 + **`since` 展示** / **老化 ⚠** / **豁免段封顶** / **净新增清单前移** / **字节上限 + 头部截断提示** / **逐 job 与豁免对账** |
| `backend/app/tests/unit/test_ci_surface.py`（改） | **+4** | 白名单数据文件 / **守卫用例文件（任意深度）** / **共享替身** / **CODEOWNERS** 属于 CI 相关面 |
| `backend/app/tests/unit/test_ci_gh.py`（改） | +1 | 改名时 `previous_filename` 也要进改动清单（SR-H-1） |

## 7. 两路评审的发现与仲裁

本批在实现完成后跑了**两路评审**：`code-reviewer`（工程正确性）与 `security-reviewer`（威胁模型）。
两路共提出 **15 条**发现（`code-reviewer` 8 条 + `security-reviewer` 7 条），
另加本会话自查的 **2 条**（`WF-1` / `WF-2`）与**变异取证自己抓出来的 1 条假绿** ⇒ 合计 **18 条**。
**全部已处置**，处置方式按「问题严重度 + 是否改变威胁模型」分四档：

| 档 | 含义 | 处置 |
|---|---|---|
| **必修** | 威胁模型上的破口（能让闸门被绕过 / 让判据静默失效） | 必须改实现 + 补守卫 + 变异取证 |
| **应修** | 判据的正确性 / 可用性缺陷（结论会误导人，但不是破口） | 改实现 + 补守卫 |
| **应改** | 纪律 / 体量问题（不改变行为，但违反本仓规则） | 改实现或改写法 |
| **记录** | 已知的取舍，写清楚免得后来人误读 | 只写文档 |

编号带来源前缀，避免两路评审的同号撞车：`CR` = code-reviewer，`SR` = security-reviewer，
`WF` = 本会话自查。

### 7.1 必修（威胁模型破口）

| ID | 来源 | 发现 | 处置 |
|---|---|---|---|
| **SR-H-1** | security-reviewer | GitHub compare 对一次**改名**只给**一个**条目（新路径在 `filename`、旧路径在 `previous_filename`）⇒ 把 `.github/ci-known-flaky-tests.json` 改名 ⇒ 它**不再出现在改动清单** ⇒ 「CI 相关面」守卫看不到 ⇒ **静默绕过**退出码 6 的人工仲裁 | `ci_gh._parseChangedFiles` 同时收 `filename` **与** `previous_filename`（去重）；守卫 `test_ci_surface.py::test_renaming_a_ci_surface_file_is_still_detected` 钉住整条链 |
| **SR-H-2** | security-reviewer | 豁免是**无条件**的 ⇒ PR 直接改掉那个用例所在的文件后，它的新红仍被豁免 ⇒ **确定性回归被当成噪声放过去** | 引入**第四条**：豁免**条件化在「该用例所在文件本次未被改动」**（`changedFiles`）⇒ 命中则**不豁免**并明示「豁免未生效」。⚠️ 该判据只依赖「改了哪些文件」（服务端事实）⇒ **不引入随机阻塞**；两条路径基准（node ID = rootdir 相对 / compare = 仓库相对）已在 `_isTestFileChanged` 里对齐 |
| **SR-M-1** | security-reviewer | `since` 用正则校验 ⇒ `2026-02-30` 能过 ⇒ 它一路活到**渲染报告**时 `date.fromisoformat` 才抛 ⇒ 闸门对**全仓每个 PR** 都以「渲染报告失败」结束（退出码 3）⇒ 判据整体瘫痪，且报错指向错误的方向 | 日历合法性在**加载期**校验（`ci_flakes._parseEntry`）⇒ 退出码 3 且报错指向「白名单非法」 |
| **SR-M-2** | security-reviewer | CODEOWNERS 反查只扫 `backend/app/tests/unit/` ⇒ 把守卫挪到 `integration/` 就同时逃出「CI 相关面」判定与 CODEOWNERS ⇒ **改守卫 = 解除守卫**且无人复核 | 改为 `rglob` **任意深度**（守卫用例 + CI 相关面判定两处同步改） |
| **SR-L-1** | security-reviewer | `_loadFlakeEntries` 只捕 `FlakeWhitelistError` ⇒ 别的异常（`OSError` / `KeyError` …）逃出加载器 ⇒ CLI 裸 traceback + 退出码 **1**（1 的语义是「净新增失败」⇒ **查错方向**） | 加载器改捕 `Exception` ⇒ 一律**失败关闭**（退出码 3） |
| **SR-L-2** | security-reviewer | check 标题从**人读报告正文**里 `grep '^gate-title: '` ⇒ 报告是给人读的文本 ⇒ 改一下渲染（加缩进、换前缀、把标题挪到会被截断的尾部）就**静默失效** ⇒ 豁免提示从标题消失 ⇒ **真相被盖住**且无人察觉 | 标题走**独立文件**（`--gate-title-file`）⇒ 报告怎么改都不影响它；守卫 `test_merge_gate_reads_the_gate_title_from_the_tool_file` 钉住 |
| **CR-M-3** | code-reviewer | 守卫只断言 `"workflow_run" in triggers` ⇒ 在保留它的同时再加一条 `pull_request:` 触发器，守卫照样绿 ⇒ 而 `pull_request` 事件用的是 **PR 分支上的 workflow 文件** ⇒ PR 能运行自己改过的闸门并 `POST /check-runs` 造一个**同名** success check ⇒ 必需检查按名字匹配 ⇒ **闸门可被绕过** | 断言落在**集合相等**上：`set(triggers) == {"workflow_run"}` |
| **CR-M-4** | code-reviewer | 守卫只扫 `failure-set-diff` 这**一个** job ⇒ 在 workflow 里加第二个 job（或给现有 job 加后续 step）调用 `--allow-ci-changes` / `--flake-whitelist`，守卫**全绿** ⇒ 「人工仲裁」被自动化掉 / 白名单可读任意路径 | 守卫改为遍历**所有** job 的 steps（`_steps()`） |
| **CR-M-5** | code-reviewer | CODEOWNERS 只检查「路径**出现过**」⇒ 在文件**末尾**追加一行 `* @someone-else` ⇒ GitHub「**最后匹配者生效**」⇒ 全部 CI 路径的**生效**复核人被换掉 ⇒ 守卫仍绿 ⇒ 授权面整体漂移 | 改为算**生效归属**（`_effectiveOwner` + GitHub 模式语义子集），并补**反向守卫** `test_a_trailing_catch_all_would_take_the_ownership_away` 证明匹配器不是假绿 |

### 7.2 应修（判据正确性 / 可用性）

| ID | 来源 | 发现 | 处置 |
|---|---|---|---|
| **CR-M-1** | code-reviewer | **两套截断单位不一致**：渲染层按**行**（`_MAX_REPORT_LINES = 200`）封顶，workflow 却按**字节**（`head -c 8000`）砍尾 ⇒ 报告自认「我没超上限」、workflow 却砍掉 ⇒ **证据被静默挤出摘要**（实测：白名单 12 条时「净新增失败用例：」落在 7284 字节处被切断，20 条时整段消失） | **统一到字节**：① 工具侧 `MAX_REPORT_BYTES = 20000`（由工具负责截断并**明示**「报告已截断」，提示插在**头部第 2 行**——尾部会被自己砍掉）；② workflow 窗口放宽到 `head -c 30000`（只搬运，不二次截断）；③ 展示顺序改为 header → 原因 → 提示 → **净丢失 → 净新增 → 豁免 → job 明细**（**证据优先于说明** ⇒ 尾部砍时最后受损的是说明） |
| **CR-L-1** | code-reviewer | 「⚠️ PR 侧命中」对**两侧都红**也触发 ⇒ 而两侧都红是**最典型**的抖动形态 ⇒ 每次抖动都报「请人工确认不是确定性回归」⇒ **告警变噪音 ⇒ 真告警被淹没** | 收窄为「**PR 侧新增红**」（`inPr and not inBase`）⇒ 报告层与 **gate 标题**两处同步收窄 |
| **CR-L-2** | code-reviewer | 逐 job 的「失败 N 条」是**原始**条数（含被豁免的），而头部是「失败 N 条（计入对差 M 条）」⇒ 两处数字**对不上账** ⇒ 读者只能「不信其中之一」 | 逐 job 行追加「（含已知抖动豁免 N 条）」⇒ 可对账 |
| **CR-L-4** | code-reviewer | `since` 允许**未来**日期 ⇒ 签发日期说不通 ⇒ 且该条豁免在老化上限内**永不老化** ⇒ 老化复核机制对它彻底失效 | 加载期拒绝未来日期（`signedOn > date.today()` ⇒ `FlakeWhitelistError`） |
| **SR-L-3** | security-reviewer | 若守卫要求白名单**非空** ⇒ 白名单清空（抖动被修好 ⇒ 该删条目了）反而需要先改守卫 ⇒ **大家就不敢清理** ⇒ 条目只增不减 | 只校验「格式合法」；**条目数量是人的判断，不是守卫的判断** |

### 7.3 应改（纪律 / 体量）

| ID | 来源 | 发现 | 处置 |
|---|---|---|---|
| **CR-L-5** | code-reviewer | `test_ci_failure_set.py` 由基线 1319 行 → 1771 行（**项目上限 800 行**），本批新增 452 行全堆在一个已经超限的文件里 | 拆成两个套件：`test_ci_failure_set_flakes.py`（白名单 CLI 用例，697 行）+ `test_ci_failure_set.py`（回到 **1233 行**，**低于**基线）；共享替身抽到 `app/tests/unit/_ci_gate_support.py`（172 行，公开名，沿用本仓 `_xxx_support.py` 既有做法） |
| **WF-1** | 本会话自查 | 闸门**没有**把 `--gate-title-file` 接线进去（工具支持了、workflow 没传）⇒ 豁免提示**根本没进标题** | workflow 补 `--gate-title-file /tmp/gate_title.txt` |
| **WF-2** | 本会话自查 | 标题写盘失败被**静默吞掉** ⇒ 标题缺了但没人知道 ⇒ 回到「真相被盖住」 | `_writeGateTitle` 失败 ⇒ 打印错误 + **返回 3**（运行错误，失败关闭）；且**故意不建父目录**（路径写错必须响亮失败） |

### 7.4 记录（已知取舍，写清楚免得后来人误读）

- **对称豁免**（方案 A）的代价：PR 侧**确定性**弄红一个白名单用例，闸门**也不拦它**。
  ⇒ 兜底 = **最大化可见性**：报告正文 + 头部条数 + **check 标题** + 「未命中 / 豁免未生效」提示，
  ⇒ **不改阻塞**（改成阻塞 = 回到方案 B 的方向性语义，见 §2）。
- **豁免条件化在「文件未改动」**引入了一条**新的失败模式**：PR 改了那个文件 ⇒ 该用例照常计入判据 ⇒
  可能被判「净新增」而拦下。**这是有意的**（确定性回归优先），且报告会明示「豁免未生效」。
- **`changedFiles=None`**（判定不出来）⇒ 照常豁免：由「CI 相关面」守卫以**退出码 6** 兜住
  ⇒ 豁免层不必重复拦（否则退出码会变成 1 =「净新增失败」⇒ 语义反而错）。
- **`_ci_gate_support.py` 也是授权面**：共享替身提供「失败集合」「分片划分」的观测对象
  ⇒ 改替身就能让全部对差守卫恒绿 ⇒ 它与守卫用例**同族** ⇒ 已进 CODEOWNERS **与** CI 相关面清单。

### 7.5 变异取证**自己抓出来的假绿**（本批最重要的一条）

第二轮变异取证时，`M-1b`（去掉报告的**字节上限**）**没有**让守卫转红 ⇒
⇒ 「字节上限」那条守卫**分辨不出**是字节上限还是行数上限在起作用（旧 fixture 恰好 19810 字节 / 198 行，
两个上限**几乎同时**触发 ⇒ 断言照样满足）。

⇒ **处置**：给那条守卫换了一个**专用 fixture**——每条豁免的 node ID 与理由都**很长**
（`sanitizeForReport` 把每个字段截到 200 字符 ⇒ 一行约 800 字节）⇒ 20 条豁免就超过 20000 字节；
而**行数**刻意压在 200 行以下 ⇒ **只有字节上限**能解释「为什么被截断」。
守卫另加两条断言：截断提示必须指明「**字节上限**」+ 场景行数必须 ≤ 200（前提自检）。

⇒ 这条本身也是一条纪律：**「守卫会红」不等于「守卫在测你想测的东西」**——
必须问「它会不会因为**别的原因**而绿」。

### 7.6 收尾时重跑变异取证：**4 条锚点失配** ⇒ 重锚后再验（第三轮）

评审修复改了实现 ⇒ 第一轮变异驱动器的 **4 条锚点失配**（`变异未命中 ⇒ 跳过`）
⇒ 那 4 条**本轮没有新证据**。纪律：**锚点失配 ≠ 免检** ⇒ 必须重锚后重跑。

| 组 | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M1 | 精确成员判定 ⇒ 前缀匹配（H1 净漏的根因） | `test_applyFlakeExemptions_only_exempts_the_exact_node_id` | ✅ 转红 |
| M8 | 「净新增失败用例」清单排到 job 明细之后 | `test_report_lists_new_failures_before_job_details` | ✅ 转红 |
| M14 | 闸门传 `--flake-whitelist`（允许读任意路径的白名单） | `test_merge_gate_never_passes_flake_whitelist` | ✅ 转红 |
| M15 | 守卫用例文件不在「CI 相关面」里 | `test_detectCiSurfaceChanges_flags_guard_test_files` | ✅ 转红 |
| M15b | CODEOWNERS 不在「CI 相关面」里 | `test_detectCiSurfaceChanges_flags_guard_test_files` | ✅ 转红 |

⇒ **B7 批变异取证合计 32 组**（第一轮 16 + 第二轮 11 + 第三轮 5），**全部实测转红，漏网 0**。
（第三轮那 5 组里 4 组是**重锚**、1 组是**新增** ⇒ 后面 §7.7 第四轮再 +3 组新增。）

### 7.7 第三轮评审（收尾后 `code-reviewer` 复审）：**1 HIGH + 2 MEDIUM + 4 LOW** ⇒ 全部处置

收尾后（文档与规则刷新完毕）又跑了一轮 `code-reviewer` 复审，发现 **7 条**：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| **CR2-H-1** | **HIGH** | `_ci_gate_support.py` 与 `test_ci_failure_set_flakes.py` **没进索引**（`git status` 显示 `??`）⇒ 若用 `git commit -a` 提交，`test_ci_failure_set.py` 的 `from ... import` 会在**收集期**抛 `ModuleNotFoundError` ⇒ **整个 unit 分片**在 main 上变红（不是只红一个文件）；且那 20 条 CLI 层覆盖**整批消失**，而 CODEOWNERS 已经指向这两个不存在的路径 ⇒ 授权面凭空消失、无任何报错 | 提交前显式 `git add` 两个文件（**本批新增的两个共享件必须显式入库**） |
| **CR2-M-1** | MEDIUM | workflow 守卫是**假绿**：`assert "gate_title=" in script` 被 `echo "gate_title=${TOOL_TITLE}"` 满足，而真正喂进 check payload 的是 `title="${TOOL_TITLE}"` ⇒ 删掉**那一行**守卫照样绿 ⇒ 豁免提示从标题静默消失（正是 SR-L-2 要堵的失败模式） | 新增守卫 `test_merge_gate_wires_the_tool_title_into_the_check_payload`：沿**数据流**追（`jq --arg title "<var>"` ⇒ `<var>` 必须由 `TOOL_TITLE` 赋值）⇒ 改**赋值来源**才红，改名变量不误报 |
| **CR2-M-2** | MEDIUM | 「未命中 N 条」把 **`skipped`**（命中了失败集合、但因文件被改动而**未生效豁免**）也算成未命中 ⇒ 报告说「本次命中 0 条，未命中 1 条（⇒ 建议清理）」⇒ 两句都是**假的** ⇒ 操作者据此删掉一条**仍然活着**的抖动条目 ⇒ 随机挡人的老毛病**静默复活**（B7 存在的目的被推翻） | `_unmatchedWhitelistNote` 改为 `matchedCount = len(exemptions) + len(skipped)`；新增两条守卫（**不得建议清理** + **命中计数含未生效条目**） |
| CR2-L-1 | LOW | `gate-title:` 仍在 stdout（报告正文里）⇒ 是 SR-L-2 拆掉的那个面的残余 ⇒ 又是「将来有人改回 grep」的邀请 | **保留**（它只让直接看 stdout 的人看到豁免条数），但**明确标注 display-only**：代码注释 + 用例 docstring + workflow 守卫断言闸门**不 grep** 这一行 ⇒ 「机器通道 = `--gate-title-file` 独立文件」这条契约不变 |
| CR2-L-2 | LOW | 两份 SSOT 的变异组数不一致（`测试规范.md` 写 27 / 本文件写 32）⇒ **证据头条数字**两处打架 | 统一口径：**31 组不同的变异**（第一轮 16 + 第二轮 11 + 第三轮新增 1（M15b）+ 第四轮新增 3）＋ **4 组重锚** ⇒ **执行次数 35**；两份文档同步改成这一句 |
| CR2-L-3 | LOW | `plan.md` §五「文件清单」漏了 6 个本批实际改动的文件 | 补齐 |
| CR2-L-4 | LOW | `test_defaultWhitelistPath_points_at_github_dir` 只断言**末两段** ⇒ 任何 `<任意目录>/.github/…` 都能蒙混过关 ⇒ 「默认路径 = 仓库根的 `.github/`」这条属性**没被钉住**，而整条「PR 不能给自己开豁免」链架在它上面 | 改为断言 `path.parent == 仓库根 / ".github"`（全路径锚定） |

**第四轮变异取证**（针对上表 3 条可执行守卫）⇒ 驱动器 `/tmp/b7_mutation_round4.py`：

| 组 | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M5 | 删掉 `title="${TOOL_TITLE}"`（工具标题不再流进 check payload） | `test_merge_gate_wires_the_tool_title_into_the_check_payload` | ✅ 转红 |
| M6 | 「未命中」计数把未生效的条目也算进去 | `test_main_does_not_advise_cleaning_up_a_skipped_whitelist_entry` | ✅ 转红 |
| M7 | 默认白名单路径不再落在仓库根的 `.github` 下 | `test_defaultWhitelistPath_points_at_github_dir` | ✅ 转红 |

⇒ **B7 批变异取证最终口径**：**31 组不同的变异**全部实测转红（另有 4 组重锚 ⇒ 执行次数 35），**漏网 0**。
⇒ 累计：2026-11-02/03 批 **28 组** + B7 批 **31 组** = **59 组**。

⚠️ 复审还**逐条追过**「白名单能不能被绕过」这条链，结论是**没有可利用路径**：
白名单读自默认分支的 checkout（`workflow_run` + 显式 `ref:`）⇒ PR 不能自开豁免；
CI 相关面清单覆盖 `ci_flakes.py` / 白名单数据文件 / `.github/CODEOWNERS` / 任意深度的守卫用例 /
共享替身；`ci_gh._parseChangedFiles` 已带 `previous_filename` ⇒ **改名逃不掉**；
豁免只影响对差 ⇒ 不可用 job / 覆盖等价 / CI 相关面 / 新鲜度四类判定**不受白名单影响**；
`_isTestFileChanged` 已对齐 rootdir 相对 ↔ 仓库相对两套路径基准（含改名）；
`applyFlakeExemptions` 不改入参（不可变）。

⇒ 顺带发现并修掉的**新问题**：收尾时 `ruff check` 抓到本批触及文件的 **8 处 lint**
（`I001` 导入块未排序 ×2 / `F401` 未用导入 ×4 / `F811` 重复定义 / `SIM110`）⇒ 已全部修掉
⇒ **本批触及的 14 个文件 ruff 全绿**（`ruff` 不在 CI 闸门里 ⇒ 靠人工收尾时补跑）。
⚠️ 其中 `F811`（`FLAKY_FILE_IN_REPO` 在 support 模块与用例文件里各定义一份）
⇒ 若不改，后面有人删掉其中一份就会**静默换掉常量** ⇒ 已统一到 support 模块单一来源。



## 8. 部署验证

**本批不部署**（零 DDL、零生产写、零生产读 ⇒ 无生产副作用）。

- **生效点**：闸门在 **GitHub 上**跑（`workflow_run` 触发）⇒ **不依赖部署**；
  合并进 main 后，**下一次** `workflow_run` 就用上了新逻辑。
- **生产 DB / Milvus / Neo4j**：本批**一次都没碰**。

**本地端到端冒烟**（用**仓库里真实的白名单文件**跑「加载 → 豁免 → 对差 → 报告」）：

```
$ .venv/bin/python /tmp/qa-flake-smoke.py
白名单路径：/private/tmp/qa-realdefects-wt/.github/ci-known-flaky-tests.json
白名单条目数：1
  - app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent（since 2026-11-06）

豁免前：基线 2 条 / PR 1 条
豁免后：基线 1 条 / PR 1 条
豁免明细：['app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent']
对差：净新增 [] / 净丢失 []

----- 报告 -----
结论：可合并（MERGEABLE）
基线 run 41961157752（head b6e809a2…）：失败 2 条（计入对差 1 条）
PR    run 42061730479（head 97047b8b…）：失败 1 条
净新增 0 条 / 净丢失 0 条
已知抖动豁免 1 条（该用例的结论不作为证据 ⇒ 不计入对差）：
  - app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent［基线侧］，since 2026-11-06：Milvus 可见性竞态：backfill = 删旧行（auto-id 主键）+ 带 external_id 重插；第二次 backfill 若在 Milvus 可见性收敛前又查到 external_id 为空的旧行 ⇒ written2 == 1 ⇒ 用例随机红。2026-11-06 PR #107 实证：同一份代码、两次 run 给出不同结论（基线红 / PR 绿）。
```

⇒ 这正是 **PR #107 当初被拦的那一条**（基线红 / PR 绿 ⇒ 净丢失 1 ⇒ 退出码 5）
⇒ 现在**退出码 0**，且豁免在报告里**显式可见**。

## 9. 关联

- **计划**：[plan.md](./plan.md)（方案 A/B/C 对比 + T1–T6 任务清单 + 硬约束表 + **第四条的实施期补注**）
- **上一批**：[2026-11-05-defect-priority-plan/summary.md](../2026-11-05-defect-priority-plan/summary.md)（§10.6 = PR #107 的闸门实操与越权口安装缺口 N7）
- **Rules**：
  - [Harness/rules/开发流程规范.md](../../rules/开发流程规范.md) —— 「CI 合并判据」**新增第 10 条**（抖动容忍）+ 退出码表更新 + 模块清单
  - [Harness/rules/测试规范.md](../../rules/测试规范.md) —— 工具化清单 9 → **10 个模块** + 白名单机制说明 + **四条硬约束** + 变异取证纪律（锚点失配 ≠ 免检；「守卫会红」≠「守卫在测你想测的东西」）
  - [Harness/rules/开发流程规范.md](../../rules/开发流程规范.md) —— 第 10 条（抖动容忍，**四条硬约束**）+ 退出码表（3 的含义扩大）+ CI 相关面清单（含改名 / 共享替身 / CODEOWNERS 生效归属）
- **代码**：
  - `backend/app/utils/ci_flakes.py`（新）/ `ci_failure_set.py` / `ci_report.py` / `ci_surface.py`
  - `.github/ci-known-flaky-tests.json`（新）/ `.github/CODEOWNERS` / `.github/workflows/merge-gate.yml`
- **MEMORY**：[qa-system-merge-gate-flake-whitelist](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-flake-whitelist.md)
  （另更新了 `qa-system-ci-failure-set-tool` 与 `qa-system-merge-gate-protection-config` 两条索引行 —— 前者的「工具不做抖动容忍」已过期）
- **相关记忆**：
  [qa-system-merge-gate-rerun-procedure](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-rerun-procedure.md)、
  [qa-system-merge-gate-protection-config](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-protection-config.md)

---

## 10. 合并路径（本批的硬约束，必须留给下一批处理者）

本批改的是 `backend/app/utils/ci_*.py` + `.github/` ⇒ **落在闸门的 CI 相关面**
⇒ 本批自己的 PR 必然被判 `CI_SURFACE_CHANGED`（退出码 6）⇒ 必须走 **owner 越权口**
（`enforce_admins: false`）显式合并。

⇒ **顺序不能反**：B7 落地**依赖**这条越权口 ⇒
**必须先落 B7，再考虑把 `enforce_admins` 收回去**（收回后就没人能推 B7 了）。

⇒ 合并**后不部署**（零 DDL、零生产写；闸门在 GitHub 上跑 ⇒ 合并即生效）。

---

## 11. 合并回填（2026-11-06 实操）

### 11.1 闸门复判证据（最终判定）

| 项 | 值 |
|---|---|
| 基线 run | `42092597549`（head `5a7ac461`，attempt **2** = 整轮复跑） |
| PR run | `42239883087`（head `e4c09e28`） |
| check | `merge-gate / failure-set-diff`（id `127993467931`） |
| 基线失败 | **139** 条 |
| PR 失败 | **139** 条 |
| **净新增 / 净丢失** | **0 / 0** |
| 闸门结论 | `CI_SURFACE_CHANGED`（**退出码 6** ⇒ failure） |
| check 标题 | 「退出码 6 ⇒ 不可合并（净新增 / 不可用 job / 待人工确认）」 |

**第一次判的却是退出码 2（UNUSABLE_JOBS），不是 6** —— 根因是**基线侧**：
基线 run attempt 1 的 `Backend / integration shard 3` 结论是 `cancelled`（撞 `timeout-minutes: 90`）
⇒ 该 job 的失败集合是**空的** ⇒ 不可用于对差
⇒ 那 35 条「净新增」全是**假净新增**（恰好等于 PR 侧 shard 3 的 35 条）。
⇒ 处置：`gh run rerun 42092597549`（**整轮**复跑；**不能**只复跑单个 job ——
单 job 复跑会让最新 attempt 只剩 1 个 job ⇒ 覆盖等价判据误判 `COVERAGE_MISMATCH`）
⇒ attempt 2 的 9 个 job **无一 `cancelled`** ⇒ 闸门得以正常对差。

### 11.2 人工仲裁（退出码 6 分支）

`merge-gate.yml` 对退出码 6 的自述口径是「人工仲裁确认**分片归属未变**后，由 bypass 账号显式合并」
⇒ 分片归属逐片核对：

| 分片 | 基线 attempt 2 | PR attempt 1 |
|---|---|---|
| unit shard 0 | 9 | 9 |
| unit shard 1 | 4 | 4 |
| unit shard 2 | 1 | 1 |
| unit shard 3 | 3 | 3 |
| integration shard 0 | 17 | 17 |
| integration shard 1 | 44 | 44 |
| integration shard 2 | 26 | 26 |
| integration shard 3 | 35 | 35 |
| Frontend / typecheck + coverage | 0（success） | 0（success） |
| **合计** | **139** | **139** |

⇒ 逐片条数**完全一致** ⇒ **分片归属未变** ⇒ 失败集合可比 ⇒ 仲裁通过。
⇒ 且「净新增 0 / 净丢失 0」是**工具自己的对差结论**，不是人工推断。

⚠️ 本次**没有**触发白名单豁免：白名单里那一条
（`app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`）
在两侧**同结论**（都没进失败集合）⇒ 无需豁免 ⇒ 报告里没有「已知抖动豁免」段。
⚠️ 这**不能**被读成「白名单没生效」：**白名单只在两侧结论不一致时才起作用**——
它的作用是「把两侧不一致的那一条从对差里剔除」；两侧一致时它是**空操作**。

### 11.3 合并与内容级仲裁

| 项 | 值 |
|---|---|
| 合并方式 | `gh pr merge 108 --squash --admin`（owner 越权口；`enforce_admins: false`） |
| main tip | **`88599b19`** |
| 内容级仲裁 | 合并后的 tree 与分支 tip tree **逐 blob 完全一致**：两边各 **2566** 个 blob，`diff` **零差异** |
| 净变更 | 相对 base `5a7ac461` 恰 **22 个文件**（+4499 / −142） |
| 部署 | **不部署**（零 DDL、零生产写、零生产读） |

⇒ squash 没丢内容 ⇒ 分支可安全删除。

### 11.4 顺序约束（仍然成立，且已落地）

B7 落地**依赖 owner 越权口**（`enforce_admins: false`）：
本批改了 `ci_*.py` + `.github/` ⇒ 落在 CI 相关面 ⇒ 闸门必判 `CI_SURFACE_CHANGED` ⇒ 只能走越权口。
⇒ **现在 B7 已在 main 上** ⇒ 「把 `enforce_admins` 收回去」这条后续项才**可以**开始排期；
⚠️ 但收紧**之前**必须先确认：**此后所有涉及 CI 相关面的改动**
（闸门代码本身、白名单文件、CODEOWNERS、CI workflow）都有一条**明确的人工合并通道**
⇒ 否则会变成「谁都不能改闸门」的死锁。

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B / C）+ 第二个决策点（读不到 ⇒ 失败关闭）
- [x] 第 3 段无迁移（零 DDL）⇒ 迁移文件名校验不适用
- [x] 第 7 段填齐（见 §7）
- [x] 第 8 段：本地端到端冒烟输出已贴出（**不部署**，理由已写明）
- [x] 第 9 段 ≥ 3 个跨文件链接（关联变更 / Rules / MEMORY / 代码）
- [x] 相关 wiki 已更新（`Harness/rules/开发流程规范.md` 第 10 条 + `测试规范.md` 工具化清单）
- [x] MEMORY 索引已加（`qa-system-merge-gate-flake-whitelist.md`）+ 两条过期索引行已更正
- [x] 不涉及真实 SQL/DB 改动 ⇒ 无真实数据验证脚本义务
