# Agent 工具维护机制（方案 3）

> 2026-10-25 数据域/数据层/工具维护三问的第三问，讨论定稿。
> SSOT 变更记录：`Harness/changes/feat-data-domain-layer/summary.md` §7。

## 一句话结论

Agent 工具采用**「代码执行体 + DB 元数据」双结构**，不是程序自动发现：
新增一个「真新能力」的工具必须发版（写 handler 进白名单），只调整既有工具的
暴露面（描述 / ACL / 数据层 / input_schema）免发版，在 `/admin/tools` 页面完成。

## 双结构总览

| 层 | 载体 | 内容 | 改动代价 |
|---|---|---|---|
| **执行体**（代码侧） | `backend/app/services/agent_tools.py` + `agent_tools_wiki.py` | handler 函数 + 白名单 `_VALID_HANDLER_REFS` + 参数提取器 `ARG_EXTRACTORS` | 要发版 |
| **元数据**（DB 侧） | `agent_tool_config` 表 | name / description / data_object（ACL 主体）/ data_layers / input_schema / handler_kind / handler_ref / arg_extractor_kind | 免发版，`/admin/tools` 页面 CRUD（菜单：系统配置 → 工具管理，sort_order 590） |

装配链路：`AgentToolAssembly.assemble`（agent_tools.py）在读取时校验
`handler_kind → handler_ref → arg_extractor_kind` 三级都在白名单内，任一不符
直接 raise（DB 层 handler_kind CHECK 约束兜底），然后合并「DB 元数据 + 代码
handler」生成 AgentTool。

## 当前白名单清单（2026-10-25 快照）

- **BUILTIN handler**（7）：`supplier_360`、`supplier_risk`、`graph_traverse`、
  `wiki_search`、`wiki_read`、`rule_evaluate`、`coverage_status`（wiki 族在
  `agent_tools_wiki.py`，经 `**WIKI_HANDLERS` 并入 `BUILTIN_HANDLERS`）
- **NL2SQL handler**（1）：`nl2sql_default`
- **arg_extractor**（5）：`supplier_key`、`supplier_risk_key`、
  `supplier_graph_key`、`wiki_text`、`wiki_no_args`

> 快照会过时——以 `agent_tools.py` 的 `BUILTIN_HANDLERS` / `NL2SQL_HANDLERS` /
> `ARG_EXTRACTORS` 字典为准；`_VALID_HANDLER_REFS` 是从字典 keys 派生的
> frozenset，**登记进 handler 字典即自动入白名单**。方向 B 落地后
> `/admin/tools` 下拉即实时清单。

## handler 写法约定（新能力工具的代码侧规格）

类型契约全部在 `backend/app/services/agent_tool_types.py`（M8 从 agent_tools
下沉，解 wiki 域循环导入；`agent_tools` 仍 re-export，旧 import 路径照旧可用）：

```python
# 签名（三参，缺一不可）
AgentHandler  = Callable[[AsyncSession, dict, AgentToolContext], Awaitable[ToolResult]]
ArgExtractor  = Callable[[str], dict | None]

@dataclass(frozen=True)
class ToolResult:
    data: dict                      # 必须 JSON-serializable（camelCase alias 对齐 API 契约）
    answer: str                     # 面向用户的文字答案
    tokens_used: int = 0            # 调了 LLM 就必须计量（项目核心约束③）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0               # 单价只能来自 ctx.llm_config 真实配置
    llm_model_name: str | None = None
```

**写 handler 的规则：**

1. **放哪个文件**：通用领域 → `agent_tools.py`；Wiki 域 → `agent_tools_wiki.py`。
   不要新建第三个 handler 模块（又要动类型下沉结构，收益为零）。
2. **不要自造返回形状**：`data` 结构对齐同文件既有 handler（参照
   `_graphTraverseHandler` 的 `model_dump(mode="json", by_alias=True)` 模式）；
   文字答案放 `answer`，不要塞进 data 让前端自己拼。
3. **`ctx` 用法**：`ctx.llm_factory` / `ctx.llm_config` 成对使用——调 LLM 的
   handler 必须按真实模型单价计量（只给工厂不给配置 ⇒ 降级路径，见
   SupplierRiskService）；纯查询 handler 三个 token 字段留默认 0。
   `ctx.actor` 供日志溯源。
4. **args 缺 key 的语义**：arg_extractor 解析失败返回 `None` ⇒ 运行时抛
   ValidationError(422)；handler 内部只管消费已解析的 args。

**写 extractor 的规则：**

1. 签名 `(raw: str) -> dict | None`，纯函数（lambda 包装既有 intent 解析函数
   即可，参照 `ARG_EXTRACTORS` 里 `supplier_key` 三兄弟的写法）。
2. **无参工具返回 `{}` 而不是 `None`**——`None` 语义是「这次输入解析不出参数」
   ⇒ 422；无参工具永远不该 422（`wiki_no_args` 的 docstring 明确此契约）。
3. 需要新解析逻辑时先看 `intent_service.py` 既有 extract 函数能否复用。

**登记（两处 dict，缺一装配即失败）：**

```python
# agent_tools.py（或领域文件后 ** 并入）
BUILTIN_HANDLERS["my_new_tool"] = _myNewToolHandler      # ← handler 入册
ARG_EXTRACTORS["my_tool_key"]   = lambda raw: ...        # ← extractor 入册
# _VALID_HANDLER_REFS 由 keys 派生，不用手改
```

**注册期 fail-fast（`AgentToolRegistry._validate`，别等运行时 403）：**
`data_object` 必须非空全大写；`data_layers` 元素必须非空全大写（与
`AgentAccessPolicy` 归一化后的精确比较，大小写不一致会静默 fail-closed）；
层无关工具允许 `data_layers=()`（回退对象粒度）。

**测试要求**：`backend/app/tests/unit/test_agent_tool_assembly.py`——
a) 白名单一致性断言（`test_builtin_has_expected_keys` /
   `test_arg_extractors_expected_keys` / `test_valid_handler_refs_mapping`
   的 keys 集合断言，新工具必须补进期望集合）；b) assemble 正例
   （DB 行 → handler 指向一致）；c) handler 行为单测（对齐既有 handler 测试）。

## 关键语义

1. **新能力 = 发版**：先写 handler + extractor 并登记白名单（随镜像发版），
   再在 `/admin/tools` 建配置行（免发版）。两步缺一不可。
2. **调暴露面 = 免发版**：description / data_object / data_layers /
   input_schema 全在 DB，页面改完即生效（registry 重载语义见
   `agent_tool_config` service）。
3. **`name` 创建后不可改**：与 `AgentDefinition.tool_name` 是 FK-by-name 引用，
   改名等于断链；要换名字就新建行 + 迁移 Agent 绑定。

## 排障速查

| 报错 | 含义 | 处置 |
|---|---|---|
| `agent_tool_config {name}: handler_kind 不支持: {kind!r}` | DB 行的 kind 不在白名单字典键里 | 核对 kind 取值（BUILTIN / NL2SQL） |
| `agent_tool_config {name}: handler_ref {ref!r} 不在 {kind} 白名单` | ref 手填错了，或代码侧还没这个 handler | 对照上文白名单清单；新 handler 需先发版 |
| `arg_extractor_kind {ext_kind!r} 不存在` | extractor 手填错 | 对照上文 extractor 清单 |

（三者都发生在装配期——即 Agent 运行时/registry warmUp，不是录入时，这是
方向 B 要收敛的体验缺口。）

## 讨论定稿（2026-10-25）

- **方向 A（做）**：本文档即方向 A 的交付——「新增工具操作手册」+ 排障速查。
- **方向 B（做，待排期）**：「工具 discovered-from-registry」——新增只读端点
  （如 `GET /admin/tools/registry`）暴露代码侧白名单 + extractor 清单，
  `/admin/tools` 表单的 handler_ref / extractor 改下拉选择，配置错误在录入时
  拦住，不再等到装配期。
- **方向 C（不做）**：插件式动态加载 handler（免发版造新能力）。与现有
  「代码执行体可信、DB 只管暴露面」的安全边界冲突（任意代码执行面、热载
  测试/回滚复杂），无明确诉求前不立项。

## 新增一个工具的标准流程（checklist）

**只调暴露面（免发版）：**
1. `/admin/tools` 找到工具行 → 编辑 description / data_object / data_layers /
   input_schema → 保存
2. 验证：用对应 Agent 跑一次真实提问，观察工具调用日志

**新能力（发版）：**
1. 在 `agent_tools.py`（或领域文件 `agent_tools_wiki.py`）写 handler：
   签名/返回结构/extractor 规则照上文「handler 写法约定」，不要自造形状
2. 需要参数提取就写 extractor 并登记 `ARG_EXTRACTORS`；不需要则复用
   `wiki_no_args` 模式（返回 `{}`，不是 `None`）
3. handler 名登记进 `BUILTIN_HANDLERS` / `NL2SQL_HANDLERS` 对应字典
   （`_VALID_HANDLER_REFS` 自动派生）
4. 单测对齐 `backend/app/tests/unit/test_agent_tool_assembly.py`
   （白名单一致性断言 + handler 行为）
5. 发版后 `/admin/tools` 建配置行：name（小写下划线，创建后不可改）、
   handler_kind / handler_ref / arg_extractor_kind、暴露面元数据
6. 给目标 Agent 绑定 `tool_name = name`（Agent Registry 页）




方案 3：Agent 工具维护机制（当时三问的第三问）

起因

你当时的问题：数据域、数据层都能在页面维护了，那 Agent 的工具是怎么维护的？新增一个工具要不要改代码？管理入口在哪？

现状结论（已核实当前代码）

工具采用的是**「代码执行体 + DB 元数据」双结构**，不是程序自动发现：

┌──────────────────┬─────────────────────────┬────────────────────────────────────────────────┬────────────────────────┐
│        层        │          载体           │                      内容                      │        改动代价        │
├──────────────────┼─────────────────────────┼────────────────────────────────────────────────┼────────────────────────┤
│ 执行体（代码侧） │ agent_tools.py +        │ handler 函数本体 + 白名单 _VALID_HANDLER_REFS  │ 要发版                 │
│                  │ agent_tools_wiki.py     │ + 参数提取器 ARG_EXTRACTORS                    │                        │
├──────────────────┼─────────────────────────┼────────────────────────────────────────────────┼────────────────────────┤
│                  │                         │ name / description / data_object（ACL 主体）/  │ 免发版，/admin/tools   │
│ 元数据（DB 侧）  │ agent_tool_config 表    │ data_layers / input_schema / handler_kind /    │ 页面 CRUD              │
│                  │                         │ handler_ref / arg_extractor_kind               │                        │
└──────────────────┴─────────────────────────┴────────────────────────────────────────────────┴────────────────────────┘

当前代码侧白名单里的可用执行体（agent_tools.py:239）：

- BUILTIN（6 个）：supplier_risk、graph_traverse + wiki 族 4 个（wiki_search / wiki_read / rule_evaluate / coverage_status）
- NL2SQL（1 个）：nl2sql_default
- arg_extractor（5 个）：supplier_key ×3、wiki_text、wiki_no_args

装配链路：AgentToolAssembly.assemble 启动/读取时校验 DB 行的 handler_kind → handler_ref → arg_extractor_kind 三级都在白名单内，任一不符直接 raise（DB 层还有 handler_kind CHECK 约束兜底），然后合并「DB 元数据 + 代码 handler」生成 AgentTool。

关键语义：
1. 新增一个「真新能力」的工具 = 必须发版——先写 handler + extractor 并进白名单（发版），再在 /admin/tools 建配置行（免发版）
2. 只调既有工具的暴露面 = 免发版——改描述、ACL 主体（data_object）、授权数据层（data_layers，这次方案 2 之后已 DB 驱动）、input_schema，都不用动代码
3. name 创建后不可改（与 AgentDefinition.tool_name 是 FK-by-name 引用）

当时记录的两个待议方向（SSOT §7）

方向 A：补文档 / runbook
写一份「新增 Agent 工具操作手册」：什么需求只改 DB、什么需求要动代码、handler 的写法约定（签名、返回结构）、extractor 选择、测试要求、装配失败时的排障路径（agent_tool_config {name}: handler_ref ... 不在白名单 这类报错的含义）。
→ 代价小，纯文档；受益者是后续维护者（包括我自己以后的会话）。

方向 B：「工具 discovered-from-registry」增强
现在的痛点是 /admin/tools 填 handler_ref 靠手写——填了白名单里不存在的 ref，要等到装配时才报错，且管理页上看不到「代码里到底有哪些 handler 可用」。可做一个只读 API（如 GET /admin/tools/registry）暴露白名单 + extractor 清单，管理页改成下拉选择而不是手填，配置错误在录入时就拦住。
→ 中等代价：1 个只读端点 + AdminToolsPage 表单改造 + 测试；不改变「新能力要发版」的本质。

方向 C（更远，不建议现在做）：插件式动态加载
让 handler 免发版动态注册。架构级变更：安全面（任意代码执行边界）、进程内热载的测试与回滚都复杂，和现有「代码执行体可信、DB 只管暴露面」的清白边界冲突。除非你有明确诉求，否则不建议。

我的建议

A + B 做，C 不做：A 解决「怎么维护」的知识断层，B 解决「配置填错才发现」的体验断层，两者都不动现有安全边界。工作量大约 B 为主（半天级），A 随 B 一起写进 Harness wiki。

你倾向哪个方向？或者你当时问这个问题的原始诉求如果有别的侧重点（比如「业务人员能不能自己造工具」），也可以直接说，那会影响 A/B 的取舍。
