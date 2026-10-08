# 变更：feat-model-config-cache-invalidation-think-hide

- **日期**：2026-10-03
- **作者**：Claude / 启琳
- **Phase**：模型配置治理 + 答案后处理
- **状态**：done
- **关联变更**：[../feat-nl2sql-share-denominator-guard/summary.md](../feat-nl2sql-share-denominator-guard/)（接线点/shareWarning 共存验证）
- **迁移版本**：0108（system_config 补种 `Think_Hide='0'`，ON CONFLICT DO NOTHING）
- **提交**：0d0a121（feat）；本文档 + wiki 随 docs 提交
- **MEMORY**：待写

---

## 1. 需求

1. **缓存失效**（2026-10-03 真机报障根因）：用户建 MiniMax-M3 配置 endpoint 填错 → Connection error；
   03:33:47 修正 endpoint 后 03:34:18 仍报错。根因：`factory._clients` 按 config id 缓存客户端单例、
   `OpenAiClient.__init__` 构造时固化 model_name/api_endpoint/key，而 `model_config_service` 的
   update/deactivate 从不失效缓存——**编辑配置对运行中的进程永远不生效**（当时靠 restart 临时恢复）。
2. **Think_Hide**：推理模型（MiniMax-M3）把 `<think>…</think>` 思维链内联在答案正文直接展示。
   用户要求系统参数 `Think_Hide`（1=隐藏 / 0=显示，参数名按用户原话逐字），先配 0，后续可翻 1。

## 2. 设计取舍

| # | 决定 | 理由 |
|---|---|---|
| 1 | 失效 = `invalidateClient(configId)`（pop + best-effort `await close()`，close 失败也必须移出） | 对齐 `embedding_provider_factory.invalidateEmbeddingClientCache` 既有范式；失效语义优先于优雅关闭 |
| 2 | create 不接失效 | 新 id 无旧缓存 |
| 3 | Think_Hide 走迁移补种、**不进 seed_system_config.py** | seed 是 on_conflict 强制覆盖语义，会把 admin 后续翻的 1 冲回 0 |
| 4 | 剥离放 service 层答案出口，不放 LLM 客户端层 | 配置读取需要 session（对齐「无缓存、每次现读」既有口径）；且只影响「展示给用户」层——计划 JSON/SQL 生成等内部消费不剥（YAGNI，若推理模型污染内部解析属另一缺陷） |
| 5 | 流式 = 逐字符状态机增量过滤（`ThinkStreamFilter`），`answerPieces`/`full_answer` 只收过滤后内容 | 标签会跨片切断；下发内容与落库内容必须一致（H4 断连兜底共用 answerPieces） |
| 6 | 未闭合 `<think>`（到流结束）视为仍在思维链内 → 隐藏 | 推理模型漏写闭合标签时宁可全隐藏也不漏思维链 |
| 7 | KPI/闲聊/不可回答/多步降级收尾**不接** | 均为模板文案（零 LLM），无思维链可漏 |

## 3. 数据模型变更

无新表。迁移 0108：`INSERT INTO system_config ... ON CONFLICT (key) DO NOTHING`（照抄 0052 模式）。

## 4. 接口契约变更

无 API 契约变更。行为变更：
- `PUT /api/v1/models/{id}`、`DELETE /api/v1/models/{id}`：即时失效缓存客户端（本次修复）；
- `Think_Hide=1` 时所有 LLM 答案出口（chat 单步/多步汇总、流式、doc_qa、wiki chat）剥离思维链，
  下发 token 与落库 assistant 消息一致；`0`/缺省 → 字节级透传。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/infrastructure/llm/factory.py` | 新增 `async invalidateClient(configId)`（pop + close，close 异常仅 warning） |
| `app/services/model_config_service.py` | update/deactivate 在 commit 后调 `invalidateClient` |
| `app/services/think_block.py` | **新建**：`THINK_HIDE_KEY`、`isThinkHideEnabled`（text() 直查、缺行/空/非法/异常→False 安全降级）、`stripThinkBlocks`（全文剥离，无标签字节级透传）、`ThinkStreamFilter`（逐字符状态机：NORMAL/IN_THINK + 候选缓冲处理标签跨片；`flush` 收尾）、`applyThinkPolicy`（非流式组合） |
| `app/services/chat_service.py` | 单步答案出口 `applyThinkPolicy`（shareWarning 前缀之前） |
| `app/services/chat_multistep.py` / `chat_stream.py` | 多步汇总（流式/非流式）整块剥；流式单步循环接 `ThinkStreamFilter` + flush 尾段 |
| `app/services/rag_qa_service.py` / `wiki_qa_service.py` | 同流式 filter 接线（full_answer 与下发一致） |
| `alembic/versions/0108_think_hide_config.py` | **新建**：补种 Think_Hide='0' |

## 6. 测试

| 文件 | 断言 |
|---|---|
| `unit/test_llm_factory.py`（增补 3 例） | invalidate → 移出缓存且 close 被调；未知 id no-op；close 抛错仍移出 |
| `integration/test_model_config_api.py`（增补 2 例） | PUT/DELETE 后哨兵客户端被移出且 close 被调（真实 PG + 公开 API 建配置） |
| `unit/test_think_block.py`（新，34 例） | strip：无标签透传/单块/多块/未闭合/大小写/None；filter：标签跨片/「<<」连写（第一个 < 是真实文本）/think 内断裂候选/未闭合 flush 空/纯空白不下发/流式==全文剥离一致性契约；isThinkHideEnabled：1/0/缺/空/异常/数字/非法；applyThinkPolicy 三路 |
| `integration/test_chat_think_hide.py`（新，6 例） | 缺省透传 / =1 剥离 / =0 透传 / 落库与下发一致 / 流式缺省透传 / 流式=1 增量过滤（标签切在片缝里，token 帧无任何思维链） |

回归（2026-10-03 实跑，真实 PG 串行）：
- think/model_config 相关 unit+integration **40 + 8 全绿**；think_block 覆盖率 **99%**。
- chat/doc_qa/model_config 集成回归 **78 passed**；wiki_api **38 passed**。
- unit 全量 **3519 passed / 48 failed / 1 skipped** —— 48 与基线逐族一致（45 chat 陈旧假替身 +
  3 既有红），**零新增红**。
- 教训：曾把 unit 全量与 wiki 集成**并行**跑在同一个测试库上 → 互相干扰卡死；停掉后严格串行重跑即正常
  （混跑陷阱的进程级变体，见 [[qa-system-mixed-suite-truncate-hazard]]）。

## 7. 安全审查

未触发 security-reviewer：无认证/SQL 执行方式变更；`isThinkHideEnabled` 用绑定参数（`bindparams`）
而非 0052/`_readBoolConfig` 的 f-string 拼接（key 来自代码常量，非用户输入，无注入面）。

## 8. 部署验证

2026-10-03 真机（`QA_BACKEND_CONTAINER=qa-backend ./scripts/deploy_backend.sh`，容器启动自动跑
`0107 -> 0108` 迁移）。admin 登录 → THBI 数据源 → modelId=9（MiniMax-M3）：

- **Think_Hide=0**：问「2026年4月收货数量」→ 答案含 `<think>…</think>` + 正文（透传 ✓）
- **PUT Think_Hide=1**（即时生效，无需重启）→ 同问 → 只剩正文，思维链消失 ✓
- **缓存修复**：`PUT /models/9` 改 temperature=0.3 → **立即**再问（2026年5月）→ 正常出数 ✓
  （旧行为此场景编辑不生效、必须重启）
- **Think_Hide 改回 0**（按用户当前要求保持不隐藏）✓

## 9. 已知边界

- 内部 LLM 消费（计划 JSON/SQL 生成/图表标签/DQ 规则）不剥 think——推理模型若污染内部 JSON
  解析，是独立缺陷，届时在对应 parse 出口处理。
- `reasoning_content`（SDK 分离字段）当前被 `completeStream` 丢弃（既有行为），本变更不改；
  若未来模型把思维链走该字段，需要在 `openai_client.py:217` 处理。
- 流式中 admin 翻转 Think_Hide 只对**新请求**生效（读参数发生在流开始前），进行中的流不受影响。
