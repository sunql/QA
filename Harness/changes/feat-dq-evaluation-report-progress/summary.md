# 变更：评估报告异步化 + 进度条轮询

- **日期**：2026-09-15
- **作者**：Claude
- **Phase**：数据质量（Phase 9 评估报告异步化）
- **状态**：done
- **关联变更**：[feat-dq-evaluation-report](../feat-dq-evaluation-report/summary.md)（前置：同步 createReport）→ 本次拆为后台任务
- **后续**：无（若评估时长 > 30s 可考虑 SSE 替代轮询）

## 1. 需求

旧 `POST /api/v1/data-quality/reports` 是同步 HTTP 请求：service 在请求生命周期里跑 `_computeSnapshot`（dispatcher 逐条评估 + 写违规样本 + 聚合），单次报告 30s~数分钟。前端只能干等 spinner，用户反馈「不要一晃而过，不知道在干什么」。

要求：

- 提交后立即返回 report 行
- 前端能看到「正在评估规则 X / 总 Y」实时数字
- 评估完成 / 失败时前端自动切到结果展示
- 失败时能展示错误信息

## 2. 设计评审

| 方案 | 描述 | 取错 |
|------|------|------|
| A. FastAPI `BackgroundTasks` + DB JSONB 进度 + 前端 1s 轮询 | 简单、复用同一服务进程；进度从 DB 读 | **选** |
| B. SSE（`StreamingResponse`） | 单向流，server push | 拒：与 FastAPI BackgroundTasks 一起用需要自管 asyncio 任务生命周期；progress 查询 N 次 ≈ SSE 的 1 个长连接，复杂度收益不成正比 |
| C. Redis / Celery 任务队列 | 重型 | 拒：当前架构无 Redis 依赖；评估 30s~数分钟不需要外部队列 |
| D. WebSocket | 双向 | 拒：YAGNI；本场景单向进度推送足够 |

最终：**A**。

### 关键设计点

1. **状态机扩展**：旧 `ReportStatus = DRAFT / PUBLISHED`；新增 `PENDING / RUNNING / COMPLETED / FAILED`。CheckConstraint 扩为这 6 个值（alembic 0074）。
2. **进度 JSONB 列**：`progress` JSONB 存 `{stage, completed, total, current_rule_id, current_rule_code, message, started_at, finished_at}`，每个进度更新独立 commit。
3. **BackgroundTasks**：FastAPI `BackgroundTasks.add_task(service.runSnapshotJob, report.id)`；函数内部自管 session（用 `app.infrastructure.database.getSessionFactory`），避免与请求 session 冲突。
4. **兜底**：catch 所有异常写 status=FAILED + progress.message，绝不让后台任务挂进程；外层 try/except 再兜一次防 status=FAILED 写入失败。
5. **前端轮询**：detail 页 useEffect 监听 `report.status`，PENDING/RUNNING 时 `setInterval(1000)` 拉 `/progress`，COMPLETED/FAILED 时拉一次完整 report 后停止。
6. **进度 UI**：antd `Progress`（active / success / exception）+ 当前正在评估的 rule_code + 失败原因 message。

## 3. 改动清单

### 3.1 后端

| 文件 | 改动 |
|------|------|
| `backend/alembic/versions/0074_dq_eval_report_progress.py` | **新文件**：drop+create CheckConstraint 扩为 6 值；新增 `progress JSONB` 列；新增 `ix_evaluation_report_status_running` 部分索引 |
| `backend/app/domain/enums.py` | `ReportStatus` 加 PENDING / RUNNING / COMPLETED / FAILED |
| `backend/app/domain/models.py` | `EvaluationReport.progress: Mapped[dict[str, Any] \| None]` + CheckConstraint 同步 |
| `backend/app/domain/schemas.py` | 新增 `EvaluationReportProgress`；`EvaluationReportRead.progress` 字段 |
| `backend/app/services/evaluation_report_service.py` | `createReport` 拆：同步只写 PENDING 行；新增 `runSnapshotJob` 自管 session + 进度更新 + 兜底写 FAILED；新增 `getProgress` |
| `backend/app/api/v1/evaluation_report.py` | `createEvaluationReport` 加 `BackgroundTasks` 依赖、`add_task(runSnapshotJob)`；新增 `GET /reports/{report_id}/progress` |

### 3.2 前端

| 文件 | 改动 |
|------|------|
| `frontend/src/types/evaluationReport.ts` | `ReportStatus` 扩 4 个值；新增 `EvaluationReportProgress` / `ProgressStage` |
| `frontend/src/api/evaluationReport.ts` | 新增 `getReportProgress(id)` |
| `frontend/src/pages/DataQualityReportCreatePage.tsx` | 重写为 4 步 wizard（选对象 → 每对象选规则 → 组合列表 → 时间窗口） |
| `frontend/src/pages/DataQualityReportDetailPage.tsx` | 顶部加进度卡；useEffect 监听 status 触发 1s 轮询；COMPLETED/FAILED 拉完整 report 后停止 |
| `frontend/src/i18n/zh-CN.ts` / `en-US.ts` | `statusLabels` 加 PENDING/RUNNING/COMPLETED/FAILED |

## 4. 验收

```bash
# 1. 后端测试
docker exec qa-backend bash -c 'cd /app && \
  TEST_DATABASE_URL="postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata_test" \
  python3 -m pytest app/tests/unit/test_data_quality_score_service.py \
            app/tests/integration/test_data_quality_score_api.py -q'
# 期望：32 passed

# 2. 迁移 apply
docker cp backend/alembic/versions/0074_dq_eval_report_progress.py qa-backend:/app/alembic/versions/
docker exec qa-backend bash -c 'cd /app && alembic upgrade head'
docker exec qa-backend bash -c 'cd /app && alembic current'
# 期望：0074_dq_eval_report_progress (head)

# 3. 后端代码 cp + restart
docker cp backend/app/domain/enums.py qa-backend:/app/app/domain/enums.py
docker cp backend/app/domain/models.py qa-backend:/app/app/domain/models.py
docker cp backend/app/domain/schemas.py qa-backend:/app/app/domain/schemas.py
docker cp backend/app/services/evaluation_report_service.py qa-backend:/app/app/services/evaluation_report_service.py
docker cp backend/app/api/v1/evaluation_report.py qa-backend:/app/app/api/v1/evaluation_report.py
docker restart qa-backend

# 4. 端点注册
curl -s http://localhost:8000/openapi.json | python3 -c "import json,sys; print('\n'.join([p for p in json.load(sys.stdin)['paths'] if 'progress' in p]))"
# 期望：/api/v1/data-quality/reports/{report_id}/progress

# 5. 前端编译
cd frontend && npx tsc --noEmit
# 期望：0 errors
```

浏览器手动：

- 进 `/data-quality/reports/new` → wizard 4 步走 → 创建后跳转 detail
- 详情页顶部立刻看到「PENDING」+ 「0 / N」+ 「排队中」；几秒后变「RUNNING」+ 「M / N」+ 「正在评估：rule_xxx」
- 完成后自动切「COMPLETED」，下方 KPI / 维度图 / 规则明细展示

## 5. 风险 & 缓解

| 风险 | 缓解 |
|------|------|
| BackgroundTasks 与 FastAPI reload 冲突 | 生产用 uvicorn workers；reload 仅 dev |
| status=FAILED 写入失败 | 外层 try/except + 日告警；进程不挂 |
| 进度更新频率过高拖慢 DB | 现状「一次性写」 RUNNING → COMPLETED + snapshot；未做 per-rule 进度更新（避免 N 次 commit） |
| 旧同步报告无 progress 字段 | `progress` nullable；前端 `progress?.stage ?? report.status` fallback |
| CheckConstraint 扩展 | 0074 迁移前旧报告 status 只有 DRAFT/PUBLISHED，迁移后新值合法；老报告 PENDING 不在历史里，无兼容问题 |
| Alembic 文件名长度 | 30 字符（≤32 OK，参见 [[alembic-version-filename-32-char-limit]]） |

## 6. 回滚

- 后端 6 文件：service / api 改回同步 `createReport`；删 `progress` 列 + 缩 CheckConstraint（downgrade 已写）
- 前端 5 文件：detail 页去掉 progress 卡 / 轮询 effect；CreatePage 回滚到 3.0 单页表单
- alembic `alembic downgrade -1`