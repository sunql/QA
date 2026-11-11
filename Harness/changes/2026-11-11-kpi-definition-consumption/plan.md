# KPI 口径消费 P0 止血 — 实施计划（按天）

> 分支 `feat/kpi-definition-consumption`，worktree `/private/tmp/qa-kpi-p0`，base `main@55259d67`。
> 设计稿：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`（🟢 APPROVED）。
> **本批零 DDL、只写本地 dev 库（5433 `qa_metadata`）、不碰生产、不部署。**
> 执行方式：逐任务 TDD（RED → GREEN → REFACTOR），每任务一次 `code-reviewer` 复核。

## 0. 验收（Definition of Done）

1. `backend/scripts/kpi_normalize_governance.py` dry-run 在 dev 库打印 12 条 before/after；
   `--apply` 后 **8 条 pass / 4 条 unknown / 0 mismatch**，8 条 `isTierAEligible=True`。
2. `GET /api/v1/admin/kpi/diagnose?question=...` 可用：admin 200、非 admin 403；
   返回命中方式、verdict/reasons、注入块是否为空、Tier A 可执行性。
3. 开关打开时，含 KPI code 的问句契约块非空（集成测试断言渲染文本，不只断言「有输出」）。
4. 定向回归净新增失败 0；`merge-gate / failure-set-diff` 净新增 0；不部署。
5. `Harness/changes/2026-11-11-kpi-definition-consumption/summary.md` 记录 before/after 与证据。

## 1. 根因（已实证，勿再猜）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 12/12 mismatch 的写入源头是回填脚本把**业务名**写进治理字段 | `backend/scripts/kpi_metric_backfill_apply.py:461-470` `_governanceValues` 写 `plan.governance*` |
| 2 | 对账层 2 只比较 `kpi_catalog` 的结构化字段 ⇄ `ontology_metric` 的 measure/formula，**不解析本体活类** | `backend/app/services/kpi_metric_reconcile.py:298-316`；`kpi_metric_spec.py:26-48` |
| 3 | 注入门禁只拦 `mismatch`；`unknown` 放行 | `backend/app/services/kpi_contract_builder.py:348` |
| 4 | MEASURE 型 6–8 的 `numerator_measure` 为空；RATIO 型 9 两侧都为空 | dev 库实测 |
| 5 | 4 条 NONE 没有可执行技术形态（物理源不存在） | metric 10–13 `execution_source=NONE` |
| 6 | 既有 `test_kpi_metric_backfill_apply.py:398/433` 钉住「回填后仍是 mismatch」 | 与本批验收冲突，见 D3-3 |

> 关于「源类被 tombstone」：`reconcileMetric` 不读活类，只读 measure JSON 与 formula；
> tombstone 只影响契约渲染里的源表清单，**不阻塞 P0**。P3 resolver 才需要活类。

## 2. 不变量与规范化规则（唯一事实源）

### 2.1 规则（脚本按此实现，禁止另写比较逻辑）

- 复用 `governanceSpecOf` / `techniqueOf` / `reconcileMetric`，**不得重写对账**。
- `execution_source == NONE` ⇒ 结构化字段全部清空（`[]`），verdict 变 `unknown`，Tier C；
  业务文本列（`numerator`/`denominator`/`grain`/`business_definition`）**不动**。
- `numerator_measure` 与 `denominator_measure` 都存在 ⇒ 治理列 := 两侧 measure `expr` 的列集合。
- `metric_type == MEASURE` 且 measure 缺失 ⇒ 从 `formula` 推导：
  外层聚合函数（`SUM/AVG/COUNT…`）+ 参数体去别名前缀、拆 `TRUNC(...)` 包裹 → `expr`；
  治理分子列 := 该 `expr` 的列集合。
- `metric_type == RATIO` 且 measure 缺失 ⇒ 取外层聚合参数体，按**顶层唯一的 `/`** 拆分：
  左 → `numerator_measure.expr`，右 → `denominator_measure.expr`。
- `grain_keys` := `ontology_metric.grain_keys`（技术粒度键；时间维在 P3 `time_grain` 落地，P0 不表达）。
- 所有列名写**大写**（`_tokens` 比较时统一小写，大小写不影响判定，但写入要符合既有数据风格）。
- 每个派生列必须能在该 metric 的 `ontology_metric_source → ontology_property.source_column`
  里找到（物理列校验）；找不到 ⇒ 拒绝写库并打印原因。

### 2.2 目标值（离线仿真已实测：8 pass / 4 unknown / 0 mismatch）

| KPI | metric | numerator_columns | denominator_columns | grain_keys | measure 变更 |
|---|---|---|---|---|---|
| KPI_SUPPLIER_OTD | 2 | `RECEIPT_DATE` | `PROMISED_RECEIPT_DATE` | `SUPPLIER_CODE,FACILITY_CODE` | — |
| KPI_SUPPLIER_OVERDUE_RATIO | 3 | `RECEIPT_DATE` | `PROMISED_RECEIPT_DATE` | `SUPPLIER_CODE,FACILITY_CODE` | — |
| KPI_PAYMENT_ON_TIME | 4 | `VALUE_DATE` | `DUE_DATE` | `SUPPLIER_CODE` | — |
| KPI_INVOICE_MATCH_RATE | 5 | `LINE_AMOUNT_EXCL_TAX` | `AMOUNT_EXCL_TAX` | `SUPPLIER_CODE,INVOICE_DATE` | — |
| KPI_PURCHASE_CYCLE_TIME | 6 | `CREATION_DATE,RECEIPT_DATE` | — | `SUPPLIER_CODE,FACILITY_CODE` | num `{agg:AVG, expr:"(RECEIPT_DATE) - (CREATION_DATE)"}` |
| KPI_SUPPLIER_DELAY_DAYS | 7 | `PROMISED_RECEIPT_DATE,RECEIPT_DATE` | — | `SUPPLIER_CODE,FACILITY_CODE` | num `{agg:AVG, expr:"(RECEIPT_DATE) - (PROMISED_RECEIPT_DATE)"}` |
| KPI_COST_SAVING | 8 | `ORDER_QTY,UNIT_PRICE` | — | `SUPPLIER_CODE,MATERIAL_CODE` | num `{agg:SUM, expr:"(UNIT_PRICE - UNIT_PRICE) * ORDER_QTY"}` |
| KPI_PURCHASE_PRICE_VARIANCE | 9 | `UNIT_PRICE` | `UNIT_PRICE` | `MATERIAL_CODE,FACILITY_CODE` | num `{agg:AVG, expr:"(UNIT_PRICE - UNIT_PRICE)"}`；den `{agg:AVG, expr:"NULLIF(UNIT_PRICE, 0)"}` |
| KPI_SUPPLIER_MAINTENANCE_RATIO | 10 | `[]` | `[]` | `[]` | 不变（NONE） |
| KPI_SUPPLIER_DEFECT_RATE | 11 | `[]` | `[]` | `[]` | 不变（NONE） |
| KPI_SUPPLIER_FPY | 12 | `[]` | `[]` | `[]` | 不变（NONE） |
| KPI_SUPPLIER_NCR_RATE | 13 | `[]` | `[]` | `[]` | 不变（NONE） |

> 9 号分子/分母物理上都是 `UNIT_PRICE`（订单价 vs 价目表价），列级集合无法区分；
> 业务文本 `numerator`/`denominator` 保留原文，P3 resolver 恢复类级区分。报告中必须写明这一点。

## 3. D1（第 1 天）— 规范化纯逻辑 + dry-run

交付物：脚本 dry-run 在 dev 打印 12 条 before/after；纯逻辑单测全绿；**不写库**。

### D1-1 纯函数 + 单元测试（TDD）

**文件**
- 新建：`backend/scripts/kpi_normalize_governance.py`
- 新建：`backend/app/tests/unit/test_kpi_normalize_governance.py`

**接口**
```python
@dataclass(frozen=True)
class MeasurePlan:            # 可直接落库的 measure JSON
    agg: str
    expr: str | None = None

@dataclass(frozen=True)
class NormalizationPlan:
    kpi_code: str
    metric_id: int | None
    tier: str                      # "A" / "B" / "C"
    basis: str                     # measure_json / formula_split_measure / formula_split_ratio / none_tier_c
    numerator_columns: tuple[str, ...]
    denominator_columns: tuple[str, ...]
    grain_keys: tuple[str, ...]
    numerator_measure: dict | None   # None = 不改
    denominator_measure: dict | None
    before: dict                     # 原值快照（报告 + 回滚依据）
    business_todo: str | None        # NONE 的业务待补项（取自 metric.formula 口径声明）

def deriveNormalization(kpi, metric, class_columns: frozenset[str]) -> NormalizationPlan
def planNormalizations(kpis, metrics_by_id, class_columns_by_metric) -> list[NormalizationPlan]
def renderReport(plans) -> str
```

**RED（单元测试，先写）**
1. `KPI_SUPPLIER_OTD`：measure 已存在 ⇒ 治理列 = measure 列（大写）、grain = 技术 grain、measure 不变。
2. `KPI_PURCHASE_CYCLE_TIME`：MEASURE 缺 measure ⇒ 从上表推导出 `AVG` + `(RECEIPT_DATE) - (CREATION_DATE)`，列 = `CREATION_DATE,RECEIPT_DATE`。
3. `KPI_PURCHASE_PRICE_VARIANCE`：RATIO 缺两侧 ⇒ 顶层 `/` 拆分，分子 `(UNIT_PRICE - UNIT_PRICE)`、分母 `NULLIF(UNIT_PRICE, 0)`。
4. `KPI_SUPPLIER_DEFECT_RATE`（NONE）：全部 `[]`、`tier="C"`、`business_todo` 非空。
5. 物理列校验：构造一个「formula 引用 `NO_SUCH_COL`」的假 metric ⇒ `deriveNormalization` 抛 `NormalizationError`（脚本退出码 2）。
6. 幂等：把 plan 的结果喂回 `deriveNormalization` ⇒ 各字段与 before 相同（`is_noop=True`）。
7. 反向守卫（防假绿）：把目标列集合手动改错 1 列（测试里 monkeypatch 派生结果）⇒ 对账必须 `mismatch`。

**GREEN**：按 §2.1 实现（可参考 `/tmp/kpi_p0_sim.py` 的已验证逻辑，但必须写成纯函数、无 IO）。

**验证命令**
```bash
cd /private/tmp/qa-kpi-p0/backend
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
pytest app/tests/unit/test_kpi_normalize_governance.py -q
```

### D1-2 读库 + 报告（无写）

- `loadSnapshot(session)`：一次 `select(KpiCatalog)`、一次 `select(OntologyMetric)`、
  一次 `select(OntologyMetricSource, OntologyProperty)` 建 `class_columns_by_metric`（按 metric_id 聚合）。
  **不要逐条查**（避免 N+1）。
- `main()`：`--database-url` 必填（不读 `DATABASE_URL`）；`assertSafeTarget(url)`；
  `SELECT current_database()` 复核；打印 `host/port/db` 与开关状态无关。
- 报告格式（stdout，UTF-8）：
  ```
  [1/12] KPI_SUPPLIER_OTD (metric=2) before=mismatch after=pass tier=A basis=measure_json
      num  ['ACTUAL_RECEIPT_DATE'] -> ['RECEIPT_DATE']
      den  ['PROMISED_DATE'] -> ['PROMISED_RECEIPT_DATE']
      grain ['供应商','工厂','月'] -> ['SUPPLIER_CODE','FACILITY_CODE']
  ...
  summary: pass=8 unknown=4 mismatch=0 (target mismatch=0)
  ```
- 若目标状态出现 `mismatch` ⇒ 打印原因并以 **退出码 1** 结束（拒绝继续）。

### D1-3 dev 库 dry-run 取证

```bash
python3 scripts/kpi_normalize_governance.py \
  --database-url 'postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5433/qa_metadata'
```
预期：12 行 `before=mismatch`、`after` = 8 pass + 4 unknown、`summary` mismatch=0。
把输出全文贴进 `summary.md`（D3）。

**D1 收口**：`code-reviewer` 复核；提交 `feat: KPI 治理规范化规则与 dry-run 报告（P0）`。

---

## 4. D2（第 2 天）— 诊断接口

交付物：`GET /api/v1/admin/kpi/diagnose?question=...` + 真实 PG HTTP 集成测试。

### D2-1 `KpiMatchResult.method`（小改动，可解释性）

**文件**：`backend/app/services/kpi_semantic_match_service.py`、对应单测
- 新增字段 `method: str = "keyword"`；`_matchExactAlias` 返回 `method="exact_code"`；
  Jaccard 分支 `method="keyword"`；`matchAll` 重建结果时透传。
- RED：单测断言含 code 问句 ⇒ `method == "exact_code"`；关键词命中 ⇒ `"keyword"`。
- 不改匹配算法本身（P1 才升级）。

### D2-2 新路由（TDD）

**文件**
- 新建：`backend/app/api/v1/kpi_diagnose.py`
- 修改：`backend/app/main.py`（在 `system_config` 之后、`/api/v1/health` 之前注册）
- 新建：`backend/app/tests/integration/test_kpi_diagnose_api.py`

**路由**
```python
router = APIRouter(prefix="/api/v1/admin/kpi", tags=["admin-kpi"])

@router.get("/diagnose", response_model=KpiDiagnoseRead)
async def diagnose(
    question: str = Query(..., min_length=1, max_length=500),
    user: CurrentUser = Depends(requireAdmin),
    db: AsyncSession = Depends(getDb),
) -> KpiDiagnoseRead: ...
```
`main.py`：`app.include_router(kpi_diagnose.router, dependencies=[Depends(mustChangePasswordGuard), Depends(rateLimitDep)])`。

**响应（camelCase，`CamelModel`）**
```json
{
  "question": "...",
  "injectionEnabled": true,
  "injectionBlockEmpty": false,
  "matches": [{
    "kpiCode": "KPI_SUPPLIER_OTD", "kpiName": "...", "confidence": 1.0,
    "matchMethod": "exact_code", "metricId": 2, "metricName": "...",
    "metricType": "RATIO", "executionSource": "ESCAPE_HATCH",
    "verdict": "pass", "degraded": false, "tierAEligible": true, "tier": "A",
    "reasons": [], "findings": [{"layer": "...", "reason": "...", "detail": "...", "verdict": "..."}],
    "injected": true
  }]
}
```
- `matches` 取 `matchAll(question, limit=KPI_CONTRACT_MAX_KPIS)`；无命中 ⇒ `[]`（200，不是 404）。
- `injectionEnabled` = `isKpiContractInjectionEnabled(db)`；
  `injectionBlockEmpty` = `not (await buildKpiContractBlock(db, question) or "").strip()`。
- `tier`：`tierAEligible` ⇒ `"A"`；`metric is None or executionSource == "NONE"` ⇒ `"C"`；否则 `"B"`。
- `injected` = 开关开 且 metric 存在 且 verdict != mismatch（与 `_buildEntry` 门禁一致）。
- 异常不吞：DB/缓存错误直接 500（诊断接口不能把错误伪装成空结果）。

**RED（集成测试，真实 PG，`client` + `dbSession`）**
1. admin + 含 code 问句 + 规范化后的数据 ⇒ 200，`verdict=pass`、`tierAEligible=true`、`injected=true`、`injectionBlockEmpty=false`。
2. 同一数据但治理列改回业务名 ⇒ `verdict=mismatch`、`injected=false`、`reasons` 含 `governance_column_not_in_technical`。
3. 非 admin（`X-User-Roles: user`）⇒ 403。
4. 空 question ⇒ 422。
5. 开关关闭（删 `system_config` 行）⇒ `injectionEnabled=false`、`injectionBlockEmpty=true`。
6. 无匹配问句 ⇒ 200 `matches=[]`。

### D2-3 手工冒烟（可选）

仅在本地 docker 栈上 `curl` 一次，确认 nginx 透传与 403；不写入证据链也可。

**D2 收口**：`code-reviewer` + `security-reviewer`（新 admin 路由必须过）；提交 `feat: KPI 诊断接口（admin）`。

---

## 5. D3（第 3 天）— 落库、对账归零与收口

### D3-1 `--apply`（TDD，本地测试库）

**文件**
- 修改：`backend/scripts/kpi_normalize_governance.py`（`applyPlans`）
- 新建：`backend/app/tests/integration/test_kpi_normalize_governance_apply.py`

语义：
- `--apply` 才写；写前 `assertSafeTarget(url, allowProdWrite=args.allowProdWrite)`；
  `--allow-prod-write` 仅对库名恰为 `qa_metadata` 的库生效（白名单本身含 dev/prod 同名，必须靠它区分）。
- 单事务写入：`kpi_catalog.numerator_columns/denominator_columns/grain_keys`、
  `ontology_metric.numerator_measure/denominator_measure`；只写有变化的行；提交前再跑一次对账，出现 mismatch ⇒ 回滚。
- 写后 `kpi_match_cache.onKpiChanged(kpiCode)` 不适用于脚本进程；脚本结束即可，运行中的服务 60s 自动过期。

RED（集成测试）：
1. 播种 3 条代表（OTD：measure 已有；CYCLE_TIME：MEASURE 缺 measure；DEFECT_RATE：NONE），
   跑 `applyPlans` ⇒ 字段等于 §2.2；`reconcileMetric` 分别为 pass/pass/unknown。
2. 幂等：再跑一次 ⇒ 0 行变更。
3. 物理列校验失败 ⇒ 抛错且**一行未写**（事务回滚）。
4. 非白名单库名（如 `postgres`）⇒ `SystemExit`。

```bash
pytest app/tests/integration/test_kpi_normalize_governance_apply.py -q
```

### D3-2 应用到 dev 库（唯一一次写库）

```bash
python3 scripts/kpi_normalize_governance.py \
  --database-url 'postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5433/qa_metadata' \
  --apply --allow-prod-write
```
- 先跑一次 dry-run 留档（stdout 重定向到 `dry-run-before.txt`）；`--apply` 后再跑一次
  dry-run（`dry-run-after.txt`）并把库内原值/新值导出为 `before.json` / `after.json`
  —— CLI **没有** `--out` 参数，快照靠 stdout 重定向与 psql 导出。
- 写后复验（只读）：
```bash
python3 scripts/kpi_normalize_governance.py \
  --database-url 'postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5433/qa_metadata'
# 预期：12 行全部 noop，summary: pass=8 unknown=4 mismatch=0
```
- ⚠️ **守卫只看库名**：`qa_metadata` 在 dev（5433）与 prod 同名，`assertSafeTarget`
  **不校验 host/port**。`--allow-prod-write` + 生产 DSN 会真的写生产。本批只写 5433
  靠的是操作者核对命令里的连接串（脚本会打印 `maskDsn` 的 host:port，不带口令）。

### D3-3 清理与既有断言的冲突

- `backend/scripts/kpi_metric_backfill_apply.py:461-470` 的 `_governanceValues` 加注释：
  「P0 起治理结构化列必须以物理列为准；本函数写业务名，重跑会覆盖规范化结果，仅作历史回填」。
- `backend/scripts/kpi_metric_realdata_lib.py:92-104` `expectedVerdictFor`：
  按规范化后的语义更新（tier A/B ⇒ pass；tier C ⇒ unknown），同步更新
  `kpi_metric_realdata.py` 顶部 docstring 的「12/12 mismatch」描述与相关单测。
- `backend/app/tests/integration/test_kpi_metric_backfill_apply.py:398/433`：
  这两个用例钉的是「回填脚本写出的业务名 ⇒ mismatch」，在脚本不再用于新库初始化后，
  改为断言「回填脚本的输出经过 normalize 后 ⇒ pass」，或标记为 legacy 并说明。
  **不得为了变绿删断言**；必须保留「回填产出与规范化输入不同」这一事实的取证。

### D3-4 定向回归

```bash
export TEST_DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test'
export TEST_NEO4J_URI='bolt://127.0.0.1:7688'
export TEST_NEO4J_PASSWORD='qa_neo4j_dev_2026'
pytest app/tests/unit/test_kpi_normalize_governance.py \
       app/tests/unit/test_kpi_metric_reconcile.py \
       app/tests/unit/test_kpi_semantic_match_service.py \
       app/tests/integration/test_kpi_normalize_governance_apply.py \
       app/tests/integration/test_kpi_diagnose_api.py \
       app/tests/integration/test_kpi_governance_contract_injection.py \
       app/tests/integration/test_kpi_catalog_search_api.py -q
```
- 另跑一次 `test_kpi_metric_backfill_apply.py`、`test_kpi_metric_realdata_lib.py`（D3-3 改过的）。
- 净新增失败 0 才提交 PR；已知基线红不在本批范围。

### D3-5 审查与文档

- `code-reviewer` + `security-reviewer`；CRITICAL/HIGH 必须修。
- 新建 `Harness/changes/2026-11-11-kpi-definition-consumption/summary.md`：
  需求 / 根因 / 变更清单 / before-after 报告全文 / 验收证据 / 未做的事
  （未做：P1 匹配升级、P2 Tier A 执行、P3 resolver 与 `time_grain`、生产部署）。
- 更新 `Harness/wiki/nl2sql-engine.md`（若存在 KPI 对账章节）说明「治理结构化列 = 物理列」的 P0 约定。

### D3-6 PR 与闸门

```bash
git push -u origin feat/kpi-definition-consumption
HTTPS_PROXY=http://127.0.0.1:12450 gh pr create --base main --title "feat: KPI 口径消费 P0 止血（规范化 + 诊断接口）" ...
```
- 合并判据：失败集合对差净新增 0（不是 CI 全绿）。
- 退出码 5 ⇒ 三件套证伪；6 ⇒ CI 面仲裁；**不部署**。

## 6. 风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| 规范化写坏 dev 数据 | 低 | 单事务 + 写前对账复验；before 快照落 `/tmp/kpi_p0_before_after.json`；dev 可重建 |
| 误写生产（库名同名） | 中 | `--allow-prod-write` 仅在显式传入时生效；脚本打印 host/port/db。⚠️ 守卫**只按库名**判定，dev/prod 同名 ⇒ 该开关对生产 DSN 同样放行，防线是操作者核对打印出的 host |
| 改了既有断言的语义（假绿） | 中 | D3-3 只改“期望”，保留取证；反向守卫（D1-1 #7）证明比较器仍会红 |
| 诊断接口泄露 | 低 | admin-only + 只读 + 不回显 SQL 结果；错误不吞 |
| 契约块因 Top-3 截断仍为空 | 低 | 诊断报告 `injectionBlockEmpty` 直接暴露；用例用单 code 问句 |
| 回填脚本重跑覆盖 | 中 | D3-3 注释警示；`summary.md` 写明「P0 后不要再对生产跑旧回填」 |

## 7. 文件清单

| 文件 | 动作 |
|---|---|
| `backend/scripts/kpi_normalize_governance.py` | 新建（纯函数 + dry-run + apply，< 400 行） |
| `backend/app/tests/unit/test_kpi_normalize_governance.py` | 新建 |
| `backend/app/tests/integration/test_kpi_normalize_governance_apply.py` | 新建 |
| `backend/app/api/v1/kpi_diagnose.py` | 新建 |
| `backend/app/tests/integration/test_kpi_diagnose_api.py` | 新建 |
| `backend/app/services/kpi_semantic_match_service.py` | 加 `method` 字段 |
| `backend/app/main.py` | 注册诊断路由 |
| `backend/scripts/kpi_metric_backfill_apply.py` | 仅注释警示 |
| `backend/scripts/kpi_metric_realdata_lib.py` / `kpi_metric_realdata.py` | 更新期望与文档 |
| `backend/app/tests/integration/test_kpi_metric_backfill_apply.py` | 调整与规范化后的期望 |
| `Harness/changes/2026-11-11-kpi-definition-consumption/{plan.md,summary.md}` | 计划 / 变更记录 |
| **不新增 Alembic 迁移** | 零 DDL |
