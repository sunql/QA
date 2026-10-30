# L4 system prompt 角色修复（任务 3 第 ④ 步，降级版）

> 日期：2026-10-31 ｜ 分支 `feat/l4-prefix-alignment`（PR #83，base `d6b2127f`）｜ 提交 `3953e085`
> 发版源 `/private/tmp/qa-deploy-1031-system-role`（detached @ `3953e085`）
> 对照：① 基线 `2026-10-31-l4-token-cost-profiling`、② 瘦身 `2026-10-31-l4-tool-slimming`

## 1. 为什么 ④ 被降级

④ 原定「前缀对齐提升 prompt cache 命中」。用 ② 的实测数据复核后，前提不成立：

| 轮次 | 本轮 prompt | 上轮 prompt（可命中前缀） | 实际命中 | 前缀命中率 |
|---|---|---|---|---|
| iter2 | 2,180 | 1,226 | 1,152 | 94.0% |
| iter3 | 8,438 | 2,180 | 2,176 | 99.8% |
| iter4 | 8,616 | 8,438 | 8,448 | 100.1% |
| iter5 | 9,065 | 8,616 | 8,832 | 102.5% |

① 报告 §5 的「iter2 仅 9.0% / iter3 33.5%」是 `cached ÷ 本轮 prompt` 的口径误读——分母含本轮新增内容。按「上轮 prompt」作分母，基线同样是 96.6% / 100.7%（1,024 ÷ 1,060、11,520 ÷ 11,442）。

两次运行的有效计费输入（② 13,973 / ① 54,488）都**低于**「每次重发全额命中缓存」的理论下限（14,180 / 55,436）——缓存已在下限，前缀对齐没有剩余收益。

## 2. 实际做了什么

`_to_llm_message`（`agent_runtime_service.py:305-355`）只处理 `human` / `ai` / `tool`，**没有 `system` 分支**，于是 `_initLoopState` 产出的消息 0 落进 fallback `LlmMessage(role="user", content=str(msg))`：

- `_L4_SYSTEM_PROMPT`（安全前言 + 行为规则）以 **user 角色**发给 provider；
- 内容是 `SystemMessage` 的 repr（生产装了 langchain-core 1.6.2）或 dict repr，带 ~77 字符噪声。

修复 = 3 行分支：

```python
if msg_type == "system":
    return LlmMessage(role="system", content=msg_content or "")
```

`LlmMessage.role` 是 `str`，各客户端一律 `{"role": m.role, "content": m.content}` 透传，无需其他改动。fallback 保持原样（反向守卫 `test_to_llm_message_fallback_still_stringifies_unknown_type` 锁死）。

## 3. 复测结果（同题：为什么5月份各供应商的供货量差异这么大？）

| 指标 | ① 基线 | ② 瘦身后 | ④ 本次 |
|---|---|---|---|
| 会话 | `l4-token-profile-20261031-2` | `l4-tool-slim-20261031-1` | `l4-system-role-20261031-1` |
| runId | `87ced51cb31d` | `30d9b42cb977` | `6ab1d5d07b05` |
| **终止** | `max_iterations` | `max_iterations` | **`answered`** ✅ |
| 工具调用 | 7 | 7 | 5 |
| Σprompt | 116,696 | 29,525 | 30,960 |
| completion | 1,035 | 1,020 | 1,910 |
| cached | 82,944（71.1%） | 20,736（70.2%） | 19,456（62.8%） |
| 有效计费输入 | 54,488 | 13,973 | 16,368 |
| 台账成本 | $0.166272 | $0.044191 | **$0.048692** |
| 实付估算 | $0.0792 | $0.0224 | $0.0283 |

**关键变化：模型第一次真正收敛到 `answered`。** 前两次复测都以 `max_iterations` 结束、答案文本是 `[L4:max_iterations] 我先看看数据库里有哪些相关表。`；本次在 5 轮内给出完整分析（供货量差异归因：品类单价/计量口径 70%、内部工厂工序流转 25%、批量模式与退货差异余量），并主动提示按 `SUPPLIER_CODE` 去重与剔除内部主体。

成本比 ② 高 10.6%（$0.0442 → $0.0487），原因是**模型真的干了活**：多 890 个 completion token、多了最后的分析输出。对基线仍是 −70.7%。

### 逐轮（证明 system 桶出现且前缀仍对齐）

| iter | prompt | completion | cached | hit% | 组件里的 system / user |
|---|---|---|---|---|---|
| 1 | 1,186 | 37 | 0 | 0% | system 844 / user 35 |
| 2 | 1,580 | 82 | 1,152 | 72.9% | 同上 |
| 3 | 7,838 | 416 | 1,536 | 19.6% | 同上 |
| 4 | 8,318 | 397 | 8,064 | 97.0% | 同上 |
| 5 | 12,038 | 978 | 8,704 | 72.3% | 同上 |

- iter1 `cached=0`：wire format 变了，缓存冷启动，属预期。
- 按上轮 prompt 作分母：iter2 97.1%、iter3 97.2%、iter4 102.9%、iter5 104.6%——修复没有破坏前缀对齐。
- 有效计费 16,368 vs 理论下限 16,769（`ΣP_i − 0.75·ΣP_{<n}`），仍在下限附近。

## 4. 验收

| 项 | 结果 |
|---|---|
| system 消息保持 `role="system"` | ✅ 容器内实测 `state roles = ['system', 'user']`，探针出现 `system` 桶 |
| preamble 留在 system 层 | ✅ 红队 `test_redteam_l4_system_prompt_survives_llm_conversion` |
| 无 provider 400 / 角色错误 | ✅ 日志无异常 |
| 探针与台账逐 token 一致 | ✅ 30,960 / 1,910 |
| 成本 | 不作为门槛（比 ② +10.6%，比 ① −70.7%） |
| **终止原因** | ✅ **`answered`（前两次都是 `max_iterations`）** |

## 5. 测试

- `test_agent_loop_security.py` +4：dict shim / 同构对象（生产 langchain 路径）/ 反向守卫 / 端到端转换
- `test_l1_prompt_injection_redteam.py` +1：转换后 system 角色与 preamble 完整
- 回归 **132 passed / 1 skipped**（`test_to_llm_message_preserves_system_role_langchain` 本地缺 langchain-core 跳过；容器内已单独验证通过）
- ruff 净新增 0（11 条均为既有基线 I001/F821/F841/F401）

## 6. 顺带更正的历史结论

- ① `2026-10-31-l4-token-cost-profiling/summary.md` §5「iter2/3 缓存断裂」→ 见本文 §1，已加更正注记。
- ② `2026-10-31-l4-tool-slimming/summary.md` §4.3 把 98.05%/97.43% 归因于「分母缩小」是对的，但当时仍接受「前缀断裂」的框架——实际从来没有断裂。

## 7. 交付

| 项 | 状态 |
|---|---|
| PR #83 | ✅ 已推送（含本文档） |
| 生产部署 | ✅ build + up，容器内 `_to_llm_message` system 分支验证通过 |
| 真实复测 | ✅ 1 次，`answered`，台账零丢轮 |
| 证据 | `evidence/l4-profile-20261031-4.log`、`evidence/report-20261031-4.md` |

**下一步建议**：「L4 不收敛」已经由本批解决（system 角色归位后模型遵守了迭代预算）。剩下的是答案质量与 ③ 模型分层，视需要再立项。
