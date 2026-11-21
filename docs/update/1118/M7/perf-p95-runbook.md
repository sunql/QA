# M7 判据 6 · 性能 P95 压测指引

> 设计稿 §11 目标：**实体精确查询 P95 < 1s / 常规图谱查询 P95 < 2s / RAG 检索 P95 < 3s**
> 脚本：`backend/scripts/bench_knowledge_p95.py`（零第三方依赖）

## 一、为什么分两站测

| 站 | 数据规模 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| **本地 dev 栈** | 图 19.3 万节点 / 52.3 万边；`entity_mapping` 35.4 万行；Milvus v2 向量 4 千余条；文档 4 份 | 端点链路无病态延迟（无 N+1、无全表扫、无逐条调用）；降级/限流不会被误记成"快" | **生产量级下的 P95**（类/向量/文档数量都远小于生产） |
| **生产/预发** | 真实量级 | §11 目标的**最终达标判定** | — |

判据 6 在 M7 验收表的结论**以生产量级那一站为准**；dev 那一站只作为发版前的回归护栏。

## 二、执行方式

```bash
cd backend
uv run python scripts/bench_knowledge_p95.py \
  --base-url https://<目标环境> \
  --username <admin 账号> --password '<口令>' \
  --iterations 30 --warmup 3 \
  --json-out ../docs/update/1118/M7/evidence/knowledge-p95-<环境>.json
```

退出码：`0` 全部达标 · `1` 有超目标项 · `2` 样本无效（限流/降级占满）。

## 三、脚本的三个诚实口径（别绕过）

1. **串行单客户端** ⇒ 测的是"单请求延迟分布"，不是吞吐。要吞吐另加并发压测，
   不要把这里的数字当 QPS 依据。
2. **限流 30/分钟** ⇒ 默认 `--delay 2.5`。被 429 的请求不算延迟样本、也不算失败，
   而是判**样本无效**（退出码 2）。想缩短时间只能调小 `--iterations`，**不要**去掉 delay。
3. **降级 ≠ 快** ⇒ `/knowledge/query` 某通道降级（如 embedding 服务不可用）仍返回 200
   且极快。脚本校验 `routeResults[].status`，有 `DEGRADED` 的样本判无效。
   如果 RAG 用例整列无效，**先修 embedding 服务再测**，不要拿 200 当通过。

## 四、被测用例与判定

| 用例 | 端点 | 目标 |
|---|---|---|
| 实体精确查询 | `GET /procurement/knowledge/entities/search?q=Q1630&limit=5` | P95 < 1s |
| 图谱查询(1 跳/2 跳) | `GET /procurement/knowledge/graph/neighbors?entityType=Supplier&entityKey=D135&maxHops={1,2}` | P95 < 2s |
| RAG 检索 | `POST /procurement/knowledge/query` `channels=["RAG"]` | P95 < 3s |
| 文档检索 | `POST /procurement/knowledge/documents/search` | P95 < 3s |
| 四通道综合 | `POST /procurement/knowledge/query` 四通道 | 无设计目标，仅观测 |

**判定口径**：每个用例 `samples = iterations`（无 429、无降级）且 `p95 < target` 才算通过。

## 五、取证清单

- `evidence/knowledge-p95-<环境>.json`：分位数、样本数、错误/限流/降级计数
- `evidence/data-scale-dev.json`：同站点数据规模（PG / Milvus / Neo4j 行数）
  —— 没有规模参照的 P95 不可复现，也不可比
- 复测时**必须**一并刷新数据规模（生产量级与 dev 不同）
