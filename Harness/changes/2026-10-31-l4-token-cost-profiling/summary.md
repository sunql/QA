# L4 纯探索问题 token 成本度量取证（任务 3 第 ① 步）

> 日期：2026-10-31 ｜ 分支 `feat/l4-token-profiling`（PR #80）｜ 发版源 `/private/tmp/qa-deploy-1031-l4profile`（detached @ `acb14d4f`）
> 性质：**纯度量，不做优化**。探针零行为变更（try/except 包裹，回归净新增失败 0）。

## 1. 样本

| 项 | 值 |
|---|---|
| 会话 | `l4-token-profile-20261031-2`（datasource 1 = THBI Oracle） |
| 问题 | 「为什么5月份各供应商的供货量差异这么大？」 |
| 结果 | `[L4:max_iterations]`，5 轮，7 次工具调用（list_tables 1 / list_joins 1 / describe_table 2 / execute_sql 3） |
| 台账 | purpose=`l4_agent_loop`：prompt=116,696 / completion=1,035 / total=117,731 / cost=$0.166272（model_name=deepseek-chat） |
| 完整性 | 探针 loop_end 116,696/1,035 == 台账 **逐 token 一致，零丢轮** ✅ |

原始证据：`evidence/l4-profile-20261031-2.log`（13 行）；报告：`evidence/report-20261031-2.md`。
样本量 1 次但与生产基线吻合（30 天 9 次 avg prompt=122,016；本次 116,696）。

## 2. 逐轮明细

| iter | prompt | completion | cached | hit% | 本轮新工具结果（sentChars） |
|---|---|---|---|---|---|
| 1 | 1,060 | 42 | 896 | 84.5% | list_tables 1,121 + list_joins **27,753** |
| 2 | 11,442 | 100 | 1,024 | 9.0% ⚠️ | describe_table 9,863 + describe_table **52,919** |
| 3 | 34,348 | 401 | 11,520 | 33.5% | execute_sql 197（错误信息） |
| 4 | 34,830 | 81 | 34,688 | 99.6% | execute_sql 222（1 行） |
| 5 | 35,016 | 411 | 34,816 | 99.4% | execute_sql **120,550**（286 行）※循环即止，未再发送 |
| **Σ** | **116,696** | 1,035 | 82,944 | 71.1% | |

## 3. 组件构成（tiktoken 估值 + 逐轮 provider 标定，占比可信）

| 组件 | 标定 tokens | 占比 |
|---|---|---|
| **tool:describe_table** | **68,334** | **58.6%** |
| **tool:list_joins** | **38,931** | **33.4%** |
| tool:list_tables | 1,692 | 1.5% |
| tool:execute_sql | 246 | 0.2% |
| user（问题+被并入的 system，见 §7） | 4,072 | 3.5% |
| assistant（含 tool_calls 回显） | 1,888 | 1.6% |
| toolSchemas | 1,533 | 1.3% |

**结论：92% 的 prompt token 是工具结果原文**，其中两张「元数据型」工具（describe_table 全列 schema + list_joins 全量 FK 图）占 92% 中的 92%。

## 4. 转义放大（wrapUntrusted HTML 实体）

- 每条工具结果 sent/raw = **1.55×~2.16×**（JSON 里 `"`→`&quot;` 为主）。
- 转义净增字符：describe_table 73,200 + list_joins 41,280 + list_tables 1,480 + execute_sql 160 ≈ **11.4 万字符 ≈ 2.9 万 tokens（按 4 char/token）**，约占总 prompt 25%。
- 即：**去掉转义（改用非 HTML 通道传 tool 结果）可白拿 ~25% 减免**，零信息损失。

## 5. resend tax 与缓存

- resend tax（Σ_{i≥2} P_{i-1}）= **81,680 tokens，占总输入 70.0%**。全量重发架构下这 70% 本可由 prompt cache 大幅折价——但见下。
- 缓存实测：总命中 71.1%，**逐轮不均**：iter1 84.5%（跨请求前缀命中），iter2 仅 9.0%、iter3 33.5%，iter4/5 却 99.4%+。
- iter2/3 断裂推测：iter1 产出的 assistant 消息（tool_calls 序列化形态）与重发时**不是逐字节前缀一致**（`_to_llm_message` dict shim 形态），prefix match 在 ~1K tokens 处断掉；iter4 起反而能对齐 iter3 的 prompt（新增内容少、且上一轮 prompt 原样保留）。**此异常是 ④ 的直接靶点**：若修复对齐，命中有望从 71% → 95%+，有效计费从 54,488 → ~25,000 tokens。
- 成本口径：台账 $0.1663 是**上界**（未透传 cachedTokens）；按 0.25 折扣实付估算 **$0.0792**。

> **更正（2026-10-31，详见 `2026-10-31-l4-system-role/summary.md` §1）**
>
> 上面「iter2 9.0% / iter3 33.5%」用的是 `cached ÷ 本轮 prompt`，**分母含本轮新增内容**，所以被稀释——不是缓存断裂。
> 正确口径是 `cached ÷ 上轮 prompt`（唯一能命中的前缀）：基线 iter2 = 1,024 ÷ 1,060 = **96.6%**、iter3 = 11,520 ÷ 11,442 = **100.7%**，iter4/5 同上。
> 佐证：有效计费输入 54,488 **低于**「每次重发全额命中缓存」的理论下限 55,436（`ΣP_i − 0.75·ΣP_{<n}`）——缓存已在下限。
>
> 因此 §6 表里 ④ 的「命中 71%→95%、计费 −50%」**不成立**，④ 已降级为 system 角色正确性修复（3 行），实测零成本收益、但首次让 L4 收敛到 `answered`。

## 6. 对 ②③④ 的量化结论（供决策，不在本批实施）

| 选项 | 证据 | 预期收益（本样本反事实手算） |
|---|---|---|
| **② 工具结果瘦身/增量注入** | describe_table 单次 52,919 chars（全列 schema）；list_joins 27,753 chars（全量 FK 图）；两者占 prompt 92% | 二者裁到 ~2K+1K tokens ⇒ 单轮 prompt 35K→~8K，Σ输入 116,696→~30K（**-75%**）；叠加免转义再 -25%。**最大杠杆，优先做** |
| **③ 模型分层** | 每轮 completion 仅 42~411 tokens；成本几乎全在输入侧 | 分层只对「输入单价」有效（换更便宜模型跑元数据阶段）；DeepSeek 已是低价档，**②落地前收益有限，后置** |
| **④ prompt cache** | ~~实测 71.1% 命中但 iter2/3 断裂~~ **（已更正：按上轮 prompt 作分母，逐轮命中 96.6%~102.5%，无断裂）**；resend tax 70% | ~~命中→95%+、实付再 -50%~~ **不成立**。实际落地为 system 角色修复（3 行），见 `2026-10-31-l4-system-role/` |

## 7. 取证途中发现（均未修，本批只记录）

1. **L4 冷启动崩溃（main 既有 bug，P1）**：`chat_l4.py:105` 传 `executor=self._adapterProvider(dto.datasourceId, None)`，`get_adapter`（`business_db_pool.py:708`）缓存未命中时解引用 `None.password_encrypted` ⇒ AttributeError → 回退 L2/L3。历史 L4 之所以能用，是同进程内其它路径先焐热了 adapter 池；容器重启后第一次 L4 **必失败**。修复方向：chat_l4 先查 ds 再传（或 get_adapter 内部懒查）。本次取证即先跑了一次 L2 回退（`l4-token-profile-20261031-1`）焐热后才成功。
2. **system prompt 以 user 角色 + dict-repr 形态发送（已知未修）**：iter1 组件里没有 `system` 桶，`user`=1,463 chars ≈ `_L4_SYSTEM_PROMPT`（1,338）+ 问题；即 system 内容被 `_to_llm_message` 折进 user 消息（str(dict)）。~~这也是 ④ 前缀断裂的帮凶（静态前缀不是规范的 system 开头）。~~ **后半句已更正：不存在前缀断裂（见 §5 更正注记），但它确实是真实的指令层级错误 + 计费噪声。已在 `feat/l4-prefix-alignment`（PR #83 / 提交 `3953e085`）修复。**
3. **模型名口径不一致**：探针逐轮 `modelName="deepseek-flash"`（llm_config）vs 台账 `model_name="deepseek-chat"`。不影响 token 数，计费单价口径待核对。
4. iter5 execute_sql 返回 286 行 / 120,550 chars —— 幸好循环即止没再发送；若 LLM 再走一轮，单轮 prompt 会飙到 ~55K tokens。**QUERY_ROW_LIMIT=0 的风险被直接拍到**。

## 8. 本批交付

| 项 | 状态 |
|---|---|
| PR #80（探针 + 报告链路 + 40 测试用例） | ✅ 已推送 |
| 部署 | ✅ 镜像重建 + up，LOG_LEVEL=INFO，探针模块在容器内可见 |
| 取证 | ✅ 1 次 L4 真实运行，台账零丢轮 |
| SSOT 本文档 | ✅ |

**下一步（等用户决策）**：按 §6 结论排 ②（工具结果瘦身 + 免转义通道）→ ④（前缀对齐）→ ③（视 ②④ 后余量决定）；L4 冷启动 bug 建议单独开 fix 分支先行。
