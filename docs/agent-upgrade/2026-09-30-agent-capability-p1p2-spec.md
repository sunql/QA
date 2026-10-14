# Agent 能力建设 · P1+P2 设计规范（引擎地基）

- 日期：2026-09-30
- 状态：**决策已锁，待评审**（评审通过后转实施计划；§6.2 的 **D11** 为本次新发现的待拍板项）
- 范围：第一份 spec = P1（工具层归一 + 多工具绑定）+ P2（多工具 agent 循环）
- 伴读文档：`2026-09-30-agent-capability-p1p2-proposal.md`（现状精查与完整证据，本文不重复其全部 `file:line` 举证）
- 落库路径：`docs/agent-upgrade/2026-09-30-agent-capability-p1p2-spec.md`

---

## 0. 一句话

给 `AgentTool` 加一个「参数来源」维度，把现有「从用户原话挖参数」的工具注册表扩成同时能承载「LLM 提供 JSON 参数」的工具；再把 `run_agent_loop` 从硬编码工具表改成消费该 Agent 绑定的工具集合，并在环内逐步过 ACL 策略。**全程只增不改**：现有 L2 管线、现有 3 个 Agent、`agent_definition.tool_name` 全部零改动。

---

## 0.1 待讨论清单（**本轮不需要逐个回答**）

以下是全部尚未拍板的项。它们**都不阻塞评审** —— 你把方案整体看完后一次性给结论即可。已锁的决策见 §2，已取证完毕的见 §6.1，都不用重开。

| # | 待讨论 | 详见 | 影响面 | 我的倾向（供参考，可推翻） |
|---|---|---|---|---|
| **D11** | `upsertSeed` 不递增 `version` + `input_schema` 是"重置"语义 —— 这个既存缺陷本 spec 修不修 | §6.1 U4 / §6.2 | **直接撞 P1**：不修则 admin 在管理页的编辑会被下次容器重启静默抹掉，且双方都发现不了 | 顺带修（改成"省略 = 保留现有" + 更新分支递增 version） |
| U1 | 生产 `ENABLE_L4_AGENT_LOOP` 当时的真值 | §6.2 | 只影响 D8 风险面的量化，不影响结论 | 需要你查 `system_config` 一行 |
| U3 | 现网 `agent_definition` 行数 / 绑定分布 / `status` 分布 | §6.2 | 决定上线时绑定表是否需要初始数据 | 需要你查库 |
| U5 | 4 个 wiki 工具 `data_layers=[]` 退化为对象粒度，与 D1 的逐层口径算不算矛盾 | §6.2 | 授权模型一致性 | 算。建议**另开一笔**，本 spec 不动它 |
| U6b | `sample_rows` 打在元数据库上（附带 bug）的处置方式 | §6.1 U6 | P2 任务 9 | 随 P2 一起修（已排进 P2 任务 9） |
| U7b | 管理台策略子表单会**自动多出** `BUSINESS_TABLE` 选项，是否期望 | §6.1 U7 | 管理台交互 | 期望。若不希望暴露需额外过滤，是产品口径 |

**另有 3 项已核实完毕、且已直接写进本 spec，不需要你回答**：U4（已升级为 D11）、U6（三个缺口 → §3.6 三层截断 + P2 任务 9/10）、U7（后端响应形状不用改；前端改动 → P1 任务 10）。

<details>
<summary>已被本 spec 消解、无需再议的旧问题（点开仅供回溯）</summary>

- **原 D3 的"待讨论"标记**：你上一轮已确认「`tool_name` 为可选主绑定」，见 §2 / §3.4
- **原 D4 的歧义**：已按「召回 + schema + KPI；wiki 交给工具」定案，见 §2 / §3.5
- **D6 / D7 的编号重复**：按「D6 = 允许作答但须标注缺口；D7 = agent-as-tool 本轮不做」记账，见 §2
- **原 U2（前端覆盖率闸门）**：只影响 P3，本 spec 不含前端页面，已移出

</details>

## 0.2 文档分工

- **本文（spec）= 决策 + 设计 + 实施分解 + 验收**，自足可评，单独看即可
- `2026-09-30-agent-capability-p1p2-proposal.md` = **取证附录**：现状精查的完整叙事与逐条 `file:line` 举证（本文 §1 是它的压缩版）。只在你需要核对某条断言时才翻

## 1. 现状锚点（详见伴读文档 §2）

| 事实 | 位置 |
|---|---|
| `AgentTool.arg_extractor` **必填**，签名 `Callable[[str], dict|None]` 锁死"从原话挖参数" | `backend/app/services/agent_tool_types.py:53,57` |
| → `execute_sql`（参数只能来自 LLM）在现有类型下**无法表达** | 结构性障碍，非"没人接线" |
| `input_schema` 列已存在（`JSONB NOT NULL DEFAULT '{}'`）但 seed 从不写 → 生产 7 个工具全是 `{}` | `models.py:1612`；`seed_agent_tool_configs.py:26-100` |
| `_needsUpdate` **不比较 `input_schema`** → 加了也是静默 no-op | `seed_agent_tool_configs.py:103-113` |
| DB 工具世界是 **7 个**（含 4 个 wiki 工具），不是 3 个 | `seed_agent_tool_configs.py:26` |
| `agent_tool_config.handler_kind` CHECK 只允许 `('BUILTIN','NL2SQL')` | `models.py:1625` |
| Alembic head = `0104`；revision id 是短字符串；**迁移打的是 prod** | `0104_report_instance.py`；`alembic/env.py` |
| 现有 3 Agent 的策略闸门在 `run()` 里（`_enforcePolicies`，单 tool 签名） | `agent_runtime_service.py:93,177` |
| 循环 `run_agent_loop` 完全不碰策略闸门 | `agent_runtime_service.py:593` |
| 精确集合断言测试（必红且须显式更新） | `test_agent_tool_assembly.py:115-134` |
| `tool_name` 列形状被硬断言（→ 不可改类型） | `test_agent_tool_binding_migration.py:8-23` |
| `upsertSeed` 更新分支**从不递增** `version`，且 `input_schema` / `data_layers` 是"**重置**"语义（seed 省略即写 `{}` / `[]`） | `agent_tool_config_service.py:277-288` |
| `queryRowLimit` 默认 **0 = 无上限**（两个 `.env` 都没设）→ 适配器走 `mappings.all()`；客户端超时 30s 是唯一约束 | `config.py:63-64`；`business_db_pool.py:506-540` |
| 环内工具结果**无任何 token/字节截断** | `agent_runtime_service.py:564-568` |
| `handle_execute_sql` 在环内不可达；`sample_rows` 拿的是**元数据库** session | `agent_runtime_service.py:559-578` |
| `/agents/options` 是扁平工具目录（无 agent→tool 映射），前端唯一消费者 `AgentRegistryPage.tsx:96`，其 `toolName` 是**单选** | `agents.py:84-108`；`AgentRegistryPage.tsx:518-528` |

---

## 2. 决策台账（已锁）

| # | 决策 | 落地含义 |
|---|---|---|
| D1 | **逐层授予** | SQL 族工具声明 `data_layers=("DIM","DWD","DWS","ADS")`，策略缺任一层即 403 |
| D2 | 业务域作为授权维度 | 域**不**放在工具 `data_object` 上（见 D10）；沿用 `PROCUREMENT/QUALITY/LOGISTICS` 词表（`AGENT_DATA_DOMAINS`） |
| D3 | `tool_name` 为**可选主绑定** | 读侧 = `[tool_name] ∪ 绑定表行`（去重）；`tool_name` 可空；绑定表重复含它不报错 |
| D4 | 前奏 = **本体召回 → schema 渲染 + KPI 匹配** | **wiki 不进前奏**（已有 4 个 wiki 工具，交给 LLM 自主调用）；前奏清单**每 agent 可配** |
| D5 | 预算**可配** | `max_iterations` / `cost_budget_usd` 放 `agent_definition`，admin 侧有硬上限 |
| D6 | 失败语义 | 工具全/部分失败**允许作答**，但必须**显式标注数据缺口** |
| D7 | agent-as-tool | **本轮不做**，只在文档留位 |
| D8 | L4 | **原样保留**（门控与实现都不动），另开一个修复 change 记那条绕过策略的 SQL 通道 |
| D9 | 独立运行页 | 支持「**不选 Agent 也能自由探索**」→ 需要一个通用探索 Agent 的默认形态 |
| D10 | **域级约束落在 `agent_definition.data_domains` + 逐表校验** | SQL 族工具用专用 `data_object`（`BUSINESS_TABLE`），执行前按 SQL 里的表名反推所属域，越域即拒 |

**为什么 D2 最终没有采用"工具的 `data_object` 填业务域"**：`_enforcePolicies` 的匹配是 `p.data_object == tool.data_object` 精确字符串（`agent_runtime_service.py:191`）。若 `execute_sql.data_object = "PROCUREMENT"`，则被授予 PROCUREMENT 的 agent 可以通过它读到 QUALITY / LOGISTICS 的表 —— 因为层闸门（DIM/DWD/DWS/ADS）是**跨域**的，域级隔离不被任何检查覆盖。且一个工具的 `data_object` 只能填一个域字符串，与 D9 的跨域探索直接冲突。故域级约束上移到 agent 属性 + 逐表校验。

---

## 3. 设计

### 3.1 核心抽象：`ArgsFrom`（参数来源三态）

```python
# app/domain/enums.py（与本项目既有枚举同处）
class ArgsFrom(str, Enum):
    TEXT_MINING = "TEXT_MINING"   # 参数从用户原话挖（arg_extractor）
    LLM = "LLM"                   # 参数由 LLM 生成 JSON（按 input_schema 校验）
    NONE = "NONE"                 # 无参

# app/services/agent_tool_types.py
@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    data_object: str
    input_schema: dict
    args_from: ArgsFrom = ArgsFrom.TEXT_MINING    # ← 新增，默认值保证向后兼容
    arg_extractor: ArgExtractor | None = None     # ← 由必填改为可选
    handler: AgentHandler = ...                   # 位置参数顺序调整见下
    data_layers: tuple[str, ...] = ()
```

**组装期校验（`AgentToolRegistry._validate` 扩展）**

| `args_from` | `arg_extractor` | `input_schema` |
|---|---|---|
| `TEXT_MINING` | 必填 | 非空（新增要求；现有 7 个靠 seed 补齐） |
| `LLM` | 必须 `None` | 非空且 `required` 与 handler 读的 key 一致 |
| `NONE` | 可选（可为返回 `{}` 的函数或 `None`） | 可为 `{}` |

**两条调用入口，同一个 handler**

- 单发（`AgentRuntimeService.run`）：`arg_extractor(raw_text)` → `args` → `handler`（**现状不变**）
- 循环（`run_agent_loop`）：LLM JSON → 按 `input_schema` 校验 → `args` → `handler`（**绕过 `arg_extractor`**）
- `args_from=LLM` 的工具在单发入口必须**明确拒绝**（409/422 + 明确文案），不是静默失败

> handler 签名 `(session, args, ctx)` 不变 —— 它只吃 `args` dict，谁造的它不关心。这是"只增不改"的关键。

### 3.2 工具目录：7 → 12

| 工具 | `args_from` | `data_object` | `data_layers` | handler 读的 key |
|---|---|---|---|---|
| `supplier_360` | TEXT_MINING | `SUPPLIER` | DIM,FEATURE | `key` |
| `supplier_risk` | TEXT_MINING | `SUPPLIER` | DIM,FEATURE | `key` |
| `graph_traverse` | TEXT_MINING | `SUPPLIER` | DIM,DWD | `key` |
| `wiki_search` | TEXT_MINING | `WIKI_PAGE` | — | `query` |
| `wiki_read` | TEXT_MINING | `WIKI_PAGE` | — | `query` |
| `rule_evaluate` | TEXT_MINING | `WIKI_RULE` | — | `query` |
| `coverage_status` | NONE | `WIKI_COVERAGE` | — | 无 |
| `list_tables` | NONE | `BUSINESS_TABLE` | DIM,DWD,DWS,ADS | 无 |
| `list_joins` | NONE | `BUSINESS_TABLE` | DIM,DWD,DWS,ADS | 无 |
| `describe_table` | LLM | `BUSINESS_TABLE` | DIM,DWD,DWS,ADS | `table_name` |
| `sample_rows` | LLM | `BUSINESS_TABLE` | DIM,DWD,DWS,ADS | `table_name`, `limit` |
| `execute_sql` | LLM | `BUSINESS_TABLE` | DIM,DWD,DWS,ADS | `sql` |

- 5 个新工具一律 `handler_kind='BUILTIN'` + 新 `handler_ref` → **不动 CHECK 约束**；`NL2SQL` 档继续留给 `nl2sql_default` 占位。
- 5 个新 handler 通过**薄适配层**复用 `agent_tools_nl2sql` 里已有的实现（`(session, args, ctx)` → 原 `(*, session, table_name=...)` 签名），**不重写逻辑**。
- `input_schema` 必须**逐个人工写**（handler 读的 key 不可推导）。示例：

```python
{"type": "object",
 "properties": {"sql": {"type": "string", "description": "只读 SELECT 语句"}},
 "required": ["sql"]}
```

- `data_layers=[]`（4 个 wiki 工具）在策略上退化为对象粒度（`_enforcePolicies` 的 `if not tool.data_layers` 分支）—— 这是既有行为，本 spec 不改（记为 U5，见 §6）。

### 3.3 域级校验（D10 的落地）

`execute_sql` 等 SQL 族工具在**执行前**多一步检查：

```
1. 解析 SQL 里出现的表名（复用 SQL Guard 已有的解析能力；若无则新增一个只读的表名提取器）
2. 表名 → ontology_class → data_domain
3. 必须 ⊆ agent.data_domains，否则拒绝（返回 tool error 结果，不是抛异常中断运行）
4. 层检查仍由 _enforcePolicies 负责（D1）
```

- 校验位置：与 SQL Guard 同层，**只挂在新循环路径**，不动 L2 管线
- 无法判定域的表（不在本体系内）→ **默认拒绝**（fail-closed）
- 该检查可独立单测（给 SQL + 假本体系 → 断言拒绝/放行）

### 3.4 多工具绑定：纯增量

**新表 `agent_tool_binding`**

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BigIntPk | |
| `agent_id` | BigIntFk → `agent_definition.id`，`ondelete=CASCADE`，NOT NULL | |
| `tool_name` | `String(64)` NOT NULL | 必须存在于 `agent_tool_config.name` |
| `created_time` | `DateTime(timezone=True)` NOT NULL | 随项目 `TimestampMixin` 惯例 |

约束：`UniqueConstraint(agent_id, tool_name)`；`Index(agent_id)`。

**读侧合成（D3）**：`getToolNames(agent_code)` = `[tool_name] ∪ 绑定表行`（去重，保持确定性顺序）。
`getToolName(agent_code)` **保留**（返回主绑定）→ 现有调用点零改动。

**`AgentBindingCache` 改动**：`warmUp` 改为 `agent_definition LEFT JOIN agent_tool_binding`，`_cache` 从 `dict[str, str|None]` 扩为 `dict[str, tuple[str, ...]]`。

**连带必改 1 处**：`agent_tool_config_service.py:196-204` 目前用 `AgentDefinition.tool_name == name` 拦截"删除还被绑定的工具"；扩表后**必须同时查绑定表**，否则能删掉一个仍在被使用的工具。

**`agent_definition.tool_name`**：原样保留，不迁移、不废弃、不改类型（`test_agent_tool_binding_migration.py:8-23` 硬断言其 `varchar(64) nullable`）。

**连带必改 2 处（管理台，U7 核实）**

- `frontend/src/pages/AgentRegistryPage.tsx:518-528`（新建）/ `:593-603`（编辑）的 `toolName` 是**单选** Select → 改多选；`:493-513` / `:568-587` 的层级校验 `tool.dataLayers ⊆ form.dataLayers` 须改为**逐工具**校验
- `/agents/options` 的**响应形状不用改**（扁平工具目录，无 agent→tool 映射）→ 后端这一块零改动

### 3.5 循环泛化

```python
# 现状
from app.services.agent_tools_nl2sql import TOOL_SCHEMAS      # 硬编码 5 个
# 目标
async def run_agent_loop(self, *, ..., tools: list[AgentTool], agent: AgentDefinition, ...)
#   tool_schemas = [_to_openai_schema(t) for t in tools]       # 从 AgentTool.input_schema 生成
```

四件配套：

1. **工具面由调用方给定** = `getToolNames(agent_code)` ∩ 注册表已注册 ∩ `enabled=true`
2. **策略进环**：每次工具调用前 `self._enforcePolicies(agent, tool)`（已是单 tool 签名，天然可搬）。deny-by-default；`FORBIDDEN`/`FORBIDDEN_WRITE` 优先于任何 READ 授予
3. **派发走注册表**：`_dispatchSingleTool` 不再对 `execute_sql` 短路直连 `executor`，改为统一 解析工具 → 策略 → 域校验 → handler
4. **分层**（D4）
   - **前奏**（不交 LLM 决定，**每 agent 可配**）：本体召回 → schema 渲染 + KPI 语义匹配（**wiki 不在内**，走工具）
   - **自由工具**：绑定的工具集合，LLM 自主编排
   - **收尾**：写 `session_query_state` + 回答（+ 图表，若该 agent 声明需要）

**顺带清掉一处死代码**：`dispatch_tool_call` 的 `execute_sql` 分支（`agent_tools_nl2sql.py:270`）在环内**永远不可达**（`_dispatchSingleTool` 已短路该名字）。归一后自然消失。

**错误语义修正**：`rule_evaluate` 在条目无规则时抛 `NotFoundError`（`agent_tools_wiki.py:378`）—— 单发路径转友好文案，**循环里抛出会中止整次运行**。循环调用时收敛为 tool error 结果，交 LLM 自行决定下一步。（正面对照：`_ambiguousResult`（`:137`）返回**正常结果**，本来就是循环友好的。）

### 3.6 治理

| 项 | 设计 |
|---|---|
| 迭代上限 | `max_iterations`，从 agent 读（D5），admin 侧有硬上限 |
| 成本上限 | `cost_budget_usd`，从 agent 读（D5）+ 硬上限 |
| 工具集合大小 | 绑定数量本身即闸门 —— **靠工具粒度控成本，不靠 prompt 求 LLM 省**（这是与现状 `_L4_SYSTEM_PROMPT`"探索≤1 轮"软约束的关键区别） |
| 工具结果体积 | **U6 已核实：三个缺口全开**。① `queryRowLimit` 默认 0 = 无上限 → `mappings.all()`（Oracle 分支还是无界 `while True` 1000 行批循环）；② 无 token/字节截断，`json.dumps({"rows": rows})` 原样进 prompt；③ `_recordEvidenceAfterSuccess` 还会把**全量结果再物化一遍**算哈希。唯一约束是 30s 客户端超时。**必须补三层**：SQL 族工具强制 `LIMIT`（或设 `QUERY_ROW_LIMIT`）、进 messages 前的 token 截断、单次结果的字节上限 |
| 计量归因 | `purpose="agent_loop:<agent_code>"`（现状 `l4_agent_loop`），让"哪个 agent 花了多少钱"可查 |
| 失败语义 | 工具异常 → tool error 结果喂回 LLM；仅「前奏失败 / 预算耗尽 / LLM 调用异常」终止。对齐现有 `terminated_reason` 四态 |
| 流式 | **本轮不做**，但"事件发射"收敛到单一出口，避免 P4 到处插桩 |

### 3.7 通用探索 Agent（D9 的后果）

「不选 Agent 也能自由探索」需要一个**内置默认形态**：

- 默认工具集 = SQL 族 5 个 + wiki 4 个（跨域，故 `data_domains` 取三域全集）
- 默认预算 = §3.6 的默认值（不配时的兜底，与 D5 的 admin 上限同一处定义）
- 默认策略：**不给它任何写/越权能力**，只有 D1 的逐层 READ

---

## 4. 实施分解

### P1（工具层归一 + 多工具绑定）

1. `app/domain/enums.py` 加 `ArgsFrom`
2. `agent_tool_types.py`：`AgentTool` 加 `args_from`，`arg_extractor` 改可选；扩展 `AgentToolRegistry._validate` 三态校验
3. `agent_tools.py`：新增 5 个 handler 的薄适配层 + 注册；`_VALID_HANDLER_REFS` 加 5 个 ref；为 `ArgsFrom.LLM` 放行 `arg_extractor_kind` 校验（`agent_tools.py:268`）
4. `seed_agent_tool_configs.py`：12 行各写 `input_schema`；**扩 `_needsUpdate`** 纳入 `input_schema`；修正 4 处"3 个工具"过期注释
5. 迁移：新表 `agent_tool_binding`（编号**从真实 head 取**，不预编；验证时显式指定测试库）
6. `agent_binding_cache.py`：`warmUp` join 绑定表；新增 `getToolNames`；`getToolName` 保留
7. `agent_tool_config_service.py`：删除拦截同时查绑定表
8. `test_agent_tool_assembly.py:115-134` 三组精确集合断言按新注册表**显式更新**（不得改成子集断言）
9. 回归：现有 3 Agent 行为 + 策略判定逐字不变
10. 管理台：`AgentRegistryPage` 的 `toolName` 单选 → 多选；层级校验改逐工具（§3.4）
11. seed 语义加固：`upsertSeed` 的 `input_schema` / `data_layers` 是"**重置**"语义（seed 省略即写 `{}` / `[]`）→ 要么给 12 行全部写死，要么把语义改成"省略 = 保留现有"（**见 D11**）

**P1 验收**：现网 3 个 Agent 行为零变化；新 5 个工具"已注册但无 Agent 引用 → 无人能调"；seed 幂等（连跑两次 `changed=0`）。

### P2（多工具循环）

1. `run_agent_loop` 签名改为消费 `tools: list[AgentTool]` + `agent`；schema 由 `input_schema` 生成
2. 派发统一：解析工具 → `_enforcePolicies` → 域校验 → handler；`args_from=LLM` 的参数按 `input_schema` 校验
3. 域级逐表校验（§3.3，只挂新循环路径）
4. 分层前奏注册表（召回 / schema / KPI）+ 每 agent 可配 + 收尾（写 `session_query_state` + 回答）
5. 治理：行数/token 截断；`purpose="agent_loop:<code>"`；预算从 agent 读 + 硬上限
6. 错误语义修正（`rule_evaluate`）
7. 清理死代码（`dispatch_tool_call` 的 `execute_sql` 分支）
8. 通用探索 Agent 默认形态（§3.7）
9. 修 `sample_rows` 的会话归属：它现在拿的是**元数据库** session（`agent_runtime_service.py:577`），应当走业务库 adapter，与 `execute_sql` 同路
10. SQL 族工具强制行数上限 + 进 prompt 前的 token 截断（§3.6）

**P2 验收**：假 LLM 驱动的循环能跑通"多工具选择 → 越层被 403 → 预算耗尽 → 异常保留已花用量"全路径；`session_query_state` 在运行后被正确写入（补上 L4 缺失的那一环）。

---

## 5. 测试与验证

| 层 | 内容 |
|---|---|
| 单元 | `AgentTool` 三态装配与校验；LLM 参数按 `input_schema` 校验；域级逐表校验（假本体系）；`AgentBindingCache` 读侧合成 |
| 集成 | 绑定表 CRUD + 删除拦截连带；`/agents/options` 形状（见 U7）；**非 admin 用户 + 缺层授权 → 403**（本仓 ACL 前科要求）；现有 3 Agent 回归 |
| **禁止** | **绝不跑裸 pytest** —— 本仓前科：未加 marker 的测试会 drop 真实 Milvus 集合、污染 prod。召回/向量只写 `FakeCollection` 单测 |
| 部署 | 迁移编号从真实 head 取；迁移验证**显式指定测试库**（`alembic` 默认打 prod）；前端覆盖率闸门本轮不涉及（P1/P2 无前端） |

---

## 6. 遗留事项

### 6.1 已核实（本次补）

**U4 · `upsertSeed` 不递增 `version`，且用"重置"语义**（`agent_tool_config_service.py:277-288`）

- 更新分支逐字段整体赋值，**从不递增 `version`**（对比 `updateTool:172` / `toggleEnabled:231` 都递增）
- `input_schema` 取 `fields.get("input_schema", {})`、`data_layers` 取 `fields.get("data_layers", [])` → **seed 省略即重置为空**。现有 `TOOL_SEEDS` 从不给 `input_schema`，所以任何被 `_needsUpdate` 判为需要更新的行，其 `input_schema` 都会被抹成 `{}`
- `enabled` 在更新分支**不赋值** → admin 的"停用"能扛过重启（这点是对的）
- 乐观锁只在 `updateTool:134-139` 做了 **Python 层"读后比"**（无 `WHERE version = :expected` 的条件 UPDATE，无 `If-Match`）；且 `toggleEnabled` 递增 version 却**不校验**调用方版本 → 锁是**半失效**的
- seed **不做 audit**（`seed_agent_tool_configs.py:5-6` 明写）→ 上述覆盖在审计上也查不到

**U6 · 单次 `execute_sql` 的体积上限（结论：三个缺口全开）**

| 项 | 事实 |
|---|---|
| 行数 | `queryRowLimit` 默认 **0 = 无上限**（`config.py:63`，`docker/.env` 与 `backend/.env` 都没设）→ `business_db_pool.py:506-540` 走 `mappings.all()`；Oracle 分支是无界 `while True` 1000 行批循环 |
| 超时 | 客户端 **30s**（`queryTimeoutSeconds`，`business_db_pool.py:540` / `:656` 的 `asyncio.wait_for`）—— **这是唯一的约束** |
| token | **无任何截断**：`agent_runtime_service.py:564-568` 的 `json.dumps` 原样进 ToolMessage，`_to_llm_message:288-333` 原样进 prompt。循环上只有迭代/成本两个预算 |
| 额外物化 | `business_db_pool.py:392-412` 的 `_recordEvidenceAfterSuccess` 会把**全量结果再物化一遍**算行哈希 |
| 可达性 | `handle_execute_sql`（`agent_tools_nl2sql.py:168`）在环内**不可达**（`_dispatchSingleTool` 已短路该名字） |
| 附带 bug | 另 4 个工具走 `dispatch_tool_call(session=session)`，拿的是**元数据库** session → `sample_rows` 的裸 SQL 打在元数据库上，不是业务库 |
| 文档漂移 | `Harness/wiki/config-reference.md:14` 称 `QUERY_ROW_LIMIT=5000`，与代码默认 0 矛盾 |

**U7 · `/agents/options` 的消费者（结论：后端响应形状不用改）**

- 后端是**扁平工具目录**（`domains` / `layers` / `tools[name, description, dataObject, dataLayers]`，`agents.py:84-108`），**没有 agent→tool 映射** → 多工具绑定不影响它
- 前端唯一生产消费者：`AgentRegistryPage.tsx:96`（经 `useAgentOptions` 的模块级缓存 + inflight 去重）
- 但该页 `toolName` 是**单选** Select（`:518-528` / `:593-603`），层级校验是 `tool.dataLayers ⊆ form.dataLayers`（`:493-513` / `:568-587`）→ **管理台要改**（已写入 §3.4 / P1 任务 10）
- 该页还从 `tools` 派生策略子表单的 `dataObject` 选项集（`:680-685` `new Set(tools.map(t => t.dataObject))`）→ 新增 `BUSINESS_TABLE` 后这里会自动多出一个选项，需要产品上确认是否期望

### 6.2 仍需你定

| # | 事项 | 说明 |
|---|---|---|
| U1 | 生产 `ENABLE_L4_AGENT_LOOP` 真值 | 只有你能查 `system_config` 表。若为 `false`，D8 的风险面几乎为零 |
| U3 | 现网 `agent_definition` 行数 / 绑定分布 / `status` 分布 | 影响上线时绑定表是否需要初始数据 |
| U5 | 4 个 wiki 工具 `data_layers=[]` 退化为对象粒度，与 D1 逐层口径是否算矛盾 | 本 spec 暂不改；要不要另开一笔由你定 |
| **D11** | **`upsertSeed` 不递增 `version` 这个既存缺陷，本 spec 修不修？**（U4 核实出来的） | 它**直接撞在 P1 上**：P1 要给 seed 加 `input_schema` → seed 会开始更新已存在的行 → 而 version 不递增 ⇒ **admin 在管理页改过的字段会被下次容器重启静默抹掉**，且因为 version 没变，admin 后续的写入还会"赢"，双方都发现不了。选项：<br>　(a) **顺带修**（更新分支递增 `version`，并把 `input_schema`/`data_layers` 改成"省略 = 保留现有"）—— 但它**改的是现有行为**，越过"只加不改"红线<br>　(b) 不改，P1 里把 12 行 seed 全部写全，从而不触发重置语义<br>　(c) 另开一个修复 change<br>**我倾向 (a)**：它不是无关重构，是 P1 的直接风险 —— P1 一上线就会踩到它 |

---

## 7. 非目标（再次明确）

- 不重写 L2 主链路：`_streamQuery` / `_twoStageGenerate` / `_executeMultiStep` / `_planAndGenerateSql` 一行不动
- 不改现有 3 个 Agent 的行为与策略判定
- 不动 L4（D8）
- 不做 agent-as-tool（D7）
- 本轮不承诺流式工具轨迹（P4）
- 不修 4 个 wiki 工具的层粒度问题（U5，另开）
