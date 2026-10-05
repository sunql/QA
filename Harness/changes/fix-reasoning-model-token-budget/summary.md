# 变更：fix-reasoning-model-token-budget

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **Phase**：推理模型可用性（NL2SQL 计划阶段）
- **状态**：done（待部署验收回填）
- **关联变更**：[../../wiki/model-router.md](../../wiki/model-router.md)
- **迁移版本**：`0109_llm_config_disable_thinking`（`llm_config.disable_thinking`）
- **提交**：（待提交时回填）
- **MEMORY**：qa-system-model-config-cache-think-hide.md（更新既有记忆，补disable_thinking）

---

## 1. 需求

2026-10-03 真机：复杂问题「B019 圣特公司供货量下降的原因，重点看不同收货地点的变化」
在 chat 链路持续 **HTTP 400**「无法生成有效的查询计划」，MiniMax-M3（modelId=9）
连续 3 次重试 reason 全为 `PLAN_REPLY_EMPTY`。同一问题 deepseek-chat（modelId=1）正常。

## 2. 根因（两层，均实测确定，非推断）

### 第一层：`<think>` 污染三条内部解析路径

推理模型把思维链内联在回复开头，导致：

| 路径 | 症状 |
|---|---|
| `_parsePlanOutcome` | `find("{")` 命中 think 内的示例 JSON ⇒ 从中间截断 ⇒ `PLAN_REPLY_JSON_INVALID` |
| `parseSqlFromResponse` | `startswith(("SELECT","WITH"))` 恒不成立（think 前缀）⇒ SQL 阶段重试耗尽 |
| `stripJsonFence` | `_JSON_FENCE_RE.search` 命中 think 内的 ``` 围栏 ⇒ **静默返回错误数据**（最危险：无异常、无日志） |

修复：`stripThinkBlocks(content)` 在三处**无条件**执行。
⚠️ **与 `Think_Hide` 无关**——该参数管「给用户看什么」，内部解析要的是「机器能读什么」，两者正交。
勿后续「优化」成读参数。

### 第二层：计划阶段 token 预算不足（本变更主体）

修完第一层后真机**仍 400**，reason 从 `PLAN_REPLY_JSON_INVALID` 变为 `PLAN_REPLY_EMPTY`
（这一变化本身就是第一层修复生效的证据）。实测同一真实 prompt
（system 19,393 字符 / user 212 字符）：

| 组成 | 字符 | tokens | 占比 |
|---|---|---|---|
| `<think>` 思维链 | 18,624 | ~5,829 | **91.3%** |
| 真正的计划 JSON | 1,783 | ~558 | 8.7% |

| maxTokens | completionTokens | 截断 | think 标签 | 解析 |
|---|---|---|---|---|
| 2048 | 2048 | ✅ | 1/1 闭合 | ❌ `PLAN_REPLY_EMPTY` |
| 4096 | 4096 | ✅ | 1/1 闭合 | ❌ `PLAN_REPLY_EMPTY` |
| 8192 | 6387 | ❌ | 1/1 闭合 | ✅ 成功 |

**真实需求 6387 > 现有封顶 4096** ⇒ 单纯补截断退避不足以修复，只能作兜底。

反直觉点：`<think>/</think> = 1/1` 说明闭合标签写了，但**JSON 部分**才是被 2048 切断的部分。

### 方案 B 已证伪（避免后人重走）

`generateValidatedPlan` 的 `common` 字典**不含 `maxTokens`**，计划阶段用的是
`generateQueryPlan` 签名硬编码默认 2048；`NL2SQL_MAX_TOKENS` 系统参数**只被 SQL 阶段读**
（`nl2sql_service.py:640`）⇒ 调该参数对计划阶段**完全无效**。

## 3. 方案 D 实测数据

MiniMax-M3 支持关闭 thinking（M2.x 不支持）。同一真实 prompt、2048 预算：

| 组 | completionTokens | 截断 | think 标签 | 解析 |
|---|---|---|---|---|
| **`thinking: disabled`** | 509 / 657 / 842 / 907 | ❌ | **0** | ✅ **4/4** |
| `thinking: adaptive`（现状） | 2048 | ✅ | 1 | ❌ |

**节省 86-92%，解析全部成功。**

## 4. 设计取舍

| # | 决定 | 理由 |
|---|---|---|
| 1 | 用 `extra_body={"thinking":{"type":"disabled"}}`，**不用顶层 kwarg** | 实测直传 `thinking=` 报 `unexpected keyword argument`（openai SDK 2.53.0 不认） |
| 2 | **merge 而非赋值** 调用方的 `extra_body` | 调用方可能自带 `reasoning_split`（M2.7 不传可能返回空响应）；`payload.update(kwargs)` 在注入之后执行，直接赋值会被冲掉 |
| 3 | 作用于**全部 LLM 调用**（计划 + SQL + 答案 + L4 tools） | 用户明确要求按模型勾选；细分会让「这个模型到底关没关」不可预测 |
| 4 | A 只补**计划阶段** | SQL 阶段早有截断退避（`nl2sql_service.py:718-724`），不重复实现 |
| 5 | 翻倍封顶沿用 `_NL2SQL_TRUNCATION_BACKOFF_DEFAULT`（4096） | 已存在的派生常量，两阶段共用一份避免漂移 |
| 6 | `disable_thinking` 默认 **False** | 默认行为不变，只有主动勾选才改变；迁移用 `server_default=false` |
| 7 | 前端 checkbox 而非下拉 | 二值语义；且 ProviderType 无关（deepseek 传了也无害） |

### ⚠️ 已知取舍（必须让用户知情）

关闭 thinking 后**计划质量可能下降**。实测同一问题：

- 带 thinking：单表 `DWD_GOODS_RECEIPT_DTL` + 干净条件 `SUPPLIER_CODE = 'B019'`
- 关 thinking：多表 `('DWD_GOODS_RECEIPT_DTL','DIM_SUPPLIER')` +
  条件 `"DIM_SUPPLIER.BPSNUM_0 = 'B019' 或 DIM_SUPPLIER.BPSNAM_0 = '圣特公司'"`

思维链让模型先推理「B019 是编码不是名称」；关掉后靠直觉猜，倾向多表 + 口径不确定，
**更容易撞上本体连通性校验失败**（`RCV_SITE_CODE → DIM_FACILITY` 无 join 边）。

这正是「按模型勾选」而非全局开关的价值：**风险由用户按模型自行承担**，不满意即时恢复。

## 5. 变更内容

| 文件 | 变更 |
|---|---|
| `alembic/versions/0109_llm_config_disable_thinking.py` | 新增列 `llm_config.disable_thinking`（`server_default=false`） |
| `app/domain/models.py` | `LlmConfig.disable_thinking` |
| `app/domain/schemas.py` | `LlmConfigCreate/Update/Read` 各加字段（alias 自动 `disableThinking`） |
| `app/services/model_config_service.py` | `create` 透传；`update` 零改动（`setattr` 循环自动处理，已调 `invalidateClient`） |
| `app/infrastructure/llm/openai_client.py` | `_applyThinkingPolicy`，三处调用（`complete` / `completeStream` / `complete_with_tools`） |
| `app/services/nl2sql_plan.py` | 计划阶段截断检测 + 预算翻倍（对齐 SQL 阶段范式） |
| `app/services/nl2sql_plan.py` / `nl2sql_service.py` / `llm_json_fence.py` | `stripThinkBlocks` 无条件剥离（第一层修复） |
| `frontend/src/types/modelConfig.ts` | 三个 interface 各加 `disableThinking?` |
| `frontend/src/pages/ModelConfigPage.tsx` | checkbox + 默认值 + 回填 + payload |
| `frontend/src/i18n/{zh-CN,en-US}.ts` | `labels.disableThinking` |

## 6. 测试

| 文件 | 用例数 | 覆盖要点 |
|---|---|---|
| `unit/test_llm_think_parse.py` | 14 | 三路径 think 隔离（含未闭合 think、think 内含围栏） |
| `unit/test_openai_client_thinking.py` | 9 | 三方法注入 / 不注入 / merge 不覆盖 / 调用方 thinking 优先 / 无属性兜底 |
| `unit/test_nl2sql_plan_truncation.py` | 5 | 翻倍 / 封顶 4096 / 未达上限不翻倍 / 近似计量不判截断 / 截断原因回进 detail |
| `integration/test_model_config_disable_thinking.py` | 4 | 默认 false / 创建 true / 更新启用 / **更新显式 false 落库**（`exclude_unset` 语义） |
| `frontend/src/tests/ModelConfigPage.test.tsx` | +4 | 回填 / 勾选传 true / **取消勾选传 false 而非 undefined** / 新增默认不勾选 |

### 测试作者踩过的坑（留给后人）

- 传给 LLM 的 kwarg 名是 **`maxTokens`**（BaseLlmClient 层），`max_tokens` 是 SDK 内部转换后的名字——
  断言写错会得到 `KeyError`，误判实现有 bug。
- 两次都截断时 `generateQueryPlan` 按契约**抛 `Nl2SqlError`**，不是返回 None；测试须 `pytest.raises`。
- 失败 detail 在 `exc.value.detail`，不在 `str(exc.value)`。
- 探测脚本必须复刻生产 token 上限：不带 `maxTokens` 的探针会「通过」而生产失败
  （本次即因此误判过一次）。

## 7. 回归证据（真实 PG `qa_metadata_test`，串行）

| 层 | 结果 |
|---|---|
| LLM 单元（think/fence/openai/factory/templates/hypothesis） | 143 passed |
| NL2SQL 单元（transient_retry/plan_gate/service/query_plan） | 248 passed |
| model config 集成（新增 + 既有 API） | 19 passed |
| unit 全量 | 48 failed / 3547 passed —— **与基线逐条身份零差异**（`diff` 空） |
| chat 集成（21 文件）+ llm detached | 26 failed / 175 passed —— 与 HEAD 基线 worktree **逐条身份零差异** |
| 前端 `ModelConfigPage.test.tsx` | 10 passed |
| 前端 `npm run build` | ✓ built |

⚠️ 回归必须**串行**。本次并发跑覆盖率与全量造成 29 个假 error（两个进程抢同一测试库）。

## 8. 部署与验收清单（2026-10-03 真机实测结果）

部署：`./scripts/deploy_backend.sh`（0108 → 0109 自动迁移已应用，`disable_thinking boolean NOT NULL DEFAULT false`）
+ `docker compose build --no-cache frontend`（compose 文件在 `docker/` 目录，不在仓库根）。

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | admin 勾选并保存 | ✅ | `PUT /api/v1/models/9 {"disableThinking":true}` → 回读 `true`；置 `false` → 回读 `false` |
| 2 | 开关真实到达 provider | ✅ | 容器内探针（真实 LlmConfig id=9 + 真实密钥）：`False → completion=74 有think=True`；`True → completion=37 有think=False` |
| 3 | 日志无 `PLAN_REPLY_JSON_INVALID` | ✅ | 20 分钟窗口内 0 次 |
| 4 | `PLAN_REPLY_EMPTY` 消失 | ⚠️ **未达成** | 仍有 7 次（attempt1×3 / attempt2×3 / attempt3×1），但被截断退避救回部分轮次 |
| 5 | 原句稳定 HTTP 200 | ❌ **未达成** | 失败点已从「计划解析」下移到「计划校验」（`计划校验未通过` 10 次，属性不属于选定类） |
| 6 | 取消勾选恢复 400 | ❌ 判据作废 | 见 §8.1 |
| 7 | deepseek（modelId=1）不变 | ✅ 未改动其配置 | — |

### 8.1 关键发现：关闭 thinking 让这道题**更难**，不是更容易

同一原句、同一数据源，交替开关各跑多轮：

| disableThinking | HTTP 结果 |
|---|---|
| `false`（保留思考） | 200 / 400 / 200 → **2/3 成功** |
| `true`（关闭思考） | 400 / 400 / 400 / 400 → **0/4 成功** |

⚠️ 样本量小（3 vs 4），**不足以定论**，但方向与 §4「已知取舍」的预判一致：
关掉思维链后 M3 倾向选不存在的属性（`RCV_YEAR_MONTH` / `TO_CHAR` / `DWD_GOODS_RECEIPT_DTL.PO_NO`
均「不属于选定的任何类」），撞本体校验失败；带思维链时它能先推理出「B019 是编码不是名称」，
产出单表 + 干净 `SUPPLIER_CODE = 'B019'`，通过校验。

**结论**：本次修复解决的是「**推理模型因 token 预算被截断而完全不可用**」，
不是「让所有问题都变对」。对需要跨表推理的复杂题，关闭 thinking 可能反而降低成功率。
开关保持**默认 false**（= 现状行为），由用户按模型自行决定。

### 8.2 剩余 400 的真实归属（不在本次范围）

主导失败已变为 `计划校验未通过`，报错形如
「聚合属性 DISTINCT DWD_GOODS_RECEIPT_DTL.PO_NO 不属于选定的任何类」。
这属于**缺陷 2 邻域**（`RCV_SITE_CODE → DIM_FACILITY` 缺 join 边 ⇒ 属性归属校验失败），
用户已决定手工补别名，本次不处理。

另记录一条**计量盲区**（未来独立缺陷）：计划解析失败的重试**不写 `session_token_usage`**，
因此失败轮次的真实 `completion_tokens` 无法从计量表回溯，只能靠容器日志推断。

## 9. 不在本次范围

- **`RCV_SITE_CODE → DIM_FACILITY` 缺 join 边**：探针证明补 JOIN 边收益低
  （`FCYDES_0` 描述几乎全是空格，且 LLM 已能绕过维度表成功取数）；用户手工补别名。
- **月度缺月无守卫**：需先查清 THBI 数据现状。
- **`reasoning_content` SDK 字段被 `completeStream` 丢弃**：独立未来缺陷。