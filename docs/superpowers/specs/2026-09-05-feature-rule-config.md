# Feature Rule Config DB-Backed Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal**: Migrate hardcoded supplier risk rules (`DEFAULT_SUPPLIER_FEATURES` + `RISK_RULES` in `backend/app/services/supplier_360_service.py:50-55` and `supplier_risk_service.py:78-91`) to a **generic DB-backed Feature 规则引擎** with structured SSOT, tiered-threshold atomic rules, optional natural-language policy description, and an LLM-assisted form-fill at config-time. Admin can manage rules via `/admin/feature-rules` UI without code changes. Runtime evaluation is fully deterministic (no LLM in hot path).

**Architecture**: Three-layer:

1. **DB layer** (`feature_rule` + `feature_rule_threshold` tables + `FeatureRuleService` + `FeatureRuleRegistry` + admin UI) — business users manage `code / data_object / data_layer / target_level / feature_name / thresholds[] / policy_description / enabled`.
2. **Evaluator layer** (`FeatureRuleEvaluator.evaluate(...)` — pure synchronous function) — one rule ↔ N tiers (severity ladder); cross-rule aggregation = MAX severity. No DB / no LLM calls.
3. **NL assistant layer** (`POST /feature-rules/parse-description` — LLM-backed, config-time only) — admin writes natural-language policy → LLM returns suggested thresholds (validated against `feature_definition` whitelist + operator/severity enum) → admin reviews → clicks save to persist via normal `POST /feature-rules` flow. LLM never touches runtime.

**Tech Stack**: FastAPI + SQLAlchemy 2.x async + Alembic + Ant Design v5 + TypeScript + React + vitest. Reuses existing patterns from `feat-agent-tool-config-db`: Alembic + DTO + Service + Registry (`warmUp` + `invalidate` + `reloadOne` + `asyncio.Lock`) + admin page + `seed_*` upsert + `conftest` autouse `warmAgentCaches` fixture.

---

## 1. Background

### 1.1 Current state

`backend/app/services/supplier_360_service.py` holds a hardcoded tuple constant:

```python
DEFAULT_SUPPLIER_FEATURES: tuple[str, ...] = (
    "SUPPLIER_OTD_3M",
    "SUPPLIER_DEFECT_RATE_3M",
    "SUPPLIER_PRICE_VARIANCE_3M",
    "SUPPLIER_RISK_SCORE",
)
```

`backend/app/services/supplier_risk_service.py` holds a hardcoded dict:

```python
RISK_RULES: dict[str, _Rule] = {
    "SUPPLIER_RISK_SCORE":        _Rule("lt_inverse", "0.80", "综合风险评分"),
    "SUPPLIER_OTD_3M":            _Rule("lt",         "90",   "准时交付率"),
    "SUPPLIER_DEFECT_RATE_3M":    _Rule("gt",         "5",    "缺陷率"),
    "SUPPLIER_PRICE_VARIANCE_3M": _Rule("gt",         "10",   "价格偏差率"),
}
```

`SupplierRiskService._decideLevel()` (lines 140-187) implements a two-stage decision:
- **Main path**: `SUPPLIER_RISK_SCORE` 0-1 mapping (`<0.60 → HIGH`, `[0.60, 0.80) → MEDIUM`, `≥0.80 → LOW`).
- **Fallback**: violation count over the other 3 features (`≥3 → HIGH`, `=2 → MEDIUM`, `≤1 → LOW`).
- **Unknown**: all 4 features missing.

`Supplier360Service._safeLoadKpis()` uses `DEFAULT_SUPPLIER_FEATURES` to pre-load 4 KPI slots for every supplier view. Hard-coded unit test (`test_default_supplier_features_count`) enforces `len == 4`.

### 1.2 What's wrong

- **No admin manageability**: changing a risk threshold (e.g., OTD from 90 → 85) requires code change + deploy.
- **No KPI extensibility**: adding a 5th risk feature requires editing two Python constants in sync (high drift risk).
- **No cross-feature reuse**: `data_quality_evaluator` would need its own duplicate threshold infrastructure if it wanted to score SUPPLIER features.
- **No natural-language policy documentation**: business analysts cannot read the rule intent in plain Chinese.

### 1.3 Known gap, explicitly deferred

`Harness/changes/feat-supplier-risk-agent-mini/summary.md` documents: *"阈值不变更 → 不引入 DB 配置复杂度；**后续 Phase 6 Agent 平台可演进为 DB 配置**"*. We are now in Phase 6 maturity (post `feat-agent-tool-config-db`).

### 1.4 Recent precedents (already validated)

- `feat-agent-tool-config-db` — mirrors this change exactly for tool metadata: Alembic 0037 + `agent_tool_config` + `AgentToolConfigRegistry` + `/admin/tools` + conftest autouse `warmAgentCaches`. Reuse every pattern.
- `AgentBindingCache` (`agent_binding_cache.py`) — DB-backed in-memory cache with `warmUp()` + `invalidate()` + `refreshOne()` + asyncio.Lock.
- `NL2SqlService` — LLM-backed config-time helper with Pydantic structured output. Reuse LLM client + schema-validation pattern.
- `conftest.py` autouse fixture `warmAgentCaches` — must be extended to also warm up `feature_rule_registry` and call `commit()` after warmUp (commit d01a4e1 lesson).

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1)

1. Persist `RISK_RULES` + `DEFAULT_SUPPLIER_FEATURES` to DB as a generic Feature Rule engine.
2. One rule ↔ N atomic thresholds (tiered ladder, ordered by severity).
3. Runtime evaluation: per-rule tier-matched → cross-rule MAX severity (deterministic, sync, no LLM).
4. New `target_level` field isolates use cases: `RISK` (supplier_risk_agent), `QUALITY_SCORE` (data_quality_evaluator), `CUSTOM` (future).
5. Admin UI: `/admin/feature-rules` with list / create / edit drawer / delete / toggle / NL-assisted form fill / live preview.
6. NL assistant: `POST /feature-rules/parse-description` — LLM turns natural language into suggested thresholds, **does not write to DB**; admin reviews and saves manually.
7. Behavior parity: existing supplier risk agent outputs (HIGH/MEDIUM/LOW/UNKNOWN) must be **byte-identical** for any supplier KPI input combination (proven by `test_feature_rule_supplier_risk_parity.py`).
8. Conftest autouse warmUp extended to include `feature_rule_registry`.

### 2.2 Non-Goals (v1)

- ❌ Compound expressions (AND/OR trees, JSONLogic) — YAGNI for v1.
- ❌ Custom severity enum (admin cannot rename HIGH/MEDIUM/LOW) — YAGNI.
- ❌ LLM at runtime evaluation (non-deterministic, expensive) — explicitly rejected.
- ❌ Cross-tenant rule override (single-tenant deployment).
- ❌ DataQualityRuleModel internal evaluator rewrite — only `target_level="QUALITY_SCORE"` plumbing left as a hook (out of scope; future change).
- ❌ Rule import/export CSV — YAGNI.

### 2.3 Out of Scope

- KpiCatalog integration with the rule engine (architecture leaves room via `target_level` field).
- Redis-backed multi-instance cache (single-instance deployment).
- Per-department threshold overrides (e.g., procurement OTD<90, quality OTD<95).

---

## 3. Global Constraints

These bind every task. Repeating them verbatim from the project's rules:

- **File size**: 200-400 lines typical, **800 lines hard cap**. (CLAUDE.md)
- **Function size**: < 50 lines. (CLAUDE.md)
- **Nesting**: ≤ 4 levels (early returns). (CLAUDE.md)
- **Immutability**: always create new objects; never mutate in-place. (CLAUDE.md)
- **DB-only SSOT**: handler engines stay in code; **only metadata** moves to DB. (Harness/rules/编码规范.md)
- **SQL safety**: business queries SELECT-only via SQL Guard. (Harness/rules/数据治理.md)
- **Token accounting**: every LLM call records prompt/completion tokens + cost. (Harness/rules/AI治理.md)
- **TDD**: RED → GREEN → IMPROVE, coverage ≥ 80%. (Harness/rules/测试规范.md)
- **Real PG testing**: backend tests must use real PostgreSQL + full API chain (no sqlite in-memory + direct service call). (Harness/rules/测试规范.md)

---

## 4. Architecture

### 4.1 Component map

```
[ Admin User ]
    ↓ HTTP
[ AdminFeatureRulesPage ] ← /admin/feature-rules
    ↓
[ /api/v1/feature-rules ] ← FastAPI router (admin-only for writes)
    ↓
[ FeatureRuleService ] ← CRUD + audit (OutboxService) + LLM parse-description
    ↓                                       ↓
[ feature_rule / feature_rule_threshold tables ]    [ LLM Client ]
                                                          ↓ (parse-description only)
                                                    [ FeatureDefinition whitelist ]
                                                    [ Severity / Operator enum ]

[ Lifespan startup ]
    ↓
[ FeatureRuleRegistry.warmUp(session) ] ← caches enabled rules + thresholds
    ↓
[ FeatureRuleEvaluator.evaluate(data_object, data_layer, target_level, feature_values) ]
    ↓ (sync, pure, no DB / no LLM)
[ SupplierRiskService._decideLevel ] ← delegates to evaluator
[ DataQualityEvaluator ] ← future (target_level="QUALITY_SCORE")
```

### 4.2 Decision table (clarified during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Scope | **通用 Feature 规则引擎** | Single generic engine, multiple `target_level`s; enables future Agent/Quality/Kpi reuse |
| NL placement | **Structure SSOT + NL doc + LLM config-time auto-fill only** | Deterministic runtime + good admin UX, no LLM cost/latency in hot path |
| Expressiveness | **Atomic thresholds + tiered ladder (per-rule) + configurable severity enum** | Matches current `RISK_RULES` 1:1 in v1; leaves compound expressions as future change |
| Scope key | **`data_object` + `data_layer`** (not per-Agent) | Cross-Agent reuse; one SUPPLIER/FEATURE rule applies to all consumers |
| Aggregation | **MAX severity across rules** | Cleanest semantic; matches current RISK_SCORE main-path "highest tier wins" behavior |

---

## 5. Data Model (Alembic 0038)

### 5.1 `feature_rule` table

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | `BigInteger` | PK, autoincrement | |
| `code` | `String(64)` | NOT NULL, UNIQUE within (data_object, data_layer, target_level) | Business unique code, e.g. `supplier_risk_score_main` |
| `data_object` | `String(64)` | NOT NULL | e.g. `SUPPLIER` (aligns with `agent_access_policy.data_object`) |
| `data_layer` | `String(16)` | NOT NULL | `DIM` / `DWD` / `FEATURE` (aligns with `AgentTool.data_layers`) |
| `target_level` | `String(16)` | NOT NULL | `RISK` / `QUALITY_SCORE` / `CUSTOM` |
| `feature_name` | `String(64)` | NOT NULL, FK → `feature_definition.feature_name` | Which KPI this rule evaluates |
| `enabled` | `Boolean` | NOT NULL, default TRUE | Hot-toggle |
| `priority` | `Integer` | NOT NULL, default 100 | Tie-break for same scope (future use) |
| `policy_description` | `Text` | NULL | Natural-language policy text (admin-readable + LLM parse source) |
| `version` | `Integer` | NOT NULL, default 1 | Optimistic lock (bump on every UPDATE) |
| `created_time` | `DateTime(timezone=True)` | NOT NULL, default NOW | |
| `updated_time` | `DateTime(timezone=True)` | NOT NULL, default NOW, onupdate NOW | |

Unique: `(data_object, data_layer, target_level, code)`.
Index: `(data_object, data_layer, target_level, enabled)` for registry lookup.

### 5.2 `feature_rule_threshold` table

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | `BigInteger` | PK, autoincrement | |
| `rule_id` | `BigInteger` | NOT NULL, FK → `feature_rule.id` (ON DELETE CASCADE) | |
| `severity` | `String(16)` | NOT NULL | `HIGH` / `MEDIUM` / `LOW` / `INFO` (Python enum `Severity`) |
| `operator` | `String(16)` | NOT NULL | `lt` / `lte` / `gt` / `gte` / `lt_inverse` (Python enum `RuleOperator`) |
| `threshold_value` | `Numeric(20, 6)` | NOT NULL | Numeric threshold (preserve precision) |
| `unit` | `String(16)` | NULL | Display unit (e.g. `%`); not used in eval |
| `threshold_order` | `Integer` | NOT NULL, default 1 | Tie-break within same severity |

Unique: `(rule_id, severity)`.

### 5.3 Why two tables (header + ladder)?

- Clean separation: rule metadata (one row) vs. tier thresholds (N rows). Avoids JSONB-with-validation complexity.
- Optimized queries: registry lookup hits `feature_rule` index; threshold loader joins with single FK index.
- Future extensibility: a rule can have multiple tiers per severity without schema change (currently 1; cap at 1 for v1).

---

## 6. Runtime Engine

### 6.1 `FeatureRuleRegistry` (mirrors `AgentToolConfigRegistry`)

```python
class FeatureRuleRegistry:
    def __init__(self) -> None:
        self._rules: dict[tuple[str, str, str], list[FeatureRuleReady]] = {}  # (data_object, data_layer, target_level) -> rules
        self._loaded: bool = False
        self._lock = asyncio.Lock()

    async def warmUp(self, session: AsyncSession) -> None:
        rows = (await session.execute(
            select(FeatureRule).where(FeatureRule.enabled.is_(True))
        )).scalars().all()
        thresholds_by_rule = await self._loadAllThresholds(session, [r.id for r in rows])
        async with self._lock:
            self._rules = self._indexByScope(rows, thresholds_by_rule)
            self._loaded = True

    def getEnabledRules(
        self, data_object: str, data_layer: str, target_level: str
    ) -> list[FeatureRuleReady]:
        if not self._loaded:
            raise RuntimeError("FeatureRuleRegistry 未 warmUp（lifespan bug）")
        return self._rules.get((data_object, data_layer, target_level), [])

    def invalidate(self, rule_id: int | None = None) -> None:
        if not self._loaded:
            return
        if rule_id is None:
            self._rules.clear()
            return
        # Mark stale; reloadOne will repopulate
        # (cheap path: drop the entire scope, reload on next query)
        self._rules.clear()

    async def reloadOne(self, session: AsyncSession, rule_id: int) -> None:
        async with self._lock:
            row = (await session.execute(
                select(FeatureRule).where(FeatureRule.id == rule_id)
            )).scalar_one_or_none()
            if row is None or not row.enabled:
                # Drop the whole scope index; re-index on next query
                self._rules.clear()
                return
            thresholds = await self._loadAllThresholds(session, [rule_id])
            self._rules.clear()  # Simplest: invalidate whole; future optimization: per-scope
            await self.warmUp(session)  # Re-warm fully (safe, infrequent)

feature_rule_registry = FeatureRuleRegistry()  # module-level singleton
```

### 6.2 `FeatureRuleEvaluator` (pure synchronous)

```python
@dataclass(frozen=True)
class RuleHit:
    rule_code: str
    feature_name: str
    severity: Severity
    operator: str
    threshold_value: Decimal
    actual_value: Decimal

@dataclass(frozen=True)
class RuleEvaluation:
    matched_severity: Severity | None          # None = OK
    matched_severity_source: str               # "rule:<code>" or "no_match" / "no_rules"
    contributing_rules: list[RuleHit]          # All hits across rules (audit)

_SEVERITY_ORDER = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2, Severity.INFO: 3}

_OPERATORS = {
    "lt":         lambda v, t: v < t,
    "lte":        lambda v, t: v <= t,
    "gt":         lambda v, t: v > t,
    "gte":        lambda v, t: v >= t,
    "lt_inverse": lambda v, t: v < t,  # for 0-1 RISK_SCORE (lower is worse)
}

class FeatureRuleEvaluator:
    @staticmethod
    def evaluate(
        data_object: str,
        data_layer: str,
        target_level: str,
        feature_values: dict[str, Decimal | None],
    ) -> RuleEvaluation:
        rules = feature_rule_registry.getEnabledRules(data_object, data_layer, target_level)
        if not rules:
            return RuleEvaluation(None, "no_rules", [])

        hits: list[RuleHit] = []
        for rule in rules:
            value = feature_values.get(rule.feature_name)
            if value is None:
                continue  # Missing value: rule does not contribute (consistent with current passed=True default)

            for tier in sorted(rule.thresholds, key=lambda t: _SEVERITY_ORDER[t.severity]):
                if _OPERATORS[tier.operator](value, tier.threshold_value):
                    hits.append(RuleHit(
                        rule_code=rule.code,
                        feature_name=rule.feature_name,
                        severity=tier.severity,
                        operator=tier.operator,
                        threshold_value=tier.threshold_value,
                        actual_value=value,
                    ))
                    break  # First matching tier wins (severity-ordered)
            # No tier matched: rule does not contribute

        if not hits:
            return RuleEvaluation(None, "no_match", [])
        worst = min(hits, key=lambda h: _SEVERITY_ORDER[h.severity])
        return RuleEvaluation(worst.severity, f"rule:{worst.rule_code}", hits)
```

### 6.3 Integration with `SupplierRiskService` (RISK-priority parity wrapper)

```python
async def _decideLevel_via_rules(
    view: Supplier360Read,
    contributions: list[SupplierRiskKpiContribution],
) -> tuple[RiskLevel, str]:
    """New version: delegate to FeatureRuleEvaluator with RISK-priority bypass.

    Behavior equivalent to legacy _decideLevel (proven by parity test in
    test_feature_rule_supplier_risk_parity.py). The wrapper applies the
    legacy two-stage semantics on top of the generic evaluator:

      1. If RISK_SCORE rule matched any tier → return that tier's severity
         (bypass MAX aggregation; matches legacy main-path bypass).
      2. Else if all feature values missing → UNKNOWN.
      3. Else aggregate MAX severity across the OTHER 3 rules' matched tiers.
      4. Else (all values present, no rule matched) → LOW.

    The generic evaluator stays untouched (no SupplierRisk-specific logic);
    parity is enforced in this wrapper, keeping the rule engine reusable.
    """
    values: dict[str, Decimal | None] = {}
    for c in contributions:
        if c.value is None:
            continue
        try:
            values[c.feature_name] = Decimal(c.value)
        except (InvalidOperation, ValueError):
            continue  # Skip non-numeric (matches legacy passed=True default)

    # Evaluate ALL rules, then take per-rule best tier hit
    rules = feature_rule_registry.getEnabledRules(
        data_object="SUPPLIER", data_layer="FEATURE", target_level="RISK"
    )
    rule_hits: dict[str, RuleHit | None] = {}  # rule_code -> best tier hit (None = no match)
    for rule in rules:
        value = values.get(rule.feature_name)
        if value is None:
            rule_hits[rule.code] = None
            continue
        for tier in sorted(rule.thresholds, key=lambda t: _SEVERITY_ORDER[t.severity]):
            if _OPERATORS[tier.operator](value, tier.threshold_value):
                rule_hits[rule.code] = RuleHit(
                    rule_code=rule.code, feature_name=rule.feature_name,
                    severity=tier.severity, operator=tier.operator,
                    threshold_value=tier.threshold_value, actual_value=value,
                )
                break
        else:
            rule_hits[rule.code] = None

    # Step 1: RISK-priority bypass (RISK_SCORE rule)
    risk_score_hit = rule_hits.get("supplier_risk_score_main")
    if risk_score_hit is not None:
        return _severity_to_risk(risk_score_hit.severity), risk_score_hit.rule_code

    # Step 2: All values missing → UNKNOWN
    all_missing = all(c.value is None for c in contributions)
    if all_missing:
        return RiskLevel.UNKNOWN, "unknown"

    # Step 3: MAX severity across other matched rules
    other_hits = [h for h in rule_hits.values() if h is not None and h.rule_code != "supplier_risk_score_main"]
    if other_hits:
        worst = min(other_hits, key=lambda h: _SEVERITY_ORDER[h.severity])
        return _severity_to_risk(worst.severity), worst.rule_code

    # Step 4: All values present, no rule matched → LOW
    return RiskLevel.LOW, "no_match"
```

### 6.4 Integration with `Supplier360Service._safeLoadKpis`

Replace `DEFAULT_SUPPLIER_FEATURES` with derived set:

```python
def _kpiSlotFeatureNames(data_object: str = "SUPPLIER", data_layer: str = "FEATURE") -> tuple[str, ...]:
    """Aggregate all feature_name values from enabled rules across target_levels."""
    seen: set[str] = set()
    for target_level in ("RISK", "QUALITY_SCORE", "CUSTOM"):
        for rule in feature_rule_registry.getEnabledRules(data_object, data_layer, target_level):
            seen.add(rule.feature_name)
    return tuple(sorted(seen))
```

`_safeLoadKpis` iterates the derived tuple instead of the hardcoded one.

---

## 7. NL Assistant (config-time only)

### 7.1 Endpoint

`POST /api/v1/feature-rules/parse-description` (admin-only)

**Request**:
```json
{
  "data_object": "SUPPLIER",
  "data_layer": "FEATURE",
  "target_level": "RISK",
  "natural_language": "OTD 低于 90% 即视为高风险，价格偏差超过 15% 是中等风险，综合评分低于 0.60 直接 High..."
}
```

**Response (200)**:
```json
{
  "suggested_thresholds": [
    {"feature_name": "SUPPLIER_OTD_3M", "severity": "HIGH", "operator": "lt", "threshold_value": 90, "unit": "%", "confidence": 0.9, "rationale": "OTD 低于 90% → 高风险"},
    {"feature_name": "SUPPLIER_PRICE_VARIANCE_3M", "severity": "MEDIUM", "operator": "gt", "threshold_value": 15, "unit": "%", "confidence": 0.85, "rationale": "价格偏差 >15% → 中等"},
    {"feature_name": "SUPPLIER_RISK_SCORE", "severity": "HIGH", "operator": "lt_inverse", "threshold_value": 0.60, "confidence": 0.92, "rationale": "综合评分 <0.60 → 高"}
  ],
  "reasoning": "识别出 3 个 feature 的阈值建议...",
  "overall_confidence": 0.88,
  "warnings": ["未识别到 DEFECT_RATE 阈值描述，建议补充"]
}
```

**Response (503)**:
```json
{"error": "llm_unavailable", "message": "AI 辅助不可用，请手动填写 thresholds"}
```

### 7.2 Implementation

- Reuse `app.infrastructure.llm.base_client.BaseLlmClient` factory (same as `NL2SqlService`).
- System prompt includes:
  - Candidate `feature_name` list (loaded from `feature_definition WHERE data_object=? AND data_layer=?`).
  - Allowed `severity` enum values.
  - Allowed `operator` enum values.
  - Each feature's Chinese alias + unit (from `feature_definition.feature_alias` / `unit`).
- Pydantic schema for `suggested_thresholds` validates enum membership + threshold_value range.
- On any validation failure → return `LLMUnavailableError(503)` with a human-readable message; **no silent fallback to malformed data**.
- Token accounting: write an `OutboxService` event `entity_type="feature_rule" action="PARSE_DESCRIPTION"` with payload containing input NL + parsed suggestions + reasoning + actual matched feature count.

### 7.3 Flow guarantee

```
admin writes NL → POST /parse-description → LLM returns suggestions
  → admin sees "AI 建议" panel with confidence + reasoning
  → admin clicks "Apply to form" → suggestions prefill thresholds fields in edit drawer
  → admin edits/reviews → clicks "Save" → POST /feature-rules (normal CRUD path) → DB write + Outbox audit
```

**Critical guarantee**: `/parse-description` **never writes to DB**. The only path that persists is the normal CRUD `POST /feature-rules`. This makes the LLM assistant advisory only.

---

## 8. Admin UI

### 8.1 Route + ACL

- Route: `/admin/feature-rules` registered in `frontend/src/App.tsx`.
- ACL: menu entry in `scripts/seed_menu_config.py` requires `admin` role.
- Sidebar: under "管理 / Administration" group, sibling of `/admin/tools` and `/admin/agents`.

### 8.2 List page (`AdminFeatureRulesPage`)

Table columns:

| Code | Scope (data_object / data_layer / target_level) | feature_name | Ladder (compact: "HIGH≥90, MEDIUM≥80") | Enabled (Switch) | Priority | Updated | Actions |
|---|---|---|---|---|---|---|---|

- Filters: data_object / data_layer / target_level (Select), enabledOnly (toggle).
- Toggle enabled: Switch triggers `POST /feature-rules/{code}/toggle` + cache reload.
- Delete: Popconfirm + 409 referencing error toast.
- "新建" button → opens Create drawer.

### 8.3 Create / Edit drawer

Form fields:

- `code` (TextInput, required, unique)
- `data_object` (Select from `feature_definition.data_object` distinct list)
- `data_layer` (Select: DIM/DWD/FEATURE)
- `target_level` (Select: RISK/QUALITY_SCORE/CUSTOM)
- `feature_name` (Select from `feature_definition WHERE data_object+data_layer 匹配`)
- `enabled` (Switch, default true)
- `priority` (Number, default 100)
- `policy_description` (Textarea, multi-line, placeholder: "用自然语言描述本规则，例如：OTD 低于 90% 即视为高风险...")
- `thresholds` (Form.List, dynamic rows):
  - `severity` (Select: HIGH/MEDIUM/LOW/INFO)
  - `operator` (Select: lt/lte/gt/gte/lt_inverse)
  - `threshold_value` (NumberInput)
  - `unit` (TextInput, optional)
  - add/remove row buttons
- **"AI 辅助填写" button**: opens modal with policy_description Textarea → calls `POST /parse-description` → displays suggestions as a confirm card with confidence + reasoning + warnings → admin clicks "应用到表单" → suggestions prefill thresholds fields.
- **Live preview panel** (top of drawer): form fields auto-update a sample rule; admin can input a mock KPI value and see `matched_severity` (calls a local evaluator mirror; no API round-trip).

### 8.4 i18n

Namespace: `featureRules.{title, columns, actions, form, messages, errors}` in `frontend/src/i18n/{zh-CN,en-US}.ts`.

Error keys: `versionConflict` / `referencing` / `nameImmutable` / `codeImmutable` / `llmUnavailable` / `parseDescriptionSuccess` / `parseDescriptionFailed`.

### 8.5 Ant Design pitfalls (learned from `feat-agent-tool-config-db`)

- `Form.List` renders dynamically — no virtual list issues.
- `Select showSearch` with candidate list ≤ 50 items is fine; `data_object` and `data_layer` lists are short.
- Modal/Button inline whitespace: `(btn.textContent || "").replace(/\s+/g, "")` to collapse.
- `destroyOnClose` deprecated → use `destroyOnHidden`.

---

## 9. REST API

All paths under `/api/v1/feature-rules`. ACL: write endpoints use `getAdminOnlyActor`; read uses `getCurrentUser`.

| Method | Path | Behavior | Errors |
|---|---|---|---|
| GET | `/` | List with filters (`?data_object=&data_layer=&target_level=&enabledOnly=`) | — |
| GET | `/{code}` | Detail (includes nested thresholds) | 404 |
| POST | `/` | Create (requires version=1) | 409 (code conflict), 422 (validation), 422 (FK feature_name invalid) |
| PUT | `/{code}` | Update with `version` (optimistic lock) | 409 (versionConflict), 422, 404 |
| DELETE | `/{code}` | Delete (cascades thresholds; checks references) | 409 (referencing: AgentDefinition / DataQualityRule), 404 |
| POST | `/{code}/toggle` | Flip `enabled` + reloadOne | 404 |
| POST | `/parse-description` | LLM NL → suggestions (no DB write) | 503 (LLM unavailable), 422 (invalid request) |

### 9.1 DTOs

```python
class FeatureRuleThresholdRead(BaseModel):
    severity: Severity
    operator: RuleOperator
    threshold_value: Decimal
    unit: str | None
    threshold_order: int

class FeatureRuleRead(BaseModel):
    id: int
    code: str
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool
    priority: int
    policy_description: str | None
    version: int
    thresholds: list[FeatureRuleThresholdRead]
    created_time: datetime
    updated_time: datetime

class FeatureRuleCreate(BaseModel):
    code: str  # immutable after create
    data_object: str
    data_layer: str
    target_level: str
    feature_name: str
    enabled: bool = True
    priority: int = 100
    policy_description: str | None = None
    thresholds: list[FeatureRuleThresholdCreate]
    # Pydantic validators:
    # - thresholds non-empty
    # - severity values unique within thresholds
    # - feature_name exists in feature_definition with matching data_object+data_layer
    # - threshold_value is finite Decimal

class FeatureRuleUpdate(BaseModel):
    # code / data_object / data_layer / target_level / feature_name are IMMUTABLE
    enabled: bool | UnsetType
    priority: int | UnsetType
    policy_description: str | UnsetType
    thresholds: list[FeatureRuleThresholdCreate] | UnsetType
    version: int  # required for optimistic lock

class FeatureRuleParseDescriptionRequest(BaseModel):
    data_object: str
    data_layer: str
    target_level: str
    natural_language: str = Field(min_length=10, max_length=4000)

class FeatureRuleParseDescriptionResponse(BaseModel):
    suggested_thresholds: list[FeatureRuleThresholdSuggestion]
    reasoning: str
    overall_confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str]
```

### 9.2 Exception taxonomy

- `FeatureRuleNotFoundError(NotFoundError)` — 404
- `FeatureRuleVersionConflictError(ConflictError)` — 409 (with `current_version` in payload)
- `FeatureRuleReferencingError(ConflictError)` — 409 (with `referencing: [{type, code}, ...]`)
- `FeatureRuleValidationError(ValidationError)` — 422
- `LLMUnavailableError` — 503 (re-raised from `parse-description`)

`_testapp.py` exception handler maps `ConflictError` (parent class) to 409 — same pattern as `_ToolInUseConflict` in `feat-agent-tool-config-db`.

---

## 10. Migration & Seed

### 10.1 Alembic 0038

(see Section 5 for full schema)

### 10.2 Seed script `scripts/seed_feature_rules.py`

Idempotent upsert (mirrors `seed_agent_tool_configs.py`):

```python
FEATURE_RULE_SEEDS = [
    {
        # RISK_SCORE 阶梯：3 个 tier（HIGH 0.60, MEDIUM 0.80, LOW 1.01）
        # LOW tier 1.01 永远命中（值域 [0,1]），保证 RISK_SCORE ≥ 0.80 时仍能产出 LOW（字节级兼容）
        "code": "supplier_risk_score_main",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_RISK_SCORE",
        "thresholds": [
            {"severity": "HIGH",   "operator": "lt_inverse", "threshold_value": 0.60},
            {"severity": "MEDIUM", "operator": "lt_inverse", "threshold_value": 0.80},
            {"severity": "LOW",    "operator": "lt_inverse", "threshold_value": 1.01},
        ],
    },
    {
        "code": "supplier_otd_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_OTD_3M",
        "thresholds": [{"severity": "HIGH", "operator": "lt", "threshold_value": 90, "unit": "%"}],
    },
    {
        "code": "supplier_defect_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_DEFECT_RATE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 5, "unit": "%"}],
    },
    {
        "code": "supplier_price_var_high_risk",
        "data_object": "SUPPLIER",
        "data_layer": "FEATURE",
        "target_level": "RISK",
        "feature_name": "SUPPLIER_PRICE_VARIANCE_3M",
        "thresholds": [{"severity": "HIGH", "operator": "gt", "threshold_value": 10, "unit": "%"}],
    },
]
```

Lifespan calls `seedFeatureRules(session)` after Alembic upgrade.

### 10.3 `DEFAULT_SUPPLIER_FEATURES` removal

- Delete the constant from `supplier_360_service.py`.
- `_safeLoadKpis` derives slot list from `feature_rule_registry.getEnabledRules(...)`.
- Remove hardcoded unit test `test_default_supplier_features_count`; replace with `test_kpi_slot_feature_names_aggregates_from_registry`.

### 10.4 Behavior parity proof

| Scenario | Old `_decideLevel` | New evaluator |
|---|---|---|
| RISK_SCORE=0.55, others missing | HIGH (main path) | HIGH (RISK_SCORE tier 0.60=HIGH matched) |
| RISK_SCORE=0.70, OTD=85 | MEDIUM (main path) | MEDIUM (RISK_SCORE tier 0.80=MEDIUM matched; OTD rule also matched HIGH — MAX = HIGH!) |

⚠️ **Parity gap identified**: in the old code, the main RISK_SCORE path **bypasses the fallback count** — it returns directly based on RISK_SCORE alone. The MAX-severity aggregation in the new evaluator would combine with OTD rule violations.

**Resolution**: To preserve byte-identical parity, the new behavior must be: **the RISK_SCORE rule's matched severity is the final answer if it matched any tier; otherwise aggregate across others**. This is a slight semantic difference from pure MAX. We document this as a v1 design choice and discuss in the spec:

> **Aggregation rule v1**: For `target_level="RISK"`, RISK_SCORE feature has **priority** in determining the final severity: if its rule matches any tier, that tier's severity is the result (bypass aggregation). If RISK_SCORE rule doesn't match (or value missing), aggregate across the other 3 features with MAX severity. This preserves byte-identical parity with legacy `_decideLevel`.

This requires adding a special-case in `SupplierRiskService._decideLevel_via_rules` (not in the generic evaluator), keeping the evaluator generic.

**Revised parity table**:

| Scenario | Old | New (with RISK-priority) |
|---|---|---|
| RISK_SCORE=0.55, others missing | HIGH | HIGH (RISK_SCORE > 0.60=HIGH) |
| RISK_SCORE=0.70, OTD=85 | MEDIUM (RISK_SCORE main) | MEDIUM (RISK_SCORE 0.60-0.80=MEDIUM, bypass OTD) |
| RISK_SCORE=0.85, OTD=85 | LOW (RISK_SCORE main) | LOW (RISK_SCORE ≥0.80, no tier match; fallback: OTD rule HIGH matched → MAX=HIGH ❌ parity break) |

⚠️ **Real parity issue**: the legacy LOW case is "RISK_SCORE ≥ 0.80 means LOW". The MAX aggregation would conflict.

**Final resolution**: encode RISK_SCORE with a **LOW tier too** (≥0.80 → LOW), so the ladder covers all three buckets. Then the bypass logic + 3-tier RISK_SCORE rule gives byte-identical parity. This is the cleanest solution.

```python
# seed for SUPPLIER_RISK_SCORE: 3 tiers
[
    {"severity": "HIGH",   "operator": "lt_inverse", "threshold_value": 0.60},
    {"severity": "MEDIUM", "operator": "lt_inverse", "threshold_value": 0.80},
    {"severity": "LOW",    "operator": "lt_inverse", "threshold_value": 1.01},  # always true
]
```

With the LOW tier always matching, the RISK-priority bypass returns LOW when RISK_SCORE ≥ 0.80.

**Final parity table**:

| Scenario | Old | New (RISK-priority bypass + 3-tier RISK_SCORE) |
|---|---|---|
| RISK_SCORE=0.55, OTD=missing | HIGH | HIGH (RISK_SCORE tier HIGH matched, bypass) |
| RISK_SCORE=0.70, OTD=85 | MEDIUM | MEDIUM (RISK_SCORE tier MEDIUM matched, bypass) |
| RISK_SCORE=0.85, OTD=85 | LOW | LOW (RISK_SCORE tier LOW matched, bypass) |
| RISK_SCORE=missing, OTD=85 | HIGH (fallback count) | HIGH (RISK_SCORE missing → fallback: OTD HIGH via MAX) |
| RISK_SCORE=missing, OTD=92, DEFECT=3 | LOW (≤1 violation) | LOW (RISK_SCORE missing → fallback: no other rule matched → LOW? parity risk) |

⚠️ **Persistent parity gap**: when all values are missing or "OK", old returns `LOW` (no violation) or `UNKNOWN` (all missing). The new MAX aggregation would return `None → matched_severity=None`. Mapping `None → LOW` is a code-level decision in `_decideLevel_via_rules`. See Section 6.3.

**Final parity resolution**: `_decideLevel_via_rules` implements the legacy logic:
1. If RISK_SCORE rule matched any tier → return that severity (bypass).
2. Else if all feature values missing → UNKNOWN.
3. Else if any other rule matched → MAX severity across matched others.
4. Else (all values present, no rule matched) → LOW.

This produces byte-identical results to legacy `_decideLevel`. The `test_feature_rule_supplier_risk_parity.py` integration test asserts this on 6+ scenarios.

---

## 11. Testing Strategy

### 11.1 Unit tests

- `tests/unit/test_feature_rule_evaluator.py`:
  - Operator matrix (5 operators × boundary values)
  - Tier severity-ordered hit (HIGH first, then MEDIUM, etc.)
  - Cross-rule MAX aggregation
  - Missing value → rule skipped
  - Empty rule set → `no_rules`
  - No match → `no_match`
- `tests/unit/test_feature_rule_schemas.py`:
  - DTO field validation (threshold_value > 0 / operator enum / severity enum / code uniqueness / version required on update)
  - Immutable fields rejected on update (code / data_object / data_layer / target_level / feature_name)
- `tests/unit/test_feature_rule_service.py`:
  - CRUD methods (with mocked session)
  - Outbox audit write on each write op
  - Version conflict detection

### 11.2 Integration tests (real PG + full API chain)

- `tests/integration/test_feature_rule_api.py`: 17+ cases (CRUD + toggle + ACL admin-only + LLM unavailable 503 + referencing 409 + version 409 + validation 422)
- `tests/integration/test_feature_rule_runtime_db_driven.py`: registry warmUp + 写时失效 + runtime evaluator integration
- `tests/integration/test_feature_rule_supplier_risk_parity.py`: **CRITICAL** — 6+ scenarios (RISK_SCORE + 3 others, all combinations) prove byte-identical parity with legacy `_decideLevel`
- `tests/integration/test_feature_rule_audit.py`: CRUD + toggle + parse-description Outbox writes
- `tests/integration/test_feature_rule_llm_parse.py`: parse-description happy path (mock LLM) + 503 path (LLM raises)

### 11.3 Frontend tests

- `frontend/src/tests/AdminFeatureRulesPage.test.tsx`: 5 cases (render / 列渲染 / toggle Switch / delete Popconfirm / NL modal)
- `frontend/src/tests/featureRules.test.ts`: types + 6 API functions + parse-description client

### 11.4 Coverage target

Backend: ≥ 80% per `Harness/rules/测试规范.md`. Run:
```bash
cd backend && TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test \
  uv run pytest app/tests/ --cov=app --cov-fail-under=80
```

Frontend: `npx vitest run src/tests/AdminFeatureRulesPage.test.tsx src/tests/featureRules.test.ts`.

---

## 12. Risks & Mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | NL assistant LLM hallucination (invalid feature_name, illegal operator) | Pydantic schema validation + candidate feature_name list injected into system prompt + validation failure → 503 (fail-loud) |
| R2 | Deleting rule referenced by AgentDefinition / DataQualityRule causes runtime breakage | DELETE checks references + returns 409 `referencing` with list of consumers |
| R3 | Cache staleness after write | All write paths call `invalidate()` then `reloadOne()` (asyncio.Lock); registry indexes by `(data_object, data_layer, target_level)` |
| R4 | Seed-derived behavior diverges from legacy | `test_feature_rule_supplier_risk_parity.py` asserts byte-identical parity on 6+ scenarios |
| R5 | Conftest fixture shared transaction deadlock (lesson from `d01a4e1`) | Extend autouse `warmAgentCaches` with `feature_rule_registry.warmUp()` + `commit()` after |
| R6 | AntD virtual list limitation on long Select | Use `Form.List` for dynamic thresholds; Select with short candidate lists (data_object, data_layer) |
| R7 | DataQualityRuleModel internal evaluator not migrated (architectural debt) | `target_level="QUALITY_SCORE"` field reserved; DataQualityRuleModel change out of scope |
| R8 | Frontend menu out of sync with backend route | `seed_menu_config.py` must add `/admin/feature-rules` entry (per `qa-system-menu-config-seed` memory) |
| R9 | LLM token cost explosion (admin repeatedly clicks "AI 辅助填写") | Rate-limit `/parse-description` per admin actor (e.g., 30 req/min); display cumulative cost in UI |
| R10 | parse-description suggestions include features not in admin's intent (extra suggestions) | Admin reviews before apply; UI shows "确认 N 条建议应用到表单" with checkboxes |

---

## 13. Implementation Phases (high-level)

Phase 1 — DB & runtime engine (no UI, no NL):
1. Alembic 0038 (feature_rule + feature_rule_threshold)
2. Domain models + Severity / RuleOperator enums
3. DTOs (Create / Update / Read / ParseDescription)
4. Service (CRUD + audit + version)
5. Registry (warmUp / invalidate / reloadOne)
6. Evaluator (pure sync)
7. Seed script + lifespan integration
8. Conftest autouse update
9. SupplierRiskService delegation + Supplier360Service `_safeLoadKpis` refactor
10. Unit + integration tests + parity test

Phase 2 — REST API:
11. Router with ACL
12. Exception taxonomy + _testapp.py mapping

Phase 3 — Frontend:
13. types + API client + i18n
14. AdminFeatureRulesPage (list + drawer + Form.List)
15. AI assistant modal
16. Menu seed
17. Frontend tests

Phase 4 — Validation:
18. Full backend test suite + coverage gate
19. Full frontend test suite
20. Harness change summary + memory update

---

## 14. Out of Scope (explicit)

- DataQualityRuleModel internal evaluator rewrite
- JSONLogic compound expressions / DSL
- Admin-editable severity enum (rename HIGH/MEDIUM/LOW)
- KpiCatalog rule reuse via `target_level`
- CSV import/export
- Multi-instance cache sync (Redis)
- Per-department threshold overrides

---

## 15. Open Decisions Resolved

| Question | Resolution |
|---|---|
| Scope | Generic Feature 规则引擎 (RISK + QUALITY_SCORE + CUSTOM target_levels) |
| NL placement | Structured SSOT + NL doc + LLM config-time auto-fill only |
| Expressiveness | Atomic thresholds + tiered ladder + configurable severity enum |
| Scope key | data_object + data_layer (cross-Agent reuse) |
| Aggregation | MAX severity across rules (with RISK_SCORE priority bypass for parity) |
