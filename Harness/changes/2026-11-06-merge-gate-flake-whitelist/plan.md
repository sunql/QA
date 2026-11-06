# B7：merge-gate 抖动容忍白名单（实施计划）

> 上一批（①②③ + PR #107）已闭环。本批是 `Harness/changes/2026-11-05-defect-priority-plan/plan.md`
> §三 第二梯队里被标为「**下一批第一优先**」的 **B7**。
>
> **本批零 DDL、零生产写、零生产读。**

---

## 一、要解决的到底是什么

合并闸门（`merge-gate / failure-set-diff`）的判据是「**失败集合对差，净新增为空才合并**」。
它**不做抖动容忍**：只要某个用例在基线与 PR 两侧的「失败与否」不同，就一律拦下。

实测样本（PR #107，2026-11-06）：

| 侧 | run | 失败条数 | 差 |
|---|---|---|---|
| 基线（main `b6e809a2`） | `41961157752` | 140 | — |
| PR（head `97047b8b`） | `42061730479` | 139 | 净新增 0 / **净丢失 1** |

那 1 条净丢失是 `app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`：
backfill = 删旧行（auto-id PK）+ 带 `external_id` 重插；第二次 backfill 若在 Milvus 可见性收敛前
又查到 `external_id` 为空的旧行 ⇒ `written2 == 1` ⇒ 用例随机红。

⇒ **同一个用例、同样的代码，两次 run 给出不同结论** ⇒ 闸门随机挡人。
按「独立同分布、每侧失败概率 p」估算，闸门因这一个用例而误拦的概率是 `2p(1-p)`；
实测 p≈0.5 ⇒ **约一半的 PR 被随机挡下**，每次都要人工做三件套证伪（见
`memory/qa-system-merge-gate-rerun-procedure.md`）。

⇒ 本批给闸门加一条**可复用的「已知抖动」通道**：让「结论不确定的用例」不再充当判据。

---

## 二、三个候选方案（设计评审）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A. 白名单对称豁免**（推荐） | 白名单里的 node ID 从**两侧**失败集合里都剔除后再对差 | 彻底消除该用例造成的随机拦截；语义单一（"这个用例的结论不是证据"）；新增用例只需加一行 | PR 侧若**确定性**弄红该用例也会被豁免 ⇒ 需要报告显式提示 + 人工仍把关 |
| B. 白名单只豁免「净丢失」方向 | 只剔除「基线红 / PR 绿」那侧；PR 侧新红仍算净新增 | 保住「PR 侧新红 ⇒ 拦下」的不变式 | 只解决一半场景：base 绿 / PR 红 仍随机拦（概率从 2p(1-p) 降到 p(1-p)）；语义别扭（"结论是噪声，但只在我占便宜时"） |
| C. 在测试侧隔离（`xfail` / rerun 插件） | 让抖动用例**根本不产生 FAILED 行** | 最彻底，对差天然无感 | 改的是**被测代码与测试依赖**；该用例恰好是「裸跑会动生产 Milvus 19530」的高危用例（记忆：`裸 pytest 会删线上 Milvus 数据`）⇒ 隔离它反而制造「它被覆盖了」的错觉；且解决不了未来的新抖动 |

**决定：A**。

理由与代价（写清楚，别后来人误读）：

1. **语义单一优先于「多挡一点」。** 白名单条目断言的是「**这个用例的结论不是证据**」，
   而不是「这个用例的失败只在某个方向上可忽略」。B 的方向性会让白名单的语义随方向漂移，
   将来没人说得清「这条豁免到底豁免了什么」。
2. **A 的代价是可控的**，靠三条硬约束兜住（见 §三；**实施期补了第四条**，
   见 §3.6 的批注与 [summary.md](./summary.md) §7.1 SR-H-2）：
   精确到**单个 node ID**（不是文件、不是前缀）⇒ 爆炸半径 = 1 个用例；
   条目只在 **main** 上由 **CODEOWNER** 维护 ⇒ PR 不能给自己开豁免；
   **每一次豁免都必须出现在报告里** ⇒ 不是静默放行。
3. **B 留作降级路径**：若将来报告里「PR 侧豁免」条目多到让人无法逐条看，
   再把语义收成 B。**现在不做**（YAGNI）。

---

## 三、机制设计

### 3.1 数据：白名单文件

路径：`.github/ci-known-flaky-tests.json`（**在 `.github/` 下 ⇒ 与 CODEOWNERS 同处 ⇒ 一眼能看出授权面**）

```json
{
  "entries": [
    {
      "nodeId": "app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent",
      "reason": "Milvus 可见性竞态：backfill 删旧行(auto-id PK)后带 external_id 重插；第二次 backfill 在 Milvus 可见性收敛前仍能查到空 external_id 的旧行 ⇒ written2 == 1。2026-11-06 PR #107 实证（基线红 / PR 绿，同代码不同结论）。",
      "since": "2026-11-06"
    }
  ]
}
```

字段约束（**加载时逐条校验，不满足即失败关闭**）：

| 字段 | 约束 | 为什么 |
|---|---|---|
| `nodeId` | 非空；**必须是精确 node ID**（含 `::`）；**不接受**通配/前缀/文件级 | 爆炸半径 = 1 个用例 |
| `reason` | 非空字符串 | 审计者必须能读到「为什么信它」 |
| `since` | 非空日期串 `YYYY-MM-DD` | 老化审查（将来做） |

`nodeId` **只做精确匹配**：日志里的失败 ID 形如
`app/tests/integration/xxx.py::test_y`；若该文件是**收集期错误**（ID 形如 `.../xxx.py`，无 `::`），
则**不豁免**（宁可漏豁免，不可错豁免）。

### 3.2 代码：新模块 `backend/app/utils/ci_flakes.py`

| 名字 | 职责 |
|---|---|
| `FlakeEntry` | 白名单条目（`nodeId` / `reason` / `since`） |
| `FlakeExemption` | 一次实际发生的豁免（`nodeId` / `reason` / `inBase` / `inPr`） |
| `ExemptionOutcome` | 豁免后的两侧失败集合 + 豁免明细 |
| `defaultWhitelistPath()` | 默认路径（仓库根 `.github/ci-known-flaky-tests.json`，由 `__file__` 上溯 3 层） |
| `loadFlakeWhitelist(path)` | 读 + 校验；**任何异常形态都抛 `FlakeWhitelistError`**（失败关闭） |
| `applyFlakeExemptions(base, pr, entries)` | 剔除两侧被豁免的 ID，产出 `ExemptionOutcome` |

### 3.3 接线：闸门 CLI（`ci_failure_set.py`）

```
原始失败集合（base/pr）
   └─ applyFlakeExemptions(白名单)  ──►  豁免后的两侧集合
          └─ diffFailureSets(...)   ──►  对差（added / removed 已不含被豁免用例）
                 └─ verdictFor(...)
```

- **豁免只影响「对差」**，不影响：不可用 job 判定、覆盖等价判定、CI 相关面判定、新鲜度判定。
  ⇒ 白名单**不能**用来掩盖「job 没跑完」「少跑了分片」「改了 conftest」。
- 报告里同时给**原始条数**与**计入对差的条数**，差额由豁免明细解释。

### 3.4 失败关闭（fail-closed）

白名单文件**读不到 / JSON 非法 / 条目不合规** ⇒ 闸门**退出码 3（运行错误）**，**不**退化成「空白名单」。

理由：白名单是**判据的一部分**。判据读不出来 ⇒「不知道」不等于「没有」⇒ 保守拦下
（与既有 `ci_surface` 的失败关闭同款纪律）。文件在 main 上、由 CODEOWNER 维护 ⇒ 现实中不会触发。

### 3.5 「PR 不能给自己开豁免」

闸门 workflow 由 `workflow_run` 触发 ⇒ `actions/checkout` 检出的是**默认分支**⇒
白名单文件读到的**永远是 main 上的版本**，PR 分支里的白名单改动**不生效**。

⇒ 本批在 `merge-gate.yml` 的 checkout 步骤上**显式**写 `ref: ${{ github.event.repository.default_branch }}`
（把这条安全属性从「action 的隐含默认」变成「workflow 里写明的契约」）。

⇒ 白名单文件本身**属于 CI 相关面**（改它 = 改判据）⇒ PR 改它 ⇒ 退出码 6 ⇒ 人工确认。

### 3.6 三条硬约束（安全栏）

> ⚠️ **实施期补了第四条**（2026-11-06 安全评审 SR-H-2）：
> **豁免条件化在「该用例所在文件本次未被改动」** —— PR 改了那个文件 ⇒ 不豁免 ⇒
> 照常计入判据 ⇒ 闸门拦下并明示「豁免未生效」。
> ⇒ 详见 [summary.md](./summary.md) §5① 与 §7.1；本表保持「计划时」的三条原样，不回改历史。

| # | 约束 | 落地 | 守卫用例 |
|---|---|---|---|
| 1 | 精确 node ID，无通配 | 加载时校验 `::` | `test_ci_flakes.py` |
| 2 | 只许 main 上 CODEOWNER 维护 | ① checkout 显式 default_branch ② 文件进 CI 相关面 ③ 进 CODEOWNERS | `test_ci_surface.py` + 新增 CODEOWNERS 守卫 |
| 3 | 每次豁免都可见 | 报告新增「已知抖动豁免」段（列出 node ID / 出现侧 / 原因） | `test_ci_report.py` + `test_ci_failure_set.py` |

### 3.7 已知副作用（必须承认）

- PR 侧若**确定性**弄红了一个白名单用例 ⇒ 闸门不再拦它（该用例的结论已被声明为噪声）。
  兜底：① 报告里 ⚠ 显式提示「PR 侧命中已知抖动用例 ⇒ 本次 CI 红在该用例上」；
  ② 该用例的**确定性**红会立刻体现在 main 的 CI 上（人看得到）。
- 本批**不**解决：`cancelled` / 超时造成的「不可用 job」（那是 B1 与 CI 基础设施问题）、
  「PR 少跑分片」（覆盖等价守卫，**故意**不放宽）。

---

## 四、TDD 任务清单

| Task | 内容 | RED | GREEN |
|---|---|---|---|
| T1 | 白名单加载与校验 | `test_ci_flakes.py`：合法文件能读；缺字段 / 空 reason / 非法 JSON / 文件不存在 ⇒ 抛 `FlakeWhitelistError`；重复 nodeId ⇒ 抛 | `ci_flakes.py` 的 `FlakeEntry` / `loadFlakeWhitelist` / `defaultWhitelistPath` |
| T2 | 豁免计算 | 两侧各命中 / 单侧命中 / 未命中 / 非白名单不受影响 | `applyFlakeExemptions` + `ExemptionOutcome` |
| T3 | 闸门接线 | CLI：白名单命中 ⇒ 净丢失不再拦（退出码 0）；未命中 ⇒ 仍退出码 5；PR 侧命中 ⇒ 同样豁免；白名单损坏 ⇒ 退出码 3 | `ci_failure_set.py` 接线 + `ci_report.py` 豁免段 |
| T4 | 授权面守卫 | `ci_flakes.py` 与白名单文件都在 CI 相关面；都在 CODEOWNERS | `ci_surface.py` 补清单 + `test_ci_surface.py` 补用例 |
| T5 | workflow 契约 + 规则文档 | checkout 显式 default_branch；白名单数据文件本身能通过加载器校验（真文件守卫） | `merge-gate.yml` + `Harness/rules/*` |
| T6 | 本批 SSOT | — | `Harness/changes/2026-11-06-merge-gate-flake-whitelist/summary.md`（9 段）+ MEMORY |

---

## 五、文件清单

| 文件 | 动作 |
|---|---|
| `backend/app/utils/ci_flakes.py` | **新建**：白名单加载 + 校验 + 豁免计算 |
| `.github/ci-known-flaky-tests.json` | **新建**：白名单数据（首条 = Milvus 竞态用例） |
| `backend/app/utils/ci_failure_set.py` | 改：接线豁免；失败关闭；CLI 加 `--flake-whitelist` / `--gate-title-file` |
| `backend/app/utils/ci_report.py` | 改：新增「已知抖动豁免」段 |
| `backend/app/utils/ci_surface.py` | 改：CI 相关面清单补 `ci_flakes.py` 与白名单数据文件 |
| `backend/app/utils/ci_gh.py` | 改：compare API 改名时 `previous_filename` 也要进改动清单（SR-H-1 ⇒ 改名逃不掉「该用例文件被改动」的判定） |
| `.github/CODEOWNERS` | 改：补 `ci_flakes.py` 与白名单数据文件 |
| `.github/workflows/merge-gate.yml` | 改：checkout 显式 default_branch（把「白名单取自 main」写成契约）+ 标题走独立文件 + 摘要窗口 > 报告上限 |
| `backend/app/tests/unit/test_ci_flakes.py` | **新建**：T1 + T2 守卫 |
| `backend/app/tests/unit/test_ci_codeowners.py` | **新建**：CODEOWNERS 反查守卫（工具模块 / 守卫用例 / 白名单数据文件 / 生效归属） |
| `backend/app/tests/unit/test_ci_merge_gate_workflow.py` | **新建**：workflow 契约守卫（checkout ref / 危险参数 / 触发器集合 / 标题通道 / 摘要窗口） |
| `backend/app/tests/unit/test_ci_failure_set_flakes.py` | **新建**：白名单 CLI 接线用例（CR-L-5 从 `test_ci_failure_set.py` 拆出） |
| `backend/app/tests/unit/_ci_gate_support.py` | **新建**：两个套件共用的**共享替身**与固定数据（只替换外部依赖，失败集合抽取走真实实现） |
| `backend/app/tests/unit/test_ci_failure_set.py` | 改：T3 接线用例 |
| `backend/app/tests/unit/test_ci_gh.py` | 改：改名时 `previous_filename` 进改动清单 |
| `backend/app/tests/unit/test_ci_report.py` | 改：豁免段渲染用例 |
| `backend/app/tests/unit/test_ci_surface.py` | 改：T4 授权面守卫 |
| `Harness/rules/开发流程规范.md` | 改：「CI 合并判据」加第 10 条（抖动容忍） |
| `Harness/rules/测试规范.md` | 改：工具化清单 9 → 10 个模块 + 白名单说明 |
| `Harness/changes/2026-11-05-defect-priority-plan/plan.md` | 改：B7 行状态回填 |
| `Harness/changes/2026-11-06-merge-gate-flake-whitelist/summary.md` | **新建**：本批 SSOT（9 段） |

---

## 六、明确不做

- 不做「净丢失 ⇒ 人工确认」规则的放宽以外的任何判据放宽（**不**放宽不可用 job / 覆盖等价 / CI 相关面 / 新鲜度）。
- 不动 Milvus / Neo4j 生产实例；**不在本地跑** `test_backfill_is_idempotent`（记忆：裸 pytest 会动生产 Milvus 19530）。
- 不改白名单语义为「按方向豁免」（降级路径留待将来）。
- 不引入新依赖（JSON 用标准库）。
- **合并后不部署**（零 DDL、零生产写；纯 CI 侧改动 ⇒ 下次常规发版自然生效，且闸门在 GitHub 上跑，不依赖部署）。

---

## 七、合并路径（本批的既有约束）

本批改的是 `backend/app/utils/ci_*.py` + `.github/` ⇒ **落在闸门的 CI 相关面** ⇒
本批自己的 PR 必然被判 `CI_SURFACE_CHANGED`（退出码 6）⇒ 必须走 **owner 越权口**
（`enforce_admins: false`）显式合并。

⇒ **顺序不能反**：B7 落地**依赖**这条越权口 ⇒ **必须先落 B7，再考虑把 `enforce_admins` 收回去**
（收回后就没人能推 B7 了）。

---

## 八、实现与本计划的差异（2026-11-06 实施后回填）

本文件是**设计记录**（实施前的决策）；实施过程中因评审意见**收紧**了若干处。
以 [summary.md](./summary.md) 为准，本差异清单说明「哪几处与原设计不同、为什么」：

| # | 本计划的说法 | 实施后的做法 | 为什么改 |
|---|---|---|---|
| 1 | `since` 是「老化审查（**将来做**）」 | `since` **必填**；老化上限 **180 天**；超龄 ⇒ 报告 ⚠「老化」（**不自动失效**） | 可选 ⇒ 没有老化锚点 ⇒ 条目永久有效 ⇒ 白名单只增不减；且「将来做」= 永远不做 |
| 2 | `FlakeExemption` = `nodeId` / `reason` / `inBase` / `inPr` | 追加 `since` | 报告要显示「授权了多久」⇒ 豁免明细必须自带签发日期 |
| 3 | `ExemptionOutcome` = 两侧集合 + 豁免明细 | 追加 `rawBaseCount` / `rawPrCount` | 头部「原始条数 vs 计入对差条数」必须**显式传参** ⇒ 渲染层不做「加法还原」（还原是隐含契约，将来必然被传错） |
| 4 | 「精确 node ID 只做精确匹配」（**注释级**） | 升级为**可执行断言** + 变异组 M1 | 注释不是守卫 ⇒ 改掉它守卫不会红 ⇒ 评审判为**净漏** |
| 5 | CI 相关面补 **2** 条 | 补 **4** 条（另加 `test_ci_*.py` 与 `.github/CODEOWNERS`） | 「改守卫 = 解除守卫」「改 CODEOWNERS = 换复核人」⇒ 这两类文件是这条链上**仅剩的人工检查点** |
| 6 | 报告只加「豁免段」 | 追加：`since` 展示 / 老化 ⚠ / **豁免段封顶 20 条** / **「净新增失败用例」清单上移** / **未命中提示** / **check 标题带豁免数** | 报告截断**从尾部砍** ⇒ 清单排最后会被先砍掉 ⇒ 失败的证据被豁免的说明挤掉（方向反了） |
| 7 | 闸门 workflow 只改 checkout 的 `ref` | 追加：把工具的 `gate-title:` 写进 check 的 `title` | 对称豁免的代价是「PR 侧确定性红也不拦」⇒ 兜底必须是**人第一眼就看得见**，而不只躺在要展开的正文里 |
| 8 | 失败关闭的错误面未列非 UTF-8 | 显式捕 `UnicodeDecodeError`（它是 `ValueError` 子类、**不是** `OSError`） | 只捕 `OSError` ⇒ 非 UTF-8 白名单逃出加载器 ⇒ CLI 退出码 1（语义变成「净新增失败」）⇒ **查错方向** |
| 9 | 未列「标题必须真的流进 check payload」这条守卫 | 收尾复审（CR2-M-1）补 `test_merge_gate_wires_the_tool_title_into_the_check_payload`：沿**数据流**追（`jq --arg title "<var>"` ⇒ `<var>` 必须由 `TOOL_TITLE` 赋值） | 原守卫只断言 `"gate_title=" in script` ⇒ 被同名的 `echo` 行满足 ⇒ 删掉真正喂 payload 的那行仍绿 ⇒ **假绿守卫** |
| 10 | 未列「未命中计数」与「未生效豁免」的区分 | 收尾复审（CR2-M-2）改 `_unmatchedWhitelistNote`：`matchedCount = len(exemptions) + len(skipped)` | 否则报告会说「建议清理」一条**仍然活着**的抖动条目 ⇒ 操作者删掉它 ⇒ 随机挡人的老毛病**静默复活** |
| 11 | 「默认白名单路径 = 仓库根的 `.github/`」只被注释声明 | 收尾复审（CR2-L-4）把它升级为**可执行断言**（`path.parent == 仓库根 / ".github"`） | 「PR 不能给自己开豁免」整条链架在这个默认路径上 ⇒ 只断言末两段 ⇒ 这条属性**没被钉住** |
