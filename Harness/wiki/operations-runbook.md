# 运维手册

## 启动

```bash
cp docker/.env.example docker/.env  # 编辑密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
```

服务端口：
- 前端 5173、后端 8000(/docs)、PostgreSQL **5433**（容器 5432 映射到宿主 5433，避 OpenMetadata 占用；详见下方「迁移与端口映射」）、Neo4j 7474/7687、Milvus 19530、MySQL 3306。

## 部署（2026-10-22）

### 构建源必须是干净的 git worktree

`docker/docker-compose.yml` 里 backend/frontend 的 build context 是仓库根，
所以**你执行 build 的那个目录的 checkout，就是会被烤进镜像的源码**。

**不要从主仓直接构建。** 2026-10-22 实测踩中：主仓当时停在 detached HEAD
`9cdd934c`，比 `origin/main` 落后 5 天，且**不含** `0132` 迁移 —— 而 prod 的
`alembic_version` 已经是 `0132`。`Dockerfile.backend` 的 CMD 是
`alembic upgrade head && uvicorn`，用它重建后重启会 `Can't locate revision`
→ `&&` 短路 → uvicorn 根本不起 → 整个后端挂掉。

主仓还会积累本地 artifact（`backend/.env`、`.pytest_cache/`、`.coverage`、
`.DS_Store`），worktree 天然没有这些。

```bash
git worktree prune
git worktree add --detach /private/tmp/qa-deploy-$(date +%Y%m%d) origin/main
cp <主仓>/docker/.env /private/tmp/qa-deploy-.../docker/.env   # gitignored，必须拷
```

### 标准部署步骤

```bash
cd /private/tmp/qa-deploy-YYYYMMDD/docker

# 1. 回滚锚点（旧镜像别变成 dangling）
docker tag qa-system-backend:latest  qa-system-backend:pre-deploy-YYYYMMDD
docker tag qa-system-frontend:latest qa-system-frontend:pre-deploy-YYYYMMDD

# 2. 构建（项目名 qa-system 硬编码在 compose 顶部，换目录不会另起一套栈）
docker compose --env-file .env build backend frontend

# 3. 密钥 preflight（见下节），不过就不要往下走

# 4. 换容器。用 stop 不要用 down —— 同栈 postgres/neo4j/milvus 都在这个 network 上
docker compose --env-file .env stop backend frontend
docker compose --env-file .env up -d backend frontend
```

后端镜像重建实测 ~9 分钟（apt + uv sync 层会 miss cache），别当卡死。

### 部署前必须过：镜像密钥 preflight

`.dockerignore` 排的是 build context，而 `Dockerfile.backend` 里
`COPY backend/ ./` 会把整个 `backend/` 塞进镜像。`backend/.env` 被 `.gitignore`
挡住所以不会进版本库，但**它照样会进 build context** —— 2026-10-22 发现
`.dockerignore` 当时没有任何 `.env` 规则，装 `localhost:5433` dev 库凭据的
`.env` 会被烤进镜像。

```bash
uv run python backend/scripts/preflight_image_secrets.py          # 默认 qa-system-backend:latest
uv run python backend/scripts/preflight_image_secrets.py <image>  # 或指定镜像
```

预期（干净镜像）：

```
[PASS] no_secret_env_files: 镜像 qa-system-backend:latest 内未发现敏感 .env
[PASS] env_example_present: 模板 .env.example 在位，探针有效
```

任一 FAIL → **不要部署**。第一项 FAIL 说明 `.env` 已在镜像里，重建并轮换凭据。

第二项是**反向守卫**，不是凑数：`.env.example` 必须存在，否则说明探针本身
失效了，第一项会因为「没查到东西」而静默 PASS。

**这不影响运行时**：`app/config.py` 的 pydantic `env_file=".env"` 只是兜底，
进程环境变量优先；compose 用 `env_file: - .env`（即 `docker/.env`）注入配置。
镜像里没有 `/app/.env` 容器照样正常跑。

### 部署后核对

```bash
docker diff qa-backend | grep -cE '^[CA]'          # 应为 0（容器与镜像零漂移）
docker exec qa-backend alembic current              # 应为 head，且与部署前一致
curl -s http://localhost:8000/openapi.json | python3 -c "import sys,json;print(len(json.load(sys.stdin)['paths']))"
curl -s http://localhost:5173/ | grep -oE 'index-[A-Za-z0-9_-]+\.js'   # bundle hash 应变化
```

探针要用**基线为 0、新树 >0** 的串（否则证明不了什么），并且**旧容器上先跑一遍
对照**：

```bash
docker exec qa-backend grep -c '<探针串>' /app/app/services/chat_context.py
```

openapi paths 数只做「不降」检查；本次改动若不涉及路由，数目本就该不变。

## 本地开发

```bash
cd backend
uv sync --extra dev
uv run alembic upgrade head       # 迁移

# 必须带 docker/.env 的 SECRET_KEY！否则 datasource 解密失败（见下方「SECRET_KEY 必带」）
export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env | cut -d= -f2)"
uv run uvicorn app.main:app --reload --port 8000
uv run pytest --cov=app --cov-fail-under=80

cd frontend
npm install && npm run dev
npm run test:coverage
```

> **SECRET_KEY 必带（2026-08-18 踩坑）**：DB 里 `data_source.password_encrypted`（ZJTH-Oracle 等）是用
> `docker/.env` 的 `SECRET_KEY` 加密的。启动后端**必须**导出同一密钥，否则 datasource 读取时
> `Fernet` 解密抛 `ConfigError: SECRET_KEY 不是合法的 Fernet 密钥`，整个 NL2SQL 链路失败。
> 代码默认值 `development-insecure-key-change-me`（33 字符）不是合法 Fernet key，**不要依赖默认值**。
> `uv run uvicorn` 不会自动读取 `docker/.env`，需显式 export。

## 数据库迁移

```bash
uv run alembic upgrade head      # 应用
uv run alembic revision --autogenerate -m "desc"  # 生成新迁移
uv run alembic downgrade -1     # 回滚一步
```

### F2 KPI–数据源 FK 上线顺序（0132，2026-10-18）

0132 把 `kpi_catalog.datasource_id` 的外键从 `ON DELETE SET NULL` 改为
`ON DELETE RESTRICT`，同时 F2 起 `KpiCatalogCreate.datasourceId` 为**必填**。
两条契约叠加决定部署顺序：

1. **先部署 backend**（镜像 CMD 启动时自动执行 `alembic upgrade head`，
   见 `Dockerfile.backend:84`）——迁移 + 新契约一起上线。
2. **再部署 frontend**。
   - 旧前端 + 新后端：创建 KPI 不带 `datasourceId` → 422（可感知、可恢复）。
   - 新前端 + 旧后端：多余字段被忽略，安全。
   因此 backend 必须先于 frontend，不能反过来。

上线前后各跑一次 preflight（只读，三项）：

```bash
docker exec -w /app qa-backend uv run python scripts/preflight_kpi_fk.py
```

预期输出：

```
[PASS] alembic_version: current=0132 required>=0132
[PASS] fk_on_delete: fk_kpi_catalog_datasource confdeltype='r' required='r' (r=RESTRICT)
[PASS] bound_kpi_rows: N 个 KPI 已绑定数据源（信息项）
```

第二项 FAIL 说明迁移没生效（或容器还在跑旧镜像），此时不要切前端流量。

**批量写入口（`POST /api/v1/kpi-catalog/bulk-bind-datasource`）的鉴权前提**：

该端点是非 admin 403 的批量绑定（单次最多 1000 行），但**在 stub 鉴权模式下
`X-User-Roles` 由客户端自填**，等于任何人都能拿到批量写权限。因此上线该端点前必须确认：

- `APP_ENV=production`、`AUTH_MODE=real`、`AUTH_STUB_ENABLED=0`（启动期校验见
  `backend/app/config.py:280`，任一不满足会 ERROR 告警）；
- 反向代理剥离客户端传入的 `X-User-*` 头；
- backend 的 `8000:8000` 端口不对公网/办公网开放（见 `docker/docker-compose.yml:170`），
  只允许经 nginx 访问——直连 8000 会绕过代理层的头部剥离。

**回滚**：`uv run alembic downgrade 0131` 恢复 SET NULL 语义（服务层 409 守卫
仍在，数据不会被静默解绑）。回滚后前端必须同步回退，否则创建 KPI 仍会 422。

## KPI 治理（P4b/P5，2026-11-14）

本批给 KPI 链路加了三块治理能力：`execution_source=COMPILED` 指标的 measure→SQL
编译器（Tier A，编译失败 fail-closed 回落 Tier B）、KPI/指标状态流转矩阵（非法流转
409）、以及 admin 治理看板（`/kpi-governance`）。**零 DDL（无迁移）**；变更记录见
`Harness/changes/feat-kpi-p4b-p5/summary.md`。

### 开关一览与回滚

六个开关都存在 `system_config`（主键 `key`），同名 env 只作**进程级止血闸**（需重启）。
读值语义：`true/1/on/yes` 为真，`false/0/off/no` 为假，其它非空按 int（>0 为真）；
空串 / 非法 / 读取异常回落到「行缺失」默认值。

| system_config key | env 杀闸 | 行缺失时默认 | 备注 |
|---|---|---|---|
| `KPI_METRIC_TIER_A_ENABLED` | 同名 env，默认 `true`；置 `false` 一刀关 | **关** | Tier A 执行器双闸 |
| `KPI_METRIC_GATE_ENABLED` | 无 env 闸 | **关** | 发布闸门；P5 明确保持默认关 |
| `KPI_CONTRACT_INJECTION_ENABLED` | 同名 env，默认 `true` | **关** | 契约注入的两个面共用一个 key |
| `KPI_MATCH_CONTAINMENT_ENABLED` | 同名 env，默认 `true` | **关** | 匹配器 v2 |
| `KPI_MATCH_VECTOR_ENABLED` | 同名 env，**默认 `false`**（要开必须 env 显式 `true` 且行 = `true`） | **关** | 向量候选步 |
| `KPI_SYNC_ENABLED` | 同名 env，默认 `true` | **开**（方向相反，刻意） | 缺行回落 on 才与旧行为一致 |

> `KPI_SYNC_ENABLED` 是唯一「缺行 = 开」的开关：旧行为完全没有同步，缺行回落 on
> 才是无行为变化；显式置 `false` 才是关同步。其余五个缺行一律关（fail-closed）。
> `KPI_MATCH_VECTOR_ENABLED` 的 env 默认值是 `false`（`backend/app/config.py`），
> 只把 `system_config` 行改成 `true` 不会打开向量步——env 与行是两个串联闸。

开 / 关 / 回滚一个开关（以发布闸门为例）：

```sql
-- 开
INSERT INTO system_config (key, value, description)
VALUES ('KPI_METRIC_GATE_ENABLED', 'true', 'KPI 发布闸门（P5，默认关；开启需人工确认）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_time = NOW();

-- 关（保留行并置 false，便于审计「何时被人关过」）
INSERT INTO system_config (key, value, description)
VALUES ('KPI_METRIC_GATE_ENABLED', 'false', 'KPI 发布闸门（P5，默认关；开启需人工确认）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_time = NOW();

-- 回滚：删除该行，回落到「行缺失」默认值
DELETE FROM system_config WHERE key = 'KPI_METRIC_GATE_ENABLED';
```

`system_config` 每请求读取，改行**不用重启**；只有 env 杀闸需要重启进程。

### 发布 / 废弃 / 恢复

写入口一律 admin（KPI 写入口走 `getAdminOnlyActor`，指标写入口走 `requireAdmin`；
两者都是 admin-only，但 403 的错误形状不同）。下面的 `X-User-Roles: admin` 只是
stub 模式示例；生产必须 `AUTH_MODE=real` +
`AUTH_STUB_ENABLED=0`，否则任何客户端都能自填该头。

```bash
BASE=http://localhost:8000/api/v1
# 发布 KPI（DRAFT → PUBLISHED，会先过发布闸门）
curl -sS -X PUT "$BASE/kpi-catalog/123" \
  -H 'Content-Type: application/json' -H 'X-User-Roles: admin' \
  -d '{"status":"PUBLISHED"}'

# 废弃（PUBLISHED → DEPRECATED）
curl -sS -X PUT "$BASE/kpi-catalog/123" \
  -H 'Content-Type: application/json' -H 'X-User-Roles: admin' \
  -d '{"status":"DEPRECATED"}'

# 恢复（DEPRECATED → DRAFT；需要重新发布时再走一次 PUBLISHED）
curl -sS -X PUT "$BASE/kpi-catalog/123" \
  -H 'Content-Type: application/json' -H 'X-User-Roles: admin' \
  -d '{"status":"DRAFT"}'

# 指标发布 / 退回（ontology_metric）
curl -sS -X PUT "$BASE/ontology/metrics/45" \
  -H 'Content-Type: application/json' -H 'X-User-Roles: admin' \
  -d '{"status":"PUBLISHED"}'
```

状态矩阵：

| 实体 | 允许流转 | 说明 |
|---|---|---|
| KPI | `DRAFT→PUBLISHED`、`PUBLISHED→DEPRECATED`、`DEPRECATED→DRAFT`；同状态 = 流转校验放行（但 PUT 仍会 +1 revision 并写审计/历史） | `DRAFT→PUBLISHED` 过发布闸门 |
| 指标 | `DRAFT↔PUBLISHED` | `PUBLISHED→DRAFT` 仅当**没有** `PUBLISHED` KPI 挂靠该指标 |

其余流转（含 `PUBLISHED→DRAFT`、`DRAFT→DEPRECATED`、`DEPRECATED→PUBLISHED`）一律
409，响应体形如：

```json
{"success": false, "error": "…", "detail": "INVALID_STATUS_TRANSITION",
 "details": {"from": "PUBLISHED", "to": "DRAFT", "allowed": ["DEPRECATED"]}}
```

处理 `INVALID_STATUS_TRANSITION`：读 `details.allowed` 走合法路径，**不要**直接改库。
指标 `PUBLISHED→DRAFT` 被拦时，先把挂靠的 `PUBLISHED` KPI 废弃或改挂到别的指标，
再重试。非法流转零副作用（不写库、不入 outbox、不写审计），因此不需要数据修复。

### 发布闸门开启前检查（`KPI_METRIC_GATE_ENABLED`）

闸门默认关；关闭时只写 warning 放行，开启后 KPI `DRAFT→PUBLISHED` 必须 5 项全过：

1. `metric_linked`：KPI 挂有 metric；
2. `metric_published`：该 metric 已是 `PUBLISHED`；
3. `execution_source`：metric 的 `execution_source` 非空且不是 `NONE`；
4. `reconcile`：层 2 对账 verdict 必须是 **PASS**（`mismatch` 与 `unknown` 都阻断）；
5. `datasource`：KPI 已绑定 `datasource_id`。

开闸前按**问题计数**核对，不要看「通过率」：

```bash
BASE=http://localhost:8000/api/v1
# 近 7 天对账问题计数（mismatchCount / unknownCount）+ 三个闸门现状
curl -sS "$BASE/admin/kpi/governance-summary" -H 'X-User-Roles: admin' | python3 -m json.tool

# 逐条确认待发布 KPI 的 verdict / tier（question 用 KPI 名称或关键词）
curl -sS "$BASE/admin/kpi/diagnose?question=<KPI名称或关键词>" \
  -H 'X-User-Roles: admin' | python3 -m json.tool

# 找出未绑定数据源的 KPI（发布闸门第 5 项会拦）
curl -sS "$BASE/kpi-catalog?unbound=true" -H 'X-User-Roles: admin'
```

`reconcile.passCount` **不是通过率，且会严重低估真实 pass**：P1 起对账只在
findings 非空时落 `audit_log`（避免噪声）⇒ **干净 pass 不落库**；但
`reconcileMetric` 的 `verdict` 只取层 2，层 1/3 的 mismatch 会并入 findings 并置
`degraded=true`，所以 **degraded pass 会以 `verdict="pass", degraded=true` 落库**。
`passCount` 只是「窗口内实际落库的 pass 行数」——**只有 degraded pass 会落库
（干净 pass 从不落库）**。

因此闸门前判据不是单看 `mismatchCount=0` / `unknownCount=0`，还要排除 degraded pass：

```sql
-- 近 7 天 degraded=true 的对账行必须为 0（层 1/3 有 finding 的对账行，或层 2 unknown）
SELECT count(*) FROM audit_log
WHERE entity_type = 'kpi_metric_reconcile'
  AND created_at >= now() - interval '7 days'
  AND after_json->>'degraded' = 'true';
```

并对每条待发布 KPI 用 diagnose 确认 `verdict == "pass"` **且 `degraded == false`**。
注意闸门本身只要求层 2 对账 PASS，degraded pass 能过闸但口径不完整，所以人工口径
必须更严。「计数为 0」也可能只是从未对账过，不能单独当作通过证据。

发现数据没补齐：保持闸门关闭（或执行上面的 `DELETE` 回滚），先修数据。回滚闸门
**只需删除 `system_config` 行**，无需回滚代码。

### 向量 / 图同步补偿

同步钩子开关是 `KPI_SYNC_ENABLED`（缺行默认开）。外部存储故障时失败只进
`failures`，不会 500；修好 Milvus / Neo4j 后补跑：

```bash
BASE=http://localhost:8000/api/v1
# 全量对账（补缺失向量/图 + 删孤儿；幂等，复跑应全 0）
curl -sS -X POST "$BASE/kpi-catalog/embeddings/sync-missing" -H 'X-User-Roles: admin'

# 单条 KPI 向量重建（仅 PUBLISHED；非 PUBLISHED ⇒ 409）
curl -sS -X POST "$BASE/kpi-catalog/123/embedding" -H 'X-User-Roles: admin'
```

批量响应字段：`vectorPublishedTotal` / `vectorMissingCount` / `vectorSyncedCount` /
`vectorOrphanCount` / `graphMissingCount` / `graphSyncedCount` / `failedCount` /
`failures[]`。`failedCount > 0` 时按 `failures[].entityType` / `failures[].error`
逐个排查；Milvus / Neo4j 不可用属于降级明细，不是接口故障。单条端点只重建向量，
不补图节点。

**admin 前提**：两个端点都是 admin-only（`getAdminOnlyActor`）。生产必须
`AUTH_MODE=real` + `AUTH_STUB_ENABLED=0`（同「批量写入口的鉴权前提」）。

### 数据修复与审计查询

KPI 合法流转沿用既有 outbox 管线，最终落到 `audit_log`（`entity_type='kpi_catalog'`）
与 `kpi_catalog_history`。指标合法流转由 `ontology_service.updateMetric` **直接**写
`audit_log`（`entity_type='ONTOLOGY_METRIC'`），**不经过 outbox，也没有
`kpi_catalog_history` 快照**。非法流转不落任何一侧；运维查指标变更只看 `audit_log`。

```sql
-- KPI 修订快照（每次合法 PUT +1 revision）
SELECT revision, changed_by, changed_at, snapshot_json->>'status' AS status
FROM kpi_catalog_history
WHERE kpi_id = 123
ORDER BY revision DESC
LIMIT 20;

-- 对账审计：仅 findings 非空才落库（干净 pass 不落；degraded pass 会落）
SELECT created_at,
       entity_id,
       after_json->>'kpi_code' AS kpi_code,
       after_json->>'verdict'  AS verdict,
       after_json->'findings'  AS findings
FROM audit_log
WHERE entity_type = 'kpi_metric_reconcile'
ORDER BY created_at DESC
LIMIT 50;

-- 按 reason 聚合对账问题
SELECT after_json->>'verdict' AS verdict,
       finding->>'reason'     AS reason,
       count(*)
FROM audit_log, jsonb_array_elements(after_json->'findings') AS finding
WHERE entity_type = 'kpi_metric_reconcile'
GROUP BY 1, 2
ORDER BY 3 DESC;
```

`after_json->>'verdict'` 可能是 `pass`（仅 degraded pass 会落库）、`mismatch` 或
`unknown`；干净 pass 不落库。看板与巡检不得把 `passCount` 当通过率。

状态与向量/图不一致时（例如 KPI 已 `PUBLISHED` 但向量缺失），先跑
`embeddings/sync-missing` 对账；状态流转本身会自动删/建向量并更新图节点 `status`。

### 生产发版硬闸门（不可自动执行）

本批零 DDL，但代码依赖 P3 已合入的 0143 三列（`kpi_catalog.time_grain` /
`ontology_metric.time_grain` / `time_column`）。生产当前仍在 0140 + 旧代码，发版必须
按序、人工确认，**不得由脚本自动串跑**：

1. `alembic upgrade head`（0140 → 0143）。
2. 立即回填（硬闸门）：
   ```bash
   uv run python scripts/kpi_backfill_time_grain.py --apply --allow-prod-write
   ```
   预期 **`changes=11 noop=1`**；幂等复跑必须 **`changes=0 noop=12`** 才算回填成功。
3. 通过后才部署新代码（backend 先、frontend 后）。

顺序反了的后果：旧库跑新代码 ⇒ ORM 已声明 `time_grain` / `time_column` ⇒
`UndefinedColumn`；回填前上线 ⇒ 治理侧有值、技术侧 NULL ⇒ 12/12 unknown，8 条基线
pass KPI 暂失 Tier A（degraded 注入，不阻断，但口径不完整）。

### 已知能力边界（本批不修）

1. **显式时间问句 + COMPILED 走不到编译器**：路由门先判
   `has_explicit_time_scope and not time_param_ready` ⇒ Tier B（`explicit_time_scope`）。
   `time_param_ready` 要求 `metric.formula` 含 `{time_scope}` 占位符**且
   `metric.time_column` 非空**；COMPILED 指标 formula 为空 ⇒ 恒 False。编译器本身
   支持 timeScope（Oracle 方言），但当前路由在到达编译器前就降级，属 fail-safe 的
   已知限制。
2. **`COUNT(*)` 形态的 COMPILED 指标进不了 Tier A**：对账层 2 对「技术侧度量无表达式」
   返回 `technical_measure_unjudgeable` / `UNKNOWN` ⇒ `isTierAEligible=False` ⇒ 路由落到
   `reconcile_not_eligible`。要进 Tier A 必须写成 `COUNT(<列>)`（治理 / 产品决策项，
   本批不改对账语义）。
3. **`reconcile.passCount` 不代表通过率**：干净 pass 不落审计（P1 避免噪声），只有
   degraded pass 落库 ⇒ 计数既漏掉干净 pass、又混入 degraded pass；闸门前须另查
   `after_json->>'degraded'='true'` 行数为 0，并逐 KPI diagnose 确认 `degraded=false`。

## 日志

- 后端日志级别由 `LOG_LEVEL` 控制。
- Token 消耗、模型路由决策、SQL 执行审计均记录日志。

## 备份

- PostgreSQL 元数据：`pg_dump`。
- Neo4j：`neo4j-admin dump`。
- Milvus：MinIO 数据卷备份。

## 常见问题

| 现象 | 排查 |
|------|------|
| 后端启动 SECRET_KEY 报错 | 生成合法 Fernet key 填入 |
| 查询报「SECRET_KEY 不是合法的 Fernet 密钥」 | 重启时漏了 `SECRET_KEY` env。`export SECRET_KEY="$(grep '^SECRET_KEY=' ../docker/.env \| cut -d= -f2)"` 后重启（必须与加密 datasource 时同一密钥，不能用新生成的） |
| NL2SQL 生成错误 SQL | 检查本体是否定义、SQL Guard 日志 |
| 模型调用失败 | 检查 API Key、网络、provider 配置 |
| Ollama Token 为 0 | 旧版无 eval_count，标记近似 |
| SQLite 测试自增失败 | 主键须用 BigIntPk（with_variant Integer） |
| 后端 500 `UndefinedTableError: relation "xxx" does not exist` | DB 落后于 ORM（迁移未应用）。`cd backend && DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata alembic upgrade head` |
| 后端 500 `ConnectionRefusedError [Errno 61] Connection refused` | 连了 5432，但 Docker 把宿主 5433 映射到容器 5432（`docker-compose.yml` 注释「避开宿主 5432（被 openmetadata 占用）」）。本机连 PG 全部走 5433 |

## 迁移与端口映射（2026-08-30 踩坑）

### 端口：DB 永远在 5433，不是 5432

`docker-compose.yml` 里 postgres 服务的端口映射是 `5433:5432` —— 宿主 5433 → 容器 5432。原因：宿主 5432 被 OpenMetadata 占用。所以：

```bash
# 元数据库（开发）
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata

# 测试库
TEST_DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata_test
```

连 5432 = ConnectionRefused；连 5433 = 正常。

### SSOT 端口契约（2026-08-31 加固）

**问题**：容器内 / 容器外用同一个 `.env` 端口契约不一致——容器内 `postgres:5432`（Docker DNS 解析容器 service）、容器外 `localhost:5433`（宿主端口重映射）。`.env` 一份文件无法同时表达两种契约，过去靠口口相传，新人必然踩坑。

**SSOT 设计**：把容器内 / 容器外的端口契约分离到两个独立 `.env`，并通过 `docker-compose env_file` + `scripts/run_local.sh` 自动注入。**改端口只改一处**。

| 场景 | 配置文件 | URL 写法 | 注入方式 |
|---|---|---|---|
| 容器内（docker compose up） | `docker/.env` | `postgres:5432`（容器 service + 容器端口） | compose `env_file: .env` |
| 容器外（本机 uvicorn / 测试） | `backend/.env` | `localhost:5433`（宿主端口） | `scripts/run_local.sh` 自动 export |

**容器内变更**：把 `docker-compose.yml` 的 backend service 从硬编码 `environment: DATABASE_URL: ...` 改成 `env_file: .env`。`docker/.env` 是容器内端口契约 SSOT。

**容器外入口**：

```bash
# 推荐：用 wrapper 脚本（自动端口可达性预检 + SSOT 防御 + 注入正确端口）
./scripts/run_local.sh --port 8001 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# 跑 alembic（自动用 5433）
./scripts/run_local.sh alembic upgrade head

# 跳过预检（如已知端口 OK）
./scripts/run_local.sh --no-preflight .venv/bin/uvicorn app.main:app --port 8001
```

**双层 fail-fast**：

1. `scripts/run_local.sh` 在启动前检查 `.env` 是否仍写 `localhost:5432` —— 是则拒绝运行并指向 SSOT 说明。
2. `app/main.py` 的 lifespan 启动预检：连不上 PG 时，如果 URL 是 `localhost:5432` / `127.0.0.1:5432`，打印精确端口契约提示（容器外 → 改 5433；容器内 → 改 `postgres:5432`）。

**未来改端口的口径**：

- 改 PG 宿主端口：只改 `docker-compose.yml` 的 `5433:5432` 映射 + `backend/.env` 的 `localhost:5433` + `scripts/run_local.sh` 提示文字。`docker/.env` 容器内端口 `postgres:5432` 不动。
- 改容器内 PG 端口：只改 `docker-compose.yml` 容器端口段 + `docker/.env` 的 `postgres:<新端口>`。宿主端口不动。

### Stub auth 默认 admin（2026-08-31 落地）

dev/test 下默认 stub user 带 `("user", "admin")` 角色，避免未登录访问 `/api/v1/features` 等带 owner-based ACL 的端点必 403。生产安全护栏不变：

- `AUTH_STUB_ENABLED=0` → 拒绝任何 `X-User-*` 头请求（`getCurrentUser` 入口校验）
- `APP_ENV=production` + stub 仍开启 → 启动时 ERROR 日志告警（`main.py`）
- 反向代理剥离 `X-User-*` 头是兜底（与 stub 模式无关）

测试非 admin 路径时显式设 `X-User-Roles: user` 即可（11 处现有集成测试已补）。详见 `Harness/changes/feat-acl-default-admin-stub/summary.md`。

### 迁移：`git pull` 后必须 alembic upgrade head

ORM 模型一旦新增表/列，必须有对应迁移文件。DB 与 ORM 不一致会出现两类故障：

1. **`alembic current` 落后于 head** —— 启动后端任何读取新表的请求 → `UndefinedTableError`，整 500。
2. **`SKIP_SCHEMA_CHECK=1` 静默放行** —— 启动校验被跳过，运行期才暴露，生产事故更难排。

**强制流程（每次 `git pull` 后 / 每次新增迁移文件后）：**

```bash
cd backend
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5433/qa_metadata \
  alembic upgrade head
```

**CI / 本地启动前置**（建议加进 pre-commit 或 docker-compose 的 backend 启动入口）：

```bash
if [ "$(alembic current 2>/dev/null | head -1)" != "0025_audit_immutability (head)" ]; then
  alembic upgrade head
fi
```

**新增迁移文件 checklist**（开发自检）：

- [ ] `alembic upgrade head` 在干净 DB 上能跑通
- [ ] `alembic downgrade -1` 能回滚（验证对称性）
- [ ] 集成测试用真实 PG（`localhost:5433/qa_metadata_test`）跑迁移 — 见 `Harness/rules/测试规范.md`
- [ ] 更新本 runbook 的「迁移与端口映射」段（如端口或流程变更）

### 真实事故回放（2026-08-30）

| 时间 | 现象 | 根因 |
|---|---|---|
| T+0 | 前端 F12 报 `/kpi-catalog` 404、`/entity-mappings` 500 | 上一台 uvicorn 进程（PID 34319）启动时间早于 `kpi_catalog.router` 注册，是个**陈旧进程** |
| T+5min | 杀掉陈旧进程并重启 → 两个端点都 500 | 重启用了 `localhost:5432`，Docker 没在那开端口 |
| T+10min | 端口改回 5433 → `UndefinedTableError: relation "kpi_catalog" does not exist` | DB 停在 Alembic 0020；后续 0021-0025（entity_mapping、kpi_catalog、audit_log、history、immutability trigger）从未应用 |
| T+12min | `alembic upgrade head` | 5 条迁移跑完，两个端点恢复 200 |

**事后动作（已落地）**：

1. 写本节「迁移与端口映射」，避免下次重复踩。
2. 启动 backend 之前必跑 `alembic upgrade head`（见上文强制流程）。
3. 排查端点 500 时，先看 `uvicorn` 日志的首个 traceback，再换端点 —— **不要只换端点不查日志**。

---

## 并发与韧性备忘（2026-09-19）

> **目的**：下次遇到「50/100 人并发降级」「LLM 持续故障」「要不要多 worker」等同类问题时，
> 先看本节确认当时是怎么定的、为什么不动、什么时候该动。
> 详细 SSOT 在 `Harness/changes/feat-chat-concurrency/summary.md`。

### 当前实测基线（2026-09-19，feat-chat-concurrency 落地后）

| 维度 | 配置 | 实测 |
|---|---|---|
| DB pool | `pool_size=20, max_overflow=10`（max 30） | 50 并发 GET /menu-config → 50/50 200, 0.38s |
| PG | `max_connections=200` | 50 并发后 idle 22 连接，自然保留 |
| LLM 闸 | `LLM_CONCURRENCY_LIMIT=20`（asyncio.Semaphore） | 50 人 → 20 in-flight + 30 排队等闸 |
| 限流 key | `RATE_LIMIT_KEY_STRATEGY=ip`（默认）/ `user_id`（50 人同一 NAT 改这个） | admin PUT 立即生效 |
| Fallback 退避 | tenacity `stop=2 wait_exponential(1,4)` + 429→retry / 401/403/400→直接抛 | 详见 `feat-chat-concurrency/summary.md` §chat_service |

### 决策一：多 worker 部署 → **暂不动**

**何时触发重新评估**：日活并发 ≥ **100 人** 或单 worker 进程 CPU/内存饱和。

**为什么现在不动**：

- 当前单 worker 跑 50 人实测 50/50 200 + 0.19s，还有 ~3 倍余量
- 多 worker → 进程内 `asyncio.Semaphore` **不再共享**（每 worker 独立），需要外部限流（Redis semaphore 或网关层限流）
- 多 worker → Alembic migration 并发抢锁风险（要加 advisory lock 或排他升级）
- 多 worker → 日志/指标/middleware 单例失效（如 `_bootstrapChatConcurrencyConfig` 跑 N 次）
- 多 worker → 内存占用 ×N（每个 worker ~150MB）

**如未来需要多 worker，步骤**：

1. 引入 Redis semaphore（如 `redis.asyncio.Semaphore` 或 `aiocache`），替换 `app/infrastructure/llm/concurrency.py` 的 `LLMConcurrencyManager`
2. DB pool 同步缩到 `pool_size / worker_count`（PG max_connections 上限保护）
3. alembic migration 加 advisory lock 或单独 worker 跑迁移
4. 日志格式加 worker_id 字段（uvicorn `--worker-id` + `logging` filter）
5. nginx 改 upstream 为 `least_conn` 或 `ip_hash`（已有 resolver 变量，见 `qa-system-nginx-upstream-ip-stale`）

**不要做**：
- ❌ 把 uvicorn workers 加到 4 但仍用进程内 Semaphore → 上限虚高 4 倍、LLM provider 必 429
- ❌ 加 gunicorn `--worker-class=uvicorn.workers.UvicornWorker` 但忘了设 `worker_tmpdir` → macOS /tmp 太小报错

### 决策二：熔断器 → **暂不引入**

**何时触发重新评估**：

- LLM provider 持续 5xx / 429 超过 **30 秒**
- fallback 也连挂（多层全断）
- 需要「保护上游」「自动恢复（半开探针）」而不是仅「延迟重试」

**为什么现在不动**：

- 当前 fallback 退避（tenacity 指数 1s~4s × 2 次）已覆盖「请求级瞬时故障」
- 熔断器（pybreaker / resilience4j / 自实现）解决的是「进程级状态机」（closed/open/half-open），需要持续观察 LLM 失败率
- 当前没有 LLM 失败率指标 → 熔断阈值（多少 % 失败开闸）拍脑袋没意义
- 多 worker 部署会让熔断状态机同步问题复杂化（先解 worker 再解 breaker）

**如未来需要熔断，步骤**：

1. 先加指标：`llm_request_total`, `llm_request_failed_total{provider,status}`（prometheus client）
2. 写一个滑动窗口失败率计算器（如 30s 内失败率 ≥ 50% 开启）
3. 用 `pybreaker` 包装 `_callWithFallback` 入口；或自实现 `CircuitBreaker`（避免额外依赖）
4. 状态持久化到 Redis（多 worker 共享）；单 worker 阶段用内存即可
5. 半开探针：开闸 30s 后放 1 个请求，成功 → 关闭，失败 → 重新开闸

**当前已有什么**：

- ✅ tenacity 退避（429/5xx 重试 + 指数退避 + reraise）
- ✅ fallback（按 weight 选备选模型，已在 `chat_service._callWithFallback`）
- ✅ Semaphore（LLM 并发上限）
- ❌ 熔断器（无）
- ❌ 失败率指标（无）

### 决策三：DB pool 调参 → **已动**（2026-09-19）

详见 `Harness/changes/feat-chat-concurrency/summary.md` §follow-up 1。

- `DB_POOL_SIZE`: 5 → **20**
- `DB_MAX_OVERFLOW`: 10 → **10**（不变）
- PG `max_connections`: 100 → **200**（docker-compose.yml `postgres.command`）
- migration `0081` 幂等 UPDATE 已 seed 的旧默认 5 → 20

### 出问题时的排查顺序

1. **看 `docker logs qa-backend 2>&1 | grep -E "并发配置|LLM_CONCURRENCY|max_conn"`** → 确认启动期读到的实际值
2. **`pg_stat_activity`** 看连接池是否打满（active 数 ≥ pool_size+max_overflow = 30 → 间隙）
3. **`curl /api/v1/admin/system-config`** 看 system_config 当前值与启动期值是否一致
4. **50 并发复现**（`python3 -c "import asyncio, aiohttp..."` 见 `feat-chat-concurrency/summary.md`）
5. **不要先调 LLM_CONCURRENCY_LIMIT** — 当前不是 LLM 闸问题，先看 DB pool / fallback 是否抛错
