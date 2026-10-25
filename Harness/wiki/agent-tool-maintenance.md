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

- **BUILTIN handler**（6）：`supplier_risk`、`graph_traverse`、
  `wiki_search`、`wiki_read`、`rule_evaluate`、`coverage_status`（wiki 族在
  `agent_tools_wiki.py`）
- **NL2SQL handler**（1）：`nl2sql_default`
- **arg_extractor**（5）：`supplier_key`、`supplier_risk_key`、
  `supplier_graph_key`、`wiki_text`、`wiki_no_args`

> 快照会过时——以 `agent_tools.py` 的 `_VALID_HANDLER_REFS` / `ARG_EXTRACTORS`
> 为准；方向 B 落地后 `/admin/tools` 下拉即实时清单。

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
   签名对齐 `AgentHandler`，返回结构对齐同文件既有 handler（不要自造形状）
2. 需要参数提取就写 extractor 并登记 `ARG_EXTRACTORS`；不需要则复用
   `wiki_no_args` 类似的空参模式
3. handler 名登记进 `_VALID_HANDLER_REFS` 对应 kind 的 frozenset
4. 单测对齐 `test_agent_tool_assembly.py`（白名单一致性断言 + handler 行为）
5. 发版后 `/admin/tools` 建配置行：name（小写下划线，创建后不可改）、
   handler_kind / handler_ref / arg_extractor_kind、暴露面元数据
6. 给目标 Agent 绑定 `tool_name = name`（Agent Registry 页）
