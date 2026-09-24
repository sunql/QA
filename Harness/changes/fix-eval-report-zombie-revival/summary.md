# 变更：feat-dq-evaluation-report zombie 复活（补 4 ORM 类 + 装 router + 测试 app 装载 + 菜单入口）

- **日期**：2026-09-16
- **Phase**：bugfix（DQ 评估报告特性 zombie 复活）
- **状态**：done
- **关联变更**：[feat-dq-evaluation-report](../feat-dq-evaluation-report/summary.md)（snapshot 同步版）/ [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md)（异步 + progress）
- **MEMORY**：[[qa-system-eval-report-zombie-revival]]

---

## 1. 需求

用户点开 DataQualityPage 的「评估报告」tab → 调用 `GET /api/v1/data-quality/reports?limit=20&offset=0` → **HTTP 404**。
`evaluationReport.ts:43` 报 `404 (Not Found)`，DataQualityReportListPage 渲染失败。

验收标准：

- 后端提供 `GET /api/v1/data-quality/reports` 真实可调（200 OK + 真实数据）
- uvicorn 启动不踩 ImportError / Schema drift 阻断
- 集成测试 `test_evaluation_report_api.py` 大部分用例通过（> 80%）
- 前端 `/data-quality` 页面 + 评估报告 tab + 菜单入口可发现
- 不破坏现有 demo / supplier-360 / 风险 等所有其他路由

## 2. 设计评审

### 根因（**zombie code** — 整特性从未真正上线过）

| # | Bug | 证据 | 修复位置 |
|---|---|---|---|
| 1 | `main.py` 缺 `app.include_router(evaluation_report.router, ...)` | grep 返回空（40+ router 列表里完全没有） | `backend/app/main.py:254,313` |
| 2 | `domain/models.py` 缺 4 个 ORM 类（EvaluationReport / DataQualityViolationSample / EvaluationReportSchedule / EvaluationReportShare） | `git log -S 'class EvaluationReport' -- backend/app/domain/models.py` **返回空** — 从未被提交过；alembic 0072/0074 已建表但 ORM 层缺失 | `backend/app/domain/models.py`（新增 ~200 行） |
| 3 | `tests/_testapp.py` 缺 `evaluation_report.router` 装载 | 与 #1 同模式：测试 app 独立装载 router（main.py 的副本），两处都得补 | `backend/app/tests/_testapp.py` |
| 4 | 菜单 seed 没 `item.dataQualityReport` | `seed_menu_config.py` 注释刻意写了"已下沉为 tab"，但生产路径靠 `?tab=reports` 间接抵达 | `seed_menu_config.py:68-70`（新增一行 + 更新注释） |

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| **(A) 补 4 ORM 类 + 4 处装载 + 菜单入口** | ✅ 推荐 | 与既有 feat-entity-mapping-seed / feat-supplier-name-resolver 等补 wiring bug 同款；4 个文件改动 |
| (B) 暂用 feature flag 把 router 装上但 disable | ❌ 不可取 | 治标不治本，下次还会绊倒；且 4 ORM 类缺位不补就还得 404 |
| (C) 删整个 feat-dq-evaluation-report 目录 | ❌ 不可取 | alembic 0072 + 0074 + PG 4 表 + 6 行历史 reports 都不能丢；删了就丢数据 |

### 关键设计点

- ORM 类与 alembic 0072 + 0074 DDL **完全对齐**（DDL 已固化在生产）；不强加新列、新约束
- EvaluationReport.status CheckConstraint 包含 6 值（DRAFT/PUBLISHED/PENDING/RUNNING/COMPLETED/FAILED），与 0074 扩值同步
- EvaluationReportSchedule.time_window_type CheckConstraint 3 值（LAST_7D/LAST_30D/LAST_RUN）
- EvaluationReportShare.share_token 用 `postgresql.UUID(as_uuid=False)` 走 PG 原生 UUID 类型（DDL 是 uuid）
- 部分索引（`ix_evaluation_report_status_running`、`ix_evaluation_report_schedule_due`）保留 `postgresql_where` 子句，SQLite 测试环境会忽略（dialect 不支持），符合项目「PG-only 优化用 postgresql_where」的既有约定
- menu_config 用 `on_conflict_do_update` 模式（已有），不影响 AdminMenusPage 用户编辑过的 sort_order

## 3. 数据模型变更

无 alembic 迁移（表早已建好，本次只补 ORM 类）。`Base.metadata.create_all` 在 SQLite 测试库会自动创建 4 张表（含 JSON/UUID 类型）。

## 4. 接口契约变更

无新接口。补齐 `GET /api/v1/data-quality/reports` 等 18 个端点（feat-dq-evaluation-report 已定义但从未装载）。OpenAPI 路径数 **189 → 207**（+18）。

## 5. 实现要点

- 4 个 ORM 类紧跟 models.py 末尾 `class InAppMessage` 之后；re-export 风格未变
- `main.py` include_router 加在 `data_quality_generate` 块后、`data_lineage` 前（按字母序 + 主题相近）
- `_testapp.py` 同样位置镜像修改（两处必须同步，缺一则 端点 404 / 测试 404）
- `seed_menu_config.py` 新菜单项 sort_order 326（夹在 `dataQualityRuleParams` 325 与 `lineage` 330 之间）；label_key `menu.item.dataQualityReport` i18n 已存在
- 部署路径：`docker cp` 三文件到容器 + `docker restart qa-backend`（无需 alembic）
- 启动期 seed_menu_config 在 lifespan 内会自动运行，把新菜单入库

## 6. 测试

| 套件 | 用例数 | 结果 |
|---|---|---|
| `test_evaluation_report_api.py` | 9 | ✅ 8 pass / 1 fail（fail 是 `test_list_filter_by_class_id` — 触发真实 evaluation 链 → 试连 THBI Oracle @ localhost:5432，被拒；非本 fix 范围，是测试环境缺 mock 的预存 issue） |
| 端到端 curl `GET /api/v1/data-quality/reports?limit=20&offset=0` | 1 | ✅ 200 OK / 18725B / 6 reports 含完整 snapshot |
| 端到端 OpenAPI 路径数 | 1 | ✅ 189 → 207（+18 eval-report 端点） |
| 端到端 menu-config | 1 | ✅ `item.dataQualityReport` 在 sort 326 可见 |
| 现有路由回归（supplier-360 / supplier-risk / data-quality/rules / data-quality/scores） | 5 | ✅ 200 OK |

## 7. 安全审查

未触发 security-reviewer。EvalReport API 走既有 `getCurrentUser` ACL；ORM 字段直接复用 alembic 既有 CheckConstraint（含 6 status 值约束 + 3 time_window_type 约束）。

## 8. 部署验证

```bash
# 备份（防止万一回滚）
mkdir -p backups/file-edit/20260916_1400
cp backend/app/main.py backend/scripts/seed_menu_config.py backups/file-edit/20260916_1400/

# 修改后端 4 个文件后：
docker cp backend/app/domain/models.py qa-backend:/app/app/domain/models.py
docker cp backend/app/main.py qa-backend:/app/app/main.py
docker cp backend/app/tests/_testapp.py qa-backend:/app/app/tests/_testapp.py
docker cp backend/scripts/seed_menu_config.py qa-backend:/app/scripts/seed_menu_config.py
docker restart qa-backend

# 验收
curl 'http://localhost:8000/api/v1/data-quality/reports?limit=20&offset=0' | jq '.total'
# 6
```

容器状态：qa-backend Up，无健康异常。

## 9. 关联

- 备份：`backups/file-edit/20260916_1400/main.py` + `seed_menu_config.py`
- 主修复：`backend/app/domain/models.py`（+~200 行 4 ORM 类）
- 4 处装载：`main.py` + `_testapp.py` + `seed_menu_config.py`（含 i18n）
- 关联变更：[feat-dq-evaluation-report](../feat-dq-evaluation-report/summary.md) / [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md)
- Memory：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-eval-report-zombie-revival.md`

### 遗留项（不在本 fix 范围）

1. **`test_list_filter_by_class_id` 测试环境缺 mock**：测试触发真实 evaluation 链，需连 THBI Oracle（test 无 Oracle）。建议给 `DataQualityEvaluatorDispatcher` 加个 test fixture 短路或在 conftest 里 mock `business_db_pool.get_adapter`。
2. **部署持久化**：本次走 docker cp（临时）；重建容器前需 `docker compose build backend && docker compose up -d backend` 固化（见 [[qa-system-stale-container-deploy]] / [[qa-system-docker-cp-merge]]）。
3. **pg dump 备份未做**：本次只备份文件，未做 PG 备份。下次类似修 OL 表前先 `pg_dump -t evaluation_report` 防 alembic 漂移。

### 同类教训

- **「git log -S 'class X' -- file 返回空」即 zombie code 红旗**：从未提交过的代码等同未实现，迁移/服务/router 全部只是死代码，必须把 ORM 也补上才算活。
- **「router 装载 + 测试 app 装载」必须成对改**：`main.py` 与 `_testapp.py` 是同一份装载逻辑的副本，单边改会出现「curl OK 但 pytest 404」。

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全
- [x] 9 段都非空
- [x] 第 2 段 3 候选方案
- [x] 第 3 段无 alembic 迁移（ORM 补全非迁移）
- [x] 第 7 段无安全敏感变更
- [x] 第 8 段 部署命令 + 验收输出
- [x] 第 9 段 5+ 跨文件链接
- [x] memory 待写（已列入第 9 段）