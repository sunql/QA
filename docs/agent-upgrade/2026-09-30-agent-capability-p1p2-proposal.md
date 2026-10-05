# Agent 能力建设：方案与问题清单（P1+P2 第一份 spec 的输入）

- 日期：2026-09-30
- 状态：**待讨论**（未拍板，未开工）
- 范围：把「可配置 Agent」作为产品能力建设起来的第一份 spec —— 引擎地基（P1+P2）
- 前置结论来源：本文件基于当日代码核查，所有断言均带 `file:line`；标注「待核实」的未验证项集中列在 §8
- 落库路径：`docs/agent-upgrade/2026-09-30-agent-capability-p1p2-proposal.md`

---

## 1. 结论先行

### 1.1 系统现在不是 agent

`aichatService` 这个名字在仓库里不存在。实际对应的是：

| 层 | 文件 | 是什么 |
|---|---|---|
| 前端 | `frontend/src/api/chat.ts:206` | 纯 SSE/HTTP 薄封装，逐帧解析 `event:/data:` 分发回调。无规划、无工具调用 |
| 后端编排 | `backend/app/services/chat_service.py:273` | `ChatService`，固定管线：意图 → 本体 schema → 模型路由 → NL2SQL → 执行 → 图表 → 回答 |
| 真 agent loop | `backend/app/services/agent_runtime_service.py:593` | `run_agent_loop`，纯 Python async while 的 tool-calling 循环 |

`ChatService` 内部**嵌着**一个 agent loop，但它是被门控的旁路，不是主路。

### 1.2 缺的不是 agent 引擎，是「把引擎接到产品上」

三块资产已存在，但都停在半成品状态：

| 已有资产 | 位置 | 状态 |
|---|---|---|
| 真 agent loop | `agent_runtime_service.py:593` `run_agent_loop` | 存在；被 `chat_l4.py:35` 双重门控（`ENABLE_L4_AGENT_LOOP='true'` **且** 问句含"为什么/怎么算/拆解"等 substring）；只服务探索性问句 |
| DB 驱动工具注册表 + ACL 策略闸门 | `agent_runtime_service.py:93` `run()` → `:177` `_enforcePolicies` | 完整（注册→绑定→deny-by-default→执行），但是**单发**：一个 agent 绑一个 tool，无循环、无 LLM 决策 |
| 12 个工具的**元数据**（7 个已 seed + 5 个硬编码） | `scripts/seed_agent_tool_configs.py:26`；`agent_tools_nl2sql.py:303` | 两个世界**完全不互通**（见 §2.1） |

### 1.3 四条导致「接不上」的硬事实

1. **L4 只在非流式端点存在**。`_handleNl2SqlAgent` 唯一调用点 `chat_service.py:407`（`POST /api/v1/chat`）；SSE 路径 `chat_stream.py:170-213` 无 L4 分支。**同一个探索性问题，走 `/chat` 命中 L4，走 `/chat/stream` 掉进 L2** —— 端点选择静默改变路由。
2. **L4 不留查询状态**。`_saveQueryState` 共 12 个调用点，全在 L2 管线；`_buildL4ChatResponse`(`chat_l4.py:161`) 只写 `session_message`。下一轮 REFINE 锚不到它。
3. **L4 绕过策略闸门**。`run_agent_loop` 全程不碰 `_enforcePolicies`；`_dispatchSingleTool`(`agent_runtime_service.py:547`) 对 `execute_sql` 直接拿裸 `session`+`executor`，唯一守卫是 `_assert_read_only` 的 SQL 形状检查。对比：`_handleAgentRun`(`chat_domain.py:248`) 走的 `run()` 是**有**策略闸门的，且流式/非流式两条路都有。
4. **两个工具世界共享 0 个工具名、0 条代码路径，连 `ToolResult` 都是两个不同的类**。

---

## 2. 现状精查（开工前的地基）

### 2.1 参数范式冲突：两个世界接不上的**结构性**原因

`AgentTool`（`agent_tool_types.py:57`）的核心字段：

```python
ArgExtractor = Callable[[str], dict | None]   # 原始用户文本 → args dict

@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    data_object: str          # ACL 主题
    input_schema: dict        # 注释原文：JSON Schema（供未来 LLM function calling 复用）
    arg_extractor: ArgExtractor   # ← 必填
    handler: AgentHandler
    data_layers: tuple[str, ...] = ()
```

`arg_extractor` **是必填的**，签名锁死"从用户那句自然语言里挖出参数"。现有 7 个工具都符合：
- `extractWikiText`(`agent_tools_wiki.py:70`) 把整句话当检索词 → `{"query": raw}`
- `supplier_key` 系用正则挖供应商编码 → `{"key": ...}`

但 `execute_sql` 的参数是 `{"sql": "SELECT ..."}` —— **只能由 LLM 生成，不可能从用户问题里正则挖出来**。

**结论：那 5 个 NL2SQL 工具在现有类型下无法表达。这不是"没人去接线"，是类型层面锁死。**

### 2.2 `input_schema` 全空（但列是好的）

- 列存在：`AgentToolConfig.input_schema` = `JSONB NOT NULL DEFAULT '{}'`（`models.py:1612`，由迁移 `0037_agent_tool_config` 创建）
- `AgentToolAssembly.assemble` 兜底：`input_schema=config_row.input_schema or {}`（`agent_tools.py:281`）
- **`TOOL_SEEDS` 里根本没有 `input_schema` 这个键**（`scripts/seed_agent_tool_configs.py:26-100`）
- → 生产里 7 个工具的 `input_schema` **全是 `{}`**。那句"供未来 LLM function calling 复用"的注释，未来一直没到。

### 2.3 seed 的静默 no-op 陷阱

`_needsUpdate`(`scripts/seed_agent_tool_configs.py:103-113`) 只比较 6 个字段（description / data_object / data_layers / handler_kind / handler_ref / arg_extractor_kind），**不比较 `input_schema`**。

→ 只把 `input_schema` 加进 seed，**已存在的 7 行永远不会被更新**。必须同步扩 `_needsUpdate`。

### 2.4 现有 7 个工具（不是 3 个）

`TOOL_SEEDS`(`seed_agent_tool_configs.py:26`) 现有 **7 个**，由 lifespan 每次启动幂等 upsert：

| 工具 | `data_object` | `data_layers` | `arg_extractor_kind` | handler 读的 key |
|---|---|---|---|---|
| `supplier_360` | SUPPLIER | DIM,FEATURE | `supplier_key` | `args["key"]` (`agent_tools.py:148`) |
| `supplier_risk` | SUPPLIER | DIM,FEATURE | `supplier_risk_key` | `args["key"]` (`:163`) |
| `graph_traverse` | SUPPLIER | DIM,DWD | `supplier_graph_key` | `args["key"]` (`:182`) |
| `wiki_search` | WIKI_PAGE | — | `wiki_text` | `args["query"]` (`agent_tools_wiki.py:168`) |
| `wiki_read` | WIKI_PAGE | — | `wiki_text` | `args["query"]` (`:270`) |
| `rule_evaluate` | WIKI_RULE | — | `wiki_text` | `args["query"]` (`:372`) |
| `coverage_status` | WIKI_COVERAGE | — | `wiki_no_args` | 无（`args` 从不读） |

（历史注释仍写着"3 个工具"：`conftest.py:132-133`、`test_agent_tool_config_api.py:74`、`test_agent_runtime_api.py:4`、`test_agent_tool_runtime_db_driven.py:61` —— 已过期，顺手修正。）

### 2.5 关键测试现状

| 测试 | 断言 | 加工具的后果 |
|---|---|---|
| `test_agent_tool_assembly.py:115-134` | `BUILTIN_HANDLERS.keys()` / `ARG_EXTRACTORS.keys()` / `_VALID_HANDLER_REFS` **精确集合**断言 | **必红**。这是防注册表漂移的守卫，须在同一提交显式更新，**不得改成子集断言** |
| `test_agent_tool_runtime_db_driven.py:138-148` | 子集断言（`_TOOL_SEEDS ⊆ registry`），并注释说明"不断言精确总数" | 安全（曾经的 `count == 3` 已不存在） |
| `test_agent_options_api.py:41` | `len(tools) >= 3` | 安全 |
| `test_agent_tool_binding_migration.py:8-23` | 硬断言 `tool_name` 是 `varchar(64) nullable` | **改 `tool_name` 类型必红** → 决定了 §5.3 的做法 |
| `test_wiki_agent_tools_api.py:642-656` | 每个 `_AGENT_DEFAULT_BINDINGS` 值须能在 registry 解析 | 安全（加工具不影响） |

另有 4 个测试文件各自持有**本地的 3 元素 `_TOOL_SEEDS`**（不 import 脚本那份），加行不影响它们。

### 2.6 迁移与部署现状

- Alembic head = **`0104`**（`0104_report_instance.py`），**revision id 是短字符串**（`"0104"`），不是文件名 stem。
- `agent_tool_config` 由 `0037_agent_tool_config` 创建（13 列 + CHECK `handler_kind IN ('BUILTIN','NL2SQL')` + 2 索引）。
- `agent_definition.tool_name` 由 `0035_agent_tool_binding` 创建。
- ⚠️ 本仓有前科：0095/0096 同父分叉导致生产部署挂起。**新迁移编号必须在实现时从真实 head 取，不许预先硬编码。**
- ⚠️ `alembic` 打的是 **prod 不是 test**（`env.py` 只认 `DATABASE_URL`，默认 5432/prod）。

---

## 3. 目标、非目标、硬约束

### 3.1 目标

把「可配置 Agent」做成产品能力：管理台可建/改/启停 Agent、给 Agent 绑**一组**工具、按数据层授权；Agent 运行时可在一个循环里自主编排多个工具；聊天与独立页面都能调用它。

### 3.2 非目标（本次不做）

- 不重写 L2 主链路（`_streamQuery` / `_twoStageGenerate` / `_executeMultiStep` / `_planAndGenerateSql` 一行不动）
- 不改现有 3 个 agent（`supplier_360` / `supplier_risk` / `graph_traverse`）的行为与策略判定
- 不把 L4 提成主路（见 D8）
- 不做 agent 调用 agent（agent-as-tool，见 D7）
- 本轮不承诺流式工具轨迹（P4 的事，但 §5.5 预留接口）

### 3.3 硬约束：「两套并行，现有对话能力不动」

| 红线 | 落地含义 |
|---|---|
| 不碰 L2 管线 | 上述 4 个入口零改动 |
| 不动现有 3 个 Agent | 行为 + 策略判定逐字不变，有回归测兜底 |
| 唯一触及现有表的地方 | **新表 `agent_tool_binding`**；`agent_definition.tool_name` 原样保留、不迁移、不废弃 |
| 前端只加不改 | `chat.ts` 的 `handleFrame` 事件名只**新增**分支；菜单只增项 |
| 菜单页新增的既有规矩 | 必须同步 `scripts/seed_menu_config.py` + i18n 用 `menu.item` 命名空间，否则页面进不去 |

---

## 4. 范围分解与顺序

| 子项目 | 内容 | 可见变化 | 依赖 |
|---|---|---|---|
| **P1** | 工具层归一 + 多工具绑定 | 无（纯后端地基） | — |
| **P2** | 多工具 agent 循环 | 无（引擎本体） | P1 |
| **P3** | 独立 Agent 运行页（选 Agent / 填参数 / 看工具轨迹与成本） | 新页面 | P2 |
| **P4** | 聊天入口接入（非流式 + 流式轨迹） | 聊天里能调多工具 Agent | P2 |
| （P5） | L4 去留 | 见 D8 | P4 讨论时定 |

**依赖关系：P1 → P2 → (P3 ∥ P4)。**
P1 单独没有消费者，P2 单独没有工具，故二者同属**第一份 spec**（本文档即为它的输入）。

### 4.1 部署与交付风险（须进验证清单）

- 前端覆盖率闸门：记忆中为 80% 门槛、当前 74.94% 已失败。P3 新页面会继续压这个数字。**P1/P2 纯后端，可先绕过此闸门**；P3 开工前必须先确认闸门口径（U2）。
- 迁移编号与 prod/test 目标（见 §2.6）。
- `AgentToolConfig.version` 是乐观锁（`models.py:1620`，"每次 UPDATE +1"）。**待核实**：seed 的 `upsertSeed` 路径是否真的递增它；若不递增，"lifespan seed"与"admin 在管理页编辑"并发时会互相覆盖（U4）。

---

## 5. P1+P2 设计

### 5.1 核心抽象：给 `AgentTool` 加「参数来源」维度

把两套参数范式收进同一个类型，而不是建第二套注册表：

| 参数来源 | 谁提供参数 | `arg_extractor` | `input_schema` | 单发 `run()` 可调 | 循环可调 |
|---|---|---|---|---|---|
| `TEXT_MINING` | 从用户原话挖（现有 7 个） | 必填（现状不变） | 补上（新增） | ✅ | ✅ 新增 |
| `LLM` | LLM 生成 JSON（新增 5 个 SQL 工具） | `None` | 必填 | ❌ 明确拒绝 | ✅ |
| `NONE` | 无参 | 返回 `{}` | `{}` | ✅ | ✅ |

两条关键性质：

1. **`arg_extractor` 从必填变可选 = 放宽，不是收紧。** 现有 7 个工具的构造点、`ARG_EXTRACTORS` 表、`AgentToolAssembly.assemble` 全部仍合法，零改动。`assembly` 里那条 `ext_kind not in ARG_EXTRACTORS` 校验（`agent_tools.py:268`）要为 `LLM` 来源显式放行。
2. **同一个 handler 两条入口。** handler 签名 `(session, args, ctx)` 不变 —— 它本来就只吃 `args` dict，谁造的它不关心。
   - 单发路径：`arg_extractor(raw_text)` → `args`（照旧）
   - 循环路径：LLM JSON → **按 `input_schema` 校验** → 直接构造 `args`（绕过 `arg_extractor`）

### 5.2 工具目录：7 → 12

| 工具 | 来源 | `data_object` | `data_layers` | 备注 |
|---|---|---|---|---|
| `supplier_360` / `supplier_risk` / `graph_traverse` | TEXT_MINING | SUPPLIER | DIM,FEATURE / DIM,DWD | 现状不变 |
| `wiki_search` / `wiki_read` | TEXT_MINING | WIKI_PAGE | — | 现状不变 |
| `rule_evaluate` | TEXT_MINING | WIKI_RULE | — | 见 5.2.2 |
| `coverage_status` | NONE | WIKI_COVERAGE | — | 现状不变 |
| `list_tables` | NONE | ⚠️ D2 | ⚠️ D2 | 循环专用 |
| `list_joins` | NONE | ⚠️ D2 | ⚠️ D2 | 循环专用 |
| `describe_table` | LLM | ⚠️ D2 | ⚠️ D2 | 循环专用 |
| `sample_rows` | LLM | ⚠️ D2 | ⚠️ D2 | 循环专用 |
| `execute_sql` | LLM | ⚠️ D1/D2 | ⚠️ D1/D2 | 循环专用 |

**实现选择：5 个新工具走 `handler_kind='BUILTIN'`**（handler 本就写在代码里，用新 `handler_ref`），从而**不动 CHECK 约束**。`NL2SQL` 那一档继续留给 `nl2sql_default` 占位。

#### 5.2.1 `input_schema` 必须逐个人工写

handler 读的 key 不可推导（`supplier_*` 读 `key`，wiki 读 `query`，`describe_table` 读 `table_name`），所以 schema 与 handler 必须**成对人工维护**。示例：

```python
# execute_sql：LLM 唯一能提供 sql 的工具
{"type": "object",
 "properties": {"sql": {"type": "string", "description": "只读 SELECT 语句"}},
 "required": ["sql"]}
```

`data_layers=[]` 的工具（4 个 wiki）在策略上退化为**对象粒度**（`_enforcePolicies` 的 `if not tool.data_layers` 分支）—— 这与 D2 的口径要统一考虑。

#### 5.2.2 一处必须改的错误语义

`rule_evaluate` 在条目没有可执行规则时抛 `NotFoundError`(`agent_tools_wiki.py:378`)。单发路径下它转成友好文案；**在循环里抛出会中止整次运行**，连带丢掉"这条规则确实存在"这个有用信息。循环调用时要收敛成正常 tool error 结果，交 LLM 自行决定下一步。

（对照：`wiki_read` / `rule_evaluate` 的多条命中走了 `_ambiguousResult`(`:137`) 返回**正常结果** —— 这个已经是循环友好的设计，是正面样板。）

#### 5.2.3 seed 改动三件套

1. 给 12 个 seed 各写 `input_schema`
2. **扩 `_needsUpdate`** 把 `input_schema` 纳入比较（否则静默 no-op，见 §2.3）
3. 修正 4 处"3 个工具"的过期注释

#### 5.2.4 必红的测试要显式更新

`test_agent_tool_assembly.py:115-134` 的三组精确集合断言按新注册表更新。**不得改成子集断言** —— 那会把一个真守卫变成橡皮图章。

### 5.3 多工具绑定：纯增量，不动 `tool_name`

`test_agent_tool_binding_migration.py:8-23` 硬断言 `tool_name` 是 `varchar(64) nullable`，改类型会红，也违反"现有功能不动"。

**做法**：新建 `agent_tool_binding` 表（`agent_id` FK → `agent_definition.id` + `tool_name` + 唯一约束），`agent_definition.tool_name` **原样保留**当作"主绑定"。

- 读侧合成 = `[tool_name] + 绑定表行`（去重）
- `AgentBindingCache`(`agent_binding_cache.py:19`) 的 `_cache: dict[str, str | None]` 扩为 `dict[str, tuple[str, ...]]`；`getToolName` 保留（返回主绑定）以兼容现有调用点，新增 `getToolNames`
- 旧 agent、旧 API、旧测试、旧前端**全部零改动**
- 老列不迁移不废弃（后续退场另开 change）

**唯一连带修改**：`agent_tool_config_service.py:196-204` 现在用 `AgentDefinition.tool_name == name` 拦截"删掉还被绑定的工具"，扩表后**必须同时查绑定表**，否则能删掉一个仍在被使用的工具。

### 5.4 循环泛化

`run_agent_loop`(`agent_runtime_service.py:593`) 从"硬编码 `TOOL_SCHEMAS`"改为"消费调用方传入的 `list[AgentTool]`"：

```python
# 现状
from app.services.agent_tools_nl2sql import TOOL_SCHEMAS   # 硬编码 5 个
# 目标
async def run_agent_loop(self, *, ..., tools: list[AgentTool], ...)
```

四件配套：

1. **工具面由调用方给定** = 该 Agent 绑定的工具集合 ∩ 已注册且 `enabled`
2. **`_enforcePolicies` 进环**：它已经是单 tool 签名（`_enforcePolicies(entity, tool)`），天然可搬到每次工具调用前逐步校验（deny-by-default；`FORBIDDEN`/`FORBIDDEN_WRITE` 优先于任何 READ 授予）
3. **派发走 registry**：`_dispatchSingleTool` 不再对 `execute_sql` 短路直连 executor，而是统一经注册表解析工具 → 策略 → 执行
4. **分层（本次已选的口径）**：
   - **固定前奏**（不交 LLM 决定，保底质量）：本体召回 → schema 渲染
   - **自由工具**（LLM 自主编排）：绑定的工具集合
   - **固定收尾**：写 `session_query_state` + 回答 + 图表
   - 前奏/收尾的具体边界见 D4

**顺带修一处死代码**：`dispatch_tool_call` 里的 `execute_sql` 分支（`agent_tools_nl2sql.py:270`）在环内**永远不可达**（`_dispatchSingleTool` 已对 `execute_sql` 短路）。它是"两套实现"的又一处证据，归一后自然消失。

### 5.5 治理（决定这套东西能不能上生产）

| 项 | 设计 | 理由 |
|---|---|---|
| 迭代上限 | `max_iterations`（现状默认 5） | 已有 |
| 成本上限 | `cost_budget_usd`（现状默认 0.5） | 已有；应按 agent 可配（D5） |
| 工具集合大小 | 绑定的工具数量本身即闸门 | **靠工具粒度控制成本，不靠 prompt 求 LLM 省** —— 这是与现状 `_L4_SYSTEM_PROMPT` 里"探索≤1 轮"软约束的关键区别 |
| 单次工具结果体积 | 需要行数/token 截断 | **待核实 U6**：环内 `execute_sql` 走 `executor.execute_read_only`；文档称连接池层有 `fetchmany(5000)` 兜底，但 5000 行进 context 仍会爆窗口，且无 token 级截断。wiki 侧已有好样板（`AGENT_SEARCH_LIMIT=5` / `AGENT_GAP_LIMIT=10`） |
| 计量归因 | per-agent：`purpose="agent_loop:<agent_code>"` | 现状 `purpose="l4_agent_loop"`；要让"哪个 agent 花了多少钱"可查 |
| 失败语义 | 工具失败 ≠ 运行失败 | 工具异常收敛为 tool error 结果喂回 LLM；仅"固定前奏失败""预算耗尽""LLM 调用异常"才终止。与现状 `terminated_reason` 四态对齐 |
| 流式 | **本轮不做，但接口预留** | 循环现在调非流式 `complete_with_tools`（`agent_runtime_service.py:452`），无流式变体。P4 要用时需新增 SSE 事件（`tool_call`/`tool_result`）。本轮至少保证"事件发射"是单一出口，避免 P4 时到处插桩 |

### 5.6 测试策略

| 层 | 做法 |
|---|---|
| 单元 | 假 LLM 驱动循环：工具选择 → 策略拒绝 → 预算耗尽 → 异常保留已花用量；`AgentTool` 三态参数来源的装配与校验 |
| 集成 | 绑定表读写 + 读侧合成 + 删除拦截的连带修改；`/agents/options` 形状 |
| **禁止** | **绝不跑裸 pytest** —— 本仓有前科：未加 marker 的测试会 drop 真实 Milvus 集合、污染 prod。召回/向量相关只写 `FakeCollection` 单测 |
| 回归 | 现有 3 个 agent 的行为与策略判定逐字不变：加断言而非改断言 |

---

## 6. 需拍板的决策（D 系列）

> 这些是**口径问题**，不是技术选型，需要你定；我各给了倾向。

**D1 · `execute_sql` 这类工具的 ACL 口径**
- ① `data_object="BUSINESS_TABLE"` + `data_layers` 留空 → 对象粒度，等于给 agent"整个业务库一把钥匙"
- ② 声明 `data_layers=("DIM","DWD","DWS","ADS")` → 策略必须**逐层授予**，缺一层即 403
- **倾向 ②**：与 deny-by-default 的设计意图一致，也与既有 `chat_layer_priority`（ADS>DWS>DWD>DIM）知识同源。但这是安全口径，得你点头。

**D2 · 5 个新工具的 `data_object` 命名**
候选：复用业务域（`PROCUREMENT`/`QUALITY`/`LOGISTICS`，见 `AGENT_DATA_DOMAINS`）？还是新建 `BUSINESS_TABLE` / `ONTOLOGY`？影响管理台授权页怎么展示、以及"一个 agent 能查哪些域"的表达力。

**D3 · 多工具绑定的读侧合成规则**
`tool_name` 与绑定表行冲突时的优先级/去重/校验（例：`tool_name=supplier_360`，绑定表里又加了 `supplier_risk`）。还要定：管理台写绑定表时要不要校验 `tool_name` 也填了（"主绑定"是否必填）。

**D4 · 固定前奏的边界**
哪些步骤算"必跑、不交 LLM"？我当前只写了「本体召回 → schema 渲染」。要不要把 **KPI 语义匹配**、**wiki 规则注入**、**值域采样**、**漂移校验** 也算前奏？（后三者在 `_buildPipelineContext` 里已经是按需注入的。）另一个子问题：前奏是**全局固定一条**，还是**每个 agent 可配**？

**D5 · 预算默认值与可配性**
`max_iterations` / `cost_budget_usd` 现状硬编码默认（5 / 0.5）。要不要提到 `agent_definition` 上按 agent 配、并给 admin 一个上限防乱填？

**D6 · 工具全失败 / 部分失败时的产出语义**
工具全失败 → 如实报数据缺口（现状 L4 有"放弃条件"文案）还是重试？部分失败 → 是否允许 LLM 基于不完整数据作答（我认为要，但必须显式标注缺口）？

**D7 · 要不要支持 agent-as-tool**
一个 agent 作为另一个 agent 的工具。**建议 YAGNI：本轮不做**，只在文档里留位。理由是它会立刻引入递归、预算继承、策略穿透三个新问题。

**D8 · L4 的处置**
你已定「现有功能保留」，所以我默认**L4 原样不动**（`ENABLE_L4_AGENT_LOOP` 门控保持现状）。但要明确记录：L4 是一条**绕过 `_enforcePolicies` 的 SQL 执行通道**（§1.3 第 3 条）。选项：
- (a) 原样保留，本轮不碰（最小改动，但留洞）
- (b) P4 阶段把 L4 的实现换成新引擎（行为等价、但走策略闸门）
- (c) 关掉 `ENABLE_L4_AGENT_LOOP`
- **倾向 (a) + 单独开一个修复 change**，不夹带进 P1/P2。

**D9 · 独立 Agent 运行页的入口形态**
P3 的事，但影响 P2 的收尾设计：运行页要不要"不选 Agent 也能自由对话式探索"（即退化成通用 N 工具 agent）？还是必须选一个已配置的 Agent？

---

## 7. 风险登记

| 风险 | 影响 | 缓解 |
|---|---|---|
| 成本失控（本仓实测 nl2sql 占 token 94.9%，每步投两遍 schema） | agent 化后 LLM 可自行多读几次 schema，成本上升 | 靠工具粒度 + 三重硬闸门；不靠 prompt 软约束（§5.5） |
| 循环里工具结果撑爆上下文 | 单次运行失败 / 成本飙升 | 行数 + token 双层截断（U6）；wiki 侧 `AGENT_SEARCH_LIMIT` 是好样板 |
| 策略闸门接错层 | 越权读数据（本仓有前科：owner ACL 闸门不生效、ontology ACL 越权洞） | `_enforcePolicies` 在环内逐步调用；补"非 admin 用户 + 缺层授权 → 403"的集成测试 |
| 迁移编号分叉 | 生产部署挂起（0095/0096 前科） | 编号从真实 head 取，不预编；迁移验证显式指定测试库 |
| 覆盖率闸门 | P3 无法合入 | P1/P2 无前端；P3 前先确认闸门口径（U2） |
| `version` 乐观锁与 seed 并发 | admin 编辑被 lifespan seed 覆盖 | 先核实 `upsertSeed` 是否递增 version（U4） |

---

## 8. 需核实的未知（U 系列）

| # | 未知 | 为什么重要 | 怎么查 |
|---|---|---|---|
| U1 | 生产 `ENABLE_L4_AGENT_LOOP` 当前值是 true 还是 false | 决定 L4 是不是**活**路径；若为 false，D8 的风险面几乎为零 | 查 `system_config` 表该行 |
| U2 | 前端覆盖率闸门当前口径与实际数字 | 决定 P3 能否合入 | 跑一次前端覆盖率，或查闸门配置 |
| U3 | 现网 `agent_definition` 有多少行、各绑了什么工具、`status` 分布 | 决定 D3 的兼容形状（读侧合成是否会撞上多绑定） | 查 `agent_definition` 表 |
| U4 | `AgentToolConfigService.upsertSeed` 是否递增 `version` | 乐观锁是否真的生效；不生效则 seed 与 admin 编辑并发互相覆盖 | 读 `agent_tool_config_service.py:266-285` |
| U5 | 4 个 wiki 工具 `data_layers=[]` 退化为对象粒度，与 D1 的逐层口径是否自相矛盾 | 授权模型的一致性 | 与 D1/D2 一起定 |
| U6 | 环内 `execute_sql` 的行数/token 上限实际是多少（文档称连接池 `fetchmany(5000)`，需核实） | 上下文爆炸风险 | 读 `business_db_pool` + `datasource_service.execute_readonly`；实测一次大表查询 |
| U7 | 谁在消费 `/agents/options`，多工具后返回形状要不要变 | 影响 P3/P4 的前端契约 | 查前端调用点 |

---

## 9. 待你确认后才能开工的三件事

1. **§5.1 的「参数来源三态」抽象**（`TEXT_MINING` / `LLM` / `NONE`）是否立得住 —— P1/P2 的地基
2. **D1 / D2 的 ACL 与命名口径** —— 决定 5 个新工具怎么被授权
3. **D4 固定前奏的边界** —— 决定 P2 循环的形状

其余 D/U 项可在讨论时一并过。

---

## 附：本次核查的可信度声明

本文件的每一条断言都来自 2026-09-30 当日的**定向代码读取**（Read + GitKraken 只读查询），**不是全仓 grep** —— 该会话的 Bash 被 worktree 隔离守卫硬阻塞（会话被固定的 worktree 目录已被删除，守卫条件永远无法满足）。因此：

- "某符号不存在"这类否定断言，是基于若干具体路径探测得出的，**不是穷举**
- 标注「待核实」的 U 系列即本次未能验证的事项，开工前需补
- 已发现并修正过两处：① 曾误以为 DB 工具世界是 3 个（实为 7 个，`TOOL_SEEDS` 含 4 个 wiki 工具）；② 曾担心 `count == 3` 断言会红（HEAD 上已换成子集断言）
