# 变更：fix-ontology-batch-frontend-timeout

- **日期**：2026-10-23
- **作者**：启琳
- **Phase**：done
- **状态**：done（本地验证绿；生产待重新构建前端镜像后复测）

## 1. 需求

**现象**：CentOS 7 首装后，本体管理 → 批量关系引擎的三个操作
（本体入图 syncGraph / 按共享列推断物理关联 inferJoins / 应用关系清单
applyManifest）统一报「网络异常，请稍后重试」。

**根因**：前端 axios 全局超时 `REQUEST_TIMEOUT_MS = 30_000`（config.ts:6），
`POST /ontology/batch` 等长操作**无 per-call 超时**，生产数据量下执行超 30s
⇒ axios 客户端掐断连接 ⇒ 无 HTTP 响应（status=0）⇒ 命中
`client.ts` 的 `errors.networkError` 文案。nginx 侧
`proxy_read_timeout=300s` 并不是瓶颈；开发环境数据少所以从未暴露。

**同族受影响调用**（共 4 个，全部无 per-call 超时）：
- `POST /ontology/batch`（三按钮共用）
- `POST /ontology/batch/preview`
- `POST /ontology/relations/backfill`
- `POST /ontology/graph/sync-missing`（图谱对账；此前 `unread-count` 502 的
  连带嫌疑——长操作占满 uvicorn worker）

**实测证据**：前端 i18n `networkError` 仅在 status=0（无响应）路径出现；
后端无对应 4xx/5xx。待生产 F12 复核 Time≈30000ms / Status=(canceled)。

## 2. 设计评审

| 方案 | 结论 |
|---|---|
| A. 调大全局 `REQUEST_TIMEOUT_MS` | 否。普通请求错误挂死 280s 才报错，伤害所有页面 |
| B. 长操作改 202 + 后台任务 + 轮询 | 正解但量大（新建 job 基建）。记入遗留，超 280s 时再启动 |
| C. 4 个长操作调用传 per-call 超时 280s | **采纳**（axios 支持逐请求覆盖；280s < nginx 300s 留余量） |

**验收标准**：新测试锁定 4 个调用都带 `{ timeout: LONG_RUNNING_TIMEOUT_MS }`
且常量 = 280_000（< nginx 300s）；tsc + 相关组件测试全绿。

## 3. 实施清单

- `frontend/src/config.ts`：新增 `LONG_RUNNING_TIMEOUT_MS = 280_000`（含
  nginx 300s 余量说明，防后人「顺手调齐 300」顶掉 nginx 上限）
- `frontend/src/api/ontology.ts`：4 个调用补第三参 `{ timeout: ... }`
- `frontend/src/tests/ontologyBatchApiTimeout.test.ts`：新增 5 断言
  （4 调用 + 常量边界守卫），TDD RED→GREEN

## 4. 验收结果

- 新测试 5/5 绿；`tsc -b` 0 错
- 回归：OntologyPage / SemanticRelationTab / JoinTab / api.test 共 **76/76 绿**
- 生产复测待前端镜像重建（`docker compose build frontend`，见部署规范）

## 5. 运维语义（重要）

axios 超时只是**前端不再等待**，后端操作仍在继续跑。超时后**不要**立刻
重试——先刷新页面看结果（之前 `unread-count` 502 即 worker 被长操作占满的
连带效应，重试只会叠加压力）。确认中断幂等前，重复点击可能产生并发重复写。

## 6. 遗留

- 若某操作实测超 280s（nginx 300s 也兜不住）⇒ 启动方案 B
  （202 + 轮询，参考研究线 bg-task 架构）
- 相关 wiki（本体管理/批量关系引擎章节）补「前端超时 280s」说明——
  合并 main 前按变更记录强制规范 §一.4 补齐
