# v3.1 架构升级 · Epic 级技术债登记

> 建档：2026-09-30（MB3 批次收尾时）
> 来源：MB1 / MB2 / MB3 各任务评审与批次级 final review 的挂账项
> **规则**：本文件是 epic 级债目的 SSOT。单项修复完成后在此标记并注明 commit，不删条目（保留决策轨迹）。

## 优先级说明

| 级别 | 含义 |
|---|---|
| P1 | 正在遮蔽真实问题 / 阻塞后续验证 —— 应尽快排 |
| P2 | 真实缺陷或契约不一致，但不阻塞当前工作 |
| P3 | 卫生问题（死代码、注释漂移、守卫弱化） |

---

## TD-1（P1）· PLAN_EMPTY 陈旧测试夹具 —— 统一刷新

**状态**：待办（用户 2026-09-30 拍板「立 epic 级独立条目统一刷新」）

### 现象

`test_chat_service_state.py` 实测 **9 failed / 20 passed**（主控 2026-09-30 实跑复现），失败全部为：

```
app.domain.exceptions.Nl2SqlError: 无法生成有效的查询计划，请换一种问法或补充本体元数据
WARNING app.services.nl2sql_plan: NL2SQL 计划解析失败 attempt=1 reason=PLAN_EMPTY
WARNING app.services.nl2sql_plan: NL2SQL 计划解析失败 attempt=2 reason=PLAN_EMPTY
WARNING app.services.nl2sql_plan: NL2SQL 计划解析失败 attempt=3 reason=PLAN_EMPTY
```

### 根因（已定位到行）

`backend/app/tests/integration/test_chat_service_state.py:66-68`：

```python
elif "解析为查询计划" in system:
    # ReAct 第一阶段：返回一个能通过校验的空计划（classes 为空时无引用可校验）
    content = '{"target":"各供应商的收货数量汇总"}'
```

夹具注释写的「**能通过校验的空计划**」在 `c3a9226`（半空计划闸门**方案B**，2026-09-27）之前成立；方案B 之后 **`target` 单独存在不再视为「有内容」**（见 `backend/app/services/nl2sql_plan.py:81` `_isEmptyPlan` docstring 第 89-103 行明确记载），于是该计划被判 `PLAN_EMPTY` 走重试，三次用尽后抛 `Nl2SqlError`。

**时序证据**：`git merge-base --is-ancestor c3a9226 842f178` 为真 ⇒ 闸门先于 B5 R1 落地，这 9 例**在 B5 R1 之前就已红**（MB2 的 C-1 缺陷只是用更早抛出的 `AttributeError` 把它们遮住了）。

### 影响面

`_PipelineLlm` 这个假客户端**不是共享 helper**——`class _PipelineLlm` 在 **8 个文件里各自重复定义**，另有 3 个文件引用它（`test_chat_model_routing_fallback.py` / `test_l4_agent_loop_metering.py` / `test_chat_service_stream.py`，定义在别处或另有来源）。

**重复定义（8，逐份核对）**：

```
backend/app/tests/unit/test_chat_service.py
backend/app/tests/integration/test_chat_api.py
backend/app/tests/integration/test_chat_service_state.py      ← 已实测 9 红
backend/app/tests/integration/test_classify_and_recall.py
backend/app/tests/integration/test_ontology_drift_api.py
backend/app/tests/integration/test_receiptdetail_qty_alias.py
backend/app/tests/integration/test_term_dictionary_inject.py
backend/app/tests/integration/test_term_dictionary_supply_qty.py
```

**引用但未定义（3，需先查清来源）**：

```
backend/app/tests/unit/test_chat_service_stream.py
backend/app/tests/integration/test_chat_model_routing_fallback.py
backend/app/tests/integration/test_l4_agent_loop_metering.py
```

**修复时以实跑为准，不要按名单盲改**——同一份夹具在不同文件里的计划形态不同，只有返回 `target`-only 计划的那份才属于本债目。

**一个易踩的误判**：`test_chat_service_state.py` 里还散布着形如
`QueryPlan(target="t", selectedClasses=("PRECEIPT",))` 的构造（如 :265），**这些不是夹具返回的计划**，而是 `_saveQueryState(...)` 的**上一轮状态种子**（含 `selectedClasses`，形态合法）。该文件的真凶只有一处——`_PipelineLlm.complete` 第 68 行那句 `'{"target":"各供应商的收货数量汇总"}'`。**别把状态种子当成要修的夹具。**

### 修法

夹具返回的计划补上**可被 `validatePlan` 校验的真实引用**，例如：

```python
content = '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"],"selectedProperties":["NAME","QTY"]}'
```

同时**删掉那条已失效的注释**（「返回一个能通过校验的空计划」）——它正是误导后来者的源头。

### 验收

- `test_chat_service_state.py` 9 例转绿，且**不是靠放宽断言**（不得删测试、不得改断言口径）
- 其余 `_PipelineLlm` 文件实跑核对，同因失败一并修
- 修完在全量 integration 基线里报出前后数字

### 为什么值得优先

它已**跨三个任务被反复挂账**（`task-b6-report.md:30`、`progress.md:68`、MB3 final review 均点名）。持续存在会：① 让真实回归淹没在噪声里；② 每次批次评审都要重新论证「这 9 例不是我弄红的」，重复消耗评审预算。

---

## TD-2（P2）· `_buildInheritedStatePrompt` 零生产调用者

**状态**：待办

`backend/app/services/chat_context.py:663` 的 `_buildInheritedStatePrompt` 全仓无生产调用点。B5 R1 曾计划用它渲染「继承字段」小节，实际改由 `_buildInheritedStateFromSnapshot`（模块级，:733）承担。二者并存造成双份渲染逻辑。

**修法**：确认 `_buildInheritedStateFromSnapshot` 覆盖了全部语义后删除前者（含其单测），或反向合并——**先查清哪个是活的**，别凭名字猜。

---

## TD-3（P3）· `0097_id_mapping.py` docstring 仍写让号前的编号

**状态**：待办（MB1 遗留）

`backend/alembic/versions/0097_id_mapping.py` 的 docstring 仍称自己是 `0095`。该迁移经历过让号（0095 被他人占用后改为 0097）。**注意：只改 docstring，不得改 `revision` / `down_revision`**（不许回头改已落地的迁移标识）。

---

## TD-4（P2）· 流式 `_streamMultiStep` aggregation 路径跨轮 priorSnapshot 持久化字段缺

**状态**：待办（MB2 遗留）

流式多步的 aggregation 路径未持久化跨轮 `priorSnapshot` 所需字段，导致该路径下多轮继承语义与单步不一致。

---

## TD-5（P1）· 路由无鉴权 —— **范围已修正：1 条 → 46 条**

**状态**：**已升级为独立安全批次**，见 `Harness/changes/2026-09-30-security-route-auth/`
（用户 2026-09-30 拍板「单独安全批次，先做」）

### 范围修正经过（保留决策轨迹）

原登记只说 `wiki_compile` 的 `run_task` 一条。2026-09-30 核实 TD-5 时改用**实测**（`app.openapi()`
路径表 + `AUTH_MODE=real` + 无头请求），发现 **46 条非公开路由匿名可达，其中 15 条是写/删**。

**为什么原登记漏了 45 条**：本仓 FastAPI 0.141 的 `app.routes` 含 49 个 `_IncludedRouter`
包装对象（`path=None`，`routes`/`app`/`router` 属性 `hasattr=False`），朴素的依赖链扫描
**会整片漏掉**。本批次期间两轮静态分析都得出过错误结论（一次误报 67 条，一次只报出 1 条）。

**生产实际状态**：`docker exec qa-backend printenv AUTH_MODE` → `real`，即 46 条是**真实的
匿名可达**，不是理论问题。8 个 router 声明为 `APIRouter()` 或 `APIRouter(dependencies=[])`，
无全局鉴权中间件，受影响服务层也无 ACL ⇒ 匿名调用者直接抵达服务层。

详见批次 `summary.md` 第三节的完整清单与逐条危害。

### 附带发现（同批次闭合）

- **容器代码漂移**：容器与工作树的 `app/api/v1/*.py` 有 5 个文件不一致。
  容器版 `wiki_compile.py` 的 `PATCH /claims/{claimId}` **在生产仍匿名**（工作树已修）；
  容器版 `evidences.py` **缺 R2 按会话归属守卫** ⇒ 跨用户 evidence 枚举。
  ⇒ 改源码是必要条件，不是充分条件，**必须部署**。

### 已核实「不是缺陷」（避免后人重复起疑）

审计曾把 `DataLineage.owner`（`data_lineage_service.py:174` 的 `owner=dto.owner`）定性为
mass-assignment。经核实该字段**不参与任何鉴权/ACL**（全局 grep 无命中），只是 ≤100 字符的
描述性元数据；加了鉴权后，登录用户设置它正是该字段的设计用途。**不据此造任务。**

---

## TD-6（P2）· `service_saveState` 用无参 `ChatService()` 构造

**状态**：待办（预存）

绕过依赖注入直接无参构造 `ChatService()`，使该路径拿不到注入的 LLM factory / KPI matcher 等，行为与主链路漂移。

---

## TD-7（P2）· `rbac_api` 11 例陈旧测试

**状态**：待办（预存）

`_mkUser` 不传 `password`，而 password 已改为必填 ⇒ 返回 422 而非 201。**不是任何 v3.1 改动破的**（见 `qa-system-rbac-api-stale-tests`）。

---

## TD-8（P3）· 测试守卫弱化与死常量

**状态**：待办

- `backend/app/tests/integration/test_authority_department_api.py:282` 的 `_HEAD` 常量在 H-1 修复后已无引用
- 同文件 head 断言现用 `!= _PREV` 口径（照抄 `test_alembic_0076.py` 的仓内惯例），**只证明「没停在上一版」，不证明落在真实 head**；`ScriptDirectory.get_current_head()` 仓内无先例。若日后要收紧，应作为一次统一改造（所有 `test_alembic_*` 一起），不要单点自创第三种口径
- `backend/app/tests/integration/test_seed_menu_config.py` 曾长期断言陈旧计数（45/38 vs 实际 48/41）而无人察觉——已随 MB3 修复，但**该守卫缺乏「计数来源」的单一事实源**，建议改为从 seed 脚本派生期望值

---

## TD-9（P3）· 假设面板 `limit=10` 截断

**状态**：待办

`GET /chat/sessions/{sessionId}/hypotheses?limit=10` 只按 session 取最新 10 条，无 turn 维度。前端已用 `turnQuestion` 收敛到本轮（M-3 修复），故**当前行为安全**（宁可不出、不挂错轮）；但单轮假设 >10 条时面板会静默缺失。`HYPOTHESIS_MAX_COUNT = 3` 下几乎不可能触发，保留观察。

---

## TD-10（P3）· L1 命中落两条 evidence

**状态**：待办（需先确认设计意图）

L1 KPI 命中一次查询执行会落**两条** evidence：`business_db_pool.py:404` 的 B2 钩子落 `SQL_QUERY` + `chat_service.py:1334` 的 MR 落 `METRIC_RESULT`。
若为「原始 SQL + 语义指标」成对设计，应写进蓝图 §4.12 的注释；否则是重复行。**先确认意图再改**。

---

## TD-11（P3）· `0103_analysis_hypothesis` 无专属往返测试

**状态**：待办

`0098` / `0099` / `0100` / `0104` 都有 `test_alembic_*` 往返测试，`0103` 仅被 `test_chat_hypothesis` 间接触及，**无 downgrade 验证**。

---

## TD-12（P3）· `llm_json_fence` SSOT 清单漏一份逐字相同实现

**状态**：待办

`backend/app/services/llm_json_fence.py` 的 docstring 列 SSOT 清单时漏了
`backend/app/services/learning/llm_invoker.py:51-62` —— 那是第 4 份**逐字相同**的 fence 剥离实现。恰是该 SSOT 想解决的 DRY 缺口本身。

---

## TD-13（P3）· `_persistBestEffort` 对 METRIC_RESULT 恒打空 sql

**状态**：待办

`backend/app/services/evidence_record_service.py` 的 `_persistBestEffort` warning 固定打印 `payload.get("sql","")`，而 `METRIC_RESULT` 型 payload 无 `sql` 键 ⇒ 该类型落库失败时日志丢掉唯一标识。应改为按 `source_type` 取标识（`metric_code`）。

---

## TD-14（P3）· 特征回流路径走不到假设站点

**状态**：待办

`backend/app/services/chat_stream.py:524-563` 的 `_tryFeatureResponse` 命中即 `return`，走不到假设生成站点 ⇒ 命中触发词且该轮有特征值时不产出假设，与「本轮有数据」的语义不一致。

---

## TD-15（P3）· 预存环境类测试失败

**状态**：待办（与任何 v3.1 改动无关）

批次评审反复对账确认的预存红（**不是本 epic 引入**）：

- `chat_stream` 环境类 10 例
- `mcp` 12 例、`ontology` 7 例、`cascade` 6 例（空本体库）
- `icon alignment` 1 例（`link` 图标来自 pre-batch 的 `item.wikiLinks`）
- `datasource_pool` 2 例、`query_plan` 1 例、`dependencies` 1 例
- `chat_service` 39 例（含 TD-1 与上述环境类）

**建议**：先修 TD-1（能一次消掉最大的一块），再重新对账基线，避免每次评审重复论证同一批红。
