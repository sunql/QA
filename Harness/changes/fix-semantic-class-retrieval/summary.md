# 变更：修复本体类语义检索（Milvus 索引 + embedding 回填 + 系统代理修复）

- **日期**：2026-08-13
- **Phase**：生产修复
- **状态**：done

## 1. 需求

`ontology_embeddings` 集合存在但从未建索引、且 0 实体，导致 `_selectRelevantClasses`
恒回退全量——本体类越多，喂给 LLM 的 schema 越大，越容易选错表/列。目标：补齐索引、
回填 19 个本体类的 embedding，让语义检索真实生效，不再回退全量。

## 2. 设计评审

- **embedding 模型**：无外部 API key，经评估弃用 oMLX（`mlx_lm/server.py` 无
  `/v1/embeddings` 端点），选用 Ollama 本地 **bge-m3**（1024 维，中文/多语种强）。
- **维度变更**：`_DIM` 从 1536 改为 1024；重建集合为 1024 维（Milvus 集合强 schema，
  维度不匹配无法插入，load 状态服务端持久，需 drop 后重建）。

## 3. 数据模型变更

- Milvus `ontology_embeddings` 重建为 1024 维并建索引（IVF_FLAT / L2 / nlist=128）。
- `session_token_usage.request_time` 修复 model/migration tz 漂移：新迁移
  `0011_request_time_tz` 改列为 `timestamptz`，存量数据按 `AT TIME ZONE 'UTC'` 重解释
  （既有值即 UTC 墙钟，时刻不变）。

## 4. 接口契约变更

无 API 契约变更。`EmbeddingClient` / `OpenAiClient`（Azure + OpenAI 分支）构造时注入
`trust_env=False` 的 httpx client。

## 5. 实现要点

- `scripts/backfill_milvus_embeddings.py`：回填脚本，`--dry-run` 只验证不写入；幂等
  （按 ontology_id 先删后插）。
- `app/infrastructure/llm/http_client.py`（新建）：`buildHttpClient()` 返回
  `httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False))`。
- **根因**：macOS 系统代理（127.0.0.1:12450，进程 `core`）劫持 httpx/openai SDK 请求
  （默认 `trust_env=True`），导致对 localhost/Ollama 的 embedding/chat 调用间歇性 502。
  curl 直连正常。修复：绕系统代理直连目标 base_url。

## 6. 测试

- 回填：19 成功 / 0 失败，全部 1024 维。
- 端到端验证：3 个真实问题（供应商收货数量汇总 / 采购订单的价格和供应商 / 物料BOM清单）
  经真实 bge-m3 → Milvus → `_selectRelevantClasses` 全部裁剪成功（15/19，命中率 > 0.5
  阈值），**不再回退全量**；embedding 请求直达 localhost:11434，全程 200 OK 无 502。
- 全量回归：**635 passed**（含 tz 修复后重跑）。

## 7. 安全审查

- 未硬编码 secrets：本地测试用 `ollama` dummy key，生产 key 仅从环境变量/docker .env 读取。
- 代理绕过属合理取舍：`trust_env=False` 使显式设置 HTTPS_PROXY 访问外部 LLM 的部署
  需在 base_url 层面显式配置（见 code-reviewer 结果）。

## 8. 部署验证

- dev DB `alembic upgrade head` 已应用（`request_time` 现为 `timestamp with time zone`）。
- 测试库由 `_pg_support.py` 自动 `upgrade head`，迁移幂等。
- 后端重启后 `_selectRelevantClasses` 生效（此前需服务重启加载新代码）。

## 9. 关联

- 任务：#30/#31/#32/#45/#46/#47/#48/#49/#44
- Wiki：`Harness/wiki/`（NL2SQL 引擎、模型路由）
