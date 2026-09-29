# research-migration.md — mcp_call_log 表设计研究

> feat-chat-mcp-integration · alembic 0084（修订任务里写的 0063 已被占用，见 §0）· 2026-09-22

## 0. 迁移号修正

任务描述写「alembic 0063 migration」，但文件系统里 `alembic/versions/0063_wiki_compile_tables.py` 已经是一个 pass 桩（down_revision = `0062`，被 `0064` 引用），不能并列第二条 0063，否则 alembic graph 多头。当前 head 是 `0083_audit_log_auth_actions`。因此本特性落地为 **`0084_mcp_call_log.py`**，down_revision = `0083`。SSOT 此处不沿用任务里的旧编号。

## 1. 设计决策与权衡

### 1.1 BIGSERIAL vs UUID 主键

选 **BIGSERIAL**。理由：

- 与既有 `audit_log` / `session_token_usage` / `*_history` 表一致；本仓库无 UUID 主键的 OLTP 表先例。
- append-only 时序数据，自增 id 即近似「按时间有序」，`SELECT ... ORDER BY id DESC LIMIT N` 不需要额外排序。
- 写吞吐高（MCP 调用一秒数十次），PG BIGSERIAL 比 UUID v4 写入快 ~30%（无 WAL FP 同步）。

UUID 适合「跨库合并」「客户端生成」「安全无序」三场景，本表全不沾。

### 1.2 JSONB vs TEXT

`args_json` 选 **JSONB**。理由：

- 未来可建 GIN 索引做 `args_json @> '{"query":"xxx"}'` 检索「哪个 actor 用了特定 query 调 MCP」，无需 ETL；
- JSONB 在 Python 侧直接 dict，不用 `json.loads`；
- 写性能略弱于 TEXT，但实际差距 < 10%，MCP 调用频率远未到瓶颈。

缺省值用 `'{}'::jsonb` 服务端兜底，Python 侧也声明 `server_default`，双保险。

### 1.3 不存响应内容，只存 result_size

呼应 **plan §6 R5 mitigation**：「不持久化外部 MCP server 响应内容」。原因：

- MCP 响应可能非常大（context7 拉整篇文档），存全文会让表被 LOB 撑死；
- 响应已被 LLM 在 chat 流里消费，写入 chat_message_history 即可，无需二次落地；
- 信泄露面：响应里可能偶发含 `<internal_url>` / `sk-` / `Bearer ` 等敏感串（plan §6 R5 mitigation 提到的「token 注入」），`MCPCallLog` 只记大小不记内容，黑客攻破 DB 也拿不到响应原文；
- 留 `result_size` 主要是给熔断 / 慢调用统计「响应大小超过阈值」告警用。

### 1.4 CHECK 约束清单

| 名称 | 表达式 | 理由 |
|---|---|---|
| `ck_mcp_call_log_status` | `status IN ('success','failure','timeout','cancelled')` | 4 态白名单，与 audit_log.action 的「扩展型白名单」风格统一（见 0083 教训） |
| `ck_mcp_call_log_latency` | `latency_ms >= 0` | 写库防腐（外部进程偶发负数未观察，写入本表的代码不能误传） |
| `ck_mcp_call_log_result_size` | `result_size IS NULL OR result_size >= 0` | 允许 NULL（fail 前无响应）但非负 |

`error_code` 不加 CHECK：MCP 错误码未来会扩（SDK 新增、上游调整），alembic 改 CHECK 需要新迁移，节奏反而拖死业务；现状仿 `audit_log.entity_type` 的「弱约束」做法。

### 1.5 索引选择与写入成本

4 个索引全部 BTREE（而非 BRIN），理由：

- `(created_at)` 单列：purge 任务用 `WHERE created_at < now() - interval '30 days'`，BTREE 范围扫描优于 BRIN；
- `(actor_id, created_at DESC)`：按用户回溯（`WHERE actor_id=? ORDER BY created_at DESC`）；
- `(server_name, status, created_at DESC)`：熔断统计（`WHERE server_name=? AND status='failure'`），复合前缀命中；
- `(tool_name, created_at DESC)`：工具热度（top N）。

写放大 4× 对 MCP 这种「秒级数十行」场景可承受。日均估算 60×60×8×3600×24 ≈ 4.1×10^9 行（高峰，**上界估**），索引总开销 << audit_log+session_token_usage 既有格局。

> 不使用 `CONCURRENTLY`：alembic `upgrade()` 默认包在事务中，PG 拒绝 `CREATE INDEX CONCURRENTLY` 在事务内。后续如果上 prod 9 月底累计上亿行、purge 后批量重建索引，可新加一个 standalone 迁移走 `op.execute("CREATE INDEX CONCURRENTLY ...")` 在 autocommit block 里。本迁移不预设。

## 2. 与既有表的边界

| 表 | 关系 | 区分点 |
|---|---|---|
| `audit_log` | **不重不混** | audit 永久保留 / 走 outbox / 低频业务变更；MCP 高频 30 天 purge / 直接 insert / 不需幂等键 |
| `session_token_usage` | **不混** | MCP server 自身的 token（如果有）不算 LLM 成本；二者 schema 不同（无 `model_config_id` FK） |
| `application_logs` | **不混** | application_logs 是运维日志（uvicorn 输出 / traceback），无 `actor_id` 维度；MCP 是产品功能日志 |
| `wiki_*` | **无关** | wiki 全链与外部 MCP 协议正交 |

schema_drift 对账工具（见 [[qa-system-schema-drift-two-dbs]]，blocking/warning 两级）现已到列+索引粒度：本迁移的所有列名 / 类型 / CHECK / 索引都必须与 ORM `MCPCallLog` **完全相同**，缺一即红。0084 与 ORM 同步一对一映射已逐字对齐。

## 3. purge 策略

| 维度 | 决策 | 备注 |
|---|---|---|
| 保留期 | **30 天** | plan §6 R5 mitigation：`purge_after_days=30`；与 audit_log 永久保留形成节奏区分 |
| 时机 | **每日 03:00 UTC** | 低峰期避开业务高峰；与 cron 路径已有 cron（backup_pg 兼容时段 02:00/04:00）错开 1h |
| 谁跑 | **单独脚本 `scripts/purge_mcp_call_log.py`** | 不与 backup_pg 合并、也不嵌入 alembic；alembic 迁移只动 schema |
| 实现 | 单 SQL：`DELETE FROM mcp_call_log WHERE created_at < now() - interval '30 days' RETURNING id` | 单次删除按 created_at BTREE 索引范围扫，无全表扫；定期 VACUUM 由 autovacuum 接手 |
| 监控 | 脚本每次跑回 `{"deleted": N, "duration_ms": M}`，worker 心跳落到 `application_logs` | 失败重试 3 次，指数退避；最终失败 alert_by_email（与现有备份 cron 同模式） |
| 谁触发 | **OS-level cron**（与 `backup_pg` 并列） | 不依赖应用进程 / pm2；与 [[qa-system-cron-silently-broken]] 教训同套兜底：`/etc/crontab` 存在则 plist 监 spool |
| 优雅降级 | purge 脚本可手工跑 `./scripts/purge_mcp_call_log.sh` 直接删除 | 与 [[qa-system-stale-container-deploy]] 教训一致：脚本独立于容器，cron 坏时仍可手动 |

## 4. 与 ORM 模型的一致性

ORM 文件 `/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend/app/domain/mcp_models.py`：

- 列名 / 类型 / nullable / server_default 与 DDL 逐项对齐（含 `'{}'::jsonb` 默认、status 4 态、CHECK 名称 `ck_mcp_call_log_*`）；
- 索引名与 DDL 完全一致（含 `created_at DESC` 用 `sa_text()` 表达）；
- 不复用 `TimestampMixin`：本表无 `updated_time`（append-only），避免误引入 future column；
- `created_at` 由 DDL `server_default=now()` 填，Python 侧不重复 `default=_utcnow`，避免双源时钟漂移（与 audit_log 同样纪律）；
- 软不可变纪律（应用层禁 UPDATE / DELETE）与 audit_log 同模式。

## 5. 与 plan §6 R5 mitigation 的对齐

| R5 mitigation | 本表落点 |
|---|---|
| 「新建 mcp_call_log 表记录 actor/server/tool/args/result_size/status」 | 9 列全覆盖（id / actor_id / server_name / tool_name / args_json / result_size / status / error_code / latency_ms / created_at） |
| `purge_after_days=30` | §3 上方保留期策略 |
| 「响应含 sk-/Bearer 正则报警并丢弃」 | **由 service 层 `MCPCallLogSink` 在写入前断言**——若 result_size 大、且 args_json 命中 secret pattern，service 应 `return` 而非写。**DB 层不拦**，避免 secret 串落在 DB；告警走应用日志 |
| `allowed_servers` 白名单启动校验防配置漂移 | **不写在 DB**——白名单是 system_config 维度，写在本表会重复 N 行同 server_name。本表只被动记录调用，无白名单职责 |

SSOT：本表是 R5 mitigation 的「下游日志」组件，**不是**白名单的 SSOT；不要把 `server_name` 当成 server 注册表。

## 6. 风险与未决项

| 风险 | 影响 | 缓解 |
|---|---|---|
| 高频 INSERT 把单表撑大（年估亿级） | BTREE 索引碎片、purge 慢 | 1) purge 每日清理；2) 月度分区（`PARTITION BY RANGE (created_at)`）作为下一迁移留给运维侧决定，本迁移**不**预设 |
| `args_json` 体积膨胀（用户把整本 PDF base64 当 args 传） | 单行 ~MB 级致 TOAST 拖累 | 1) CHECK 在 service 层加 `length(args_json::text) <= 65536` 软拦截；2) 真要拦写 `_validateArgs` 抛 4xx 而非 silent drop |
| 多副本 chat_service 并发 INSERT | BIGSERIAL 不会冲突（PG 内部串行化） | 不需要额外处理；但 INSERT 频率上限 ~10K row/s，到位前先 scale out pgbouncer |
| `created_at` 时区漂移 | DB `now()` 与 app `_utcnow` 不一致时审计失真 | 全走 DB server_default；application_logs 同时记 app 端时间便于事后对账 |
| 4 索引写放大被忽视 | 长尾 RT 抖动 | 写入链路监控 p99（落 application_logs），slow path 触发 DDL 评估 |
| purge 误删早 30 天但被 incident 调阅需要的数据 | 调试视线丢失 | 1) 取数前 ARCHIVE 远端归档到 `s3://qa-mcp-logs/<yyyy>/<mm>/` 7×30 = 24 月；2) incident 由 oncall 按需拉；本迁移不实现，先用 stdout 兜底 |

## 7. 观测 / 监控面板（建议）

本表给运维 / SRE 暴露以下查询做 Grafana panel（实现留给运维任务）：

- 5xx 等价：`SELECT count(*) FROM mcp_call_log WHERE status IN ('failure','timeout','cancelled') AND created_at > now() - interval '5 minutes'`，每 server 切片；
- 熔断阈值：单 server 5 分钟内 `failure + timeout` 占比 ≥ 30% 触发熔断告警；
- 长尾：`SELECT server_name, tool_name, percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms) ... GROUP BY ...`，p99 > 5s 告警；
- 热点：`SELECT tool_name, count(*) FROM mcp_call_log WHERE created_at > now() - interval '1 hour' GROUP BY 1 ORDER BY 2 DESC LIMIT 20`。

无需新加索引——4 索引已覆盖。

## 8. 测试策略（提示给 tdd-guide）

- **schema drift 必跑**：`pytest -q tests/test_schema_drift.py` 必须绿；新表任何缺列即失败；
- **集成测试必带真机链路**（参照 [[qa-system-runbook-quirks]]）：TRUNCATE 后 INSERT 1 行 → SELECT 回看；
- **CHECK 必测**：`status='illegal'` 必 23514；`latency_ms=-1` 必 23514；`result_size=-1` 必 23514；
- **索引必查**：`pg_stat_user_indexes` 跑 100 行后 `idx_scan > 0` 至少 1（actor_id 维度的 SELECT 会触发 `ix_mcp_call_log_actor_id_created_at`）；
- **purge 必测**：手工 `UPDATE mcp_call_log SET created_at=now()-interval '31 days'` 后跑 `purge_mcp_call_log.py --dry-run` 不删、跑真的删；autouse TRUNCATE 与 purge 顺序见 [[qa-system-mixed-suite-truncate-hazard]]，**purge 测试须独占进程**。

## 9. 决策回顾（简短）

| 决策 | 选项 | 选定 | 推翻代价 |
|---|---|---|---|
| 主键 | UUID / BIGSERIAL | **BIGSERIAL** | UUID 化 = 改 PK + 索引全重建 + 全链路代码改，向后不兼容 |
| args_json | JSONB / TEXT | **JSONB** | TEXT 切 JSONB = 加迁移 + 历史数据 `jsonb()` 转换 |
| result 落库 | 落 / 不落 | **不落** | 改「落」 = 加列 + 加索引 + 改 §6 R5 mitigation 文案 |
| 保留期 | 7d / 30d / 永久 | **30d** | 改永久 = 增 ARCHIVE 逻辑；改 7d = incident 调试空间小 |
| 错误码约束 | 强 / 弱 | **弱** | 升级强约束 = 加新迁移扩白名单 |
| 索引写并发 | 普通 / CONCURRENTLY | **普通**（事务内） | 升级 CONCURRENTLY = 重写为 op.execute + autocommit block，不可逆 |
