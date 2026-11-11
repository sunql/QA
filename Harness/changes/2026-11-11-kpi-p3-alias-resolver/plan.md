# KPI P3 别名解析层 + 对账改造 + 时间粒度迁移 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地设计 §7——治理 token → 物理列的别名解析层（`kpi_alignment_resolver`）、层 2 对账改用解析结果、`time_grain`/`time_column` 结构化字段（迁移 0143）与幂等回填，为 P4 参数化编译器铺路。

**Architecture:** 纯函数 resolver（零 IO）+ 薄 loader（读 `ontology_metric_source` 并集的属性）；层 2 在「解析结果可用」时按解析结果比较，不可用时保持 P0/P2 现状（恒等回退）；三档判定 pass/mismatch/unknown 不变，unknown 语义按设计 §7.3 补 degraded 标注。

**Tech Stack:** Python 3.14 / SQLAlchemy async / PostgreSQL（真实库测试）/ alembic（新增 0143，单头链）/ React+antd（治理表单字段）。

## Global Constraints

- **不部署、不改生产库**：生产仍在 0140 + 旧代码；迁移 0143 只进链，随下次发版生效。
- **写 dev 元数据库（5433 `qa_metadata`）必须先获用户批准**：回填脚本默认 dry-run；`--apply` + `--allow-prod-write` 执行前要向用户出示 before/after 报告并获批。
- 不升级依赖、不改 lockfile；新 worktree 用 `uv sync --frozen --extra dev`。
- 基础架构版本用当前版本（用户既有约束）。
- 测试：真实 PostgreSQL（5434 `qa_metadata_test`，白名单 `^qa_metadata_test($|_)`）+ 完整 API 链路；禁 sqlite 内存库；停 pytest 用 SIGINT；**不跑后端全量 unit 套件做门禁**（27min/28 红基线），只跑定向。
- **Milvus 19530 是生产——不跑任何 Milvus 用例**；Neo4j 测试 `bolt://127.0.0.1:7688`。
- 改过迁移须重建语义发布测试库 `qa_metadata_test_semantic`（5434）后跑其套件。
- 合并判据 = 失败集合对差**净新增 0**（不等 CI 全绿）；新增集成测试文件会触发分片轮转假 net-new，仲裁时按 [[qa-system-ci-shard-order-net-new]] 处理。
- ORM/Pydantic 字段 `snake_case` + camelCase alias（仓库既有约定）；函数 `camelCase`；常量 `UPPER_SNAKE_CASE`；不可变数据（frozen dataclass / 新对象）。
- admin-only 写入口不放松；`time_grain`/`time_column` 写入跟随既有 ACL。
- 每个 subagent 派发必须显式指定 model；变异取证「改→红→还原→复绿」。

---

## 0. Definition of Done

1. 迁移 `0143`（`down_revision="0142"`）：`kpi_catalog.time_grain VARCHAR(20) NULL`、`ontology_metric.time_grain VARCHAR(20) NULL`、`ontology_metric.time_column VARCHAR(100) NULL`；ORM 同步声明；全新库 `upgrade head` → `schema_drift --strict` exit 0。
2. 新建 `TimeGrain` 枚举（`DAY/WEEK/MONTH/QUARTER/YEAR`），**独立于** `time_scope_resolver.granularity`（后者是问句区间口径，不动）。
3. `kpi_alignment_resolver.py` 纯函数：结构化治理 token → 物理列；唯一命中→列名；多候选→unknown；无候选→missing（层 2 判 mismatch）；纯时间词→`TimeGrain`（从列比较中剔除）。别名分支（`property_alias`/`business_aliases`）按现数据为「未来准备」，不计命中率验收。
4. 层 2 改造：`resolution` 可用时，治理列集合 = resolver 结果；粒度比较 = `time_grain` 枚举相等 + 维度列集合相等（不再直接比中文字符串）；`resolution=None` 时行为与现状逐字相同（恒等回退）。
5. **存量 12 条 KPI 的层 2 verdict 不得改变**：8 pass / 4 unknown（`p3-recon.md` §5 实测基线），用回归测试钉死。
6. unknown 放行注入 + `degraded=True`（设计 §7.3）；mismatch 仍阻断；Tier A 资格判定不变（`isPass and not degraded`）。
7. DTO/API：KPI 目录与本体指标两个 admin 写入口可读写 `timeGrain`/`timeColumn`；发布闸门感知新字段（加入 `_GOVERNANCE_KEYS` 等）；非 admin 403 回归不破。
8. 回填脚本 `scripts/kpi_backfill_time_grain.py`：默认 dry-run；`time_grain` 从 `kpi_catalog.grain` 文本确定性推导（11 条 MONTH + 1 条 null）；`time_column` 按裁决 R3 出候选报告，唯一才写。
9. 前端：KPI 表单与指标技术形态表单可编辑 `timeGrain`/`timeColumn`，i18n 中英齐全，测试同步。
10. 变异取证 ≥4 组（resolver 映射改坏 / grain 比较改坏 / 恒等回退删除 / degraded 扩展删除，各须红）；新模块覆盖率 ≥80%；定向测试全绿；邻近套件失败集合对差净新增 0。

**明确不做**：不改层 1/层 3 语义；不动 `hasExplicitTimeScope`（P2 交付）；不做别名数据补录（登记为独立数据工作项）；不改 `time_scope_resolver`；P4 参数化/编译器另立项；不改前端渲染逻辑（只加表单字段）。

---

## 1. 事实基线（全部实测，见 `p3-recon.md`，2026-11-11）

| 事实 | 证据 |
|---|---|
| 层 2 现状：8 pass / 4 unknown / 0 mismatch（4 条 unknown 全是 `execution_source=NONE`） | recon §5（纯函数重算 + DB 原值逐条列出） |
| 31/31 结构化治理 token 已是物理列（源类 `source_column` 命中），中文 token 0 条 | recon §5（逐 token SQL 校验） |
| `business_aliases` 3717 行全 JSON null；`property_alias` 非空仅 3 条；`source_column == property_name` 3717/3717 | recon §1 |
| `target_class_id` 12/12 全 NULL ⇒ 源类必须用 `ontology_metric_source`（role: NUMERATOR/DENOMINATOR/FILTER/BASE） | recon §3（`models.py:423-453`、`enums.py:580-589`） |
| metric 11/12/13（3 条 NONE KPI）无 source 行 ⇒ 无源类 ⇒ unknown | recon §3.2 |
| 分子/分母跨类（OTD、OVERDUE_RATIO、INVOICE_MATCH_RATE）；metric 6/8/9 的 measure JSON 无 `class_id` | recon §3.2 |
| 源类并集 DATETIME 列 7–18 个/metric ⇒ `time_column` 无法仅靠源类唯一解析 | recon §6.1 |
| `kpi_catalog.grain` 自由文本：11 条含「月」、1 条 `供应商（快照）` 无时间词 | recon §6 |
| 无可复用 time_grain 枚举；`time_scope_resolver.granularity` 只有 `quarter/half_year` | recon §6 |
| 层 2 verdict 消费点：`kpi_contract_builder.py:403/413/441`（只拦 mismatch）、`kpi_contract_reconcile.py:281`、`chat_stream_output.py:426-435`（`isTierAEligible`）、`kpi_diagnose.py:184-196` | recon §4.1 |
| unknown 已放行（两注入面都只拦 mismatch），但 `degraded` 不因 unknown 置真（只由层 1/3 mismatch 置真，`kpi_metric_reconcile.py:355-357`） | recon §4.2 |
| alembic 单头 `0142`，`0143` 未占用；列新增写法样例 `0136_metric_structured.py:33-51` | recon §7 |
| 回填脚本骨架样例 `scripts/kpi_normalize_governance.py`（dry-run→--apply→--allow-prod-write→`current_database()` 校验→幂等 noop）+ 守卫 `scripts/kpi_metric_db_guard.py`（`assertSafeTarget`） | recon §8 |
| 写入口：`PUT /api/v1/ontology/metrics/{id}`（`api/v1/ontology.py:342-355` requireAdmin）、`POST/PUT /api/v1/kpi-catalog`（`kpi_catalog.py:105/146` getAdminOnlyActor）；`kpi_catalog_service.py:39-61` `_GOVERNANCE_KEYS`/`_governanceOverride` | recon §9.2 |
| 前端触及：`KpiCatalogPage.tsx`、`MetricTechniqueForm.tsx`、`MetricTab.tsx`、`types/kpiCatalog.ts`、`types/ontology.ts`、`kpiCatalogPayload.ts`、i18n zh/en | recon §10 |
| 测试主战场：`test_kpi_metric_reconcile.py`（420 行纯函数）、`test_kpi_metric_spec.py`、集成 `test_kpi_metric_reconcile.py` / `test_kpi_governance_contract_injection.py` / `test_ontology_metric_write_gate.py` / `test_kpi_normalize_governance_apply.py` | recon §9.1 |

---

## 2. 裁决（与设计稿的显式偏差 / 补充，全部已按设计原则推导）

| # | 裁决 | 依据 |
|---|---|---|
| R1 | resolver 输入域 = `numerator_columns` / `denominator_columns` / `grain_keys` 的**结构化 token**；`kpi.numerator/denominator/grain` 自由文本不进 resolver | 设计 §7.1 输入是「治理侧 token」即结构化列；recon §5 中文只在自由文本 |
| R2 | 源类 = `ontology_metric_source WHERE metric_id` 的 class_id **并集**（角色相关判定按 NUMERATOR/DENOMINATOR 取类；粒度/时间列/别名在并集内查）；`target_class_id` 不用；无 source 行 ⇒ unknown（非 mismatch） | recon §3（`target_class_id` 全 NULL；`models.py:426-428` docstring 明确新表为权威） |
| R3 | **`time_column` 唯一化规则**（设计缺口）：① 治理 `grain_keys` 中的日期 token 唯一 → 用它；② 否则收集「measure expr + formula 实际引用的日期列」（`data_type` 为日期型或列名含 DATE/TS）→ 唯一 → 用它；③ 多候选/零候选 ⇒ **不写**（null），dry-run 报告列候选，由治理侧经 admin API 人工指定。「不猜」原则（§7.1-4）优先于自动化率 | recon §6.1（7–18 个日期列，按字面规则 12/12 unknown 无意义） |
| R4 | unknown ⇒ `degraded=True`：`reconcileMetric` 的 degraded 扩展为 `层1 mismatch or 层3 mismatch or 层2 verdict==unknown`；`isTierAEligible` 语义不变（unknown 本来就非 pass） | 设计 §7.3「unknown 放行并标注 degraded」；现状差距 recon §4.2 |
| R5 | 别名数据补录**不在本批**（`business_aliases` 全 null）：resolver 别名分支做到位 + 测试覆盖即可，验收不设「别名命中率」 | recon §1/§11.5 |
| R6 | `time_grain` 回填来源 = `kpi_catalog.grain` 文本确定性推导（含「月」→MONTH；无时间词→null）；`ontology_metric.time_grain` 同值回填 | recon §6（11 MONTH + 1 null，可确定性推导） |
| R7 | `reconcileGovernanceVsTechnique` / `reconcileMetric` 新增**可选**参数 `resolution`；`None` = 现状恒等行为（P2 的 parity 测试与探针不受影响） | 向后兼容；存量 verdict 不变是硬性 DoD#5 |
| R8 | 纯时间词集合：`月/月份/季度/年/周/日/天`（及 `按X` 前缀形态）；从 grain_keys 列比较中剔除并映射 `TimeGrain`；`grain_keys` 现存量无时间词 ⇒ 纯前瞻 | 设计 §7.2「grain_keys 只放物理维度列」；recon §6 |

**执行前须用户确认的点**（已按推荐方案写入计划，启动前随计划一起过目）：
- R3 的 time_column「不猜、人工指定」路线（自动化率会很难看，但符合设计原则）；
- D5 回填写 dev 库 5433 需届时单独批准；
- R4 会给 4 条 NONE KPI 的诊断/契约块加 degraded 提示（行为变化，视觉可见）。

---

## 3. 不变量

1. resolver / `deriveTimeGrain` 纯函数零 IO（同输入同输出）；IO 只在 loader。
2. **存量 12 条层 2 verdict 不变**（8 pass / 4 unknown）——回归测试钉死，任何改造后先跑它。
3. mismatch 仍阻断注入；unknown 放行 + degraded；层 1/3 语义不动。
4. `resolution=None` ⇒ 与 P2 行为逐字相同（恒等回退），P2 parity 测试不改断言仍绿。
5. 写入口 admin-only；发布闸门按新值判定（`_GOVERNANCE_KEYS` 纳入 `time_grain`）。
6. 零生产写；迁移只进链；env/lockfile 不动。
7. 迁移 downgrade 对称（drop 3 列）；ORM 与迁移同步（drift 零漂移）。

---

## Task 1（D1）— TimeGrain 枚举 + 迁移 0143 + ORM 同步

**Files**
- Modify: `backend/app/domain/enums.py`（新增 `TimeGrain`）
- Modify: `backend/app/domain/models.py`（`KpiCatalog` + `OntologyMetric` 各加列声明）
- Create: `backend/alembic/versions/0143_kpi_time_grain.py`
- Test: `backend/app/tests/unit/test_kpi_time_grain_model.py`（新建）
- Gate: 全新库迁移 + drift `--strict`

**Interfaces（产出，后续任务依赖）**
- `TimeGrain(StrEnum)`：`DAY/WEEK/MONTH/QUARTER/YEAR`，`.value` 小写字符串；模块级 `TIME_GRAIN_VALUES: frozenset[str]`。
- ORM：`KpiCatalog.time_grain: str | None`（String(20)）、`OntologyMetric.time_grain: str | None`（String(20)）、`OntologyMetric.time_column: str | None`（String(100)），全部 nullable，注释标注「0143，P3 时间粒度」。

- [ ] **Step 1 RED**：`test_kpi_time_grain_model.py` 断言 `TimeGrain` 存在且五值齐全；`KpiCatalog.__table__.columns` 含 `time_grain`（String(20)、nullable）、`OntologyMetric` 含 `time_grain`/`time_column`（String(100)、nullable）。跑 `pytest app/tests/unit/test_kpi_time_grain_model.py -q` ⇒ 全红（ImportError/KeyError）。
- [ ] **Step 2 GREEN 枚举+ORM**：按 Interfaces 落地；重跑全绿。
- [ ] **Step 3 迁移 0143**：仿 `0136_metric_structured.py:33-51` 的 `_JSON` 风格写 `0143_kpi_time_grain.py`——`revision="0143"` / `down_revision="0142"`；upgrade 三条 `op.add_column`（`sa.String(20)`/`sa.String(20)`/`sa.String(100)`，nullable=True）；downgrade 对称 `op.drop_column`。
- [ ] **Step 4 全新库 Gate**（照 [[qa-system-semantic-release-test-db]] 流程）：
  ```bash
  docker exec qa-pg-a1 psql -U qa_user -d postgres -c "DROP DATABASE IF EXISTS qa_metadata_test_p3gate"
  docker exec qa-pg-a1 psql -U qa_user -d postgres -c "CREATE DATABASE qa_metadata_test_p3gate"
  cd backend && DATABASE_URL='postgresql+asyncpg://qa_user:qa_pg_dev_2026@127.0.0.1:5434/qa_metadata_test_p3gate' uv run alembic upgrade head
  python3 -m app.infrastructure.schema_drift --database-url '...qa_metadata_test_p3gate' --strict; echo EXIT=$?   # 必须 0
  # downgrade 对称性：alembic downgrade 0142 → 三列消失 → 再 upgrade head
  docker exec qa-pg-a1 psql -U qa_user -d postgres -c "DROP DATABASE IF EXISTS qa_metadata_test_p3gate"
  ```
- [ ] **Step 5 语义发布测试库重建**（改过迁移的既定动作）：DROP/CREATE `qa_metadata_test_semantic` → upgrade head → 跑语义发布定向套件全绿。
- [ ] **Step 6** 定向回归：`test_check_schema_drift.py`、`test_orm_schema_ssot.py`、`test_domain_model_registration.py` 全绿（新列两侧一致）。
- [ ] **Step 7 commit**：`feat: TimeGrain 枚举与 0143 迁移（kpi_catalog/ontology_metric 时间粒度列）`

---

## Task 2（D2）— `kpi_alignment_resolver` 纯函数

**Files**
- Create: `backend/app/services/kpi_alignment_resolver.py`
- Test: `backend/app/tests/unit/test_kpi_alignment_resolver.py`（新建）

**Interfaces**
- ```python
  @dataclass(frozen=True)
  class SourceProperty:
      property_name: str
      source_column: str | None
      property_alias: str | None
      business_aliases: tuple[str, ...] = ()
      data_type: str | None = None

  @dataclass(frozen=True)
  class ResolutionOutcome:
      resolved: tuple[tuple[str, str], ...] = ()     # (原始 token, 物理列)
      unknownTokens: tuple[str, ...] = ()            # 多候选（不猜）
      missingTokens: tuple[str, ...] = ()            # 无候选（层 2 判 mismatch）
      timeGrain: TimeGrain | None = None             # 纯时间词映射结果
      timeCandidates: tuple[str, ...] = ()           # 源类并集中的日期列（R3 报告用）

  def resolveGovernanceTokens(
      tokens: tuple[str, ...] | list[str] | None,
      sourceProperties: tuple[SourceProperty, ...],
  ) -> ResolutionOutcome: ...
  ```
- 解析顺序（每 token）：① 归一化（strip/lower）后命中某 `source_column` ⇒ 原样列名；② 命中 `property_name` ⇒ 其 `source_column`（无 source_column ⇒ unknown）；③ `property_alias` 精确（归一化后）命中 ⇒ 同上；④ `business_aliases` 命中 ⇒ 同上；⑤ 命中多列 ⇒ unknown；⑥ 无命中 ⇒ missing。
- 纯时间词（R8）：token 归一化后 ∈ {月,月份,季度,年,周,日,天} 或 `按(月|季度|年|周|日|天)` ⇒ 映射 `TimeGrain`（月→MONTH/季度→QUARTER/年→YEAR/周→WEEK/日|天→DAY），**不进**列解析；多个不同时间词 ⇒ `TimeGrain` 取 None 且记 unknown（不猜）。
- `timeCandidates`：`data_type` ∈ 日期型集合（`DATE/DATETIME/TIMESTAMP`，大小写不敏感）或列名含 `DATE/_TS` 后缀的 source_column 全集（排序去重）。
- Loader（同文件，薄 IO）：
  ```python
  def sourcePropertiesForMetric(session, metricId: int) -> tuple[SourceProperty, ...]:
      """ontology_metric_source 并集 → ontology_property 行；无行 ⇒ ()。"""
  ```
  （join `OntologyMetricSource` × `OntologyProperty.class_id`，去重；只读 SELECT。）
- `deriveTimeGrain(grainText: str | None) -> TimeGrain | None`：自由文本含「月」→MONTH、「季度」→QUARTER、「年」→YEAR、「周」→WEEK、「日|天」→DAY（按最长词优先避免「季度」被「月」…无冲突；实测 12 条 grain 值全对）；无时间词 ⇒ None。**独立函数**，回填与层 2 共用。

- [ ] **Step 1 RED**：表驱动单测——恒等命中 / property_name 映射 / 别名映射 / business_aliases 映射 / 多候选 unknown / 无候选 missing / 纯时间词剔除+映射 / 大小写与空白归一 / 空输入 / 无源类（空 properties ⇒ 全 missing）/ `deriveTimeGrain` 12 条真实 grain 值（11 MONTH + 1 None，字面量来自 recon §5/§6）。跑 ⇒ 全红（模块不存在）。
- [ ] **Step 2 GREEN**：实现纯函数；重跑全绿。
- [ ] **Step 3 变异取证**（归档 `Harness/changes/2026-11-11-kpi-p3-alias-resolver/evidence/mutation-resolver.txt`）：① 删 property_alias 分支 ⇒ 别名用例红；② 删时间词剔除 ⇒ grain 用例红；③ 多候选改取 first ⇒ unknown 用例红。各「改→红→还原→复绿」。
- [ ] **Step 4 commit**：`feat: KPI 别名解析层纯函数（token→物理列 + 时间词→TimeGrain）`

---

## Task 3（D3）— 层 2 对账改造 + degraded 扩展 + 接线

**Files**
- Modify: `backend/app/services/kpi_metric_reconcile.py`（`GovernanceSpec`/`MetricTechnique`/层 2/reconcileMetric）
- Modify: `backend/app/services/kpi_metric_spec.py`（`governanceSpecOf`/`techniqueOf` 携带新字段）
- Modify: `backend/app/services/kpi_contract_builder.py:403` 附近（构造 resolution 传入）
- Modify: `backend/app/services/chat_stream_output.py:426-435` 附近（同上）
- Modify: `backend/app/api/v1/kpi_diagnose.py:184-196` 附近（同上）
- Test: `backend/app/tests/unit/test_kpi_metric_reconcile.py`（追加）、`backend/app/tests/unit/test_kpi_tier_parity.py`（确认不破）、`backend/app/tests/integration/test_kpi_stock_verdict_regression.py`（新建）

**Interfaces**
- `MetricTechnique` 增 `time_grain: str | None = None`、`time_column: str | None = None`（`techniqueOf` 从 ORM 读）。
- `GovernanceSpec` 增 `time_grain: str | None = None`（`governanceSpecOf` 用 `deriveTimeGrain(kpi.grain)` 推导，R6）。
- `reconcileGovernanceVsTechnique(governance, technique, resolution: ResolutionOutcome | None = None)`；`reconcileMetric(governance, technique, resolution=None)`。
- resolution 非 None 时的层 2 新逻辑：
  - 列比较输入 = resolver 结果：`missingTokens` ⇒ 新 finding `governance_token_unresolvable`（mismatch）；`unknownTokens` ⇒ 新 finding `governance_token_ambiguous`（unknown）；resolved 列集合替代原 token 集合进 `_checkRole`。
  - 粒度比较（`_checkGrain` 改造或并列新函数）：`governance.time_grain` 与 `technique.time_grain` 枚举相等（任一侧 None ⇒ unknown finding `time_grain_absent_*`，不 mismatch）；维度列集合（grain_keys 去时间词后经 resolver）集合相等，不一致 ⇒ mismatch（沿用 `REASON_GRAIN_MISMATCH`）。
  - `technique.time_column` 非空且不在源类 `timeCandidates` ⇒ degraded finding `time_column_not_in_source`（unknown，不阻断）。
- degraded（R4）：`reconcileMetric` 中 `degraded = layer1.verdict==MISMATCH or layer3.verdict==MISMATCH or layer2.verdict==UNKNOWN`。
- 接线纪律：三处调用点 try/except 包住 loader（DB 异常 ⇒ `resolution=None` 走恒等回退 + warning），**绝不让解析失败变成请求失败**。

- [ ] **Step 1 RED**：① 新单测——resolution 带 missing ⇒ mismatch + 新 reason；unknown ⇒ unknown finding；resolved 集合参与比对（技术列不在解析结果 ⇒ `technical_column_not_in_governance`）；time_grain 枚举相等/不等/缺侧；`degraded` 在层 2 unknown 时为 True；`resolution=None` 时全部旧行为不变（现有 40+ 用例零修改仍绿是硬门槛）。② `test_kpi_stock_verdict_regression.py`（真实 PG）：用 recon §5 的 12 条数据作夹具（治理 token + measure JSON + source 行），断言逐条 verdict 与基线一致（8 pass / 4 unknown，unknown 的 reasons 含 `governance_columns_absent×2 + governance_grain_absent`）。跑 ⇒ 新用例红。
- [ ] **Step 2 GREEN**：实现改造 + 三处接线；重跑定向（`test_kpi_metric_reconcile.py` + parity + 新回归文件）全绿。
- [ ] **Step 3 恒等回退变异**（evidence `mutation-reconcile.txt`）：① 删 `resolution=None` 恒等回退 ⇒ 旧用例红；② 删 degraded 扩展 ⇒ 新用例红；③ grain 比较改回字符串直比 ⇒ 新用例红。各「改→红→还原→复绿」。
- [ ] **Step 4 存量复算**：用探针/脚本对 dev 5433 只读重算 12 条 verdict（resolution 接线后路径），与 recon §5 基线逐条比对 ⇒ 0 变化，输出存 `evidence/stock-verdict-after.txt`。
- [ ] **Step 5 commit**：`feat: 层 2 对账接入别名解析与 time_grain 比较（unknown 补 degraded）`

---

## Task 4（D4）— DTO/API + 发布闸门 + admin 写测试

**Files**
- Modify: `backend/app/domain/schemas.py`（`KpiCatalogCreate/Update/Read` + `OntologyMetricCreate/Update/Read`）
- Modify: `backend/app/services/kpi_catalog_service.py:39-61`（`_GOVERNANCE_KEYS`/`_governanceOverride` 纳入 `time_grain`）
- Modify: `backend/app/services/ontology_service.py:1016-1056`（确认 `enforceMetricPublishGate` 是否需感知；需要则加）
- Test: `backend/app/tests/integration/test_kpi_time_grain_write_api.py`（新建；真实 PG + httpx 全链路）

- [ ] **Step 1 RED**：① admin `PUT /api/v1/kpi-catalog/{id}` 设 `timeGrain="MONTH"` ⇒ 200 且落库、GET 回读一致；`PUT /api/v1/ontology/metrics/{id}` 设 `timeGrain`/`timeColumn` 同理；② 非 admin 写 ⇒ 403（沿用 `test_ontology_metric_write_gate.py` 模式）；③ 发布闸门：改 `time_grain` 后闸门按**新值**判定（对照 `_governanceOverride` 既有测试写法）。跑 ⇒ 红。
- [ ] **Step 2 GREEN**：DTO 字段（snake_case + camel alias）+ 服务透传 + 闸门集合更新；重跑全绿 + `test_ontology_metric_write_gate.py`、KPI catalog 既有写测试零回归。
- [ ] **Step 3 commit**：`feat: time_grain/time_column 进 admin 写入口与发布闸门`

---

## Task 5（D5）— 回填脚本 + dev 数据执行（**写库前须用户批准**）

**Files**
- Create: `backend/scripts/kpi_backfill_time_grain.py`
- Test: `backend/app/tests/integration/test_kpi_time_grain_backfill_apply.py`（真实 PG，白名单测试库）
- Evidence: `Harness/changes/2026-11-11-kpi-p3-alias-resolver/evidence/backfill-report.txt`

**脚本规格**（骨架复制 `kpi_normalize_governance.py`，守卫用 `kpi_metric_db_guard.assertSafeTarget`）：
- 默认 dry-run；`--database-url` 必填；`--apply` 才写；写 `qa_metadata` 需 `--allow-prod-write`；连接后 `current_database()` 与 DSN 校验；幂等 noop 判定；退出码语义沿用。
- `time_grain`：`deriveTimeGrain(kpi.grain)` ⇒ 同值写 `kpi_catalog.time_grain` 与挂靠 `ontology_metric.time_grain`。
- `time_column`（R3）：候选 = grain_keys 日期 token（经 resolver 的 `timeCandidates` 过滤）→ 唯一 ⇒ 写 `ontology_metric.time_column`；否则 measure/formula 引用的日期列 → 唯一 ⇒ 写；多候选/零候选 ⇒ 不写，报告列候选。
- 报告：逐 KPI before/after + 推导依据 + time_column 候选清单。

- [ ] **Step 1 RED**：集成测试——测试库播种 2 条 KPI（一条 grain 含「月」+ formula 引用唯一日期列 ⇒ 两字段都回填；一条 grain 无时间词 ⇒ time_grain null 不写）；幂等（二次 apply 变更 0）；`--apply` 缺 `--allow-prod-write` 写 `qa_metadata` ⇒ 拒绝。跑 ⇒ 红。
- [ ] **Step 2 GREEN**：实现脚本；重跑全绿。
- [ ] **Step 3 dev dry-run**（只读，不需批准）：`--database-url ...5433/qa_metadata` dry-run ⇒ 报告存 evidence；**把报告摘要给用户，获批后** `--apply --allow-prod-write` 执行（前置：备份提示按守卫四道闸）；执行后复跑 dry-run ⇒ 0 变更。
- [ ] **Step 4 commit**：`feat: time_grain/time_column 幂等回填脚本（默认 dry-run）`（dev 写库动作单独记录在 evidence，不进 commit）

---

## Task 6（D6）— 前端字段

**Files**
- Modify: `frontend/src/types/kpiCatalog.ts`、`frontend/src/types/ontology.ts`
- Modify: `frontend/src/pages/KpiCatalogPage.tsx`、`frontend/src/components/ontology/MetricTechniqueForm.tsx`、`frontend/src/components/ontology/MetricTab.tsx`、`frontend/src/utils/kpiCatalogPayload.ts`
- Modify: `frontend/src/i18n/zh-CN.ts`、`frontend/src/i18n/en-US.ts`
- Test: `frontend/src/tests/KpiCatalogPage.test.tsx`、`MetricTab.test.tsx` 追加

- [ ] **Step 1 RED**：表单断言——KPI 表单出现 `timeGrain` 下拉（五枚举 + 留空）；指标技术形态表单出现 `timeGrain` + `timeColumn` 文本输入；提交 payload 携带两字段；回填正确。跑 ⇒ 红。
- [ ] **Step 2 GREEN**：类型 + 表单 + payload 映射 + i18n（zh：`时间粒度`/`时间列`；en：`Time grain`/`Time column`）；前端定向测试全绿 + `npx tsc --noEmit` 0 错误（基线口径）。
- [ ] **Step 3 commit**：`feat: 治理表单支持时间粒度/时间列字段`

---

## Task 7（D7）— 回归 + 双评审 + 文档 + PR

- [ ] **7.1 定向回归**：新文件全绿（resolver/reconcile/写 API/回填/time_grain 模型 + P2 的 10 文件）；邻近套件失败集合对差 vs 合并后 main（新集成测试文件 ⇒ 分片轮转假 net-new 面，按 [[qa-system-ci-shard-order-net-new]] 仲裁）。
- [ ] **7.2 双评审**：`code-reviewer` + `security-reviewer`（静态，禁跑 pytest）；重点：resolver 归一化注入面（token 值进 finding detail 是否可控）、回填脚本写库护栏、发布闸门绕过、unknown⇒degraded 的语义回归。发现按 Critical/Important 修。
- [ ] **7.3 文档**：`Harness/changes/2026-11-11-kpi-p3-alias-resolver/summary.md`（背景/裁决 R1-R8/逐日 commit/DoD 10 条逐项/未做事项：别名数据补录、P4、部署；回滚=revert + 迁移 downgrade 0142）+ 更新 `Harness/wiki/kpi-metric-technical-form.md`（对账语义改造）与 `nl2sql-engine.md`（Tier A 资格受 time_grain 影响）。
- [ ] **7.4 收尾**：`git status` 干净；push + PR（`HTTPS_PROXY=http://127.0.0.1:12450`）；**不合并、不部署、不改生产库**。

---

## 4. 文件清单

| 文件 | 动作 |
|---|---|
| `backend/app/domain/enums.py` | +`TimeGrain` 枚举 |
| `backend/app/domain/models.py` | +3 列声明（KpiCatalog.time_grain；OntologyMetric.time_grain/time_column） |
| `backend/alembic/versions/0143_kpi_time_grain.py` | 新建（upgrade/downgrade 对称） |
| `backend/app/services/kpi_alignment_resolver.py` | 新建（纯函数 + loader + deriveTimeGrain） |
| `backend/app/services/kpi_metric_reconcile.py` | 层 2 接 resolution + grain 枚举比较 + degraded 扩展 |
| `backend/app/services/kpi_metric_spec.py` | spec 快照带新字段 |
| `backend/app/services/kpi_contract_builder.py` / `chat_stream_output.py` / `api/v1/kpi_diagnose.py` | 三处接线（try/except 回退） |
| `backend/app/domain/schemas.py` | DTO 新字段 |
| `backend/app/services/kpi_catalog_service.py` / `ontology_service.py` | 透传 + 闸门感知 |
| `backend/scripts/kpi_backfill_time_grain.py` | 新建（dry-run 默认） |
| 前端 7 文件 + i18n 2 文件 | 表单字段 |
| 测试 | unit 3 新 + 追加 2；integration 3 新 |
| `Harness/changes/2026-11-11-kpi-p3-alias-resolver/{plan.md,summary.md,evidence/}` | 计划/总结/取证 |

## 5. 风险与回滚

| 风险 | 评估 | 处置 |
|---|---|---|
| 存量 verdict 意外变化 | 高 | DoD#5 回归测试 + Task 3 Step 4 dev 复算取证；resolution=None 恒等回退是默认路径 |
| degraded 扩展误伤（pass 但层 2 有 unknown finding 的场景语义） | 中 | R4 只在 verdict==unknown 时置真；pass 不受影响；单测锁死三态矩阵 |
| time_column 回填大面积 null | 中 | 按 R3 属预期；报告给治理侧人工指定；不阻塞 P4（P4 编译器可先吃 time_grain） |
| 迁移/ORM 漂移 | 中 | Task 1 Step 4 全新库 --strict Gate + 语义发布库重建 |
| dev 写库误伤 | 高 | dry-run 报告 + 用户批准 + `assertSafeTarget` 四道闸 + 备份前置 |
| 分片轮转假 net-new | 中 | 新集成文件已知效应，7.1 按 net-new 0 仲裁并在 PR 描述注明 |
| 回滚 | — | 代码 revert；迁移 `alembic downgrade 0142`（drop 3 列，无数据风险，列均为 nullable 新增）；回填写入的值可用同一脚本反向置 null（或随 downgrade 消失） |
