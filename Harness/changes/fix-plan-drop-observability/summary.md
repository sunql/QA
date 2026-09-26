# 变更：fix-plan-drop-observability

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 3/4 NL2SQL 计划解析（`app/domain/plan_drop.py`（新）+ `app/domain/query_plan.py` + `app/services/nl2sql_service.py` + `app/services/chat_service.py`）
- **状态**：done
- **关联变更**：[refactor-score-ssot](../refactor-score-ssot/summary.md)（同批次）、[chore-chart-service-deadcode](../chore-chart-service-deadcode/summary.md)（同批次）、[fix-chat-retry-failure-details](../fix-chat-retry-failure-details/summary.md)（「失败详情必须回灌/可观测」的同思路前序批次）、[fix-sql-guard-side-channel-and-reject-feedback](../fix-sql-guard-side-channel-and-reject-feedback/summary.md)（本批次的上一批，M1/M2 收尾）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M3** + §3 P1 第 9 项（评估日期 2026-09-25）
- **commit**：`a6371de`

---

## 1. 需求

`QueryPlan.from_dict` 是「**绝不抛错**」的容错解析（历史 JSONB 与 LLM 回复都可能损坏，这是刻意契约），但它有 **11 处静默丢弃点**：损坏字段静默变成空 tuple，调用方无从知道「模型说了什么、被丢掉了什么」。同类问题在解析出口还有一层：

| # | 缺陷 | 影响 |
|---|---|---|
| **M3-1** | `_parsePlanFromResponse` 的 4 条失败 `return None` 中只有 1 条写日志；`from_dict` 内部丢弃零上报 | 计划解析失败率与失败原因**不可观测**（只能看到"未能解析出计划"一句），线上只能靠猜；根因（模型输出格式漂移 / 上下文污染 / 版本不兼容的历史 JSONB）无法归因 |
| **M3-2**（本批额外发现的**真实旁路**） | 返回**非 None 的空计划**：既不进重试、也不写日志，直接进 `generateSql` | 空计划没有任何引用可校验 ⇒ **能通过 `validatePlan`** ⇒ 模型可自由编造表名 ⇒ 执行报错被包装成「服务内部错误」。属「只读 SELECT 经 SQL Guard」之外的**语义安全漏洞**（守卫拦的是语法与函数，拦不住「查一张不存在的表」） |
| **M3-3** | 历史计划（`session_query_state.last_plan`）解析丢弃同样无声 | 旧版本写入的计划被悄悄降级，行为漂移无痕迹 |

**验收标准**：①每一类解析失败都有独立、可按原因聚合的 `reason=` 日志；②内容级丢弃有分类报告且**不把模型原始输出写进日志**；③**全空计划判失败**，走既有重试 → 用尽后「无法回答」；④`from_dict` 的既有语义**逐字节不变**（历史会话不能被本批改死）。

## 2. 设计评审

### 候选方案

| # | 决策点 | 候选 | 最终 |
|---|---|---|---|
| A | 解析口径 | ① **改为严格解析（损坏即抛）**（评估文档 §3 P1 第 9 项的原建议）② 照旧容错 + 分类上报（选定） | ② 。理由：`from_dict` 还承担「历史 JSONB 永不致命」的契约（`chat_service._statePlan`、REFINE 直写闸门都依赖它返回**计划对象**而非异常）；抛错会让旧会话整段失败 —— 用「容错 + 把该失败的形态单独判失败」替代「一律抛」 |
| B | 报告放哪一层 | ① domain 层直接 `logger.warning` ② domain 返回报告、调用方记日志（选定） | ② 。domain 层（`app/domain/`）保持**无日志、无 IO**（项目分层约束）；调用方单点记日志，也让同一份报告能被两个入口各自决定口径 |
| C | 报告粒度 | ① 逐条记录（50 条垃圾 = 50 行日志）② 按 `(字段, 原因, 原始类型)` 聚合计数（选定） | ② 。日志单行、不随垃圾条目数量膨胀（`x50` 而不是 50 行），同时**保留原始类型**——类型信息是定位「模型到底给了什么」的关键，不能为了聚合而丢 |
| D | 空计划判定放哪 | ① domain（`from_dict` 里判空）② service 调用点（选定） | ② 。`from_dict` 必须保持语义不变；且历史路径（`_statePlan`）**不能**被收紧（见 E） |
| E | 历史路径口径 | ① 与 LLM 路径同口径收紧 ② 只上报、不收紧（选定） | ② 。历史 JSONB 可能来自旧版本，收紧会把**历史会话整段判失败**；且 REFINE 直写闸门以「`plan is None`」判成败，误判会把成功的重写判死 |
| F | 报告是否存原始值 | ① 存原始值便于排查 ② 只存 `field/reason/rawType`（选定） | ② 。原始值可能含模型输出/用户问句片段，日志是长期留存面，**不引进敏感内容** |

### 空计划的判定口径（避免误杀）

`_isEmptyPlan = target（strip 后）为空 且 selectedClasses / selectedProperties / conditions / aggregations / groupBy / joins / sortBy / partitionBy / rowLimit / perGroupLimit **全空**`。取**最窄口径**：

- `target = "无法回答"` 是**合法**的空计划（模型判定超纲），由 `isUnanswerable` 走自己的短路路径，不受影响；
- 只填 `interpretation` 的载荷**判为空计划**（解释不是可查询目标）——但此时它本来也无法生成任何 SQL。

**⚠️ 实测边界（2026-09-26 容器内探针校正）**：`rowLimit` / `perGroupLimit` **不计入**「空」的证据，反而算「有内容」——`rowLimit is not None` 本身就使 `_isEmptyPlan` 返回 `False`。故 `{"rowLimit": 10}` **不**判失败（它既通过解析、也通过 `validatePlan`，因为没有任何引用可校验）。本文件早期草稿曾写成「只填 rowLimit 的载荷同理判失败」，那是**想当然而非实测**，已按探针结果更正。

这正是 security-reviewer **MEDIUM-1** / code-reviewer **LOW-6** 指出的**残差**：`validatePlan` 只校验「已存在的引用」，`{"target": "..."}`、`{"conditions": [...]}`、`{"rowLimit": 100}` 这类**半空**计划仍可直达 SQL 生成。放宽判定属**口径变更**（现网用例把「target 非空即合法」写死）+ 需先核对多步/REFINE，未在本批实施，已转提案：`../2026-09-26-plan-scope-gate-proposal.md`。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本（`迁移版本：无`）。新增的是**内存中的报告结构**（`PlanDrop`）与 domain 常量模块，不落库。

## 4. 接口契约变更

### 新增公开契约

| 位置 | 契约 |
|---|---|
| `app/domain/plan_drop.py`（新） | `PlanDrop(field, reason, rawType, count=1)`（frozen，**不存原始值**）、`DROP_*` 原因常量、`formatPlanDrops(drops) -> str`（单行，空报告返回 `""`） |
| `QueryPlan.from_dictWithReport(payload) -> (QueryPlan, tuple[PlanDrop, ...])` | 与 `from_dict` 同语义（后者内部委托给它），额外返回丢弃报告 |

### 日志契约（`reason=` 即监控口径，改名等于改口径）

| reason | 触发 | 后续行为 |
|---|---|---|
| `PLAN_REPLY_EMPTY` | 回复里没有可解析内容 | 重试 |
| `PLAN_REPLY_NO_JSON` | 回复里找不到 JSON 起始 | 重试 |
| `PLAN_REPLY_TOO_LARGE` | JSON 超过 `_MAX_PLAN_JSON_BYTES`（64 KiB） | 重试 |
| `PLAN_REPLY_JSON_INVALID` | JSON 语法坏 | 重试 |
| `PLAN_EMPTY` | 解析成功但**计划全空**（本批新增的失败形态） | 重试 → 用尽后「无法回答」 |
| `PLAN_DEGRADED` | 解析成功、但有内容级丢弃（如 `groupBy` 非列表） | **不失败**，仅上报 |
| `PLAN_HISTORY_DEGRADED` | 历史 JSONB 解析有丢弃（`chat_service._statePlan`） | **不失败**，仅上报（见 §2 E） |

### 行为变更（用户可见）

全空计划此前会一路走到 SQL 生成（可能编造表名、报"服务内部错误"），现在会**重试**，用尽后给出「无法回答」。重试预算由既有 `maxPlanAttempts` 控制，未新增参数。

### 改名

`Nl2SqlService._parsePlanFromResponse` → `_parsePlanOutcome`（返回 `_PlanParseOutcome`，含 `plan/reason/drops`）。这是私有方法，仓库内零外部调用；两处引用旧名的注释（`nl2sql_service.py`、`test_property_ref_normalize.py`）已同步更新。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/domain/plan_drop.py`（新） | 8 个 `DROP_*` 常量、`PAYLOAD_FIELD = "<payload>"`、`PlanDropKey = tuple[str, str, str]`、frozen `PlanDrop`、`formatPlanDrops` |
| `app/domain/query_plan.py` | `from_dict` 改为「委托 + 丢弃报告」；新增 `from_dictWithReport` 与 `_DropCollector`（**有意的可变累加器**：生命周期仅限单次调用，`record()` 按 `(field, reason, type(value).__name__)` 计数，`freeze()` 产出 frozen 元组）；`rowLimit` 保持不校验（下游 `_coerceRowLimit` 归一化，故既不过滤也不上报 —— 否则会把**正常形态**误报成丢弃） |
| `app/services/nl2sql_service.py` | 6 个 `REASON_PLAN_*` 常量；模块级 `_isEmptyPlan`；frozen `_PlanParseOutcome`；`_parsePlanOutcome` 每个失败分支返回独立 reason（并删除确为不可达死分支的 `isinstance(data, dict)`，附不可达性证明）；重试循环单点输出失败/降级日志 |
| `app/services/chat_service.py` | `_statePlan` 改用 `from_dictWithReport`，有丢弃时记一条 `PLAN_HISTORY_DEGRADED`，**返回值仍为计划对象**（不改成 None） |

日志样张（单行）：

```
NL2SQL 计划解析失败 attempt=1 reason=PLAN_REPLY_NO_JSON
NL2SQL 计划解析降级 attempt=1 reason=PLAN_DEGRADED drops=groupBy:DROP_FIELD_NOT_A_LIST(str)
历史查询计划解析降级 reason=PLAN_HISTORY_DEGRADED drops=joins:DROP_FIELD_NOT_A_LIST(str)
```

## 6. 测试

| 文件 | 用例 |
|---|---|
| `app/tests/unit/test_plan_drop.py`（新，18 个测试函数） | **语义不变 = 黄金快照**：24 组载荷（18 类损坏 + 6 组有效/混合）逐例断言 `astuple(plan) == _GOLDEN_SEMANTICS[i]`，快照由 M3 **之前**的实现（`aefabd3`）机械导出（导出命令写在文件注释里），另配等长断言防「加了载荷忘了黄金」；逐原因报告（非 dict 载荷、字段类型错、条目类型错、嵌套非 dict、嵌套缺必填、`perGroupLimit` 非法、`interpretation` 类型错）；**同类聚合计数**（`int`×2 → 一条 `count=2`；混合类型分开记）；join `columns` 等式拆分**不算丢弃**；报告不可变（`FrozenInstanceError`）；`formatPlanDrops` 单行 + `x2` + 空报告 `""`；**`test_every_reason_constant_has_a_live_drop_path`**（每个 `DROP_*` 常量都必须被某条损坏载荷真实触发，防常量退化为装饰品） |
| `app/tests/unit/test_nl2sql_service.py` | 新增 `TestPlanParseObservability`（14 个测试函数；其中「失败原因」一条按 6 类回复参数化 ⇒ 收集 19 例）：失败原因（`""`→EMPTY / `"抱歉，我无法回答这个问题。"`→NO_JSON / JSON 数组→NO_JSON / 坏 JSON→JSON_INVALID / 超大→TOO_LARGE / `{}`→PLAN_EMPTY）、空计划**重试后成功**、重试耗尽抛 `Nl2SqlError`、恰一条 `reason=` 日志、降级日志含 `groupBy:PLAN_FIELD_NOT_A_LIST(str)`、**空计划的失败日志带 `drops=`**（审查整改项 2，见 §7）；**迁移 5 处**把 `"{}"` 当假 LLM 回复的既有用例（`{}` 现已成为失败计划） |
| `app/tests/unit/test_chat_service.py` | 新增 `TestStatePlanObservability`（4 例）：损坏历史 → 仍渲染 + 恰一条 `reason=PLAN_HISTORY_DEGRADED`（含 `joins:DROP_FIELD_NOT_A_LIST(str)`）、损坏历史**不返回 None**、干净历史零日志、缺失历史返回 `None` 且零日志 |
| `app/tests/unit/test_query_plan.py` | **未改一行**（30+ 条既有断言全绿，是「语义未变」的第二道独立证据） |

**关于「迁移 5 处假回复」的定性**：这 5 处用例断言的对象是 **prompt 文本**（`_buildUserPrompt` / `_buildPlanSystemPrompt` 的输出），与计划字段无关，`"{}"` 只是「任意可解析计划」的占位；改动后 `{}` 已成为失败计划，故把占位替换为 `'{"target": "查询"}'`。属**测试数据修正**，不是「把测试改成迁就实现」——断言本身一条未动。

实测：`test_plan_drop.py + test_query_plan.py + test_nl2sql_service.py` = **192 passed**；`test_chat_service.py` = **134 passed + 1 failed**（该失败为**预存**：`TestSearchByKeywordAdsWeighting::test_weight_from_system_config_db_value`，根因是 ADS 加权重排早已被 `_rankByLayer` 覆盖成死代码，见 memory `qa-system-ads-weight-dead-code`，与本批无关）。

## 7. 安全审查

**触发条件**：本批触及「计划 → SQL」的语义闸门（M3-2），且新增日志面，故送 security-reviewer 与 code-reviewer 双审。

| 审查 | 结论 | 本批处置 |
|---|---|---|
| code-reviewer | APPROVE（0 CRITICAL / 0 HIGH / 2 MEDIUM / 4 LOW） | MEDIUM-1（等价测试恒真）**当场修**、MEDIUM-2（PLAN_EMPTY 的 drops 被丢）**当场修**；LOW-3（注释行号漂移）**当场修**；LOW-4（AST 守卫缺口，属 M6）**当场修**；LOW-5（无 try/except）**加注释固化不变量**；LOW-6（半空计划）= security MEDIUM-1 |
| security-reviewer | 无 CRITICAL / 无 HIGH；2 MEDIUM | MEDIUM-1（半空计划仍穿 `validatePlan`）→ **未采纳、转提案**（见下）；MEDIUM-2（NaN/±inf）属 H6 → 当场修，见 `../refactor-score-ssot/summary.md` §7。另：LOW-1 = code MEDIUM-2（当场修）、LOW-2/LOW-3 记录 |

**整改 1：等价断言恒真是真缺陷（code-reviewer MEDIUM-1，本次最严重的一条）**

`from_dict` 现在**就是** `from_dictWithReport(data)[0]`（纯委托），故原测试的
`assert plan == QueryPlan.from_dict(payload)` 等价于「自己跟自己比」，**恒真** —— 一条永远
通过的断言，使「语义逐字节不变」这一验收标准在本批**实际未被验证**（§6 早期版本据此作了
过度声明，已更正）。改为在 M3 **之前**的实现（`aefabd3`）机械导出的黄金快照。

**反向探针（证明新断言承重、旧写法恒真）**：把 `_strings` 变异为「不再过滤非 str」后

```
新断言：FAILED test_from_dict_with_report_matches_pre_m3_golden[payload6/payload12]  ← 真漂移被抓
旧写法：plan == QueryPlan.from_dict(payload) -> True   （同一次变异下仍通过）
        实际解析结果 selectedProperties = ('BPSNUM', 42, None)
```

**整改 2：PLAN_EMPTY 的 drops 被构造出来又丢弃（security LOW-1 + code-reviewer MEDIUM-2，两人独立命中）**

`_parsePlanOutcome` 在 PLAN_EMPTY 分支携带 `drops`，但重试循环的失败分支只打 `reason=`。
而 PLAN_EMPTY 的成因往往**正是**「字段被丢光」（如 `{"target": 123}` ⇒ target 被丢 ⇒ 全空），
于是运维只看到「空了」、看不到「为什么空」—— 正是 M3 要消灭的无声降级，在失败路径上还留着。
已改为：有 drops 时一并输出（`reason=… drops=…`），无 drops 时保持原单行（不引入空 `drops=` 噪声）。
TDD：`test_empty_plan_failure_log_keeps_drops` 先 RED（实测只见 `reason=PLAN_EMPTY`）后 GREEN。

**未采纳：半空计划仍可穿过 `validatePlan`（security MEDIUM-1 = code LOW-6，两人独立命中）**

`validatePlan` 的语义是**只校验已存在的引用**，引用为空则 `issues == []` 即通过；故
`{"target": "..."}` / `{"conditions": [...]}` / `{"rowLimit": 100}` 仍直达 SQL 生成（详见
§2 的实测边界）。**不修的理由**：放宽判定是**口径变更**而非缺陷修复 —— ①现网/新增用例把
「target 非空即合法」写死（`test_empty_plan_retries_and_succeeds_on_second_attempt` 的第二跳
回复就是 `{"target": "查询"}`），②多步/REFINE 是否依赖「引用由 `prior_state` 文本复述」尚未
核对。已转独立提案：`../2026-09-26-plan-scope-gate-proposal.md`（含三档候选方案与前置核对清单）。

设计上的安全结论（自审，经审查确认成立）：

- **收紧了**一条真实旁路：空计划不再直达 SQL 生成（此前可让模型编造表名）。判据取最窄口径以**避免误杀**造成反向可用性故障（把可回答问题判成「无法回答」）；残余形态见上「未采纳」。
- **日志不引进敏感内容**（security 复核为「无」）：`PlanDrop` 只存 `field / reason / rawType`（原始类型名，如 `int`/`str`），**不存原始值**；`formatPlanDrops` 只渲染这三者与计数 ⇒ 模型输出、用户问句、表名/列名都不会进日志。聚合按 `(field, reason, rawType)` 计数，日志长度有界、无换行注入面。
- **不吞编程错误**：`_nested` 里 `factory(**filtered)` 的 `except TypeError` 仅用于「载荷字段类型不符」这一既有容错语义（与改动前一致），并被计入 `DROP_NESTED_INVALID` 上报（不再静默）。security 复核确认三个 factory 均为纯 frozen dataclass、无 `__post_init__`，当前不存在可被它掩盖的真实编程错误（若日后加 `__post_init__` 须改为按缺必填字段显式判断）。
- **`_parsePlanOutcome` 不再套 try/except**（code LOW-5）：安全性依赖 `from_dictWithReport` 的「绝不抛错」契约，已在该调用点写明该不变量与它的失效条件（新增必填字段 / `__post_init__` 校验）。
- 无新增 secrets、无 SQL 字符串拼接、无用户输入直通、无文件/网络操作。

## 8. 部署验证（2026-09-26）

- **镜像重建**（非 `docker cp`）：`docker compose build backend && docker compose up -d backend`；容器 `/api/v1/health` 直连 200；
- **仓库 ↔ 容器 md5**：本批 12 个文件 **12/12 MATCH**，含 `app/domain/plan_drop.py`、`app/domain/query_plan.py`、`app/services/nl2sql_service.py`；
- **容器内真机探针**（真实模块）**21 项全 PASS**，本变更相关项（原文）：

  ```
  PASS  M3 rowLimit=10 不算空(边界) isEmpty=False
  PASS  M3 损坏字段有报告
       drops: target:PLAN_TARGET_NOT_STR(int)
       degraded: aggregations:PLAN_NESTED_NOT_A_DICT(str)
  PASS  M3 from_dict 委托且语义不变
  PASS  M3 PlanDrop 不存原始值 ['count', 'field', 'rawType', 'reason']
  ```

  探针同时实测了 §2 记录的边界（`rowLimit` 计入「有内容」）与「`PlanDrop` 只存 `field/reason/rawType/count`、不含原始值」两条契约；
- **网关**：`localhost:8000/api/v1/health` = 200；经 nginx `localhost:5173/api/v1/health` = 200；
- **测试**：
  - 全量 unit（最终 hash `3309a71`）`2 failed, 2411 passed, 1 skipped`；两条失败为**预存**、delta=0（判别实验见 `../refactor-score-ssot/summary.md` §8）；
  - `test_plan_drop.py` 18 函数 / 收集 41 例（黄金快照 24 例 + 结构断言）；`test_nl2sql_service.py::TestPlanParseObservability` 14 函数 / 19 例；`TestStatePlanObservability` 4 例 —— 均在最终 hash 上复跑通过；
  - 集成切片 94 例 `2 failed / 92 passed`，两条失败是 Milvus 残留累积（已证明非本批回归，同上）；
  - `ruff` 与基线同集合同为 42 个错误（本批的 `query_plan.py` 1 → 0，其余不在本批文件内），新增文件 `All checks passed`。
- ⚠️ 一致性：本批**只改解析与观测**，不改 SQL 生成与只读闸门；`_isEmptyPlan` 在 service 层、`from_dictWithReport` 在 domain 层，`chat_service._statePlan` 的 REFINE 直写路径未受影响（`outcome.plan is None` 仍只在真失败时为真）。

## 9. 关联

- SSOT / 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M3（本次标 ✅ 并补「空计划旁路」）、§3 P1 第 9 项（本次闭合）、§13 遗留第 4 条（「M3 仍未做」已划掉）、新增 §14 批次记录
- 机制文档：`Harness/wiki/nl2sql-engine.md` 新增「计划解析契约（2026-09-26，M3）」段（`from_dictWithReport`、`reason=` 口径、空计划判失败）
- 代码契约：`app/domain/plan_drop.py`、`QueryPlan.from_dictWithReport`、`app/tests/unit/test_plan_drop.py`
- 规则：根 `CLAUDE.md` 核心约束 #4（TDD）、#6（显式错误处理）、#7（真实数据库测试）；`Harness/rules/开发流程规范.md`（TDD + 双审）；`~/.claude/rules/common/coding-style.md`（不可变 / 显式错误处理）
- Memory：`qa-system-score-ssot-plan-drop.md`（新增）
- 关联变更：`../refactor-score-ssot/summary.md`、`../chore-chart-service-deadcode/summary.md`、`../fix-chat-retry-failure-details/summary.md`

## SSOT 校验清单

- [x] 第 1 段 需求：11 个静默丢弃点 + 4 条 `return None` 只有 1 条写日志 + 空计划旁路
- [x] 第 2 段 设计评审（含 `_isEmptyPlan` 边界的**实测修正**：`rowLimit is not None` 计入「有内容」，原写「rowLimit-only 即失败」为错）
- [x] 第 3 段 无 alembic 迁移（domain + service 层改动）
- [x] 第 4 段 接口契约变更：`from_dictWithReport` 新增、`from_dict` 纯委托语义不变、`reason=` 取值枚举
- [x] 第 5 段 实现要点（`PlanDrop` 不存原始值 / 失败分支补 `drops=` / 判定在 service 层不影响 `_statePlan`）
- [x] 第 6 段 测试：黄金快照（M3 前实现机械生成，非自比）+ 结构断言 + `reason=` 日志锁定 + 变异探针
- [x] 第 7 段 安全审查：两位审查结论表 + 整改 4 项（恒真断言 / PLAN_EMPTY 详情 / 非有限 / 守卫缺口）+ 未采纳项转提案
- [x] 第 8 段 部署验证：镜像级证据 + 12/12 md5 + 21 项探针（含边界与 `PlanDrop` 字段契约）+ 网关 200 + 判别实验
- [x] 第 9 段 跨文件链接（评估文档 §14 / `nl2sql-engine.md` 契约段 / 代码契约 / 规则 / Memory / 关联变更）
- [x] `Harness/wiki/chat-service-assessment.md` §2.3 M3 标 ✅ + §13 遗留第 4 条划掉 + §14 批次记录
- [x] 未采纳项已落独立提案 `../2026-09-26-plan-scope-gate-proposal.md`（含前置核对清单与验收标准）
