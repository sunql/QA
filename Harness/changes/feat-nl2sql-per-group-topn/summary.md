# 变更：NL2SQL 逐组 Top-N —— partitionBy / perGroupLimit

- **日期**：2026-09-09
- **作者**：QA System
- **Phase**：feature（NL2SQL 计划语义补全）
- **状态**：done（已部署 + 真机冒烟通过，2026-09-09）

## 1. 需求

用户报障（多步场景第二步）：

> "分别看这三个供应商供货量最大的三种物料分别是什么，为什么SQL语句是看这三个供应商的总供货量前9个物料，而不是分别计算这三个供应商的最大的三种物料，这两个逻辑完全不同"

观察：第二步 SQL = `GROUP BY 物料 ... WHERE 供应商 IN (三个) ORDER BY SUM DESC FETCH FIRST 9` ——
即「三个供应商**总量**前 9 个物料」，而非每个供应商**各自**的 Top3。两套语义确实完全不同。

## 2. 根因

`QueryPlan`（`app/domain/query_plan.py`）只能描述**一条 SELECT = 一组 groupBy + 一个全局 sortBy + 一个全局 rowLimit**，
没有任何「先按 X 分区、区内各自排名取前 N」的表达槽（无 partition / per-group limit 字段）。

计划阶段 prompt（`_buildPlanSystemPrompt` 规则 7）只教「top N → 全局 rowLimit=N」；
模型面对「3 个供应商各自的 3 种物料」没有对的空间可写，便把 **3 供应商 × 每种取 3 折成全局 9** → `rowLimit=9`。
SQL 阶段又被「行数限制以计划为准」（`_PLAN_ROW_LIMIT_RULE`）严格约束，于是忠实产出全局 Top-9。
若某家供应商独大，全局 Top-9 可能被其物料占满，其余两家的 Top3 被整体挤掉。

不是切句 bug（该问题已由 `fix-multi-step-ordinal-adverb-split` 修）；这是 ReAct **单查询计划模型的天花板**，
在第二步真正拿到供应商后暴露。

## 3. 设计

在计划层补「逐组 Top-N」语义，禁止模型用全局 N×组数近似：

- `QueryPlan` 新增 `partitionBy: tuple[str,...]`（分区维，须 ⊆ groupBy）与 `perGroupLimit: int|None`（每组保留行数）。
- `planToText` 渲染专属行 `- 每组 Top-N：按 <分区> 分区，组内按 <sortBy> 排序，每组取前 N 行`。
- 计划阶段规则 8：问题含「分别/各/每个/每家 X … 最大 N 个 / top N」→ `groupBy` 含分区维+取数维、
  `partitionBy` 填分区维、`perGroupLimit=N`、`rowLimit=null`；明确禁止 `rowLimit = N×组数` 近似。
- SQL 阶段规则（并入 `_PLAN_ROW_LIMIT_RULE`）：计划含「每组 Top-N」时用
  `ROW_NUMBER() OVER (PARTITION BY … ORDER BY …)` + 外层 `WHERE 排名列 <= N`，禁止全局 LIMIT/分页近似。
- `validatePlan` 强校验（可操作报错引导重试自愈）：
  - `partitionBy` 属性须真实存在且 ∈ `groupBy`；
  - `partitionBy` 与 `perGroupLimit` 成对（缺一不可）；
  - 两者齐全时 `rowLimit` 必须为 null、`sortBy` 非空（组内排序用聚合别名 desc）。

校验强制使「rowLimit=9 坍缩」在重试阶段即被拦截修正，不会带着双重限制进入 SQL。

## 4. 数据模型变更

`app/domain/query_plan.py`：

| 字段 | 类型 | 语义 |
|---|---|---|
| `partitionBy` | `tuple[str,...]=()` | 分区维（每组 Top-N 的分区列，须 ⊆ groupBy） |
| `perGroupLimit` | `int\|None=None` | 每组保留行数（N） |

- `to_dict` 输出 `partitionBy: list` / `perGroupLimit`；`from_dict` 容错读入，
  `perGroupLimit` 用本地 `_coercePositiveInt`（同 `nl2sql_service._coerceRowLimit` 口径，避免导入环）归一为正 int，损坏值 → None。
- JSON 落库/前端 plan JSON 兼容：`from_dict` 忽略缺失/损坏；to_dict 新增键不破坏读取。

## 5. 接口契约变更

无 API 契约变更。`QueryPlan.to_dict()` 多出两个键，前端/EVENT_PLAN/JSONB 均容错；多步 state 序列化往返由 `from_dict` 覆盖。

## 6. 测试

### 新增（17 条）

| 文件 | 用例 |
|---|---|
| `test_query_plan.py` | partition 默认空 / to_dict-from_dict roundtrip / 缺失容错 / perGroupLimit 正 int 归一（"3"→3、0/-1/垃圾→None）/ planToText 渲染与省略 |
| `test_query_plan_validation.py` | 合法逐组 Top-N 通过；分区属性不存在 / 不在 groupBy / 缺 perGroupLimit / 缺 partitionBy / rowLimit=9 冲突 / 缺 sortBy 均报可操作错 |
| `test_query_plan_generation.py` | partition JSON 经 generateValidatedPlan 存活（partition/perGroupLimit 保留、rowLimit=None）；SQL 阶段 prompt 含「每组 Top-N」渲染 |
| `test_nl2sql_service.py` | 计划 prompt 含 partitionBy/perGroupLimit 槽位 + 规则 8（每组各取前 N、禁 N×组数）；SQL prompt 含 ROW_NUMBER 指令，且既有「行数限制以查询计划为准」「不要自行限制行数」不丢、不引入 `FETCH FIRST N ROWS ONLY` 字面量 |

### 回归

- `test_query_plan*.py` + `test_nl2sql_service.py`：**176 通过**
- `test_scope_row_limit.py` + `test_step_query_planner.py` + `test_multi_step_plan.py` + `test_chat_service.py`：**203 通过**
- `test_join_graph.py` + `test_chat_service_stream.py`：**56 通过**

## 7. 安全审查

- 新校验纯引用检查，无 LLM、无 DB；新增 prompt 文本为静态中文说明，无注入面。
- `from_dict` 对损坏 perGroupLimit 归一为 None，绝不抛错；frozen dataclass 无 mutation。
- ruff 未引入新告警（该批文件 3 条既有告警均存在于 HEAD：nl2sql I001 导入排序、query_plan UP037、query_plan B017，本次不动）。

## 8. 部署验证

2026-09-09 已部署 qa-backend 容器（`docker cp backend/app/. qa-backend:/app/app/` + `docker restart qa-backend`，uvicorn 无 --reload 需重启）。
容器内新代码核对：`query_plan.py` 含 `perGroupLimit` ×7、`step_query_planner.py` 含 `_ORDINAL_SELF_START_MARKERS` ×2。

真机冒烟（POST /api/v1/chat，datasourceId=1，modelId=1，原始问题"第一步找出公司上半年供货量最大的三个供应商，第二步分别看这三个供应商供货量最大的三种物料分别是什么，第三步最后分析供货的情况"）→ intent=multi_step，3 步：

- **Step 1**：`FETCH FIRST 3 ROWS ONLY` → B125、D1、B019（上半年，带 RECEIPT_DATE 过滤）
- **Step 2**：`ROW_NUMBER() OVER (PARTITION BY SUPPLIER_CODE ORDER BY SUM(RECEIVED_QTY) DESC NULLS LAST) AS RN` + 外层 `WHERE RN <= 3`，`SUPPLIER_CODE IN ('B125','D1','B019')` —— **不再是全局 FETCH FIRST 9**；返回 9 行 = 3 供应商 × 各自 3 物料
- **Step 3**：三家明细聚合（773 行）

Step 2 三家各自 Top3 与旧答案 message-24 中"正确期望"手工表完全一致（B125 502011070001=5,308,830 …；B019 5A2100000002=4,846,916 …），证明逐组 Top-N 语义落地。最终 answer 无"全局 Top 9""历史累计口径"等旧告警段落。

### 8.1 冒烟暴露的环境告警（与本特性无关，另立跟进）

不带 `modelId` 调用 chat 直接 500（`AttributeError: 'NoneType' object has no attribute 'complete'`）：
模型路由按 `weight` 选主模型，`llm_config` 中 Qwen3.8-27B-4bit（weight=12，endpoint=http://localhost:8888/v1，**无 api_key**）权重最高被选中 →
`createClient` 无可用 key 返回 None，factory 的 None 未被 `_callWithFallback` 捕获（仅捕获 LlmClientError/Nl2SqlError），
裸 AttributeError 上抛成 500。docqa 会话 msg 5-18 的"服务内部错误"与此吻合。带 `modelId=1`（deepseek，含密文 key）即正常。
**候选修复**：router 应跳过无法解析 key 的配置；或禁用/删掉无 key 的 Qwen/MiniMax 行；factory 返回 None 处按 503 语义处理而非裸 AttributeError。

## 9. 已知遗留（另立跟进，不属本次范围）

- **第二步时间范围缺失**：本次 12:38 报障聚合层自述第二步数据无「上半年」过滤（量级为历史累计）。
  `_finalizePlan` 的 `scopeText` 只用于 `_applyScopeRowLimit` 行数判定，未注入计划/SQL prompt 的条件段；
  子问题被切句丢失时间范围后不保证模型从主问/历史补回条件。待单独评估是否把 scopeQuestion 的时间范围并入 prompts。
  （注：2026-09-09 冒烟该问题未复现——Step 2 SQL 自带 `RECEIPT_DATE 2025-01-01~07-01` 过滤；
  但属模型 best-effort 自补，非结构性保证，仍留跟进。）

## 10. 关联

- 前置 bugfix：`Harness/changes/fix-multi-step-ordinal-adverb-split/summary.md`（首段锚点被切句器丢弃，2026-09-09）
- Wiki：`Harness/wiki/nl2sql-engine.md`（可补「逐组 Top-N 计划语义」小节）
