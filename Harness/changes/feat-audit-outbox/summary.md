# 变更：feat-audit-outbox

- **日期**：2026-08-30
- **作者**：启琳
- **Phase**：Phase 4.5 扩展
- **状态**：✅ 完成

## 1. 需求

把 Phase 4.5 的「业务 + 审计同事务」改为 outbox 模式，**审计失败不回滚业务**。

当前模式（脆弱）：

```python
# 业务 service (KpiCatalogService.createKpi)
session.add(entity)
await session.flush()
await self._audit.record(session, ...)   # 写 audit_log
await self._history.snapshot(session, ...) # 写 kpi_catalog_history
await session.commit()  # 业务 + 审计一起 commit
# 如果 audit_log INSERT 失败 → 业务回滚 → 用户 KPI 没创建成功
```

目标模式（解耦）：

```python
# 业务 service
session.add(entity)
await session.flush()
await self._outbox.enqueue(session, payload={...})  # 写 outbox
await session.commit()  # 业务 + outbox 标记一起 commit（同事务即可）

# 独立 worker 进程
while True:
    pending = SELECT * FROM audit_outbox WHERE processed_at IS NULL LIMIT 100;
    for row in pending:
        write_to_audit_log(row)
        write_to_kpi_history_if_kpi(row)
        UPDATE audit_outbox SET processed_at = now() WHERE id = row.id;
    sleep(1)
```

**验收标准**：

- 引入 `audit_outbox` 表 + 配套索引
- 业务 service 改造：审计写入替换为 `outbox.enqueue`，原 `audit.record` / `history.snapshot` 直接调用删除
- 新增 worker 进程 `qa-system-audit-worker`：每 1 秒扫一次 outbox，写 audit_log + kpi_catalog_history
- 模拟故障：worker 进程崩溃 → 重启后从中断处继续（不丢消息、不重复写）
- 业务 service 改动后所有现有测试通过
- 新增专项测试：outbox enqueue / worker drain / 重启恢复 / 重复处理幂等
- 覆盖率 ≥ 80%

## 2. 设计评审

**方案对比**：

| 方案 | 优点 | 缺点 |
|---|---|---|
| **A. 独立 worker 进程**（已确认） | 部署独立、可监控、可水平扩展 | 多一个容器 |
| B. 同进程后台 task | 部署简单 | 业务进程崩 = 审计停；监控困难 |
| C. CDC + Kafka | 最强保证 | 运维复杂度爆炸；本期不做 |

**Outbox 表设计**：

```sql
CREATE TABLE audit_outbox (
  id BIGSERIAL PRIMARY KEY,
  event_type VARCHAR(50) NOT NULL,        -- 'kpi_created' / 'kpi_updated' / 'kpi_deleted' / 'ontology_class_updated' / ...
  entity_type VARCHAR(50) NOT NULL,
  entity_id INTEGER,                       -- 业务表 id（DELETE 后 entity 可能为 NULL；用 entity_id_history 软引用）
  actor VARCHAR(100) NOT NULL,
  actor_departments JSONB,                 -- tuple[str, ...]
  payload JSONB NOT NULL,                  -- { before: {...}, after: {...} }
  created_time TIMESTAMP WITH TIME ZONE DEFAULT now(),
  processed_at TIMESTAMP WITH TIME ZONE,
  attempts INTEGER DEFAULT 0,
  last_error TEXT
);
CREATE INDEX ix_audit_outbox_pending ON audit_outbox (created_time) WHERE processed_at IS NULL;
CREATE INDEX ix_audit_outbox_processed ON audit_outbox (processed_at) WHERE processed_at IS NOT NULL;
```

**Worker 进程**：

- 启动方式：docker-compose 新增 `audit-worker` service
- 命令：`uv run python -m app.workers.audit_worker`（新模块）
- 依赖：共享 `app/infrastructure/database.py` 引擎 + `app/services/audit_service.py` + `HistoryService`（**这两个 service 不能改**，worker 只是迁移调用方）
- 优雅停机：捕获 SIGTERM → 处理完当前 batch 后退出
- 重试：`attempts >= 5` 时停止重试，标记 `last_error`，alert 上报

**幂等性**：

- 审计表（audit_log）目前无幂等键（id 自增）
- 改造后：worker 用 `(entity_type, entity_id, action, outbox_id)` 做幂等键；重复处理同一 outbox_id 直接跳过（UPDATE audit_outbox WHERE id=? AND processed_at IS NULL）
- `kpi_catalog_history` 已用 `(kpi_id, revision)` 唯一约束兜底

## 3. 数据模型变更

**新迁移**：`backend/alembic/versions/0027_audit_outbox.py`

（详细 schema 见 §2）

## 4. 接口契约变更

**业务 service 改造**（删除 `audit_service.record` / `history_service.snapshot` 的直接调用）：

| Service | 原调用 | 新调用 |
|---|---|---|
| `KpiCatalogService.createKpi` | `audit.record(...)` + `history.snapshot(...)` | `outbox.enqueue(event_type="kpi_created", payload=...)` |
| `KpiCatalogService.updateKpi` | 同上 UPDATE | `outbox.enqueue(event_type="kpi_updated", payload={before, after})` |
| `KpiCatalogService.deleteKpi` | `audit.record(...)` | `outbox.enqueue(event_type="kpi_deleted", payload=before)` |
| `OntologyService.updateClass` (ACL 完成后) | `outbox.enqueue(event_type="ontology_class_updated", ...)` | 同模式 |
| `EntityMappingService.updateMapping` | 同上 | 同上 |
| `DataQualityService.updateRule` | 同上 | 同上 |

**`AuditService` / `HistoryService`**：

- **保留**作为 worker 进程内的写入路径（worker 调用它们写 audit_log / kpi_catalog_history）
- 不再有业务 service 直接调用

## 5. 实现要点

**关键文件**：

| 文件 | 改动 |
|---|---|
| `backend/alembic/versions/0027_audit_outbox.py` | 新增 audit_outbox 表 |
| `backend/app/services/outbox_service.py` | 新增（enqueue） |
| `backend/app/workers/audit_worker.py` | 新增（worker 主循环） |
| `backend/app/workers/__init__.py` | 新增 |
| `backend/app/services/kpi_catalog_service.py` | 替换 audit.record → outbox.enqueue |
| `backend/app/services/ontology_service.py` | 同上 |
| `backend/app/services/entity_mapping_service.py` | 同上 |
| `backend/app/services/data_quality_service.py` | 同上 |
| `backend/app/main.py` | lifespan 不再处理 audit（worker 是独立进程） |
| `docker/docker-compose.yml` | 新增 `audit-worker` service |
| `docker/Dockerfile.audit-worker` | 新增（基于 backend image，加 `CMD ["python","-m","app.workers.audit_worker"]`） |
| `backend/pyproject.toml` | 无新依赖 |

**Worker 主循环模板**：

```python
# app/workers/audit_worker.py
import asyncio, signal, logging
from app.infrastructure.database import getEngine, disposeEngine
from app.services.audit_service import AuditService
from app.services.history_service import HistoryService

class AuditWorker:
    def __init__(self, poll_interval: float = 1.0, batch_size: int = 100, max_attempts: int = 5):
        ...
    async def run(self) -> None:
        while not self._stop_event.is_set():
            await self._drain_once()
            await asyncio.sleep(self._poll_interval)
    async def _drain_once(self) -> None:
        # 1. SELECT ... WHERE processed_at IS NULL AND attempts < max
        # 2. for each: write audit_log + kpi_history + UPDATE outbox processed_at
        # 3. on error: UPDATE outbox attempts = attempts + 1, last_error = ...
```

**优雅停机**：注册 `signal.SIGTERM` handler 设 `_stop_event`，循环检测后退出。

**监控指标**（worker 日志，Phase 7+ 接 Prometheus）：

- `outbox.pending_count`：`SELECT COUNT(*) FROM audit_outbox WHERE processed_at IS NULL`
- `outbox.oldest_unprocessed`：超过 60s → alert

## 6. 测试

**单测**：

- `test_outbox_service.py`：enqueue / 序列化 payload / tuple → list 转换
- `test_audit_worker.py`：mock drain_once / 错误重试 / 超过 max_attempts 停止 / 优雅停机

**集成**（**worker 必须在真实 PG 上跑**，sqlite 不支持 SKIP LOCKED）：

- `test_outbox_integration.py`（新）：
  - 业务 service 触发 → outbox 写入 → 调用 worker.drain_once → audit_log 落地
  - 重复 drain（幂等）：再跑一次 → audit_log 不增加
  - DELETE 后再 drain → audit_log 仍有（FK ON DELETE SET NULL）
  - 异常注入（让 audit_service 抛错）→ outbox.attempts += 1，last_error 有内容
- `test_kpi_catalog_service_outbox.py`（新）：业务 service 改造后所有原行为不变

**手动冒烟**：

```bash
docker compose up -d audit-worker
# 触发 KPI 创建
curl -X POST .../api/v1/kpi-catalog ...
sleep 2
# 验证 audit_log 有这条
psql -h localhost -5433 -U qa_user qa_metadata -c "SELECT * FROM audit_log ORDER BY id DESC LIMIT 1"
```

## 7. 安全审查

- **触发 security-reviewer**：是。
- **关注**：
  - Outbox payload 是否包含敏感字段（password / secret）→ 在 `enqueue` 前过滤（白名单字段）
  - Worker 进程只读权限（数据库用户应只允许 `SELECT/INSERT/UPDATE audit_outbox / audit_log / kpi_catalog_history`，不能改业务表）→ 创建专用 DB user `qa_audit_writer`
  - SQL 注入：worker 的 `UPDATE` 用参数化（不要 f-string）
  - 重试风暴：`max_attempts` + `last_error` 上限，避免 poisoned message 反复拉崩 worker

## 8. 部署验证

```bash
cd backend
TEST_DATABASE_URL=... .venv/bin/pytest \
  app/tests/unit/test_outbox_service.py \
  app/tests/unit/test_audit_worker.py \
  app/tests/integration/test_outbox_integration.py \
  app/tests/integration/test_kpi_catalog_service_outbox.py -v

# Docker 冒烟
cd docker && docker compose up -d audit-worker
docker logs qa-system-audit-worker --tail 20
# 触发业务操作，验证 audit_log 落地
```

## 9. 真实数据验证

- 跑 Phase 4.5 留下的全套 KPI 操作（CREATE 3 / UPDATE 2 / DELETE 1）
- 立即查 `audit_log` → 应为空（worker 还没消费）
- 等 2 秒 → 再查 → 应有 6 条
- 强杀 worker（docker kill）→ 重启 → 应从 pending 继续
- 全部输出贴进本节

## 10. 关联

- 前置：`feat-governance-hardening`（audit_service / history_service 接口）
- 前置：`feat-acl-extension-3-entities`（所有 service 都已写 audit）
- 前置：`feat-audit-history-api`（worker 写出去的 schema 必须被查询 API 看见，反向验证）
- 后置：`feat-jwt-keycloak`（outbox 入队时 actor 是 CurrentUser，JWT 接入不影响 dataclass）
- 规则：`Harness/wiki/operations-runbook.md`「迁移与端口映射」、`Harness/rules/开发流程规范.md`