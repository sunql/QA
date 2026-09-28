# API 参考

基址：`/api/v1`。JSON 字段为 camelCase（如 `modelName`、`sessionId`、`tokenUsage`）。

## 系统
| Method | Path | 说明 |
|--------|------|------|
| GET | /health | 健康检查 |

## 模型配置
| Method | Path | 说明 |
|--------|------|------|
| GET | /models | 列出（`?activeOnly=true`） |
| POST | /models | 创建（含明文 apiKey，服务端加密） |
| GET | /models/{id} | 获取 |
| PUT | /models/{id} | 更新 |
| DELETE | /models/{id} | 停用（软删除） |

## 会话
| Method | Path | 说明 |
|--------|------|------|
| GET | /sessions/{sessionId}/usage | 聚合摘要（请求数/Token/成本/按模型） |
| GET | /sessions/{sessionId}/usage/list | 明细流水 |

## 后续 Phase
| Method | Path | Phase |
|--------|------|-------|
| POST | /chat | 4 |
| POST | /knowledge/{define,map,metric} | 2 |
| GET/POST | /ontology/{classes,properties,metrics} | 2 |
| GET | /ontology/search | 2 |
| GET/POST | /datasource | 3 |
| POST | /datasource/{id}/test | 3 |
| POST | /datasource/{id}/query | 3 |
| GET/POST/PUT/DELETE | /data-quality/rules | 8 |
| POST | /data-quality/evaluate | 8 |
| POST | /data-quality/scores/compute | 9 |
| GET/POST | /data-quality/reports | 9 |
| GET | /data-quality/reports/{report_id} | 9 |
| GET | /data-quality/reports/{report_id}/progress | 9 |
| POST | /data-quality/reports/{report_id}/regenerate | 9 |
| DELETE | /data-quality/reports/{report_id} | 9 |

## 错误响应

统一 `ErrorResponse`：`{ success: false, error: <message>, detail: <optional> }`。

| 异常 | HTTP |
|------|------|
| NotFoundError | 404 |
| ValidationError | 422 |
| 其他 DomainError | 400 |

## Phase 9 增量：数据质量评分 / 评估报告

**日期**：2026-09-15 · **变更**：[feat-dq-scores-multiselect](../changes/feat-dq-scores-multiselect/summary.md) · [feat-dq-evaluation-report](../changes/feat-dq-evaluation-report/summary.md) · [feat-dq-evaluation-report-progress](../changes/feat-dq-evaluation-report-progress/summary.md)

### POST /data-quality/scores/compute

```typescript
// Request
{
  datasourceId: number,             // 单选，必填
  targetTables?: string[],         // 多选；null/[] = 全表
  ruleTypes?: RuleType[],          // 多选；null/[] = 全规则类型
  timeWindow?: { start: string, end: string }
}
// Response: { rows: ScoreRow[], total: number }
```

`datasourceId` 单选；`targetTables` / `ruleTypes` 多选，AND 组合三条件。

### POST /data-quality/reports（异步）

```typescript
// Request
{
  name: string,
  datasourceId: number,
  classIds: number[],              // 多对象
  rules: { classId: number, ruleIds: number[] }[],
  timeWindowStart: string,
  timeWindowEnd: string
}
// Response: { id, status: 'PENDING', ... }   // 立即返回，后台跑 snapshot
```

提交后**立即**返回 `status: 'PENDING'` 的报告行；不再同步等待 snapshot 生成。

### GET /data-quality/reports/{report_id}/progress

```typescript
// Response
{
  stage: 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED',
  completed: number,
  total: number,
  current_rule_id: number | null,
  current_rule_code: string | null,
  message: string | null,
  started_at: string | null,
  finished_at: string | null
}
```

注意：**该路由必须注册在 `/reports/{report_id}` 之前**，否则会被路径参数匹配吞掉。

### 状态机

| status | 含义 | 是否终态 |
|--------|------|----------|
| DRAFT | 草稿（用户编辑中） | 否 |
| PUBLISHED | 已发布（业务可见终态） | 是 |
| PENDING | 排队等待后台任务拉起 | 否 |
| RUNNING | snapshot 进行中 | 否 |
| COMPLETED | snapshot 完成 | 是 |
| FAILED | snapshot 失败（progress.message 有原因） | 是 |
