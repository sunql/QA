# M0-P0.4 Milvus 3-Collection Refactor — Implementation Plan

> **Status**: SUPERSEDES `2026-09-29-m0-unified-id-development.md` Tasks 9-15
> **For agentic workers**: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> **Date**: 2026-09-29
> **Decision source**: human partner chose "重构为 3 collection" (refactor to 3 collections) when plan contradicted actual Milvus code architecture.

---

## Goal

Refactor Milvus ontology embeddings from single collection `ontology_embeddings` (with `type` field discriminator) to 3 separate collections: `ontology_class_embeddings`, `ontology_property_embeddings`, `ontology_metric_embeddings`. Then backfill `external_id` (unified_id) into each, with reconcile against PG `id_mapping` to verify consistency.

**Why**: human partner chose to follow original plan architecture. Aligns with plan-as-written, enables cleaner per-type schema evolution.

## Architecture

**Before**:
- 1 collection `ontology_embeddings` with `type` field (`class`/`property`/`metric`)
- `id_mapping.milvus_collection` = "ontology_embeddings" (single value)

**After**:
- 3 collections: `ontology_class_embeddings`, `ontology_property_embeddings`, `ontology_metric_embeddings`
- `id_mapping.milvus_collection` discriminates per row
- New `external_id` field on each new collection (= unified_id, to be backfilled)

## Tech Stack

- pymilvus (existing, v2.4.6 per docker image `qa-milvus`)
- SQLAlchemy async (existing)
- TDD per task (RED → GREEN → IMPROVE)
- Real Milvus (container `qa-milvus:19530`), no mocks

---

## Global Constraints

[Project-wide requirements, verbatim from `Harness/CLAUDE.md` and `CLAUDE.md`]

1. **不可变数据** — always create new objects, never mutate.
2. **真实数据库测试** — must use real Milvus (qa-milvus:19530) + real PG (qa_metadata_test:5434). No mocks.
3. **SQL 安全** — `:param` named bindings, no f-string into SQL.
4. **TDD** — RED → GREEN → IMPROVE per task, coverage ≥80%.
5. **小文件** — 200-400 lines typical, 800 max; functions <50 lines; nesting ≤4.
6. **显式错误处理** — each layer handles errors explicitly.
7. **编码约定** — camelCase 函数/变量; PascalCase 类型; UPPER_SNAKE_CASE 常量; ORM/Pydantic snake_case (deliberate).

**Dual-write window** — during refactor transition, new writes go to BOTH old `ontology_embeddings` AND the appropriate new collection. Once all callers migrated + data migration complete + backfill external_id verified, can drop old collection.

---

## File Structure

| File | Action | Lines target |
|------|--------|--------------|
| `backend/app/infrastructure/milvus_client.py` | Modify (extend with 3-collection API; keep old API for dual-write) | +200-400 lines (current ~620) |
| `backend/scripts/migrate_ontology_embeddings_to_3_collections.py` | Create | ≤200 lines |
| `backend/scripts/backfill_milvus_external_id.py` | Create | ≤200 lines |
| `backend/scripts/reconcile_milvus_id_mapping.py` | Create | ≤200 lines |
| `backend/app/tests/integration/conftest_milvus.py` | Create | ≤100 lines (merged into existing conftest.py) |
| `backend/app/tests/integration/test_reconcile_milvus_id_mapping.py` | Create | ≤200 lines |
| `backend/app/tests/integration/test_backfill_milvus_external_id.py` | Create | ≤200 lines (or merged with above) |
| `~/.claude/projects/.../memory/qa-system-milvus-3-collection-refactor.md` | Created (already exists) | (decision memo) |

---

## Task Decomposition (15 tasks)

### Phase 2-A: Milvus schema + dual-write (Tasks 1-3)

#### Task 1: TDD RED — Milvus collection fixtures

**Files**:
- Modify: `backend/app/tests/integration/conftest.py` (merge Neo4j fixtures pattern)

**Steps**:
1. Write fixture `milvusCleanClient` (yield connected client, teardown drops 3 collections).
2. Write fixture `milvusSeedOntology` (depends on `milvusCleanClient`, seeds 3 ontology rows in `ontology_embeddings` with type=class).
3. Write fixture `milvusSeedAllTypes` (seeds 1 class + 1 property + 1 metric).
4. Run pytest — fixtures should be importable, no test functions yet.

**Verify**: `pytest --fixtures app/tests/integration/conftest.py | grep -E "milvus"` → 3 fixtures visible.

**Commit**: `test: Milvus 真实库测试夹具（清空 + seed ontology）`

**Out of scope**: actual test cases (Task 4+).

#### Task 2: TDD RED — `external_id` field on new collections

**Files**:
- Modify: `backend/app/infrastructure/milvus_client.py` (add `_classFields`, `_propertyFields`, `_metricFields` schemas with `external_id` VARCHAR field)

**Steps**:
1. Write test asserting that `ensureClassCollection()` creates a collection with `external_id` field.
2. Run pytest — RED (no new collections exist yet).
3. Implement `_classFields()`, `_propertyFields()`, `_metricFields()` (each returns base ontology fields + `external_id` VARCHAR max_length=128).
4. Implement `ensureClassCollection()`, `ensurePropertyCollection()`, `ensureMetricCollection()` (parallel to existing `ensureCollection()`).
5. Run pytest — GREEN.

**Verify**: 3 new tests PASS; `wc -l milvus_client.py` ≤800 lines.

**Commit**: `feat(milvus): 3-collection schema with external_id field (class/property/metric)`

**Out of scope**: dual-write logic (Task 3), data migration (Task 5).

#### Task 3: TDD — dual-write API

**Files**:
- Modify: `backend/app/infrastructure/milvus_client.py` (add `_dualWriteInsert(type, records)` helper)

**Steps**:
1. Write test: call `insertEmbeddingsDual([{type:"class", ...}])` → row appears in BOTH `ontology_embeddings` AND `ontology_class_embeddings`.
2. RED (function doesn't exist).
3. Implement `insertEmbeddingsDual(records)`: routes each record by `type` to appropriate new collection, AND inserts into old collection.
4. Keep existing `insertEmbeddings()` unchanged for backward compat (dual-write opt-in via new function).
5. GREEN.

**Verify**: dual-write test PASS; existing `insertEmbeddings()` tests still PASS (backward compat).

**Commit**: `feat(milvus): dual-write insertEmbeddingsDual (new + old collection)`

**Out of scope**: migrating old callers (Task 11); backfill (Task 7).

### Phase 2-B: Data migration + backfill external_id (Tasks 4-7)

#### Task 4: TDD RED — reconcile Milvus tests (3-collection)

**Files**:
- Create: `backend/app/tests/integration/test_reconcile_milvus_id_mapping.py`

**Steps**:
1. Write 4 reconcile tests (mirror Neo4j tests structure):
   - clean align (PG ↔ all 3 collections)
   - placeholder write (Milvus has row, PG missing)
   - warning (PG has row, Milvus missing)
   - unified_id mismatch
2. Run pytest — RED (no `scripts.reconcile_milvus_id_mapping` module yet).
3. Don't commit RED state per TDD discipline.

**Verify**: 4 tests FAIL with `ModuleNotFoundError`.

**Commit**: none (RED state, will commit in Task 6 after impl).

#### Task 5: Data migration script

**Files**:
- Create: `backend/scripts/migrate_ontology_embeddings_to_3_collections.py` (≤200 lines)

**Steps**:
1. Run against real Milvus:
   - Read all rows from `ontology_embeddings` via `listAllEmbeddings()`.
   - Group by `type`.
   - For each row, INSERT into appropriate new collection (class/property/metric).
   - Skip if already exists (idempotent via ontology_id primary key check).
2. Print counts: read old vs read new per type.
3. Don't drop old collection (dual-write window).

**Verify**: After running, total rows across 3 new collections = total rows in old collection.

**Commit**: `feat(milvus): data migration script (ontology_embeddings → 3 collections)`

**Note**: This is a SANITY CHECK task (no code review). Similar to Task 6 in Phase 1.

#### Task 6: Implement reconcile_milvus_id_mapping.py

**Files**:
- Create: `backend/scripts/reconcile_milvus_id_mapping.py` (≤200 lines)

**Steps**:
1. Implement `reconcile(session: AsyncSession, client: MilvusClient) -> ReconcileReport`:
   - Read all 3 collections.
   - Cross-reference PG `id_mapping` by `(business_object, external_id)` + `milvus_collection`.
   - Same report structure as Neo4j reconcile: `placeholder_written`, `pg_only_warning`, `unified_id_mismatch`, `placeholder_failed`.
2. Run 4 RED tests from Task 4 → GREEN.

**Verify**: 4/4 PASS; reconcile diff_count=0 on a clean fixture.

**Commit**: `feat(milvus): reconcile_milvus_id_mapping (PG ↔ 3 collections)`

**Out of scope**: backfill (Task 7).

#### Task 7: TDD RED + GREEN — backfill external_id

**Files**:
- Create: `backend/scripts/backfill_milvus_external_id.py` (≤200 lines)
- Append RED tests to `test_reconcile_milvus_id_mapping.py` (or create separate file)

**Steps**:
1. Write 3 backfill tests:
   - writes external_id for rows missing it
   - idempotent re-run (written=0)
   - backfill → reconcile diff=0
2. RED.
3. Implement `backfill(session, client) -> int`:
   - For each row in 3 collections without `external_id`, look up PG `id_mapping` by `(business_object, external_id)`.
   - If found, use existing `unified_id`; else generate placeholder `obj:{type}:{ontology_id}` and INSERT PG.
   - UPSERT `external_id` field in Milvus.
4. GREEN.

**Verify**: 3 backfill + 4 reconcile tests all PASS.

**Commit**: `feat(milvus): backfill_milvus_external_id (writes unified_id to 3 collections)`

### Phase 2-C: Caller migration + reviewer gate (Tasks 8-10)

#### Task 8: Migrate existing callers to new 3-collection API

**Files**:
- Modify: any caller of `insertEmbeddings`, `searchByEmbedding`, `listAllEmbeddings`, `deleteByOntologyId` that should now use type-specific versions.

**Steps**:
1. Find all callers (grep `from app.infrastructure.milvus_client import`).
2. For each caller, decide: switch to 3-collection API OR keep dual-write (during transition).
3. Update caller signatures; ensure existing tests still pass.

**Verify**: `grep -rn "insertEmbeddings\|searchByEmbedding" backend/app/` → only callers + tests show usage.

**Commit**: `refactor(milvus): migrate callers to type-specific API (with dual-write fallback)`

#### Task 9: code-reviewer + security-reviewer 闸门（Phase 2）

Same gate as Phase 1 Task 7. Run both reviewers in parallel on the 3 scripts + milvus_client.py changes.

#### Task 10: Phase 2 整体覆盖率 + memory 更新

- Full pytest with coverage on `scripts.reconcile_milvus_id_mapping` + `scripts.backfill_milvus_external_id` + `scripts.migrate_ontology_embeddings_to_3_collections` + `app.infrastructure.milvus_client`.
- Update existing memory `qa-system-milvus-3-collection-refactor.md` with concrete schema facts + commit hashes.
- Append index entry.

### Phase 2-D: Cleanup + decision (Tasks 11-15)

#### Task 11: Verify dual-write completeness

- Run reconcile against real Milvus + PG.
- Confirm 0 rows missing across 3 collections.
- Confirm all new code paths use 3-collection API.

#### Task 12: Decision gate — drop old collection?

- **STOP HERE** for human partner review.
- Present: counts, reconcile diff=0, all callers migrated.
- Ask: drop old `ontology_embeddings` collection? OR keep indefinitely?
- This is a destructive decision; do not auto-execute.

#### Tasks 13-15: Reserved for follow-up decisions

(Task 12's decision determines whether Tasks 13-15 are needed.)

---

## Quality Gates

Per task: RED → GREEN → IMPROVE → review → fix loop (≤5 rounds).
Per Phase: code-reviewer + security-reviewer gate.
Per project: coverage ≥80%.

---

## Out of Scope

- `wiki_page_embeddings`, `query_embeddings`, `document_embeddings` — separate Milvus collections, untouched.
- `ontology_embeddings` deletion — gated on Task 12 human decision.
- Production Milvus data validation — done via Task 11 reconcile.
- Alembic migration — N/A (Milvus has no SQL schema; fields defined in app code).

---

## Risks

| Risk | Mitigation |
|------|------------|
| Existing `ontology_embeddings` data loss during migration | Idempotent migration; skip-if-exists; don't drop old collection until Task 12 |
| Dual-write inconsistency | Add a counter / verification step in Task 11 |
| New `external_id` field too narrow (128 chars) | Same as `unified_id` VARCHAR(128) in id_mapping; consistent |
| Backfill reads stale PG data | Use same async session + commit pattern as Neo4j backfill (savepoint per row) |

---

## Sequencing Notes

This plan supersedes Tasks 9-15 of the original `2026-09-29-m0-unified-id-development.md`. Phase 1 (Tasks 1-8 of original) is complete and unaffected. After Phase 2 completion, resume with Phase 3 (PR merges) which is Tasks 16-19 of the original plan.