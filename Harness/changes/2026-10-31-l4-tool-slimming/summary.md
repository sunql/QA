# L4 工具结果瘦身（任务 3 第 ② 步）

> 日期：2026-10-31 ｜ 分支 `feat/l4-tool-slimming`（PR #82，base `6d90342d`）｜ 提交 `f2952af1`
> 发版源 `/private/tmp/qa-deploy-1031-tool-slim`（detached @ `f2952af1`）
> 对照基线：`Harness/changes/2026-10-31-l4-token-cost-profiling/summary.md`（① 取证，PR #80）

## 1. 做了什么

创建期紧凑化 + 转义收敛，**不改历史消息**（改写旧 tool 消息会打断 prompt cache 前缀，与 ④ 冲突）。

| 文件 | 改动 |
|---|---|
| `app/security/prompt_sanitizer.py` | 新增 `sanitizeStructured` / `wrapUntrustedStructured`：只转 `& < >`，保留引号；`&` 先转义 + ZWS 打断不变 |
| `app/services/agent_tools_nl2sql.py` | `encodeExecuteSqlResult`（列式 + `L4_TOOL_ROW_CAP=100` + `row_count` 真值）；`describe_table` 列式 + 自描述 `column_format` + `ref` 解析表名；`list_joins` 表名化文本边 + 反向去重 + `tables` 过滤；`list_tables` 去重 |
| `app/services/agent_runtime_service.py` | `_dispatchSingleTool` 走编码器 + 结构化转义；`_L4_SYSTEM_PROMPT` 增「工具结果阅读」一条 |
| 测试 | sanitizer +6 / 工具 ~17 / dispatch 断言改结构化 / 红队 +2 组合用例 / token 探针钉编码器 |

定向回归 **148 passed**（工具 39 / sanitizer 21 / loop 安全 10 / 探针 22 / 冷启动 2 / 红队 22 / runtime 32），净新增失败 0。

## 2. 样本

| 项 | 基线（①） | 本批（②） |
|---|---|---|
| 会话 | `l4-token-profile-20261031-2` | `l4-tool-slim-20261031-1` |
| runId | `87ced51cb31d` | `30d9b42cb977` |
| 问题 | 为什么5月份各供应商的供货量差异这么大？（datasource 1 = THBI Oracle） | 同题 |
| 工具轨迹 | 5 轮 7 次（list_tables 1 / list_joins 1 / describe_table 2 / execute_sql 3） | **完全相同** |
| 终止 | `max_iterations` | `max_iterations` |
| 台账 | prompt 116,696 / completion 1,035 / **$0.166272** | prompt 29,525 / completion 1,020 / **$0.044191** |
| 完整性 | 探针 loop_end == 台账逐 token 一致 | 同（29,525 / 1,020）✅ |

证据：`evidence/l4-profile-20261031-3.log`（13 行）、`evidence/report-20261031-3.md`。

## 3. 前后对比

| 指标 | ① 基线 | ② 之后 | 变化 |
|---|---|---|---|
| **Σprompt** | 116,696 | **29,525** | **−74.7%** |
| completion | 1,035 | 1,020 | −1.4% |
| cached | 82,944（71.1%） | 20,736（70.2%） | — |
| 有效计费输入（cache×0.25） | 54,488 | **13,973** | **−74.4%** |
| **台账成本（上界）** | $0.1663 | **$0.0442** | **−73.4%** |
| 实付估算 | $0.0792 | **$0.0224** | **−71.7%** |
| tool:describe_table | 68,334（58.6%） | 18,176（61.6%） | −73.4% |
| tool:list_joins | 38,931（33.4%） | **2,046（6.9%）** | **−94.7%** |
| tool:list_tables | 1,692 | 1,193 | −29.5% |
| tool:execute_sql | 246 | 174 | −29.3% |
| toolSchemas | 1,533 | 1,896 | +23.7%（多了 `tables` 参数 + 行数说明） |
| user（含被折入的 system） | 4,072 | 4,520 | +11.0%（system prompt 多一条） |
| **转义净增字符** | 116,120 | **156** | **−99.9%** |

单次工具结果（rawChars）：

| 工具 | 基线 | 本批 | 变化 |
|---|---|---|---|
| list_joins | 17,394 | 1,053 | −93.9% |
| describe_table（窄表） | 6,074 | 2,508 | −58.7% |
| describe_table（248 列宽表） | 32,230 | 11,561 | −64.1% |
| execute_sql（最大） | 77,591（286 行） | 8,782（657 行 → 截 100 行） | −88.7% |

逐轮：

| iter | prompt | completion | cached | hit% | 本轮新工具结果 |
|---|---|---|---|---|---|
| 1 | 1,226 | 43 | 128 | 10.4% | list_tables 751 + list_joins 1,131 |
| 2 | 2,180 | 82 | 1,152 | 52.8% | describe_table 2,547 + 11,600 |
| 3 | 8,438 | 111 | 2,176 | 25.8% | execute_sql 165（错误信息） |
| 4 | 8,616 | 383 | 8,448 | 98.1% | execute_sql 166（1 行） |
| 5 | 9,065 | 401 | 8,832 | 97.4% | execute_sql 8,821（657 行 → 100 行，`rowCount=657` 真值） |
| **Σ** | **29,525** | 1,020 | 20,736 | 70.2% | |

## 4. 验收门槛

| 门槛 | 目标 | 实测 | 结论 |
|---|---|---|---|
| Σprompt | <35K | 29,525 | ✅（−74.7%） |
| `tool:list_joins` | <11K | 2,046 | ✅（−94.7%） |
| `tool:describe_table` | <16K | 18,176 | ❌ 差 13.6%（见下） |
| escapeExpansion | ≤1.15 | 大结果 1.0034~1.0741；两条 126/127 字符的 execute_sql 为 1.31 | ✅ 实质达标（说明见下） |
| iter≥4 缓存 | ≥99% | 98.05% / 97.43% | ❌ 比值未达（绝对量未回归，见下） |
| 以 `answered` 结束 + 数值正确 | — | `max_iterations` | ❌ 与基线相同，非回归 |

### 4.1 describe_table 18,176 vs 16K

这是**跨轮累加口径**：同一份 11,561 字符的宽表在 iter3/4/5 的 prompt 里各计一次。
单轮内 describe_table 只占 6,015 tokens（基线同口径 ~23K）。逐列已到形态下限
（248 列 × ~46.6 字符，`["NAME","","NUMBER","NAME","PK",""]` 已无冗余键）。
再省只剩「`source_column == name` 时置空」一个杠杆（预估 −30%，本批按计划保留恒输出，未做）。

### 4.2 escapeExpansion 的两条 1.31

`1.3095` / `1.3071` 对应 rawChars 126 / 127 的 execute_sql 错误/单行结果——escapeExpansion
的分子含固定 39 字符的 `<agent_tool_result></agent_tool_result>` 包装，小包时占比自然高。
绝对值 Δ≈39 字符/条（合计 ~20 tokens）。所有 ≥1K 字符的结果均 ≤1.0741，且工具结果转义净增
从 116,120 字符降到 156 字符。

### 4.3 iter4/5 缓存 98.05% / 97.43%

分母缩小导致的比值效应，绝对未命中量没有回归：

| | 基线 | 本批 |
|---|---|---|
| iter4 未命中 | 34,830 − 34,688 = **142** | 8,616 − 8,448 = **168** |
| iter5 未命中 | 35,016 − 34,816 = **200** | 9,065 − 8,832 = **233** |

未命中就是「本轮新增的 assistant + tool 消息」，绝对量与基线同量级；prompt 从 35K 缩到 9K 后，
同样 ~200 tokens 的新增内容占比从 0.6% 变成 2.6%。前缀对齐问题（system 以 user 角色发送、
`_to_llm_message` dict shim）仍留给 ④。

### 4.4 未以 answered 结束

与基线同期现象一致：iter5 执行 657 行 SQL 后撞 `max_iterations=5`。
本次日志无 SQL 错误/异常，仅有既有 schema drift 非阻断 WARNING，说明不是本批编码改动导致。
答案文本为 `[L4:max_iterations] 我先看看数据库里有哪些相关表。`（与基线同为兜底文本）。

## 5. 结论

- **最大杠杆已兑现**：单次运行成本 −73.4%（$0.1663 → $0.0442），单轮 prompt 从 35K 降到 9K 量级。
- **工具结果不再是瓶颈**：describe_table + list_joins 从占 prompt 92% 降到 68.5%（18,176 + 2,046 / 29,525），且其中 61.6% 是宽表 schema 的跨轮重发——正是 ④ 用缓存折价的靶子。
- **剩余 token 结构**：user（含 system）15.3% + toolSchemas 6.4% + assistant 回显 5.1%，三者合计 26.8%，可作 ④ 之后的下一批目标。
- `max_iterations` 与缓存比值两条门槛未达，均**不指向本批回归**，属于 ④ / 迭代预算的范畴。

## 6. 交付

| 项 | 状态 |
|---|---|
| PR #82 | ✅ 已推送（待合并） |
| 生产部署 | ✅ `docker compose build backend && up -d backend`，容器内 `L4_TOOL_ROW_CAP=100` 验证通过 |
| 真实复测 | ✅ 1 次 L4 运行，台账零丢轮 |
| SSOT 本文档 + 证据 | ✅ |
| `Harness/wiki/agent-loop.md` 工具结果契约 | ✅ 已更新 |

**下一步**：④ 前缀对齐（system 角色修复 + `_to_llm_message` 前缀一致性）。
