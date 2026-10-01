# 变更：fix-datasource-type-failfast

- **日期**：2026-10-02
- **作者**：Claude / 启琳
- **Phase**：Phase 12 NL2SQL 引擎（方言分发边界）
- **状态**：done
- **关联变更**：[../fix-nl2sql-topn-share-denominator/summary.md](../fix-nl2sql-topn-share-denominator/summary.md)（同一批方言加固）
- **迁移版本**：无
- **MEMORY**：[qa-system-datasource-type-failfast.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-datasource-type-failfast.md)

---

## 1. 需求

用户问「nl2sql 是否按数据库类型分发方言」→ 核实确认架构已是「单引擎 + 按类型方言库」（`resolveDialect`），但存在缺口 #1：**未知/None 类型静默回退 Oracle 11g**。若 `data_source.type` 被手工改库/seed 写脏，会给 MySQL 库生成 ROWNUM 语法，执行必错且用户只看到莫名其妙的数据库报错。

验收标准：脏类型在 **LLM 消费之前**被拒绝，错误消息可定位、可操作；合法类型（含大小写脏值）不受影响。

## 2. 设计评审

| # | 候选方案 | 取舍 | 决定 |
|---|---|---|---|
| A | **消费边界 fail fast**：`_buildPipelineContext` 加载 ds 后立即严格校验，脏值抛 `ValidationError` | 在任何 LLM 调用之前拒绝（省 token）；错误可定位到具体数据源；不动 `resolveDialect` 历史行为 | ✅ 采用 |
| B | 改 `resolveDialect` 本身为严格版 | 该函数的宽容回退被既有测试钉死（`test_resolve_dialect_coerces_str_and_enum`），且大量测试用它取方言——改语义会大面爆炸 | ❌ 拒绝 |
| C | `DataSourceService.get` 里校验 | 会让 admin 端**无法加载**脏数据源（加载失败就没法编辑修复）——「修不了看不到的东西」 | ❌ 拒绝 |

**分层结论**：入口校验已存在（`DataSourceCreate.type: DataSourceType` 枚举 DTO → API 422），本次补的是**消费边界**；`resolveDialect` 宽容回退降级为最后防线。

## 3. 数据模型变更

无（`type` 列本就 NOT NULL）。

## 4. 接口契约变更

无新增 API。chat 流式端点新增一种 error 事件文案（`errorType: domain`），消息模板 `MSG_DATASOURCE_TYPE_UNKNOWN`。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/nl2sql_dialects.py` | 新增 `coerceDatasourceType(value, *, name)`：枚举直取 → 大小写归一 → 否则抛 `ValidationError`（与 `resolveDialect` 相邻放置，宽容/严格对比可见） |
| `app/services/chat_service.py` | `_buildPipelineContext` 在 `ds` 加载后立即调用（所有意图路径的公共入口，CLARIFY 也覆盖） |
| `app/services/messages_zh.py` | `MSG_DATASOURCE_TYPE_UNKNOWN`（数据源名 + 脏值 + 指引） |

## 6. 测试

| 用例 | 断言 |
|---|---|
| `TestCoerceDatasourceType` 6 例 | 合法值直通；大小写脏值归一（`MySQL`→MYSQL）；`None`/未知值拒绝且消息含数据源名、脏值原文、可操作方向（双向守卫） |
| `TestDatasourceTypeFailFast::test_unknown_type_fails_fast_before_llm` | `ValidationError` + **`llm.calls == []` + adapter 零执行**（fail fast 的实质证据） |

回归：`test_nl2sql_service.py` **177 passed**；`test_chat_service.py` **35 failed 与基线逐条一致**（陈旧红，与本改动无关）+ 126 passed（+1）。

## 7. 安全审查

未触发 security-reviewer：不触及认证/输入注入面；`type` 值进入的是错误消息（`{type}` 原文回显）——经 `ValidationError` → SSE JSON 通道，无 HTML/SQL 拼接点。

## 8. 部署验证

`./scripts/deploy_backend.sh` 启动成功。真机端到端：

1. API 建临时数据源（合法枚举）→ 201；**API 直接写脏类型 → 422**（入口校验仍在）；
2. 手工 `UPDATE data_source SET type='bogusdb'`（模拟脏值来源）→ 对该数据源提问：
   事件序列仅 `meta + error`（**零 LLM 消耗**），error 消息：
   > 数据源「type-guard-probe」的类型「bogusdb」无法识别，已拒绝生成 SQL。请编辑该数据源重新保存（支持：mysql / postgresql / oracle），或联系管理员修正。
3. 恢复 type → API 删除探针（204）→ 数据源列表恢复原 5 条。

## 9. 关联

- **Wiki**：[Harness/wiki/nl2sql-engine.md](../../wiki/nl2sql-engine.md) §多数据源（方言规则注入机制小节）
- **提交**：`7eb0b92`（代码）
