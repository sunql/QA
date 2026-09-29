# 变更：评估报告（同步 createReport + 多对象创建向导）

- **日期**：2026-09-15（事后补档；特性实现于同日更早的 commit）
- **作者**：Claude
- **Phase**：数据质量（Phase 9 评估报告）
- **状态**：done（snapshot 部分 superseded by [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md)）
- **关联变更**：被 [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md) 把 snapshot 拆为后台任务；多对象创建向导保持不变

## 1. 需求

数据质量模块在 Phase 8 已经有「规则定义 + 单条评估」能力，但缺乏「一份聚合报告」：用户需要「选定一组业务对象（ontology_class）→ 每对象选规则 → 时间窗口 → 一次性产出快照（snapshot），含：KPI、维度分布、规则明细、违规样本」。本特性建立 `evaluation_report` 表 + `evaluation_report_rule` 关联表 + 创建/详情/重新生成/删除 API + 4 步 wizard UI。

## 2. 设计评审

### 2.1 快照生成时机

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 创建时同步生成 snapshot | 简单 | **初选**（同 commit 实现） |
| B. 创建时只写 PENDING，snapshot 后台跑 | 解耦但需 background infra | 拒：初期同步够用，snapshot 慢的事后续单独改 |
| C. 物化视图定时刷新 | 重型 | 拒：报告按需生成，无需持续刷新 |

最终：**A**；snapshot 耗时由 [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md) 异步化解决。

### 2.2 创建向导步骤

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. 4 步 wizard（选对象 → 每对象选规则 → 组合列表 → 时间窗口） | 步骤清晰 | **选** |
| B. 单页表单 | 简单 | 拒：选多个对象时规则筛选体验差；保留 `class_ids` 数组 schema 但 UI 重做 |

最终：**A**；schema 沿用 `class_ids: list[int]`，UI 重做。

### 2.3 snapshot 存储

| 方案 | 描述 | 取舍 |
|------|------|------|
| A. JSONB 整块存 `snapshot: dict` | 灵活 | **选** |
| B. 拆表 `evaluation_report_metric` 等多张表 | 强结构 | 拒：报告形态多变（不同规则产出不同结构），JSONB 解析时按 snake_case 读，Phase 4 强类型化再切 |

最终：**A**。

## 3. 改动清单

### 3.1 后端

| 文件 | 改动 |
|------|------|
| `backend/alembic/versions/0069_dq_evaluation_report.py` | **新文件**：新建 `evaluation_report` 表（含 `snapshot JSONB`、`class_ids JSONB`、`status CHECK (DRAFT/PUBLISHED)`）；新建 `evaluation_report_rule` 关联表 |
| `backend/app/domain/enums.py` | 新增 `ReportStatus = DRAFT / PUBLISHED` |
| `backend/app/domain/models.py` | 新增 `EvaluationReport` / `EvaluationReportRule` ORM |
| `backend/app/domain/schemas.py` | `EvaluationReportCreate / EvaluationReportRead / EvaluationReportRuleRead` |
| `backend/app/services/evaluation_report_service.py` | `createReport`（同步）+ `regenerateSnapshot` + `deleteReport`；snapshot 计算走 dispatcher 逐条评估 |
| `backend/app/api/v1/evaluation_report.py` | 5 个端点：list / create / detail / regenerate / delete |

### 3.2 前端

| 文件 | 改动 |
|------|------|
| `frontend/src/types/evaluationReport.ts` | `EvaluationReport / EvaluationReportRule / ReportStatus` |
| `frontend/src/api/evaluationReport.ts` | CRUD + regenerate API |
| `frontend/src/pages/DataQualityReportListPage.tsx` | 报告列表 |
| `frontend/src/pages/DataQualityReportCreatePage.tsx` | 4 步 wizard |
| `frontend/src/pages/DataQualityReportDetailPage.tsx` | 详情页（snapshot 渲染 + KPI 卡片 + 维度图 + 规则明细表 + 违规样本表） |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | 报告相关翻译 |

## 4. 验收

```bash
docker exec qa-backend bash -c 'cd /app && \
  TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata_test" \
  python3 -m pytest app/tests/integration/test_evaluation_report_api.py -q'
# 期望：18 passed
```

手动：进 `/data-quality/reports/new` → wizard 4 步 → 创建后跳详情页 → 看到 KPI/维度图/规则明细/违规样本。

## 5. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| snapshot 同步生成阻塞 HTTP | 后被 [feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md) 拆为 BackgroundTasks |
| snapshot 内层键 Pydantic alias_generator 不递归 | 前端先按 snake_case 读；Phase 4 强类型化时再切 camelCase（见 [[dq-eval-report-snapshot-snake-case]]） |
| `class_ids` schema 不够灵活 | 暂时满足多对象场景；后续若要支持跨数据源组合再加 `datasource_id` |
| dispatcher 内部 silent except | 见 [[eval-dispatcher-silent-except]]，本特性未修；snapshot 仍可能被静默吞错 |

## 6. 回滚

- 后端：删除 2 张表 / 删 ORM / 删 service / 删 API；alembic downgrade
- 前端：3 页 + i18n 删

## 7. 后续 → Snapshot 异步化

本特性的 snapshot 部分（`createReport` 内 `_computeSnapshot`）由
[feat-dq-evaluation-report-progress](../feat-dq-evaluation-report-progress/summary.md)
拆为后台任务：`runSnapshotJob(report_id)` + `BackgroundTasks` +
`progress JSONB` + 前端 1s 轮询 `GET /reports/{id}/progress`。

创建向导（4 步 wizard）保持不变 —— 异步化只影响「创建后是否立刻看到结果」，
不影响「用户怎么提交一份报告」。
