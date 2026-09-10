# Metric Pipeline (Phase 5)

Cold-metric auto-promotion from L2 routing logs + routing layer observability.

## MetricPromotionService

Scans `session_message` for frequently repeated L2 questions and promotes them to `kpi_catalog` as DRAFT entries for admin review.

### Workflow

```
1. Scan  (runs daily at 02:00 UTC, or on-demand via admin endpoint)
   └─ SELECT md5(question), question, COUNT(*)
      FROM session_message
      WHERE routing_layer = 'L2'
        AND created_time > now() - interval '7 days'
      GROUP BY md5(question), question
      HAVING COUNT(*) >= 3

2. For each frequent question (cnt >= 3/wk):
   └─ Upsert kpi_catalog:
        code     = AUTO_<md5[:8]>        (e.g., AUTO_a3f5c9d2)
        question = <original question>
        sql_template = NULL               (filled by admin)
        semantic_keywords = tokenize(question)
        status   = DRAFT
        source   = 'auto_promotion'

3. Admin reviews at /admin/kpi-catalog
   └─ Approve  → status = ACTIVE, fill sql_template
                  next L1 hit returns pre-defined SQL (zero LLM cost)
   └─ Reject   → status = REJECTED, excluded from future scans

4. Active KPI hit at L1:
   └─ KpiSemanticMatchService.match() returns sql_template
      → execute directly, bypass L2/L3/L4 entirely
```

### Keyword Extraction

`semantic_keywords` are stored as `TEXT[]` (PostgreSQL array) on `kpi_catalog`:

```sql
ALTER TABLE kpi_catalog ADD COLUMN semantic_keywords TEXT[];
```

Tokenization pipeline (`kpi_semantic_match_service.py`):

```python
def tokenize(text: str) -> list[str]:
    # lowercase, strip punctuation, remove stopwords
    tokens = re.sub(r'[^\w\s]', '', text.lower()).split()
    stopwords = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人'}
    return [t for t in tokens if t not in stopwords and len(t) > 1]
```

### Jaccard Match (L1)

```python
def jaccard(set_a: set, set_b: set) -> float:
    return len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0

# threshold = 0.4 (configurable via KPI_MATCH_THRESHOLD env var)
match = jaccard(tokenize(question), row.semantic_keywords) >= threshold
```

### Admin Review API

| Method | Path | Description |
|--------|------|-------------|
| GET | /admin/kpi-catalog | List all KPI (filter by status) |
| PUT | /admin/kpi-catalog/{id} | Update sql_template / status |
| POST | /admin/kpi-catalog/{id}/approve | status → ACTIVE |
| POST | /admin/kpi-catalog/{id}/reject | status → REJECTED |
| DELETE | /admin/kpi-catalog/{id} | Hard delete |

### Data Freshness / TTL

- Scan window: **7 days** rolling.
- DRAFT entries **expire after 30 days** if not reviewed (cron job sets status = EXPIRED).
- Re-promotion: after rejection, same md5 can be re-promoted only after 30-day cool-down.

## RoutingMetricsService

Aggregates `session_message.routing_layer` hits per layer for observability and cost attribution.

### Aggregation Query

```python
# app/services/routing_metrics_service.py
async def aggregate(session_id: str) -> RoutingMetrics:
    rows = await db.fetch("""
        SELECT
            routing_layer,
            COUNT(*)                           as hit_count,
            AVG(latency_ms)::INTEGER           as avg_latency_ms,
            SUM(token_cost_usd)                as total_cost_usd,
            AVG(token_cost_usd)                as avg_cost_usd
        FROM session_message
        WHERE session_id = $1
          AND routing_layer IS NOT NULL
        GROUP BY routing_layer
        ORDER BY hit_count DESC
    """, session_id)
    return [RoutingMetricsRow(**r) for r in rows]
```

### RoutingMetricsRow

```python
@dataclass(frozen=True)
class RoutingMetricsRow:
    layer: str           # L1 / L2 / L3 / L4
    hit_count: int
    avg_latency_ms: int
    total_cost_usd: float
    avg_cost_usd: float
```

### Session-level Summary

```python
async def session_summary(session_id: str) -> SessionRoutingSummary:
    rows = await aggregate(session_id)
    total = sum(r.hit_count for r in rows)
    return SessionRoutingSummary(
        total_queries=total,
        by_layer={r.layer: r.hit_count for r in rows},
        total_cost_usd=sum(r.total_cost_usd for r in rows),
        layers=[r for r in rows],
    )
```

### Exposed Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /sessions/{sessionId}/routing-metrics | Session-level routing summary |
| GET | /admin/routing-metrics/global | Global aggregation (all sessions, last 30 days) |

## Cost Attribution

Each layer's token cost is recorded at the moment the layer produces a result (or fails):

```python
# In chat_service._handleNl2SqlAgent
layer = "L2"   # determined by routing decision
latency_ms = int((time.time() - start_time) * 1000)
token_cost = token_usage.total_cost  # from TokenUsageService

await db.execute("""
    INSERT INTO session_message (..., routing_layer, latency_ms, token_cost_usd)
    VALUES (..., $layer, $latency_ms, $token_cost)
""")
```

L1 has zero token cost (pure Python Jaccard, no LLM call). L3/L4 costs accumulate across multiple LLM calls per question.

## Related Files

| File | Purpose |
|------|---------|
| `app/services/metric_promotion_service.py` | Scan + DRAFT upsert logic |
| `app/services/routing_metrics_service.py` | Aggregation queries |
| `app/services/kpi_semantic_match_service.py` | L1 Jaccard match + keyword tokenization |
| `migrations/versions/0051_add_routing_fields.py` | session_message routing columns |
| `Harness/wiki/nl2sql-engine.md` | 4-layer routing overview |
| `Harness/wiki/agent-loop.md` | L4 LangGraph Agent Loop detail |
