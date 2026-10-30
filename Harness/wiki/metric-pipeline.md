# Metric Pipeline (Phase 5)

Cold-metric auto-promotion from L2 routing logs + routing layer observability.

> ⚠️ **2026-10-28 全文事实核正**：本文件多处描述与代码不符，已逐条按
> `metric_promotion_service.py` / `routing_metrics_service.py` / `kpi_semantic_match_service.py` /
> `models.py` / `enums.py` / `api/v1/kpi_catalog.py` 核实并修正。主要漂移：
> `sql_template` 列不存在、状态枚举是 `DRAFT/PUBLISHED/DEPRECATED`（非 `ACTIVE/REJECTED/EXPIRED`）、
> 阈值是每 KPI 的 `match_threshold`（默认 0.75，非环境变量 0.4）、
> API 前缀是 `/api/v1/kpi-catalog`（非 `/admin/kpi-catalog`）、
> 两个 service **均无生产调用方/无 API 暴露**。
> 与指标口径治理的关系见 [kpi-metric-technical-form](kpi-metric-technical-form.md)。

## MetricPromotionService

Scans `session_message` for frequently repeated L2 questions and promotes them to `kpi_catalog` as DRAFT entries for admin review.

### Workflow

> ⚠️ **2026-10-28 事实更正**：本节原文多处与代码不符，已按 `backend/app/services/metric_promotion_service.py`
> 与 `backend/app/domain/models.py` 逐条核实修正。**关键漂移**：不存在 `sql_template` 列（真实列是 `formula`）；
> 不存在 `ACTIVE`/`REJECTED`/`EXPIRED` 状态（真实枚举见下）；`MetricPromotionService` **无生产调用方**。

**状态枚举**（`app/domain/enums.py:279` `KpiStatus`）：

| 值 | 语义 |
|---|---|
| `DRAFT` | 草稿，可任意编辑 |
| `PUBLISHED` | 已发布，进入指标目录；PUT 仅修订（`revision_count+1`） |
| `DEPRECATED` | 已停用（被替代或下线），列表保留供历史溯源 |

```
1. Scan  (⚠️ 当前无生产调用方 —— 仅 tests/unit/test_metric_promotion_service.py 覆盖；
           原文"每天 02:00 UTC 跑 / admin 端点手动触发"两个入口都不存在)
   └─ SELECT lower(trim(question)), count(*), min(created_time), max(created_time)
      FROM session_message
      WHERE created_time >= :since
        AND question IS NOT NULL
        AND role = 'assistant'          ← 只统计 assistant 消息（即 LLM 响应）
      GROUP BY lower(trim(question))
      HAVING count(*) >= :min_hits     ← 默认 3
      ORDER BY count(*) DESC
      ⚠️ 原文漏了 role='assistant' 与 question IS NOT NULL 两个过滤条件

2. For each frequent question (hits >= min_hits):
   └─ Upsert kpi_catalog（幂等，按 kpi_code）:
        kpi_code         = "AUTO_" + md5(semantic_key).hexdigest()[:12].upper()
                          ⚠️ 原文写 md5[:8]，真实是 12 位
        kpi_name         = f"Auto: {sample_question[:80]}"
        formula          = sample.sql_generated or ""   ← 无 sql_template 列
        semantic_keywords = _extract_keywords(sample_question)  ← 最多 5 个
        datasource_id    = NULL                          ← 刻意留空
        status           = DRAFT
        ⚠️ 原文写 question=<原问题> / source='auto_promotion' —— 两个列都不存在

3. Admin reviews at /api/v1/kpi-catalog
   └─ 发布  → status = PUBLISHED, 补 formula / datasource_id
                  next L1 hit 返回预定义 SQL（zero LLM cost）
   └─ 停用  → status = DEPRECATED，列表保留供历史溯源
   ⚠️ 原文写 ACTIVE / REJECTED，均不存在；且无 approve/reject 端点（见下表）

4. Active KPI hit at L1:
   └─ KpiSemanticMatchService.match() 命中 → chat_service._resolveCalculationFeature
      → 按 kpi.formula 找 FeatureDefinition → 执行其 calculation_logic
      ⚠️ 该路径当前 100% 失效，详见 kpi-metric-technical-form.md §9 T2
```

### Keyword Extraction

`semantic_keywords` 存储为 `TEXT[]`（PostgreSQL 数组）在 `kpi_catalog` 上：

```sql
-- 实际由 migration 建立（模型侧：ARRAY(String(64))，nullable）
```

关键词提取（`metric_promotion_service.py:35` `_extract_keywords`，**中文走 bigram，不是通用分词**）：

```python
def _extract_keywords(question: str, max_tokens: int = 5) -> list[str]:
    words: list[str] = []
    # 英文/数字：按空格/标点拆分，保留长度 >= 2 的片段
    for part in re.split(r"[\s,，、。！？]+", question):
        if len(part) >= 2:
            words.append(part)
    # 中文：2-char sliding window 取 bigram，去重后保序
    chinese_chars = [c for c in question if "一" <= c <= "鿿"]
    for i in range(len(chinese_chars) - 1):
        bigram = chinese_chars[i] + chinese_chars[i + 1]
        if bigram not in words:
            words.append(bigram)
    return words[:max_tokens]
```

> ⚠️ 原文给的 `tokenize()`（小写 + 去标点 + 中文停用词表）**与实现不符** —— 实现里没有停用词表，
> 中文一律切成 bigram（故长中文句的 Jaccard 交集天然很低，见下）。

### Jaccard Match (L1)

```python
# app/services/kpi_semantic_match_service.py:195 / :249
score = jaccard(tokenize(question), set(kpi.semantic_keywords))
if score >= threshold and score >= float(kpi.match_threshold):
    ...  # 命中
```

- **阈值是每条 KPI 自己的** `kpi_catalog.match_threshold`（`Numeric(3,2)`，列默认值 **0.75**）
- ⚠️ 原文写 "threshold = 0.4 (configurable via `KPI_MATCH_THRESHOLD` env var)" —— **无此环境变量**
- ⚠️ 原文的 `jaccard()` 伪码展开方向写反（真实实现是交集/并集，标准 Jaccard）
- **先走 exact-alias 快车道**（`:212` `_matchExactAlias`）：问句含 KPI code / 别名即 `confidence=1.0` 直接命中，
  不受 `match_threshold` 约束；Jaccard 是慢车道（`:226` `_matchByKeywords`）
- ⚠️ dev 库 `semantic_keywords` **12/12 全空** ⇒ Jaccard 恒不命中，实际只有 exact-alias 生效

### Admin Review API

> 路由前缀 `/api/v1/kpi-catalog`（`backend/app/main.py:525`），**不是**原文写的 `/admin/kpi-catalog`。
> 依赖 `mustChangePasswordGuard` + `rateLimitDep`。

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/v1/kpi-catalog | List all KPI（`status` 过滤） |
| GET | /api/v1/kpi-catalog/search | 语义搜索 |
| POST | /api/v1/kpi-catalog | 新建 |
| POST | /api/v1/kpi-catalog/bulk-bind-datasource | 批量绑数据源 |
| GET | /api/v1/kpi-catalog/{id} | 详情 |
| PUT | /api/v1/kpi-catalog/{id} | 更新（PUBLISHED 后仅修订，`revision_count+1`） |
| DELETE | /api/v1/kpi-catalog/{id} | 硬删 |
| GET | /api/v1/kpi-catalog/{id}/datasource-suggestions | 数据源候选 |
| GET | /api/v1/kpi-catalog/{id}/history | 修订历史 |
| GET | /api/v1/kpi-catalog/{id}/history/{revision} | 某版详情 |

> ⚠️ 原文列的 `/approve`、`/reject` 端点**不存在** —— 状态通过 `PUT /{id}` 的 `status` 字段流转。

### Data Freshness / TTL

- Scan window：由调用方 `since` 参数决定（服务层不写死 7 天）。
- 命中下限：`min_hits` 默认 **3**。
- ⚠️ 原文「DRAFT 条目 30 天过期（cron 置 `EXPIRED`）」**不存在** —— 无 `EXPIRED` 状态、无此 cron。
- ⚠️ 原文「拒绝后 30 天冷却期才可再晋升」**不存在** —— `auto_promote` 按 `kpi_code` 幂等，
  已存在的 code 直接返回既有 id，**不会**再写一条；但也**不会**自动复活被停用的 KPI。
- 晋升写出的 DRAFT KPI **`datasource_id` 刻意为 NULL**（`auto_promote` 注释明确说明），
  由 `scripts/backfill_kpi_datasource.py` 回填。

## RoutingMetricsService

> ⚠️ **2026-10-28 事实更正**：本节原文的类名、方法签名、数据类、聚合口径、端点**全部与实现不符**，已按
> `backend/app/services/routing_metrics_service.py` 重写。

聚合 `session_message.routing_layer` 的**全局时间窗**命中数 / 平均延迟 / 平均 token 成本。

> ⚠️ **不是**按 `session_id` 聚合 —— 真实实现没有 session 维度；原文的 `aggregate(session_id)` /
> `session_summary()` / `RoutingMetricsRow` / `SessionRoutingSummary` **均不存在**。

### Aggregation Query

```python
# app/services/routing_metrics_service.py:41
class RoutingMetricsService:
    def __init__(self, session: AsyncSession) -> None: ...

    async def get_snapshot(
        self, *, since: datetime, until: datetime | None = None
    ) -> RoutingMetricsSnapshot:
        if until is None:
            until = datetime.now(since.tzinfo or UTC)
        stmt = (
            select(
                SessionMessage.routing_layer.label("layer"),
                func.count(SessionMessage.id).label("hits"),
                func.avg(SessionMessage.latency_ms).label("avg_ms"),
                func.avg(SessionMessage.token_cost_usd).label("avg_cost"),
            )
            .where(SessionMessage.role == "assistant")
            .where(SessionMessage.created_time >= since)
            .where(SessionMessage.created_time <= until)
            .group_by(SessionMessage.routing_layer)
        )
```

- 走 SQLAlchemy `select()`（**不是**原文写的裸 `db.fetch`）
- ⚠️ 原文漏了 `role == "assistant"` 过滤
- 只保留 `_ROUTING_LAYERS = ("L1","L2","L3","L4")` 内的层；**L3 已于 2026-09-27 删除**
  （见 `nl2sql-engine.md` L3 段），故该层恒为 0 命中

### Data Classes

```python
@dataclass(frozen=True)
class LayerMetric:
    layer: str
    hit_count: int
    avg_duration_ms: float
    avg_token_cost: float

@dataclass(frozen=True)
class RoutingMetricsSnapshot:
    since: datetime
    until: datetime
    layer_distribution: list[LayerMetric]
    total_queries: int
    avg_total_duration_ms: float
```

- 4 层**每层必出现**（缺失补 0 命中占位），保证前端图表列稳定
- `avg_total_duration_ms` = 各层 `avg_duration_ms` 按命中数加权的均值（**不是**各层简单平均）
- 原文的 `avg_latency_ms` / `total_cost_usd` / `avg_cost_usd` 字段名**不存在**；成本只有 `avg_token_cost`

### Exposed Endpoints

**无。** ⚠️ 原文列的 `GET /sessions/{sessionId}/routing-metrics` 与
`GET /admin/routing-metrics/global` 两个端点**均不存在** —— `routing_metrics_service` 在
`backend/app/api/` 下零引用（仅 `tests/unit/test_routing_metrics_service.py` 覆盖）。
调用方需自行实例化 `RoutingMetricsService(session).get_snapshot(since=..., until=...)`。

## Cost Attribution

Each layer's token cost is recorded at the moment the layer produces a result (or fails):

```python
# 落库走 ORM（不是原文写的裸 db.execute + $layer 占位符）
# backend/app/services/chat_service.py:447
routing_layer=ROUTING_LAYER_L1, latency_ms=int((time.monotonic() - _t0) * 1000),
```

| 落库点 | routing_layer |
|---|---|
| `chat_service.py:447` | `L1`（KPI 直答，**0 LLM ⇒ token_cost 0**） |
| `chat_service.py:708` / `:1775`、`chat_multistep.py:574/733/948`、`chat_stream.py:598/836/1099/1376` | `L2` |
| `chat_l4.py:182` | `L4` |
| ⚠️ `ROUTING_LAYER_L3` | **常量已定义但全树无使用点** —— `chat_multistep` 路径落的是 `L2`；`routing_metrics_service` 的 L3 层恒 0 命中 |

> ⚠️ 原文示例把 `layer` 写死 `"L2"`，实际由路由决策决定（L1/L2/L4）。
> L1 有 0 token 成本（纯 Python 匹配，无 LLM 调用）；L2/L4 成本按多次 LLM 调用累加。
> `nl2sql-engine.md` 里 2026-09-27 删除的 "L3" 指的是**单轮 NL2SQL 内的链式步**，
> 与此处 `routing_layer='L3'`（多步持久化执行标签）**不是同一层**，不要混谈。

## Related Files

| File | Purpose |
|------|---------|
| `app/services/metric_promotion_service.py` | Scan + DRAFT upsert logic（**无生产调用方**，仅单测覆盖） |
| `app/services/routing_metrics_service.py` | 时间窗聚合（**无 API 暴露**，仅单测覆盖） |
| `app/services/kpi_semantic_match_service.py` | L1 匹配：exact-alias 快车道 + Jaccard 慢车道 |
| `app/domain/models.py:362` | `KpiCatalog`（`formula` / `semantic_keywords` / `match_threshold` / `metric_id`） |
| `app/domain/enums.py:279` | `KpiStatus`（DRAFT / PUBLISHED / DEPRECATED） |
| `app/api/v1/kpi_catalog.py` | KPI 目录端点（prefix `/api/v1/kpi-catalog`，`main.py:525`） |
| `app/services/chat_service.py:1499` | `_resolveCalculationFeature` —— L1 直通断点（100% 失效） |
| `migrations/versions/0051_add_routing_metrics_fields.py` | session_message routing 列（`routing_layer` / `latency_ms` / `token_cost_usd`） |
| `scripts/backfill_kpi_datasource.py` | 回填晋升 DRAFT KPI 的 `datasource_id` |
| `Harness/wiki/nl2sql-engine.md` | 4-layer routing overview |
| `Harness/wiki/kpi-metric-technical-form.md` | **指标技术形态治理**（口径权威链 + Tier A/B/C + 现状陷阱 T1–T7） |
| `Harness/wiki/agent-loop.md` | L4 纯 Python async while loop Agent detail（**原文写「L4 LangGraph」，agent-loop.md 整篇已重写为 async while loop**） |
