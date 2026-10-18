# KPI Catalog datasource_id 接入写入流 — 设计稿

> **状态**：🟡 OPEN-SSOT-CLOSED-BY-SPEC（SSOT 收口，spec-only，不实施）
> **继承**：[F2 AIChat 生产缺陷 phase01 计划 §1.2a ba3303e + §1.2c 7b0544c + §1.2b 188cf84（已 merged main）](../../.superpowers/sdd/2026-10-14-aichat-phase01-implementation/progress.md)
> **SSOT 收口**：[Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md](../../Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md)（不属 P2 SSOT `aichat-p2-followups-2026-10-16`；本项是 r234 recon §F2a 后续独立 lineage）

---

## 1. 目的

F2 已 merged 的 `kpi_catalog.datasource_id` 列（migration 0125, FK → `data_source.id`）在生产全表 NULL ⇒ chat 路径的 L1 datasource-mismatch 警告（per F2c meta 帧合并）实际不触发。本 spec 落定"接入写入流"的设计边界，让后续 spec/plan 有目标。

**本质**：F2 是"加列 + 加比较 + 加 UI"，**没回答"列怎么填"**。本 spec 补这块。

## 2. 范围与非范围

### 2.1 In scope（本 spec 落地）

- DTO 改造：`KpiCatalogCreate` / `KpiCatalogUpdate` 加 `datasource_id: int` 字段
- service 校验：`KpiCatalogService.createKpi` / `updateKpi` 校验 datasource 存在 + `is_active=true`
- 新增 4 类业务错误码：必填 / 不存在 / 失效 / 数据源被删被拒
- FK 行为变更：migration `ondelete="SET NULL"` → `"RESTRICT"`（纯 schema 改，不需数据迁移）
- datasource_service 改造：删除前查 KPI 引用计数，>0 抛 409
- TDD 8 用例清单（详见 §5）
- 历史 NULL 行的处置：保持 NULL 不动，service 模块 docstring 标注 + 注释 explanation

### 2.2 Out of scope（本 spec 不做）

- **不实现"自动反查"流程**：不写 batch backfill 脚本、不走 ontology_metric.target_class_id → ontology_class.source_table → data_source.name 反查链。原因：用户裁定 A 方案（必填），反查属后续独立工单。
- **不改 chat 路径的 L1 mismatch 逻辑**：F2 现状的 `datasource_id IS NULL ⇒ 不校验` 保留；仅当**新建/更新后** datasource_id 真正被填上时，警告才能触发。
- **不动历史 NULL 行**：本 spec 不迁移数据；history rows 在 chat 路径上保持"NULL ⇒ 不校验"。
- **不加 KpiCatalog update 显式 unbind 端点**：用户在 §1 选 A 方案，未要求独立 unbind；KpiCatalogUpdate 接受 `datasource_id: int | None = None` 已允许显式置 null。
- **不动 `data_source` text 列**：保留为 legacy 字段（业务源名），与 `datasource_id` FK 并存，不强制绑定。
- **不动 ontology 反查链**：用户选 A，反查属后续独立工单。

## 3. 当前现状冻结（as of 2026-10-17 main `aef0124e`）

### 3.1 Schema 状态

`backend/app/domain/models.py:387`：

```python
datasource_id: Mapped[int | None] = mapped_column(
    BigIntFk, ForeignKey("data_source.id", ondelete="SET NULL"), nullable=True
)
```

`backend/app/domain/models.py:732` — `DataSource` 表已含 `id` / `name` / `is_active` 三字段。

### 3.2 DTO 状态

`backend/app/domain/schemas.py:680` — `KpiCatalogCreate` 字段：kpi_code / kpi_name / business_definition / formula / numerator / denominator / grain / unit / **data_source (text, 200 字符)** / owner / version / status / metric_id / created_by / semantic_keywords / match_threshold。

`backend/app/domain/schemas.py:713` — `KpiCatalogUpdate` 字段同 Create，全部可选，exclude_unset 语义。

**两者均无 `datasource_id` 字段。**

### 3.3 Service 状态

`backend/app/services/kpi_catalog_service.py:80-109` — `createKpi` 直接拼字段插入，**无 datasource 校验**。
`backend/app/services/datasource_service.py:185-211` — `delete` 直接 `await session.delete(ds)`，**无 KPI 引用计数**。

### 3.4 错误码现状

`backend/app/services/messages_zh.py:503-505` 已有：
- `MSG_KPI_CATALOG_NOT_FOUND`
- `MSG_KPI_CATALOG_DUPLICATE_CODE`
- `MSG_KPI_CATALOG_STATUS_NULL`

需新增（§4.4）。

## 4. 设计

### 4.1 DTO 改造

**`KpiCatalogCreate`（schemas.py:680）加必填字段**：

```python
datasource_id: int = Field(..., ge=1, description="绑定的业务数据源 ID（FK → data_source.id）")
```

位置：放在 `metric_id` 之前（与 DB 列顺序对齐）。

**`KpiCatalogUpdate`（schemas.py:713）加可选字段**：

```python
datasource_id: int | None = Field(default=None, ge=1, description="重绑数据源；显式传 null 解绑")
```

位置：同 Create。

**`data_source` text 字段保留**：不删（与 `datasource_id` 并存），是历史 legacy 字段；不强制与 `datasource_id` 同名。

### 4.2 Service 校验

`KpiCatalogService.createKpi`（kpi_catalog_service.py:80-109）末尾加：

```python
ds = await session.get(DataSource, dto.datasource_id)
if ds is None:
    raise ValidationError(MSG_KPI_DATASOURCE_NOT_FOUND.format(id=dto.datasource_id))
if not ds.is_active:
    raise ValidationError(MSG_KPI_DATASOURCE_INACTIVE.format(id=ds.id, name=ds.name))
```

`updateKpi` 末尾加同款校验（`datasource_id` is None 时跳过；非 None 时校验存在 + is_active）。

**为何用 `ValidationError` 而非 `NotFoundError`**：422 校验错误（per Pydantic 422 规范，与本仓其他 DTO 校验失败同款）。`NotFoundError` 通常 404 留给 GET 单资源的场景；DTO 字段级校验 422 更语义。

**为何不抽 `_validateDatasource` 到 helper**：单一调用点，DRY 收益 < 抽象成本；按 YAGNI 保持内联。

### 4.3 datasource_service.delete 改造

`backend/app/services/datasource_service.py:185-211` 改：

```python
ref_count = await session.execute(
    select(func.count()).select_from(KpiCatalog).where(KpiCatalog.datasource_id == id)
)
if ref_count.scalar_one() > 0:
    raise ConflictError(MSG_DATASOURCE_HAS_KPI_BINDINGS.format(
        id=id, count=ref_count.scalar_one()
    ))
await session.delete(ds)
await session.commit()
```

### 4.4 新增 messages_zh 常量

`backend/app/services/messages_zh.py` 在 §503 后追加：

```python
# KPI Catalog datasource 绑定（Phase 1.2 写入流收尾）
MSG_KPI_DATASOURCE_REQUIRED = "KPI 必须绑定一个数据源（datasource_id 必填）"
MSG_KPI_DATASOURCE_NOT_FOUND = "KPI 绑定的数据源 id={id} 不存在"
MSG_KPI_DATASOURCE_INACTIVE = "KPI 绑定的数据源 id={id}（{name}）已停用"
MSG_DATASOURCE_HAS_KPI_BINDINGS = "数据源 id={id} 被 {count} 个 KPI 引用，无法删除"
```

### 4.5 FK 行为变更（migration 0132）

新 migration `0132_kpi_catalog_datasource_fk_restrict.py`：

```python
def upgrade() -> None:
    op.drop_constraint(
        "fk_kpi_catalog_datasource", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_kpi_catalog_datasource",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="RESTRICT",
    )

def downgrade() -> None:
    op.drop_constraint(
        "fk_kpi_catalog_datasource", "kpi_catalog", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_kpi_catalog_datasource",
        "kpi_catalog", "data_source",
        ["datasource_id"], ["id"],
        ondelete="SET NULL",
    )
```

> ⚠️ **FK 约束名说明**：原 migration 0125 创建时使用显式名 `fk_kpi_catalog_datasource`（非 Alembic 默认 `kpi_catalog_datasource_id_fkey`）。本 spec §4.5 示例代码已与实际对齐；若未来数据库重构改名，须同步更新此处。**禁止**依赖 Alembic 默认命名（实施时务必 `SELECT conname FROM pg_constraint WHERE conrelid = 'kpi_catalog'::regclass AND contype='f';` 查实际名）。

**不需数据迁移**：当前 `kpi_catalog.datasource_id` 全表 NULL（per F2 1.2a 部署后实情），FK 行为变更无碰撞。

**部署前自检**：`SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NOT NULL` 应返回 0；若非 0，§2.2 的 backfill 工单需提前介入，FK 行为变更需暂停。

**为何不放在原 migration 0125**：纯 schema 改但语义变更（删除 UX 变严格），单独 migration 便于 audit 追溯。

### 4.6 历史 NULL 行处置

**保持现状**：chat 路径的 L1 mismatch 检查（`chat_stream_output.py:197`）已实现 `if feat.datasource_id is None: skip_mismatch_check`。所有 NULL 行 → 不警告。

**service 模块 docstring 标注**（`kpi_catalog_service.py` 顶部）：

```python
"""KPI Catalog service.

Legacy rows: kpi_catalog.datasource_id IS NULL（migration 0125 之前的历史行 +
未填的运营行）— chat 路径 L1 mismatch 检查会跳过（per chat_stream_output.py:197
"NULL ⇒ 不校验"分支），不警告。要让警告生效，必须先 PUT 一次以填补 datasource_id。
"""
```

不写自动 backfill 脚本（用户裁定 A 方案）。

## 5. TDD 用例清单

### 5.1 DTO 校验（unit / Pydantic 内存，3 用例）

| # | 描述 | 期望 |
|---|---|---|
| U1 | `KpiCatalogCreate(datasource_id=5)` 合法 | 通过 |
| U2 | `KpiCatalogCreate(datasource_id=0)` 拒绝 | 422 `ge=1` |
| U3 | `KpiCatalogCreate(datasource_id=-1)` 拒绝 | 422 `ge=1` |

### 5.2 service 校验（integration / 真 PG 5434，3 用例）

| # | 描述 | 期望 |
|---|---|---|
| I1 | `createKpi` datasource_id=不存在 | 422 + `MSG_KPI_DATASOURCE_NOT_FOUND` |
| I2 | `createKpi` datasource_id=is_active=false | 422 + `MSG_KPI_DATASOURCE_INACTIVE` |
| I3 | `updateKpi` datasource_id=None 跳过校验 | 通过 + 列解绑到 None (per §4.1) |

> 2026-10-18 实施注：实际验证 9 用例 PASS = 3 DTO + 1 happy-path lock-test + 2 updateKpi/createKpi invalid + 2 DS delete 边界。happy-path lock-test (`createKpi` 持久化 `datasource_id` 到列 + DB 二次读落库) 是 §4.2 隐含必验项，防 subagent 漏写 entity 字段。

### 5.3 datasource_service.delete 校验（integration / 真 PG 5434，2 用例）

| # | 描述 | 期望 |
|---|---|---|
| I4 | `delete` datasource 无 KPI 引用 | 204 + DS 行消失 |
| I5 | `delete` datasource 有 1 个 KPI 引用 | 409 + `MSG_DATASOURCE_HAS_KPI_BINDINGS` + DS 行保留 |

**合计 8 用例**，per memory `qa-system-integration-env-contract`：**unit 也要 TEST_DATABASE_URL**（U1-U3 是 Pydantic 内存不需 PG，但放 unit 目录以集中断言）；integration 5 个需 TEST_DATABASE_URL + 真 PG 5434。

### 5.4 既有测试兼容性

`backend/app/tests/integration/test_stream_meta_single_frame.py:81` 和其他使用 `KpiCatalog(...)` 的测试需要补 `datasource_id=<some valid DS>`（或 fixture 化）。

新增 fixture `tests/integration/conftest.py::seed_data_source`：

```python
@pytest_asyncio.fixture
async def seed_data_source(dbSession: AsyncSession) -> DataSource:
    ds = DataSource(name="TEST_DS_DEFAULT", type="mysql", host="x", port=3306,
                    database_name="x", username="x", password_encrypted="x",
                    is_active=True)
    dbSession.add(ds)
    await dbSession.commit()
    return ds
```

KpiCatalog 测试 fixture 全量加 `datasource_id=seed_data_source.id`。

## 6. SSOT 收口要点

收口 P2 SSOT 显式 ⏳ 的"后续任务 #1"项：

1. **SSOT 状态变更**：`后续任务 #1 kpi_catalog.datasource_id 接入写入流` → `✅ spec 入口 / 8 用例清单明确`
2. **cross-ref 链接**：本 spec 路径写入 SSOT §"后续任务 #1" 段
3. **实施触发条件**（任一满足才开新 plan）：
   - 用户/产品要求"自动反查"（本 spec §2.2 标记的独立工单）
   - 用户报障 ≥ 3 次："datasource 删除失败"（per §4.3 409 行为）
   - 用户报障 ≥ 3 次："chat 警告实际不触发"（说明历史 NULL 行还没补完）

## 7. 实施计划

**本 spec 不写实施计划**。当 §6 触发条件满足时，新建独立 plan（per `superpowers:writing-plans` skill 流程）：
- Task 1：DTO 改造（U1-U3）
- Task 2：service 校验（I1-I3）
- Task 3：FK 改 RESTRICT migration 0132
- Task 4：datasource_service.delete 校验（I4-I5）
- Task 5：fixture + 既有测试兼容
- Task 6：5 reviewer gate + 实跑 8 用例

当前 spec 到此为止，SSOT 收口走 §6。

## 8. 风险与决策记录

### 8.1 决策：D 方案（强填 + RESTRICT + 409）vs 用户原始 A 方案

**用户裁定**：D = A 强填 + RESTRICT 删 + 409 错误 + 反向 unbind 走 KpiCatalogUpdate 显式 null。

**理由**：纯 A 方案（仅强填，FK 保留 SET NULL）下，运营误删一个被 KPI 用的 DS → KPI 失联但无迹象 → 后续 KpiCatalogUpdate 不报 FK 错（因为已 NULL）。D 方案在删 DS 时就拒。

**风险**：改 FK 行为（SET NULL → RESTRICT）属 schema 行为变更。需：
- 单独 migration 0132（不进原 0125）便于追溯
- 部署顺序：先合 backend migration，再启用 service 校验（CI 时机）
- rollback plan：downgrade 回 SET NULL + service 校验也可单独回滚

### 8.2 历史 NULL 行的 UX 缺口

历史 NULL 行（实数待 prod 查询 `SELECT count(*) FROM kpi_catalog WHERE datasource_id IS NULL`）运营无法批量补。本 spec 不写 backfill。

**后续独立工单**（per §6 触发条件 #3）：
- 决策点：用户裁定后开独立 plan
- 候选方案：batch backfill 脚本（不属生产请求路径） / admin-only backfill 接口 / 强制运营手工填

### 8.3 既有测试 fixture 改动面

per `grep -rn "KpiCatalog(" backend/app/tests/` 实测 15 个测试文件 15 处 `KpiCatalog(...)` 构造点 + `backend/scripts/seed_kpi_catalog.py` 1 处 = **16 处 / 16 文件** 需补 `datasource_id=<some DS>`。属 TDD 配套改动，**不属回归**。

> 2026-10-18 实施注：实测 31 处影响面 = 16 文件 (上) + 15 旧 fixture 文件因 `seed_data_source` 新 fixture 化引起的间接 fixture chain 刷新。Task 6 commit `3e195b35` 完成。

### 8.4 chat 警告实际生效条件

只有同时满足：
1. 用户在 chat 提问命中 L1 KPI 短路
2. 该 KPI 在创建/更新时填了 `datasource_id`
3. 用户当前选的数据源（`dto.datasourceId`）≠ 该 KPI 绑定的 `datasource_id`
4. 产生 mismatch_warning meta 帧 → 1.2b 前端 i18n 查表 → 渲染 Alert

**第 2 条是当前最大缺口**。本 spec 修的就是这个。

## 9. 链接

- F2 phase01 SDD ledger：[.superpowers/sdd/2026-10-14-aichat-phase01-implementation/progress.md](../../.superpowers/sdd/2026-10-14-aichat-phase01-implementation/progress.md)
- F2 1.2a commit（migration 0125）：`ba3303e`
- F2 1.2b commit（前端 i18n 查表 + 显式清除）：`188cf84`
- F2 1.2c commit（meta 帧合并）：`7b0544c`
- PR #18（phase01 全部）：merged 2026-10-14
- SSOT 入口：[Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md](../../Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md)
- chat 路径 L1 检查：[backend/app/services/chat_stream_output.py:197](../../backend/app/services/chat_stream_output.py)
- KpiCatalog 模型：[backend/app/domain/models.py:362](../../backend/app/domain/models.py)
- KpiCatalogCreate DTO：[backend/app/domain/schemas.py:680](../../backend/app/domain/schemas.py)
- KpiCatalogService：[backend/app/services/kpi_catalog_service.py](../../backend/app/services/kpi_catalog_service.py)
- DataSource 模型：[backend/app/domain/models.py:732](../../backend/app/domain/models.py)
- DataSource service：[backend/app/services/datasource_service.py](../../backend/app/services/datasource_service.py)
- 错误码 SSOT：[backend/app/services/messages_zh.py](../../backend/app/services/messages_zh.py)
- 记忆：[qa-system-messages-ops-422-handler](~/.claude/projects/.../memory/qa-system-messages-ops-422-handler.md)
