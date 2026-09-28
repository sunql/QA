# 变更：fix-chat-recall-fallback-and-context-budget

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 对话服务健壮性（`app/services/chat_service.py`）
- **状态**：done
- **关联变更**：[fix-multistep-failure-isolation](../fix-multistep-failure-isolation/summary.md)（同文件同主题的前序批次）、[fix-chat-retry-failure-details](../fix-chat-retry-failure-details/summary.md)（本批的直接前一批，沿用其「两段式文案 / 单点收口」思路）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.2 **H3 / H5** + §3 P1 第 5、6 项（评估日期 2026-09-25；H5/H3 是 P0 清空、§2.2 计量类 H1/H2/H8/H9 修完后剩余的静默降级与成本膨胀两项）

---

## 1. 需求

两处「降级路径不是免检路径」的欠账，同文件、各自独立可测：

| # | 缺陷 | 影响 |
|---|---|---|
| **H5** | `_selectRelevantClasses` 的**五个**回退分支一律 `return list(allClasses)`（检索异常 / 无命中 / 命中全被 ODS 过滤 / 命中全为 ODS / 命中解析不出真实类）。**不过滤 ODS、不截 `CLASS_FILTER_MAX_CLASSES`** | ①把 2026-09-19 ODS_BPARTNER 事故（LLM 在贴源备份表上幻觉属性名）原样放回来 —— 只要 Milvus/embedding 挂掉就复现，而「挂掉」是**静默**的（用户只看到答得差）；②`CLASS_FILTER_MAX_CLASSES`（本系统的核心规模化闸门）在最需要它的时刻失效：表越多越选错，降级时反而全量灌进 prompt |
| **H3** | `_buildContextPrompt` 全文拼接 `session_message.content` + `[SQL: …]`，无任何长度约束 | 该文本被注入 **plan / SQL / answer / 聚合各阶段共 9 处** prompt（`grep -c contextPrompt`）。一条超长答案或长 CTE 会**逐轮重复注入**：轮次一长，每轮 prompt 成本随历史线性膨胀（既是钱也是延迟），且没有任何闸门——属核心约束 #3「Token 计量」的上游缺失（计了量但没控量） |

**验收标准**：①降级路径与正常裁剪**同口径**（ODS 过滤 + 上限截断），且过滤/截断后仍不改动入参、不抛错；②降级**必须可观测**（谁降级了、降级后做了什么），且不破坏既有「回退率按 `reason=` 行聚合」的口径；③历史上下文长度**恒有界**，且被丢的是最旧轮次而非最新轮次（最新一轮是 REFINE/FOLLOW_UP 的锚点）；④`CLASS_FILTER_MAX_CLASSES` 被配成非正值时不得静默失效。

## 2. 设计评审

### 2.1 五个回退分支怎么收口

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 各分支就地补过滤+截断 | 五处各写一遍 ODS 过滤 + `_rankByLayer` + `[:max]` + warning | ❌ 5 份重复逻辑，且这正是 H5 的成因模式——「同一份降级知识散落在 5 个出口，靠人记得改」。DRY 违反会直接变成下次漏改 |
| B 收敛为单一 helper `_fallbackRecall`（**选定**） | 五个分支改为 `return await self._fallbackRecall(session, question, allClasses, reason=…)` | ✅ 一个出口 = 一处不变量；顺带消掉 5 处重复的 `ClassRecallInfo(...)` 构造；`reason` 作为参数保持既有 5 个场景名不变（观测口径零变更） |
| C 在 `_selectRelevantClasses` 出口统一收口 | 保持分支裸返回，函数末尾对返回值统一过滤截断 | ❌ 出口无法区分「正常裁剪结果」与「降级全量」，会二次裁剪正常结果（正常结果已 `_expandByJoinNeighbors` 扩边，再截会破坏扩边语义）；也拿不到 `reason` |

### 2.2 截断之前要不要排序

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 直接截前缀 `candidates[:max]` | 少一次排序 | ❌ **入参是库表顺序（DB 自然序）**，截前缀等于「随机丢掉后半张表」——被丢的可能正是问题需要的那张。这与 H5 想修的病同源 |
| B 先层优先排序再截（**选定**） | `_rankByLayer(candidates, dimension_hint=False, session=session)[:max]` | ✅ 与正常裁剪路径同序（ADS > DWS > DWD > DIM > UNKNOWN），降级时也优先保留黄金路径表；`dimension_hint=False` 是刻意的：入参本就是全量类（DIM 已在其中），`_rankByLayer` 的 DIM 补拉是给**召回子集**用的，降级路径不该再引入一次 DB 读取 ⇒ 该 helper 成为「纯排序、零 IO」⇒ **不可能抛错**（降级路径的首要性质） |

### 2.3 历史预算的裁剪方向与形态

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 复用 `_clipText` 对拼接结果一次性截断（评估文档 §3 P1 第 6 项的原建议） | `_clipText("\n".join(parts), 4000)` | ❌ **方向错**：`_clipText` 是头裁（保前 N 字），而这里要保的是**最新**的轮次。头裁等于每轮都保留最老的 5 轮、永远看不到最新一轮的 SQL ⇒ REFINE/FOLLOW_UP 直接失去锚点 |
| B 尾裁（保后 N 字） | `joined[-4000:]` | ❌ 会从**中间**切开一条消息（可能切出半个 `[SQL:` 标记），且角色归属丢失（用户/助手分不清） |
| C 从最新往回整块保留 + 最新块兜底裁剪（**选定**） | `_fitPartsToBudget(parts, 4000)`：倒序累加整块，超预算即停；若唯一留存的最新块自身超预算则裁它 | ✅ 丢的是**整块**最旧历史，消息边界与说话人前缀完整；最新一轮必在；预算被误配得过小时退化为「只剩最新一轮（已裁）」而非清空历史——**任何配置下总长都恒有界** |

### 2.4 单条限量是否区分正文与历史 SQL

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 正文 + SQL 合并限量 | 拼成一个字符串再 `_clipText(…, 500)` | ❌ 一条 600 字的答案正文会把同一轮的 SQL **整段挤掉**，而追问恰恰靠历史 SQL 复用/微调 ⇒ 用一条很长的答案换掉追问能力 |
| B 分开限量（**选定**） | 正文 500、`[SQL: …]` 另 500（`_CONTEXT_CONTENT_SEGMENT_LIMIT` / `_CONTEXT_SQL_SEGMENT_LIMIT`） | ✅ 正文被裁不影响历史 SQL 可见性；两个常量与总预算（4000 ≥ 2×500 + 前缀）同处声明，预算需 ≥ 单块之和这一依赖写在注释里 |

### 2.5 全库皆 ODS 的退化场景

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 按规则过滤到底，返回空类集 | 规则一致 | ❌ 空 schema 会让**所有**问题都变成「无法回答」（比答得差更糟），且现有用例 `test_all_ods_hits_filtered_returns_empty_relevant_fallback` 钉的是「至少还能试」 |
| B 保留原列表并单独告警（**选定**） | 宁可回到旧行为，也不能给出空 schema | ✅ 该场景在真实本体（96 类，含 DWD/DWS/ADS/DIM）中不可达，属防御性兜底；单行 warning 使其一旦出现即可见 |

## 3. 数据模型变更

无。复用既有 `system_config.CLASS_FILTER_MAX_CLASSES`（既有闸门，本次仅新增「非正值」守卫）与 `session_message`（历史来源不变），无迁移、无新表、无新列。

## 4. 接口契约变更

**无字段 / 类型 / 状态码变更**；变的是两个字段的**含义边界**、一处文案与日志口径：

| 面 | 修复前 | 修复后 |
|---|---|---|
| `ClassRecallInfo.truncated`（fallback 模式） | 恒 `False`（降级分支从不设它，且降级确实没截断） | 如实反映降级后的截断状态（降级同样受 `CLASS_FILTER_MAX_CLASSES` 约束，故该字段在 fallback 下**开始有意义**）；`classCount` 由「全量类数」变为「降级后实际类数」 |
| `ClassRecallInfo.mode` / `hitCount` | `fallback` / `0` | 逐字不变（前端与既有用例的判据不变） |
| 前端提示（`classRecallFallback`，zh/en） | 「本次已加载**全部**数据表」 | 「已按数仓**分层顺序**选取数据表（张数受召回窗口上限约束）」——旧文案在 H5 之后**变成假话**（这正是本次目的），属必须同步的用户可见契约 |
| 服务端日志 | 每个回退分支一行 `…回退到全量类 reason=<场景> total=<n>` | `本体类回退降级 reason=<场景>[ hits=<n>] total=<n> kept=<n> odsFiltered=<n> truncated=<bool>`，**每个降级事件恰好一行含 `reason=`**（退化场景那行用 `scene=`，避免同一事件被算两次）；`reason=<场景> total=<n>` 这段 token 相邻性保持，既有聚合/断言逐字不变 |
| 历史上下文（注入文本） | 无上限 | ≤ `_CONTEXT_PROMPT_CHAR_BUDGET`（4000，超出时为 4000 + 省略号）；短历史逐字不变 |

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/chat_service.py` | ①新增 `_CONTEXT_CONTENT_SEGMENT_LIMIT=500` / `_CONTEXT_SQL_SEGMENT_LIMIT=500` / `_CONTEXT_PROMPT_CHAR_BUDGET=4000`（含「总预算须 ≥ 单块之和」的注释）；②新增纯函数 `_fitPartsToBudget(parts, budget)`；③`_buildContextPrompt` 改为单条裁剪 + 预算裁剪 + 裁剪时记 `历史上下文按预算裁剪 kept=/chars=/budget=`；④新增 `_fallbackRecall(session, question, allClasses, *, reason, hitCount=None)`（ODS 过滤 → 层排序 → 截断 → 单点日志 → `ClassRecallInfo`）；⑤五个回退分支改为调用它，并删掉各自的 `reason=` 行（`search_error` 分支只留 `exc_info=True` 的堆栈行）；⑥`_getClassFilterMaxClasses` 增加非正值守卫（→ 默认 30 + warning）；⑦`_selectRelevantClasses` / `_buildContextPrompt` / `_fallbackRecall` 的 docstring 同步到新口径 |
| `app/domain/schemas.py` | `ClassRecallInfo` 的 `fallback` / `truncated` 字段说明更新（fallback 下 truncated 有意义） |
| `app/tests/unit/test_chat_service.py` | 新增 `TestClassRecallFallbackAppliesPruning`（9 例）与 `TestContextPromptBudget`（6 例）+ `_StoredRoundsSession` 桩 + `_getClassFilterMaxClasses` 非正值用例；共 +310 行 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | `classRecallFallback` 文案改写（保留 `/智能召回暂不可用/` 子串，`MessageItem.test.tsx` 25 例无需改动） |

**不可变数据**：`_fitPartsToBudget` 返回新列表（`kept` 局部构造，最后 `kept.reverse()` 只动局部）；`_fallbackRecall` 构造新 `candidates`/`selected` 列表，不改动 `allClasses`；`_buildContextPrompt` 返回新字符串。唯一「原地写」是 `kept[-1] = _clipText(...)` —— 写的是本函数自己的局部列表，不涉及入参。

## 6. 测试

TDD：先写用例、看它按**预期原因**红，再改实现。红有两类证据：

**RED-1（H5/H3 主用例）** —— 部署后用「反向探针」重放修复前形态（把 `_fallbackRecall` 的返回改回 `list(allClasses)`、把 `_buildContextPrompt` 的 `kept = _fitPartsToBudget(...)` 改回 `kept = parts`），10 failed / 5 passed：

```
E       AssertionError: assert 5069 <= 4000          ← H3 预算未生效（5069 = 全量历史）
E       AssertionError: assert 'msg8' not in '用户：msg8-xxx…'   ← H3 丢的是最新而非最旧
E       AssertionError: assert '历史上下文按预算裁剪' in ''      ← H3 无裁剪日志
E       assert 2 not in {2, 4}                        ← H5 ODS 类未被过滤
E       assert {2, 4} == {4}                          ← H5 截断未生效
E       assert 37 == 30                               ← H5 降级返回全量（37 类，含 30 张 ODS）
E       assert {2, 3, 5} == {5}                       ← H5 ods_only_hits 分支未过滤
10 failed, 5 passed, 116 deselected in 5.84s
```

（5 例在两种实现下都通过 —— `test_short_history_is_passed_through_unchanged` 这类「不该变」的用例，正是它们证明本批没有误伤短历史与既有契约。）

**RED-2（收口后真机探针新发现）** —— 退化分支（全库皆 ODS）那行日志原本也带 `reason=`，同一降级事件因此有两行含 `reason=`：

```
WARNING … 本体类回退降级无可用非 ODS 类，保留原列表 reason=search_error total=2
WARNING … 本体类回退降级 reason=search_error total=2 kept=2 odsFiltered=0 truncated=False
1 failed, 8 passed, 122 deselected in 3.94s
```

修法：该行改用 `scene=`（场景名仍可见，且紧随其后的主行带同 `total` 可对齐），并补断言 `caplog.text.count("reason=search_error") == 1`。

**GREEN**：

```
app/tests/unit/test_chat_service.py -k "TestClassRecallFallbackAppliesPruning or TestContextPromptBudget"
                                                           15 passed, 116 deselected
app/tests/unit/test_chat_service.py + test_chat_step_error_text.py
                                          1 failed, 146 passed（失败项 = 预存 ADS 死代码用例）
chat 集成切片（chat_api / chat_stream_api / chat_multi_step / follow_up_cascade / chat_history_api
  / l1_routing / model_routing_fallback / pipeline_layer / service_state / chat_agent_run
  / token_usage_service）                                            82 passed
app/tests/unit（全量目录，带 TEST_DATABASE_URL）   2 failed, 2273 passed, 1 skipped（11:44）
  ↑ 2 项失败均为预存、与本批无关：test_chat_service.py::TestSearchByKeywordAdsWeighting
    （ADS 加权重排死代码，已被 _rankByLayer 覆盖）+ test_dependencies.py::test_stub_disabled
    （Header.lower() 版本漂移）—— 两者此前已用 HEAD worktree 单跑证明前置存在
前端 src/tests/MessageItem.test.tsx                                 25 passed
ruff 诊断：三个改动文件的诊断项与 HEAD 逐条一致（63 条，分布相同）——本批零新增 lint
```

**刻意避免的三种假绿**：①`test_fallback_ranks_by_layer_before_truncation` 的输入把 5 张 UNKNOWN 层放在队列**最前面**、ADS/DWD 放尾部，断言 `{1,2} <= ids` —— 只有「先排序再截断」才能通过，单纯截前缀必挂（这是对 2.2 决策的真断言，不是对实现细节的断言）；②断言两个场景的**结果集合**而非 `_fallbackRecall` 被调用的次数；③H3 用例断言长度**上界**与「最新在、最旧不在」，不断言裁剪后的确切字符串。

## 7. 安全审查

两轮静态审查（`code-reviewer` 收口复审 + `security-reviewer`），均不跑 pytest 以免与主套件争用同一测试库：

| 轮次 | 结论 |
|---|---|
| code-reviewer（实现后） | **APPROVE** — 0 CRITICAL / 0 HIGH / 1 MEDIUM / 2 LOW，三条当场修完（见下） |
| code-reviewer（收口复审，本批最终形态） | **APPROVE-WITH-NITS** — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW（两条均为「不改但记录」，见下表末两行） |
| security-reviewer | **PASS-WITH-WARNINGS** — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW（其中 1 条当场修完） |

| 级别 | 发现 | 处置 |
|---|---|---|
| MEDIUM-1（code） | **降级事件打了两行 `reason=`** ⇒ 回退率（按含 `reason=` 的行聚合）翻倍；且四个调用方残留的旧提示语「回退到全量类」在 H5 之后变成假话 | 先修：`_fallbackRecall` 成为唯一 `reason=` 出口，删掉 5 处调用方日志（`search_error` 只留 `exc_info=True` 堆栈行），把 `hits=` 折叠进同一行以保持既有断言（`reason=search_error total=1`、`reason=no_hits total=2`、`reason=no_match hits=1 total=2`）逐字可匹配；补回归断言 `count("reason=search_error") == 1`。**收口期真机探针又发现同一根因的第三处**（退化分支那行）⇒ 改用 `scene=`（见 §6 RED-2） |
| LOW-1（code） | `_fitPartsToBudget` 兜底裁的是 `kept[0]`（最旧），与 docstring 写的「裁最新」不符 | 改为 `kept[-1]`，并注明「循环保证多块留存时每块都在预算内，故此处只可能对唯一留存的最新块生效」 |
| LOW-2（code） | 唯一被重接线、但其新截断行为无用例的分支是 `ods_only_hits`（非退化形态） | 补 `test_ods_only_hits_branch_falls_back_filtered`（ODS 命中 + 1 张 DWD ⇒ 只留 DWD），先红后绿 |
| LOW-1（security） | **`_getClassFilterMaxClasses` 接受 0 / 负值** ⇒ 闸门**静默失效**（`ranked[:0]` 返空、`ranked[:-5]` 返「除末位以外全部」、`truncated = len > max` 同时失真）。上限是正常裁剪与降级路径**共用的唯一闸门** | 先写红用例（`_sessionReturning("0")`）再修：非正值 → 默认 30 + warning（见 §8 探针「cap 守卫 0→30 -5→30 7→7」） |
| LOW-2（security，**残差**） | 显式点名 ODS 表即可豁免过滤（`_isExplicitOdsRequest`），理论上可被问句轻易触发，让贴源表重回 prompt | **不改**：这是**质量闸门**而非安全边界（安全边界是 SQL Guard 的只读校验，与是否进 prompt 无关）；且过度收紧会让「ODS_BPARTNER 里有什么」这类正当问题无法回答。已记录 |
| LOW-3（security，**残差**） | `_clipText` 按码点切片，可能切开代理对（surrogate pair）⇒ 未配对的代理字符在 LLM 边界可能触发 `UnicodeEncodeError` | **不在本批修**：`_clipText` 有 9+ 调用方，属跨切面加固，应单独一批统一修（否则本批要改 9 处与本需求无关的调用点）。本批 H3 的新调用点与既有调用点风险同构，未**扩大**暴露面 |
| INFO（security，**残差**） | `StepExecutionContext.context` 在 `chat_service` 两处被设置但 `step_query_planner` 从不读取（死接线） | 预存问题，与本批无关，记录待清理 |
| LOW-1（code，收口复审） | `_clipText(text, limit)` 用 `text[:limit]` 切片，**负**预算下变成 Python 负索引（`text[:-5]` = 「除最后 5 字符外全部」），于是 `_fitPartsToBudget` 注释里「任何预算下总量恒有界」的断言在 `budget < 0` 时为假 | **不改代码，改记「触发器」**：①当前预算硬编码 `_CONTEXT_PROMPT_CHAR_BUDGET = 4000`，负值**不可达**（`budget == 0` 仍成立：`"..."` 恰为 3 = 0 + 省略号）；②任何代码改动（哪怕只改注释）都会让 §8 的 `md5 MATCH` 失效 —— 而该 md5 是「容器 == 仓库」的唯一凭据，留一处不匹配会给下一次部署留下「容器是否跑旧代码」的假信号，代价大于收益。**触发条件已明确**：一旦把该预算改成可配置/可计算（可能 ≤ 0），必须同时加 `budget = max(budget, 0)` 钳制并补负预算用例 |
| LOW-2（code，收口复审） | 全库皆 ODS 的退化分支日志写「**保留原列表**」+ docstring 写「保留原样而非返回空列表」，但实际仍执行 `selected = ranked[:max_classes]`；类数 > 上限时 `truncated=True`，措辞与事实不符（复审给出的失败场景：50 张全 ODS + 默认上限 30 ⇒ 日志说全留、实际只注入 30） | **不改**：该分支的真实意图是「**绝不返回空 schema**」（返回空 ⇒ plan 阶段无表可选 ⇒ 整问失败），而不是「无条件全量注入」；上限是全局闸门，在退化路径上同样应当生效。措辞歧义由**紧随其后的主行消除**（同一 `total` 可对齐，且该行明确 `truncated=True`），故不构成误导运维的实际风险（与 MEDIUM-1 的「同一事件两行 `reason=` 直接污染回退率聚合」有本质区别 —— 那是**数值**错误，这里是**措辞**歧义）。**触发条件**：若将来运维需要单看这一行做判断，改为「回退到原列表（仍受上限约束）」并补一个「类数 > 上限的全 ODS」用例 |

**复核方法**：逐项查证 + 反证，重点确认三条推断 —— ①降级路径**不可能抛错**（`_rankByLayer(dimension_hint=False)` 是纯排序，`_getClassFilterMaxClasses` 自带 try/except 返默认，ODS 判定只读 `source_table`）；②`_fitPartsToBudget` 的长度不变量（`used = sum(len(part)) + (kept_count - 1)` 恰为 `"\n".join` 的长度；返回长度 ≤ `budget + 3`，且 ≥2 块留存时 ≤ `budget`）；③既有契约零破坏（`test_fallback_on_search_error` 的 `len==2` / `classCount==2` / `truncated is False`、`test_all_ods_hits_filtered_returns_empty_relevant_fallback` 的 `{2,3}`、三条 caplog 断言、前端 25 例全部照旧通过）。

## 8. 部署验证

代码提交 `750a513`；按「最新代码重建镜像并启动」执行（后端 + **前端**均重建，因 i18n 字符串被 Vite 编译期固化进 bundle）：

```bash
cd docker
docker compose build backend && docker compose up -d backend
docker compose build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend \
  && docker compose up -d frontend      # 默认源 900MB 依赖必撞 EIDLETIMEOUT，走 npmmirror
# => Image qa-system-backend Built（22.1s）/ Image qa-system-frontend Built / 两容器均 Started
```

**镜像确实装了本次代码**（不是「重建了但跑旧代码」）：

| 检查 | 结果 |
|---|---|
| `docker exec qa-backend md5sum /app/app/services/chat_service.py` vs 仓库 md5 | `cedcfc626db14beca66961bb1a4964c8` == `cedcfc62…` **MATCH** |
| 新符号在容器内出现次数（`_fallbackRecall` / `_fitPartsToBudget` / `_CONTEXT_PROMPT_CHAR_BUDGET` / `_CONTEXT_CONTENT_SEGMENT_LIMIT`） | **17** |
| 启动日志 / 容器状态 | `Application startup complete` + `Uvicorn running on http://0.0.0.0:8000`；`Up`（无 ORM drift、无迁移告警） |

**容器内行为探针**（真机跑部署后的代码，非仅比对 hash；`docker exec -i -w /app qa-backend python -`，用假 session/stub 历史驱动真实 `_fallbackRecall`、`_buildContextPrompt`、`_getClassFilterMaxClasses`）：

```
H3 纯函数 kept=6/20 chars=3629 budget=4000 有界=True 留最新=True
H3 极小预算 kept=1 len=13 <=13=True                 ← 预算误配为 10 时退化为「只剩最新一轮（已裁）」，仍恒有界
H3 端到端 chars=3049 有界=True 留最新=True 丢最旧=True 含SQL=True 以]结尾=True
H3 裁剪日志=['历史上下文按预算裁剪 kept=4/10 chars=3049 budget=4000']
H5 mode=fallback kept=4 truncated=True 层=['ADS_SUPPLIER_360']   ← 输入序为 30×ODS → 5×DWD → 5×ADS；截断后留下的全是
                                                                   ADS ⇒ 排序确在截断之前（截前缀本会留下 DWD）
H5 显式 ODS 请求保留贴源表=True n=30
H5 cap 守卫 0→30 -5→30 7→7（默认 30）
H5 全 ODS 退化 kept=3（不返空）
退化场景日志：该事件只有 1 行含 reason=（另一行为 scene=）
```

**前端 bundle 确认**（i18n 是编译期注入，本地 `npm run build` 与容器 nginx 所服务的 bundle 不是一回事）：

| 检查 | 结果 |
|---|---|
| 新文案在容器内 bundle 命中（zh `按数仓分层顺序选取数据表` / en `bounded by the recall window limit`） | 均命中 `assets/index-sxjqC8Os.js` |
| 旧文案（zh `本次已加载全部数据表`）残留 | **0** 命中 |

端点抽样（验证整体可用性未被本次双重建破坏）：

| 端点 | 结果 |
|---|---|
| `GET /api/v1/health`（直连 8000 / 经 nginx 5173） | **200 / 200** |
| `GET /api/v1/menu-config`（经 nginx） | 403（鉴权闸正常） |
| `GET /api/v1/ontology/health/joins`（经 nginx） | 200 |

**附带确认**：本次后端容器被重建两次（IP 变更），经 nginx 的请求全程无 502 ⇒ `fix-nginx-upstream-stale-ip` 的 `resolver` + 变量 `proxy_pass` 再次经受住容器重建。

> **未做端到端「真实降级」演练**：要让生产链路真的走进降级，需停掉 Milvus 或 embedding provider（会影响同时在用的其它会话）；行为等价性由容器内探针（真机代码 + 真实分层/过滤/截断/预算逻辑）+ 15 个单元用例共同覆盖。H5 的「检索不可用」入口本身由既有 `test_fallback_on_search_error` 等用例覆盖。

## 9. 关联

- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.2 H3 / H5（本次标 ✅）、§3 P1 第 5 / 6 项（本次完成）、§0 修复进度
- 机制文档：`Harness/wiki/nl2sql-engine.md`「类召回窗口与规模化风险」新增「降级路径同口径（H5）」段；`Harness/wiki/data-model.md` 的 `session_message` 行补 H3 预算与日志口径
- 契约出处：`app/domain/schemas.py`（`ClassRecallInfo`）、`app/services/chat_service.py`（`_CLASS_FILTER_MAX_CLASSES_DEFAULT` / `_LAYER_RANK` / `_isOdsBusinessTable` / `_isExplicitOdsRequest`）、`frontend/src/i18n/zh-CN.ts`（`classRecallFallback`）
- 历史事故：2026-09-19 ODS_BPARTNER 召回污染（memory `qa-system-ods-partner-recall-exclusion`、`qa-system-ontology-recall-pruning`）—— 本批修的正是「该修复在降级路径上被绕过」
- 规则：`Harness/rules/开发流程规范.md`（TDD + 双审）；根 `CLAUDE.md` 核心约束 #3（Token 计量）、#6（显式错误处理）
- Memory：`qa-system-h5-h3-recall-fallback-and-context-budget.md`（新增）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / SSOT 出处）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（5 处设计决策，各 2–3 案，含被否理由）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：code-reviewer（1 MEDIUM + 2 LOW 全部当场修完）+ security-reviewer（1 LOW 当场修完 + 3 条残差记录）+ 收口复审结论；预存 lint 如实标注（零新增）
- [x] 第 8 段给出部署命令 + 真机验证结果（后端 md5 MATCH + 容器内行为探针 + 前端 bundle 新串命中/旧串清零 + 端点抽样，已执行）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档（`chat-service-assessment.md` §0 / §2.2 H3·H5 / §3 P1 / 新增修复段；`nl2sql-engine.md`；`data-model.md`）已同步
- [x] 至少 1 条 MEMORY 索引已添加（`qa-system-h5-h3-recall-fallback-and-context-budget.md`）
