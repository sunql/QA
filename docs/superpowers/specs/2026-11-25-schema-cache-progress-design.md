# 缓存 Schema 进度弹窗 —— 设计（2026-11-25）

## 背景与问题

数据源页「缓存 Schema」当前走同步 `POST /datasources/{id}/introspect`：

1. **无进度**：后端一次性执行 5 个大查询（columns / PK / FK / 表注释 / 列注释）后直接返回，
   中途不产出任何可观测状态；大 owner（如 `LC019999`）耗时数分钟，用户只看到按钮转圈。
2. **误报超时**：前端默认 30s 超时（上一批已临时改为 280s 兜底）⇒ 超时后 `status=0` ⇒
   拦截器提示「网络异常，请稍后重试」，而内省其实仍在正常进行。
3. **无法区分两类失败**：「数据源连不上」与「内省耗时长」在用户侧表现相同。

## 目标

- 缓存 Schema 改为**带真实进度的弹窗**：先算表总数，按「已处理表数 / 总表数」推进进度条。
- **正常进行中永不判超时**；只有与服务器失联（轮询连续失败）或后端报错才提示。
- 数据源连接失败由后端明确回报（job `failed` + 错误文案）。

## 非目标

- 不改导入向导（`ImportWizard`）与 3 个 DQ 页面（`DataQualityPage` / `DataQualityRuleParamsPage` /
  `DataQualityRuleBatchCreatePage`）的同步调用 —— 下一批单独任务处理。
- 不做 job 持久化（刷新页面/后端重启后进度丢失可接受，重跑即可）。
- 不引入 SSE / WebSocket。

## 架构

### 后端

**新增内存任务注册表** `SchemaIntrospectJobRegistry`（模块级单例）：

| 字段 | 说明 |
|---|---|
| `jobId` | uuid4 hex |
| `datasourceId` / `schema` | 归属与归一后的 owner |
| `status` | `running` / `succeeded` / `failed` |
| `total` | 表总数（内省开始时查明） |
| `completed` | 已完成批覆盖的表数 |
| `error` | 失败原因（字符串） |
| `updatedAt` | 用于终态淘汰（终态保留 10 分钟） |

**并发保护**：同一 `(datasourceId, schema)` 已有 `running` job ⇒ 再次 POST 返回该 jobId，不重复启动。

**分批内省** `SchemaIntrospectionService.introspectBatched(ds, owner, onProgress)`：

```
1. 轻查询列出该 owner 全部表名（ALL_TABLES / information_schema.tables）
   total = N；onProgress(0, N)
2. 按 BATCH_SIZE = 50 张表切批，每批执行 5 类查询，均追加
   AND table_name IN (<本批表名>) 过滤：
     columns / PK / FK / 表注释 / 列注释
   合并进累积 dict（_mergeRows / _annotateComments 可跨批累积）
   onProgress(本批后已覆盖表数, N)
3. 全部完成 → 复用现有组装/版本计算/写缓存逻辑（与 introspectAndCache 一致）
```

**表名安全**：表名来自数据库元数据（非用户输入），单引号按 SQL 字面量转义（`'` → `''`），
不改变既有 owner 白名单校验。

**执行方式**：`BackgroundTasks` + `ownSession()`（后台任务自开会话，不复用请求会话 ——
见 `infrastructure/database.py` 的事故说明），与 `research.py` / `ontology_discovery.py` 同模式。

### API

```
POST /datasources/{id}/introspect-jobs?schema=LC019999
  202 { jobId, total, status: "running" }

GET  /datasources/{id}/introspect-jobs/{jobId}
  200 { status, total, completed, error }
  404 job 不存在或已被淘汰
```

旧同步接口 `POST /datasources/{id}/introspect` **保持不变**（导入向导与 DQ 页面继续使用）。

### 前端（仅 DatasourcePage）

```
点击「缓存 Schema」
  → POST /introspect-jobs → 打开 Modal
  → 每 1.5s 轮询 GET /introspect-jobs/{jobId}
  → Progress 条 +「正在缓存 schema：{completed}/{total} 张表」
```

弹窗状态机：

| job 状态 | 表现 |
|---|---|
| `running` | 进度推进；允许关闭弹窗（后台继续，再点按钮拿回同一 job） |
| `succeeded` | 自动关闭 + `message.success("已缓存 N 张表")` |
| `failed` | 弹窗内红字错误 + 「关闭」按钮，不自动关 |

## 错误处理

| 场景 | 处理 |
|---|---|
| 数据源连不上 | 第一批查询失败 ⇒ job `failed` + 连接错误文案；不写缓存 |
| 任一批 SQL 失败 | job `failed`；**不写缓存**（保持原子性，避免半成品污染 NL2SQL） |
| job 不存在 / 已淘汰 | GET 返回 404；前端停止轮询并提示重新发起 |
| 轮询连续 5 次失败（≈7.5s 失联） | 前端显示「与服务器连接中断，请检查网络后重试」，停止轮询 |
| 内省耗时长 | **不判超时**：轮询成功即视为进行中 |

**超时参数**：

- 轮询请求 per-call 超时 10s（读内存状态，正常毫秒级）
- 后端每个 SQL 查询仍受 `queryTimeoutSeconds`（默认 30s）保护 —— 这是「数据源无响应」的后端判据

## 测试策略（TDD）

后端：

- 分批合并正确性（多批结果与单次全量结果等价）
- 进度回调序列（0 → 批边界 → total）
- 任一失败 ⇒ job `failed` 且缓存表无写入
- 同源并发 POST ⇒ 返回同一 jobId，只启动一次
- 终态 job 过期淘汰

前端：

- POST 成功 ⇒ Modal 打开，Progress 按轮询数据推进
- `succeeded` ⇒ 自动关闭 + success toast
- `failed` ⇒ 显示错误、不自动关
- 连续 5 次轮询失败 ⇒ 中断提示并停止轮询
- 关闭弹窗后后台继续，再次点击拿回同一 job

## 影响面与部署

- 后端：新增 service 方法 + 注册表 + 2 个端点；无迁移。
- 前端：`api/datasource.ts` 新增 2 个函数；`DatasourcePage` 弹窗；无其他页面改动。
- 生产部署：随下次 backend + frontend 镜像构建生效（thdeploy 流程）。
