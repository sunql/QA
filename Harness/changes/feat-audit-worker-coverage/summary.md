# 变更：feat-audit-worker-coverage

- **日期**：2026-08-31
- **作者**：AI Assistant
- **Phase**：质量硬化（横切）
- **状态**：done

## 1. 需求

`app/workers/audit_worker.py` 在 Phase 5 验证时覆盖率仅 79.49%，低于 80% 门槛。补齐覆盖：
- `_applyEvent` 幂等前置检查（行 198）
- `_writeHistorySnapshot` FeatureDefinition 分支（行 244-245）
- `_processOne` 错误消息截断（>500 chars）+ max-attempts 告警 + 并发处理 no-op
- `run()` 主循环异常 + sleep 被打断
- `registerSignalHandlers` 主线程/非主线程路径
- `main()` 进程入口

验收：覆盖率 ≥80%，全部测试通过。

## 2. 设计评审

TDD RED → GREEN 流程：

1. 先按覆盖率报告的 missing 行清单设计针对性测试（11 个新用例）
2. 测试失败暴露实现缺陷（如 DataSource 字段名 `db_type` vs `type`、FeatureDefinition 有 NOT NULL FK）
3. 修测试 fixture 让 GREEN 通过（不修改被测代码）

## 3. 数据模型变更

无。测试 fixture 新增 `_enqueueFeatureEvent`：seed `DataSource` + `FeatureDefinition`，因 FeatureDefinition 有 `datasource_id` NOT NULL 约束（Phase 4 加的）。

## 4. 接口契约变更

无。

## 5. 实现要点

- `app/tests/unit/test_audit_worker.py` 新增 11 个用例：
  - `test_drain_once_writes_feature_history_snapshot`
  - `test_drain_once_skips_history_when_entity_deleted`
  - `test_drain_once_skips_non_routed_entity_history`
  - `test_drain_once_idempotent_pre_check`
  - `test_drain_once_truncates_long_error_message`
  - `test_drain_once_logs_error_at_max_attempts`
  - `test_process_one_returns_false_when_concurrent_worker_processed`
  - `test_run_loop_handles_drain_exception`
  - `test_register_signal_handlers_main_thread`
  - `test_register_signal_handlers_non_main_thread`
  - `test_main_entry_invokes_run`

## 6. 测试

- 18 个 audit_worker 用例（7 旧 + 11 新），全部通过
- 覆盖率：**79% → 97%**

## 7. 安全审查

无 auth / 权限 / 外部 API 变更。

## 8. 部署验证

`pytest app/tests/unit/test_audit_worker.py --cov=app.workers.audit_worker` 全过，覆盖率 96.58%（含 import 行）/ 97%（按 stmts 算）。

## 9. 关联

- 被测代码：`app/workers/audit_worker.py`（feat-audit-outbox 引入）
- 同类覆盖率补齐机会：未来跑 `pytest --cov-fail-under=80` 时按 missing 行清单逐个补测试

## 10. 决策记录

- 不修改 `audit_worker.py` 实现：测试已能覆盖其正确行为；改动实现会引入风险且偏离 TDD 意图
- 用 `monkeypatch.setattr(audit_worker.AuditService, "record", ...)` 替代全局替换：定位更精确、副作用小
- `run_loop_handles_drain_exception` 用 monkeypatch `app.infrastructure.database.getSessionFactory`：因 `audit_worker.run()` 内部函数体内 `from ... import`，必须在 `app.infrastructure.database` 命名空间 patch

---

**审计 worker 覆盖率从 79% → 97%，所有 18 个测试通过，质量门槛达标。**