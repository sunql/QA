# F2 收尾：PR #55 修复 + P0/P1/P2 后续项 — SSOT（2026-10-18）

> **状态**：✅ IMPL-COMPLETE（2026-10-18；followup 分支 `feat/f2-followups`，base = 修复后的 PR #55）
> **前序 SSOT**：[`Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/summary.md`](../2026-10-18-kpi-datasource-write-flow-spec/summary.md)（spec + 8 用例，PR #55）
> **Spec**：[`docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md`](../../docs/superpowers/specs/2026-10-18-kpi-datasource-write-flow-design.md)
> **Plan**：`~/.claude/plans/eager-puzzling-creek.md`（Step 0 + Task 1–5 + 最终评估）
> **本 SSOT 范围**：修复 #55 的不可合并问题（生产级 `auto_promote` 缺陷 + 存量测试 422 + 前端 422 + 读契约缺字段 + 0132 下 SET NULL 测试语义），再按 P2a → P1b → P2b → P1a → P0 完成后续项。

---

## 1. PR #55 修复（Step 0，不进入 followup PR）

recon 发现 #55 「只跑了 9 个新用例，整条存量链路没有回归」，共 4 类问题，其中 2 类是生产级：

| # | 问题 | 修复 |
|---|---|---|
| 1 | `metric_promotion_service.auto_promote` 构造 `KpiCatalogCreate` 缺必填 `datasource_id` → 运行时 `ValidationError`（L1 冷指标晋升整条路径挂死） | 改为直接构造实体，`datasource_id=None`（晋升路径无数据源上下文，交给回填工具）；commit `61ef2c1d` |
| 2 | 7 个 API/unit 测试文件 payload 缺 `datasourceId` → 全部 422 | 统一 `_kpiPayload(seed_data_source)` / 注入 fixture；`test_kpi_catalog_schemas.py` 补 `datasource_id=1` |
| 3 | 前端创建 KPI 不发 `datasourceId` → 422；且 `KpiCatalogRead` 不返回绑定关系 | 前端必填 Select + 列表列 + i18n；`KpiCatalogRead.datasource_id` |
| 4 | `test_delete_datasource_sets_kpi_datasource_id_to_null` 在 0132 RESTRICT 下失效 | 改名 `test_delete_referenced_datasource_raises_fk_violation`，断言 `IntegrityError` + KPI 未解绑 |

另修 model/migration 漂移：`models.py` FK 仍写 `SET NULL` → 改为 `RESTRICT`（commit `10d9bb60`）。

**验证**：0.1 的 9 个文件 **98 passed**（修复前 48 failed）；前端 `KpiCatalogPage` 15 passed。

## 2. 后续项交付

### 2.1 P2a — DS 删除守卫 corner cases（Task 1，`3425e7cb`）

`test_datasource_delete_kpi_guard.py` 新增 4 例：>1 引用 count 精确；DEPRECATED 仍阻止；HTTP 链路 409（断言 `"KPI"` + `"1 个"`）；改绑后原 DS 可删（计数实时）。

### 2.2 P1b — 部署顺序门禁（Task 2，`4fccf8f8`）

- `test_alembic_0132.py`：upgrade → `confdeltype='r'`；downgrade 0131 → `'n'`；链路可达。含写库闸（只认测试库）。
- `scripts/preflight_kpi_fk.py`：alembic_version / FK `confdeltype` / 已绑定行数（信息项）三项；任一 FAIL → exit 1。适配 asyncpg 对 `"char"` 返回 bytes。
- `test_preflight_kpi_fk.py`：head 全 PASS + 反向守卫（约束名写错必须 FAIL）。
- `operations-runbook.md`：先 backend 后 frontend；旧前端+新后端 → 422，反向安全；回滚口径 `downgrade 0131`。

### 2.3 P2b — 未绑定 KPI 报告（Task 3，`91bc278a`）

`KpiCatalogService.listKpis(unbound=True)` + `GET /api/v1/kpi-catalog?unbound=true`；前端 client 可选参数。3 例集成测试（只返回 NULL / 默认全部 / 显式 false 同默认）。

### 2.4 P1a — 数据源建议（Task 4，`c698c8e4`）

`KpiDatasourceSuggestionService`：legacy 名称精确 0.90 / ontology 表反查 0.80 / legacy 模糊 0.55；只取启用源、同源合并 reasons、最多 5 条。
`GET /api/v1/kpi-catalog/{id}/datasource-suggestions`。前端编辑表单展示 Alert（建议名 + 依据）+「采用」按钮——**绝不自动写入**；建议与当前绑定相同时不提示。
测试：后端 7 例（三类信号 / 停用排除 / 空 / 404 / 上限），前端 2 例。

### 2.5 P0 — 回填工具（Task 5，`2d088f99` + `b810ef08`）

- **批量 admin API**：`POST /api/v1/kpi-catalog/bulk-bind-datasource`（`getAdminOnlyActor`，上限 1000 行，`?force=true`）。`KpiDatasourceBindingService.bulkBind` 逐行 commit、逐行交代 `bound / skipped / failed`；写路径复用 `updateKpi`（校验 + ACL + outbox/审计/history + 匹配缓存）。
- **回填脚本** `scripts/backfill_kpi_datasource.py`：默认预览（未绑定 KPI + 建议，绝不写库）；`--mapping m.csv --apply` 落库（`datasource` 可填 ID 或名称）；`--force` 覆盖已绑其他源的行；幂等（二次执行全 skipped）；有 failed → exit 1。
- 测试：bulk API 5 例（混合批次/force/幂等 skip/403/超限 422）；脚本 4 例（CSV 解析守卫/预览只读/名称+ID 解析/幂等）。
- **安全审查加固**（`d7da6680`）：
  - 请求体在 Pydantic 解析阶段即限制 1000 行（`Field(max_length=...)`），不再先解析超大 JSON；
  - `bulkBind` 服务层显式断言 admin（**绕过路由直调也 fail-closed**），新增直调 service 的 403 用例；
  - 回填数据源名称只接受**无歧义**解析（精确大小写优先，其次唯一的大小写不敏感匹配）；重名多源时该行 failed 并提示改用 ID，新增歧义用例；
  - runbook 记录批量写端点的 stub 鉴权前提（`APP_ENV=production` + `AUTH_MODE=real` + `AUTH_STUB_ENABLED=0` + 不裸露 backend `8000` 端口）。

## 2.6 代码审查整改（`review-fixes` 提交）

- **消除假绿断言**：bulk skip 用例改用 `_reload(populate_existing)`；删除守卫用例跨 session 重读 DS；无信号用例先 seed 一个无关启用源（否则空源早退，删掉整个匹配器测试仍绿）；preflight 断言改为检查具体文案。
- **补覆盖**：批量绑定**停用数据源** → failed；同源双信号 → reasons 合并；`schema_data` 畸形条目（非 dict）不 500；墓碑 class（`valid_to`）不参与建议。
- **建议服务**：`SuggestionContext` 一次加载数据源 + schema_cache，回填预览 N 个 KPI 只扫一遍（此前逐 KPI 全量加载）。
- **preflight 版本比较**：只认数字 revision（hash revision `b1ea6cb32588` 此前被字符串比较误判为 `>=0132`）；多 head 时逐行要求达标。
- **回填脚本**：CSV 空 `kpi_code`/`datasource`、超过 1000 行 → 干净报错 + exit 1（不再抛 traceback）。
- **删除竞态**：`datasourceService.delete` 的计数查询与 commit 之间若有 KPI 并发绑定，FK RESTRICT 抛 `IntegrityError` → 映射为 409 而非 500。
- `bulkBind` docstring 澄清：只有三类领域错误逐行隔离，意外错误直接抛出（已提交行保留、重跑幂等）。

## 3. 用户已定决策

| 决策点 | 落定值 |
|---|---|
| 分支 | 先在 #55 补 fix commit；followups stack 在更新后的 #55 之上，1 分支 1 PR |
| P0 形态 | 回填脚本 **+** 批量 admin API |
| P1a 范围 | 建议 API + 前端提示 + 一键采用；**不自动绑定** |
| 执行顺序 | 修 #55 → P2a → P1b → P2b → P1a → P0 → 评估 |

## 4. 关键设计取舍

1. **建议永不自动绑定**：基于启发式自动改数据不可接受；只有用户点「采用」才写字段。回填脚本同理——默认只预览，`--apply` 才落库。
2. **bulk 逐行 commit**：部分成功是设计语义（同 `entity-mappings/bulk`）；不做整批事务。
3. **单一路径**：建议实现只有 `KpiDatasourceSuggestionService` 一处，脚本预览与编辑页提示共用；写路径只有 `updateKpi`，bulk/脚本/单条共用。
4. **RESTRICT 是双保险**：service 409 守卫给可读错误，DB FK 兜底防绕过。

## 5. 剩余风险与下一步

- **legacy NULL 行仍需人工确认映射**：脚本只做建议与落库，不判断业务正确性；建议里 `legacy_name_fuzzy` 分值低（0.55），批量落库前应先抽样核对。
- **ontology 反查依赖 `schema_cache`**：未 introspect 的数据源不会命中 `ontology_table` 信号（静默少一条建议，不报错）。
- **部署顺序**：backend 必须先于 frontend（0132 + 必填契约）；上线前后跑 `preflight_kpi_fk.py`。
- **批量写入口的鉴权依赖部署配置**：stub 模式下 `X-User-Roles` 由客户端自填，任何人都能请求 admin 端点。代码侧已做路由 + service 双层 admin 断言，但最终安全边界是 `AUTH_MODE=real` + `AUTH_STUB_ENABLED=0` + 代理剥离 `X-User-*` + 不暴露 backend 端口（见 runbook）。
- **测试环境**：全部集成测试跑真实 PG 上的**隔离测试库** `qa_metadata_test_solo`（5433 实例；与业务库 `qa_metadata` 同名不同库，勿混用）。

## 6. 相关

- PR #55：F2 spec + 实施（修复后 mergeable）
- followup PR：本分支（P2a / P1b / P2b / P1a / P0）
- 运行手册：[`Harness/wiki/operations-runbook.md`](../../wiki/operations-runbook.md) §「F2 KPI–数据源 FK 上线顺序」
