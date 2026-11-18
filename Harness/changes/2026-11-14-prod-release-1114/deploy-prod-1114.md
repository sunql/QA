# 生产发布方案：截至 2026-11-14 的全部变更（1103 → 1114）

- **日期**：2026-11-14
- **目标**：192.168.205.82（CentOS 7.9，内网隔离，Docker Compose；**无 SSH 免密通道 ⇒ 生产命令由你在宿主机执行**，本文档 + 镜像包由开发机交付）
- **生产现状基线**：`main` @ `0e248bca`（1101 chat 中文化批），alembic **0140**
- **目标代码基线**：`main` @ `d001e38e`（含 #126 M4 运行时看板）
- **代码落差**：`git log 0e248bca..origin/main` = **103 个 commit**，**369 个文件**（backend/app 215、frontend/src 34、backend/scripts 32、Harness 74、alembic 3）
- **迁移落差**：`0140 → 0143`（**0141 / 0142 / 0143 三步，生产 DDL，须单独评审**）
- **发版源**：worktree `/private/tmp/qa-deploy-20261114`（detached @ `d001e38e`，工作树干净）

---

## 1. 对现有功能的影响评估（先看这节）

结论：**大部分新能力是开关门控的（默认关 ⇒ 合入即无行为变化），但有 8 处不靠开关、会直接改变现有行为的改动**，另有 3 条迁移改生产库结构。

### 1.1 开关门控 —— 灰度旋钮是 `system_config` 行，env 只作「一键止血」

判定规则（2026-11-14 实读 `backend/app/services/kpi_metric_switches.py` + `backend/app/config.py` 确认）：
**env 杀闸关 ⇒ 一律关；env 杀闸开 ⇒ 读 `system_config` 行，行缺失按各开关自己的 default**。

> ⚠️ `system_config` 的列名是 **`key` / `value`**（不是 `config_key` / `config_value`）。
> ⚠️ env 杀闸是**进程级、需重启生效**；行是**每请求读取、即时生效** ⇒ 行才是回滚闸门。

| 能力 | 开关键 | env 杀闸默认 | 行缺失默认 | 生产当前生效 |
|---|---|---|---|---|
| 匹配器 v2（包含式 + 歧义澄清） | `KPI_MATCH_CONTAINMENT_ENABLED` | true（开） | **关** | **关** ⇒ 仍走 v1 关键字路径 |
| Tier A 确定性执行 | `KPI_METRIC_TIER_A_ENABLED` | true（开） | **关** | **关** ⇒ 仍走 LLM |
| 发布闸门（P5 五项检查） | `KPI_METRIC_GATE_ENABLED` | 无 env 闸 | **关** | **关** ⇒ 不拦发布 |
| 向量候选匹配 | `KPI_MATCH_VECTOR_ENABLED` | **false（默认关）** | 关 | **关**（env 与行双闸都关） |
| 治理契约注入（nl2sql + chat 两面共用一个决策） | `KPI_CONTRACT_INJECTION_ENABLED` | true（开） | **关** | **关** ⇒ **生产 KPI 口径一条都没进 LLM**（生产缺陷根因，见 1.2 ⑦ / 1.4） |
| 语义发布（P1b 整组路由） | `ENABLE_SEMANTIC_RELEASE` | false（开） | — | **关** ⇒ 路由不注册（404） |
| KPI 向量/图同步 | `KPI_SYNC_ENABLED` | true（开） | **开**（方向相反，刻意） | **开** ⇒ 见 1.2 ⑥ |

### 1.2 不靠开关、随代码生效的改动（8 条，按影响排序）

| # | 改动 | commit | 对现有功能的影响 |
|---|---|---|---|
| ① | **治理写入口收 admin**：KPI 写入口（`POST/PUT/DELETE /kpi-catalog`、`/bulk-bind-datasource`、`/embeddings/*`）、本体类/属性/指标写入口（`POST/PUT/DELETE /ontology/classes|properties|metrics`）、词条字典写入口（`POST/DELETE /term-dictionary`）、chat 斜杠命令 | `851d6049`(#116) `00f3b13e`(#120) | **非 admin 的现有用户会失去这些写入口**（403）。只读不受影响。`POST /classes/{id}/restore` 维持 owner 语义，不收 admin |
| ② | **ontology 同步端点收 admin-only**（`POST /ontology/embeddings/sync`、`/embeddings/sync-missing`、`/graph/sync-missing`、`/classes/{id}/embedding`）+ 前端按钮对非 admin 隐藏 | `30c4097f`(#124) | 非 admin 用户点不到同步按钮；脚本/接口直调会 403 |
| ③ | **KPI / 指标状态流转矩阵**（P5）：`PUBLISHED→DRAFT` 等非法流转一律 409 `INVALID_STATUS_TRANSITION`；指标侧新增 `COMPILED` 等 | `3029a559`(#125) | 现有"把 KPI 改回 DRAFT"的操作会开始报错（之前可写）。合法流转路径不变 |
| ④ | **P4a 写入校验**：指标 formula 含 `{time_scope}` 占位符但 `time_column` 为空（或反之）⇒ 写入口拒绝 | `71b53bf4` | 影响**新建/编辑指标**（不是现有存量）；存量 12 条指标 formula 无占位符 ⇒ 不受影响 |
| ⑤ | **「最近 N 天」时间窗口多算一天**修复（`nl2sql_dialects.py`，三方言） | `6bef7b6a`(#107) | **现有 chat 回答的数字会变**（变正确）。问「最近 7 天…」类问题的口径由 [T-6,T] 改为 [T-7,T-1] |
| ⑥ | **KPI 向量/图同步默认开**（`KPI_SYNC_ENABLED` 缺行 = on）：KPI 新建/变更/删除会写 Milvus（kpi 向量）与 Neo4j（`KpiCatalog` 节点 / `MEASURED_BY` 边） | `e85f1ff8`(#123) | **生产新增后台写入面**，但触发时机是**写钩子**（create/update/delete），**不是启动批量任务**——`main.py` 启动只做 `kpi_match_cache.warmUp`（纯读缓存）。⇒ 存量 16 条 KPI **不会**自动补向量/图，除非显式跑 `syncMissingKpi` 或编辑该 KPI。失败只进 `failures` 不 500（best-effort）；生产 embedding 若未配好 ⇒ KPI 向量缺失（不影响召回，向量匹配本身是关的） |
| ⑦ | **治理列解析后的对账语义**（P3）：`mismatch` 仍拒绝注入契约块，但 `unknown` **不再阻断** | `e31367c1`(#122) | 🔴 **不是「休眠」，是「本该生效却没生效」** —— 契约注入总闸 `KPI_CONTRACT_INJECTION_ENABLED` 是 fail-closed（行缺失 ⇒ 关），而生产该行不存在 ⇒ **口径契约块永远为空** ⇒ LLM 拿不到签字口径 ⇒ **生产实测：问「9月的采购订单完成率」时 LLM 自编公式，与指标定义不符**（见 1.4）。本项要显现，**必须显式把该行置 true**（env 杀闸保持 true 即开）；但契约块只对 `verdict ≠ mismatch` 的 KPI 生效 ⇒ **开闸前必须先看 Phase 8 ④ 的 diagnose verdict** |
| ⑧ | **前端新增/改版**：KPI 治理看板页（admin-only 只读）、KPI/指标 `time_grain` 表单项、术语词典抽屉 admin 新增入口、ontology 同步按钮按角色隐藏 | `3029a559` `e31367c1` `55259d67` `30c4097f` | 需重新构建前端；新菜单项 `item.kpiGovernance` 需跑 `seed_menu_config.py` 才可见 |

> 另：`37969855` 把「近 N 月/季/年」的 N 上界设为 120（越界返回 None 不解析）——这是**修 500**（`近99999个月` 曾抛 ValueError/OverflowError），属恢复上线前行为，不是新行为。

### 1.3 迁移对生产库结构的改动（3 步，见 §2 逐条 SQL）

| 迁移 | 性质 | 生产影响 |
|---|---|---|
| 0141 | 改类型 / 收窄 / 加 NOT NULL / 加 2 条 FK | 涉及 4 张表；`ALTER TYPE` 会重写表并取 ACCESS EXCLUSIVE 锁；加 FK 会全表校验 |
| 0142 | 纯建表 `semantic_release` | 不触碰既有表 |
| 0143 | 纯 `ADD COLUMN`（3 列，nullable） | 不触碰既有数据；`downgrade` 直接删列 |

### 1.4 生产实测暴露的既有缺陷（**非本批引入**，但本批的开关决定了它可见不可见）

2026-11-14 生产实测（用户问「统计一下9月的采购订单完成率，每个基地分析一下，直接返回：指标「采购订单完成率」（…定义…）」）⇒ 返回**只有口径、没有数字**，`Tokens: 0 / 成本 $0`。同一指标不带定义地问 ⇒ 正常出数但**公式与指标定义不符**。代码定位（两个症状同源）：

| 症状 | 路径 | 根因 |
|---|---|---|
| 0 token、只回口径 | `_routeKpi`（`chat_stream_output.py:376`）命中 v1 → `_planKpiTier`（`:447`）因 Tier A 开关关 ⇒ **TIER_LEGACY** → `_buildL1Response`（`chat_service.py:1727`）→ `_resolveCalculationFeature`（`:1770`）按 `metric.formula` / `kpi.formula` **等值**找 `feature_definition`（`is_enabled`）⇒ 生产无此锚点 ⇒ `data=None` → `_buildAnswerText`（`:1914`）走无数据分支输出 `指标「X」（business_definition）` | 存量 KPI **无 FeatureDefinition 锚点**；且 legacy 分支 `l1 is not None` 就直接返回（`chat_stream_output.py:691-728`），**不降级到 LLM** ⇒ 既无数字，「按基地分组」也被丢弃 |
| 出数但公式不对 | 弱问句 v1 Jaccard 匹配不上 ⇒ 无 KPI 命中 ⇒ 走 LLM 流水线；而 `KPI_CONTRACT_INJECTION_ENABLED` 行缺失 ⇒ **契约块为空** ⇒ LLM 无签字口径，自编公式 | 口径契约**从未进过 prompt** |

⇒ **生产现状：KPI 口径一条都没进 LLM，也一条都不能确定性执行。**

⇒ **开关顺序因此被改**（见 §10）：
- **不能只开 `KPI_MATCH_CONTAINMENT_ENABLED`** —— 弱问法命中 KPI 后，Tier A 仍关 ⇒ 仍走 legacy L1 ⇒ 变成「0 token 只回口径」，**比现在更差**；
- **Tier A 的前置是 `time_grain`/`time_column` 回填** —— 问句含「9月」⇒ `hasExplicitTimeScope=True` ⇒ 要留在 Tier A 必须 `TIME_SCOPE_PLACEHOLDER in metric.formula` 且 `metric.time_column` 非空（`chat_stream_output.py:487-491`），而生产两列**现为全 NULL**；
- **契约注入是 Tier B（LLM）路径拿到口径的唯一通道**，但它只对 `verdict ≠ mismatch` 的 KPI 生效 ⇒ 开闸前必须先看 diagnose verdict；
- 弱问法能否被命中，取决于该 KPI 的 `semantic_keywords` / `metric_alias` 里**有没有简称**（如「订单完成率」）——containment 判定是「KPI 短语 ⊂ 问句」，问句比指标名短 ⇒ 没有简称则任何匹配方案都命中不了，只能治理侧补关键词。

> 🐛 **建议下批修的行为缺陷**（本批不改，需单独发版）：legacy L1 在「命中 KPI 但执行不出数」时返回 0-token 口径答案并终结整轮，**应改为降级到 LLM**（带契约），而不是把「定义」当成答案。

#### 1.4.1 生产实测数据（2026-11-14，`KPI_PUR_001` / metric id=1）

| 项 | 实测值 | 含义 |
|---|---|---|
| 后端日志 | `L1 metric 链路未命中 FeatureDefinition kpi=KPI_PUR_001 metric_id=1，回退 formula 等值` → `L1 回退路径未命中 FeatureDefinition …——执行降级为无数据回答` | 症状 A 根因**逐字命中** |
| `feature_definition` 全库行数 | **0** | ⇒ 生产**任何** KPI 的 legacy L1 都必然是「0 token 只回口径」 |
| `kpi_catalog.semantic_keywords` | `{采购订单完成率}`（**只有全名**） | 弱问法「订单完成率」Jaccard 交集为空（<0.75 阈值）、containment 也命中不了（KPI 短语 ⊄ 问句）⇒ **任何开关组合下都命中不了** ⇒ 需治理侧补简称 |
| `ontology_metric.metric_alias` | `采购订单完成率` | 同上 |
| `ontology_metric.execution_source` | **空** | ⇒ Tier A 即使开闸也会在 `execution_source_missing` 短路到 **Tier C** ⇒ **Tier A 要能跑必须先把它置 COMPILED / ESCAPE_HATCH** |
| `ontology_metric.metric_type` / `aggregation_order` | `MEASURE` / 空 | 业务定义却是**比率** ⇒ `_checkRatio` 不判、且契约块的除法形态指引 `ratioSqlGuidance` **只对 RATIO 生效** ⇒ 契约注入开了也拿不到「禁止 AVG(分子/分母)」的硬约束 ⇒ **技术形态必须改成 RATIO + WEIGHTED_RATIO** |
| `ontology_metric.time_column` | **空** | ⇒ Tier A 时间轴不可用（要求 `formula` 含 `{time_scope}` **且** `time_column` 非空） |
| `kpi_catalog.time_grain` / `ontology_metric.time_grain` | `month` / `month` | 双侧相等 ⇒ 时间粒度轴对账无 finding。⚠️ 与 Phase 1 取证的「`time_grain` 全 NULL、计数 0」矛盾 ⇒ **回填现状需复测** |

### 1.5 不影响生产的部分（省心清单）

- CI / merge-gate 工具链、分片编排、失败集合对差闸门（`83581e10` `6b479f68` `a37e4c09` 等）——只在 GitHub Actions 内；
- Schema SSOT 对齐（`ef90ea10` `29a63b80` `5f688cf1`）——**只改 ORM 声明，不产生 DDL**；效果是启动日志里的 drift warning 变少；
- 全部 `Harness/**` 与 `docs/**` 文档；
- Milvus 回填脚本修复（`87f8e8e6`）——脚本侧，不影响运行时。

---

## 2. 生产 DDL 评审（逐条 SQL，执行前请过目）

### 2.1 `0141_alembic_residual_36.py`（6 条，唯一有数据风险的一步）

```sql
-- ① data_quality_violation_sample.sample_pk_values: JSONB → JSON
ALTER TABLE data_quality_violation_sample ALTER COLUMN sample_pk_values DROP DEFAULT;
ALTER TABLE data_quality_violation_sample ALTER COLUMN sample_pk_values TYPE json
  USING sample_pk_values::json;
ALTER TABLE data_quality_violation_sample ALTER COLUMN sample_pk_values SET DEFAULT '[]'::json;

-- ② document_entity_relation.entity_type: VARCHAR(30) → VARCHAR(20)（收窄）
ALTER TABLE document_entity_relation ALTER COLUMN entity_type TYPE varchar(20);

-- ③ l3_acl.user_id / granted_by: INTEGER → BIGINT（放宽）
ALTER TABLE l3_acl ALTER COLUMN user_id TYPE bigint;
ALTER TABLE l3_acl ALTER COLUMN granted_by TYPE bigint;

-- ④ wiki_token_usage.compile_task_id: BIGINT → INTEGER（收窄）
ALTER TABLE wiki_token_usage ALTER COLUMN compile_task_id TYPE integer;

-- ⑤ l3_acl.granted_at: 加 NOT NULL
ALTER TABLE l3_acl ALTER COLUMN granted_at SET NOT NULL;

-- ⑥ wiki_compile_task 两条模型 FK（幂等：该列已有 FK 则跳过）
ALTER TABLE wiki_compile_task ADD CONSTRAINT fk_wiki_compile_task_selected_model
  FOREIGN KEY (selected_model_id) REFERENCES llm_config(id);
ALTER TABLE wiki_compile_task ADD CONSTRAINT fk_wiki_compile_task_fallback_model
  FOREIGN KEY (fallback_model_id) REFERENCES llm_config(id);
```

**风险与前置条件**

| 风险 | 说明 | 前置检查（Phase 1 ④） |
|---|---|---|
| ④ 收窄溢出 | `compile_task_id > 2147483647` 的行会让 ALTER 失败 | 查 `count(*) WHERE compile_task_id > 2147483647`，期望 0 |
| ⑤ 加 NOT NULL 失败 | 表里有 `granted_at IS NULL` 的行 | 查 `count(*) WHERE granted_at IS NULL`，期望 0 |
| ② 收窄失败 | `entity_type` 长度 > 20 的行 | 查 `max(length(entity_type))`，期望 ≤ 20（该列有 FK 指向 `business_object.code VARCHAR(20)`，理论上不可能超） |
| ⑥ FK 添加失败 | `wiki_compile_task.selected_model_id` / `fallback_model_id` 存在**在 `llm_config` 中找不到的行** | 查孤儿行数，期望 0（见 §5 Phase 1 ④） |
| ①③④ 锁 | `ALTER TYPE` 重写表，取 ACCESS EXCLUSIVE；`ALTER TABLE` 默认不并发 | 在低峰期执行；`data_quality_violation_sample` / `l3_acl` / `wiki_token_usage` 三表行数须小（见 Phase 1 ④） |

**回滚**：`alembic downgrade 0140` 可逆（脚本已写对称 downgrade）；④ 的降级方向是 INTEGER→BIGINT，安全；② 是放宽，安全；⑤ 撤销 NOT NULL 安全；⑥ 只删本迁移新增的命名约束。

### 2.2 `0142_semantic_release.py`（纯建表，不碰既有表）

```sql
CREATE TABLE semantic_release (
  id BIGSERIAL PRIMARY KEY,
  domain_code VARCHAR(20) NULL REFERENCES data_domain(code) ON DELETE RESTRICT,
  version VARCHAR(32) NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
  schema_version VARCHAR(20) NOT NULL DEFAULT 'v1',
  manifest_json JSONB NOT NULL,
  manifest_hash VARCHAR(64) NOT NULL,
  created_by VARCHAR(64) NULL,
  activated_time TIMESTAMPTZ NULL,
  note TEXT NULL,
  created_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_time TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_semantic_release_status CHECK (status IN ('DRAFT','VALIDATED','ACTIVE','ARCHIVED')),
  CONSTRAINT ck_semantic_release_manifest_is_object CHECK (jsonb_typeof(manifest_json) = 'object'),
  CONSTRAINT ck_semantic_release_manifest_hash_sha256 CHECK (manifest_hash ~ '^[0-9a-f]{64}$')
);
CREATE UNIQUE INDEX uq_semantic_release_global_version ON semantic_release (version)
  WHERE domain_code IS NULL;
CREATE UNIQUE INDEX uq_semantic_release_one_active ON semantic_release (COALESCE(domain_code, ''))
  WHERE status = 'ACTIVE';
CREATE INDEX ix_semantic_release_status ON semantic_release (status);
```

**前置条件**：`data_domain` 表必须存在（0133/0134 建，生产在 0140 ⇒ 已存在）。
**回滚**：`downgrade` 只删本表与其索引；表内数据（生产应为空）随删。

### 2.3 `0143_kpi_time_grain.py`（纯 ADD COLUMN，3 列全 nullable）

```sql
ALTER TABLE kpi_catalog     ADD COLUMN time_grain   VARCHAR(20) NULL;
ALTER TABLE ontology_metric ADD COLUMN time_grain   VARCHAR(20) NULL;
ALTER TABLE ontology_metric ADD COLUMN time_column VARCHAR(100) NULL;
```

**前置条件**：无（纯加列）。**回滚**：直接 drop 三列，无数据损失。

---

## 3. Phase 0 交付物（开发机已产出）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1114.tar.gz` | `qa-system-backend:amd64-1114`（linux/amd64，源 `main @ d001e38e`） | `8964765d2b26f25e0acf24c84de7d5ccba86e5ec2b258c0e5bbe81ce4d4f2a44` | 218M |
| `/tmp/qa-frontend-amd64-1114.tar.gz` | `qa-system-frontend:amd64-1114`（linux/amd64，`VITE_API_BASE_URL=` 空 ⇒ 同源相对路径） | `00502718f707bbdc8122ed50eba614f211a370b868b88160a6a91259be9aba9c` | 26M |

镜像侧核验（开发机已实测，全部通过）：

- backend：镜像内 `alembic/versions/` 有 `0141_alembic_residual_36.py`、`0142_semantic_release.py`、`0143_kpi_time_grain.py`；
  `scripts/kpi_backfill_time_grain.py` 在（16769 B）；新模块 `kpi_vector_graph_sync.py` / `semantic_release_service.py` / `kpi_alignment_resolver.py` / `api/v1/kpi_diagnose.py` / `api/v1/semantic_releases.py` 均在；**无 `/app/.env`**；
- frontend：bundle `index-C2wvQyYm.js`；`localhost:5173` 引用 **0** 处；`kpiGovernance` 引用 **43** 处。

---

## 4. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口（与 1101 / 1031 批发版一致）
docker-compose version; ls -la /opt/qa-system/docker/

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.yml \
    -f /opt/qa-system/docker/docker-compose.centos.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK

# 若上面的 centos.yml 不存在（生产改用 centos7.yml），把函数里的文件名换成 centos7.yml：
#   ls /opt/qa-system/docker/docker-compose*.yml
# ⚠️ 不要修改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役镜像名（tag 前以它为准，别照抄仓库名）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend'

# ② 当前迁移版本（预期 0140，不是则停下）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ③ 现有 KPI 开关行（生产现状，决定 Phase 7 怎么开）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT key, value FROM system_config WHERE key LIKE '\''KPI%'\'' ORDER BY key;"'

# ④ 0141 前置检查
#   ④-A 违规项：**必须全 0**，任一非 0 ⇒ 停下发我（DDL 会失败）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
  SELECT '\''orphan_selected_model'\'', count(*) FROM wiki_compile_task t
    WHERE t.selected_model_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM llm_config c WHERE c.id = t.selected_model_id)
  UNION ALL SELECT '\''orphan_fallback_model'\'', count(*) FROM wiki_compile_task t
    WHERE t.fallback_model_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM llm_config c WHERE c.id = t.fallback_model_id)
  UNION ALL SELECT '\''compile_task_id_overflow'\'', count(*) FROM wiki_token_usage
    WHERE compile_task_id > 2147483647
  UNION ALL SELECT '\''l3_acl_granted_at_null'\'', count(*) FROM l3_acl WHERE granted_at IS NULL
  UNION ALL SELECT '\''entity_type_too_long'\'', count(*) FROM document_entity_relation
    WHERE length(entity_type) > 20;"'

#   ④-B 行数信息项：**非 0 是正常的**（这几张表本来就有数据），
#        用途 = 评估 ALTER TYPE 的锁表时长。经验阈值：> 10 万行要挑低峰或改用 pg_dump/restore 方案
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "
  SELECT '\''rows_l3_acl'\'', count(*) FROM l3_acl
  UNION ALL SELECT '\''rows_wiki_token_usage'\'', count(*) FROM wiki_token_usage
  UNION ALL SELECT '\''rows_dq_sample'\'', count(*) FROM data_quality_violation_sample
  UNION ALL SELECT '\''rows_doc_entity_rel'\'', count(*) FROM document_entity_relation;"'

# ⑤ 磁盘余量（两包 ~250M，load 后镜像层翻倍）
df -h /var/lib/docker
```

**停下条件**

- ② 不是 `0140` ⇒ 停（版本对不上，迁移计划作废）。
- **④-A 任一项非 0** ⇒ 停，发我输出（对应 DDL 会直接失败）。
- ④-B 只是行数，用来估锁表时间；四张表若都在万级以下 ⇒ `ALTER TYPE` 瞬时完成，可继续。
---

## 5. Phase 2 备份 + 回滚锚点

```bash
mkdir -p /root/backup
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_1114_$(date +%m%d).sql

# 业务三表单独备份（0141 会改它们的结构）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  -t l3_acl -t wiki_token_usage -t document_entity_relation -t data_quality_violation_sample \
  > /root/backup/qa_metadata_pre_1114_0141tables_$(date +%m%d).sql

# 锚点（tag 前先按 Phase 1① 的实际镜像名）
docker tag qa-backend:offline       qa-backend:pre-1114
docker tag qa-system_frontend       qa-system_frontend:pre-1114
```

---

## 6. Phase 3 传输 + 加载 + tag

开发机 → 生产（scp / U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 qa-backend-amd64-1114.tar.gz qa-frontend-amd64-1114.tar.gz   # 开发机
sha256sum  qa-backend-amd64-1114.tar.gz qa-frontend-amd64-1114.tar.gz      # 生产
```

生产：

```bash
docker load -i qa-backend-amd64-1114.tar.gz
docker load -i qa-frontend-amd64-1114.tar.gz

docker tag qa-system-backend:amd64-1114   qa-backend:offline
docker tag qa-system-frontend:amd64-1114  qa-system_frontend

qa_compose config | grep image     # 确认解析到新 tag
```

---

## 7. Phase 4 迁移（**版本核对 + 条件迁移**）

> 🛑 **2026-11-14 实测更正（文档事实修正）**：新 backend 容器**已经在生产起过**，
> 而 `docker/Dockerfile.backend` 的 CMD 是 `alembic upgrade head && uvicorn ...`
> ⇒ **启动时 0141 / 0142 / 0143 已被自动应用**。
> 取证：`semantic_release` 表已存在、`kpi_catalog.time_grain` / `ontology_metric.time_grain`
> 已存在（0143 的三个 ADD COLUMN）。
> ⇒ 本阶段**降级为核对**；只有版本未到 0143 时才需要显式迁移。
> ⇒ 附带教训：**任何一次用新镜像起容器都会无声地迁移生产库**，所以「先迁移后起服务」的
> 硬闸门在新镜像下是自动满足的，但**回填（Phase 5）必须自己盯**。

```bash
# ① 核对版本（决定要不要迁）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'
# 期望 0143 ⇒ 直接进 Phase 5（迁移跳过）
# 若 < 0143 ⇒ 才执行下面两行（backend 先停）：
# qa_compose stop backend
# qa_compose run --rm backend alembic upgrade head
```

```bash
# ② 三列存在性（用 heredoc 走 stdin，避免 sh 双引号把 "\".\"" 吃掉）
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA' <<'SQL'
SELECT table_name, column_name FROM information_schema.columns
 WHERE column_name IN ('time_grain','time_column')
   AND table_name IN ('kpi_catalog','ontology_metric')
 ORDER BY 1;
SQL
# 期望 3 行：kpi_catalog.time_grain / ontology_metric.time_grain / ontology_metric.time_column

# ③ semantic_release 表存在
docker exec -i qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA' <<'SQL'
SELECT count(*) FROM information_schema.tables WHERE table_name='semantic_release';
SQL
# 期望 1
```

> ⚠️ 0141 若中途失败：库停在 0141 之前的状态 ⇒ 直接跳回 §12 回滚。
>
> ⚠️ 若 `qa_compose run --rm backend ...` 报**容器名冲突**（服务带 `container_name: qa-backend`，
> 被 stop 的容器仍占名）⇒ 换一次性容器（env 来自 compose 的 `.env`，等价）：
> ```bash
> docker run --rm --network qa-system_default \
>   --env-file /opt/qa-system/docker/.env \
>   qa-backend:offline alembic upgrade head
> # 网络名以 `docker network ls | grep qa-system` 实测为准
> ```

---

## 8. Phase 5 回填（硬闸门：迁移 → 回填 → 才上代码）

> 🛑 **2026-11-14 实测更正**：backend **已经在线**（新镜像已起、迁移已自动到 0143），
> 原「保持 stop 跑回填」的前提已不成立。两条路：
>
> - **方案 A（默认，推荐，无需停机）**：backend 保持在线，用一次性容器跑回填脚本。理由：
>   ① 回填只把 `kpi_catalog` 16 行 + `ontology_metric` 17 行的 `time_grain` 从 NULL 填成值，
>   **幂等、纯列写、不动其它列**；② 依赖 `time_grain` 的能力（Tier A / 契约注入 / 发布闸门）
>   在生产**全部是关的** ⇒ 「服务中读到 NULL」当前**不产生任何用户可见影响**；
>   ③ 停机代价（生产中断）远大于收益。
> - **方案 B（保守）**：`qa_compose stop backend` → 回填 → `qa_compose up -d backend`。
>   若你要求「代码上线那一刻治理列已齐」，用这条（多一次重启）。
>
> ⚠️ 无论 A/B，**回填必须在 Phase 7 开闸之前完成**（开 `KPI_MATCH_CONTAINMENT_ENABLED`
> 之前把 `time_grain` 填齐，别让 v2 匹配器拿到半成品数据）。
> 🔴 **2026-11-14 实测升级：回填不是「整洁性」，是 Tier A 的硬前置** ——
> 问句含显式时间（「9月」）时，Tier A 要求 `metric.formula` 含 `{time_scope}` 占位符
> **且** `metric.time_column` 非空（`chat_stream_output.py:487-491`）⇒ 生产两列现为 NULL ⇒
> **不先回填，Tier A 开了也会当场降级 Tier B**。
> ⚠️ 方案 A 下若有用户正巧同时在编辑 KPI/指标，会有一次并发写覆盖（33 行量级，概率极低）。

```bash
# 方案 A：backend 保持在线，用一次性容器跑脚本（env 来自 compose 的 .env）

# ① dry-run（只读；脚本会打印 target: user:***@host/qa_metadata database=qa_metadata）
qa_compose run --rm backend sh -c \
  'python scripts/kpi_backfill_time_grain.py --database-url "$DATABASE_URL"'

# ② apply（写生产）
qa_compose run --rm backend sh -c \
  'python scripts/kpi_backfill_time_grain.py --database-url "$DATABASE_URL" --apply --allow-prod-write'

# ③ 幂等复跑（硬性判据：changes=0）
qa_compose run --rm backend sh -c \
  'python scripts/kpi_backfill_time_grain.py --database-url "$DATABASE_URL" --apply --allow-prod-write'
```

**判据**：`changes` 与 `noop` 的具体数字取决于**生产数据**（开发库实测是 `changes=11 noop=1`，幂等复跑 `changes=0 noop=12`）——生产 KPI/metric 快照可能不同，**以生产 dry-run 的计划为准**；硬性判据是 **apply 成功后幂等复跑 `changes=0`**。

> ⚠️ 若 dry-run 报 `[schema-not-migrated]` ⇒ 0143 没生效，回 §7。
> ⚠️ 若脚本报 `拒绝执行：目标库 'xxx' 不在白名单内` ⇒ 生产的 POSTGRES_DB 库名不是 `qa_metadata`（白名单只允许 `qa_metadata` / `qa_metadata_test*`）⇒ 停下发我，别改脚本。
> ⚠️ 回填前窗口：8 条基线 pass KPI 会暂失 Tier A（单侧缺失 ⇒ unknown + degraded）。**生产 Tier A 本来就是关的**（无 `KPI_METRIC_TIER_A_ENABLED` 行）⇒ 无实际影响。
> ⚠️ `qa_compose run` 若报容器名冲突 ⇒ 用 §7 的 `docker run --rm --network ... --env-file ...` 替代
> （把 `alembic upgrade head` 换成 `sh -c 'python scripts/kpi_backfill_time_grain.py --database-url "$DATABASE_URL" ...'`）。

---

## 9. Phase 6 起服务 + 菜单 seed

> 🛑 **2026-11-14 实测更正**：**backend 已经起来了**（`qa-backend:offline` = 新镜像
> `qa-system-backend:amd64-1114`，同 image ID `354b9c552d4e`），启动时迁移已自动到 0143。
> ⇒ 本阶段降级为「**确认 frontend 是否也是新镜像 + 补菜单 seed**」。

```bash
# ① 确认 frontend 容器跑的是不是新镜像（不匹配 ⇒ 换新镜像重启 frontend）
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}" | grep -Ei "frontend|NAMES"

# ② 若需要统一重起（backend + frontend）
qa_compose up -d backend frontend
docker logs --tail 50 qa-backend | grep -E "Application startup complete|Traceback"

# ③ 菜单 seed（幂等；新增 item.kpiGovernance ⇒ 治理看板入口）
docker exec -e PYTHONPATH=/app -w /app qa-backend python scripts/seed_menu_config.py
```

> 镜像 CMD 是 `alembic upgrade head && uvicorn ...` ⇒ 每次启动都会再跑一次迁移
> （已在 0143 ⇒ 空操作，无害）。但**每次启动都会自动迁移** ⇒ 换镜像前先核对 `alembic_version`。

---

## 10. Phase 7 「统一开闸」（六开关最终态）

用户口径：**生产部署新代码时统一开闸**。生产现状（Phase 1③ 实测为准）：

> 🛑 **2026-11-14 实测更正：开闸顺序必须改。** 生产实测（见 §1.4）暴露：生产**一条 KPI 口径都没进 LLM**，也**一条都不能确定性执行**。
>
> - **不能只开 containment**：弱问法命中 KPI 后若 Tier A 仍关 ⇒ 走 legacy L1 ⇒「0 token 只回口径」，**比现状更差**；
> - **Tier A 的前置是回填**：问句含显式时间（「9月」）⇒ 要留在 Tier A 必须 `metric.formula` 含 `{time_scope}` 占位符**且** `metric.time_column` 非空 ⇒ 生产两列现为 NULL ⇒ **必须先跑 Phase 5 回填**；
> - **契约注入是 Tier B（LLM）路径唯一能拿到口径的通道**，但只对 `verdict ≠ mismatch` 的 KPI 生效 ⇒ **开闸前必须先看 Phase 8 ④ 的 diagnose verdict**。

| key | 生产现状 | 建议终态 | 理由 |
|---|---|---|---|
| `KPI_METRIC_TIER_A_ENABLED` | 无行 | **true**（**条件**：Phase 5 回填完成 **且** Phase 8 ④ 的 verdict 全 pass） | 8 条 ESCAPE_HATCH 指标可 0 token 确定性作答。**必须先于 containment 开** |
| `KPI_MATCH_CONTAINMENT_ENABLED` | 无行 | **true**（**条件**：Tier A 已开且冒烟通过） | 匹配器 v2 已过 50 条回归集；开启后 KPI 问句走包含式匹配（不等长则澄清，不猜）。⚠️ **单独开会把弱问法推进「只回口径」路径** |
| `KPI_CONTRACT_INJECTION_ENABLED` | **env=true**（1030 批用户两次批准）+ **行缺失** ⇒ **实际生效=关** | **条件开**（见 §10.1） | 这是 Tier B（LLM）路径**唯一**把签字口径送进 prompt 的通道 ⇒ 生产「公式不对」的病根。⚠️ **写行即激活**，会改 chat prompt；且只对 `verdict ≠ mismatch` 的 KPI 生效 ⇒ **开前先看 diagnose** |
| `KPI_METRIC_GATE_ENABLED` | 无行 | 保持 **无行（关）** | P5 明确「默认关，开启需人工确认」；开着会让不合规指标 409 挡住现有人工维护流程 |
| `KPI_MATCH_VECTOR_ENABLED` | 无行 + env 默认 false | 保持 **关** | 向量召回会改 chat 行为；且 `KPI_VECTOR_DISTANCE_MAX=0.5`（L2⇒cos≈0.875）实测近乎打不中，收益≈0 |
| `KPI_SYNC_ENABLED` | 无行（⇒ on） | 保持 **on**（或不写行） | 新增向量/图副本是本批目标；失败只进 `failures` 不影响主流程。若担心生产后台写负载，可显式置 `false` |

### 10.1 契约注入要不要开：按 diagnose verdict 决策

| diagnose `verdict` | `tierAEligible` | `timeColumn` | 处方 |
|---|---|---|---|
| `pass` | ✅ | 有 | 回填已够 ⇒ **开 Tier A** → 冒烟 → 再开 containment；契约注入**可开可不开**（Tier A 覆盖到的问句不需要它） |
| `pass` | ✅ | 无 | **先跑 Phase 5 回填** ⇒ 再开 Tier A；契约注入**建议开**（兜住 Tier A 降级到 Tier B 的问句） |
| `mismatch` | ❌ | — | Tier A 与契约注入**都被堵**（mismatch 的 KPI 不进契约块）⇒ 先做**口径对账的治理动作**，再谈开闸 |

### 10.2 开 / 关（幂等，key 是主键）

⚠️ **执行顺序**：**回填 → Tier A → 冒烟 → containment → 契约注入**。每一步独立可回滚（删行即回到「缺行默认」）。

```sql
-- Phase 7①：Tier A —— **条件**：Phase 5 回填完成 **且** Phase 8 ④ 的 verdict 全 pass
INSERT INTO system_config (key, value, description) VALUES
  ('KPI_METRIC_TIER_A_ENABLED', 'true', 'KPI Tier A 确定性执行（1114 发版统一开闸）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_time = NOW();
```

```bash
# Phase 7②：匹配器 v2 —— **必须在 Tier A 已开且冒烟通过之后**
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "INSERT INTO system_config (key, value, description) VALUES
     ('\''KPI_MATCH_CONTAINMENT_ENABLED'\'', '\''true'\'',
      '\''匹配器 v2 包含式匹配（1114 发版统一开闸）'\'')
   ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_time=now();"'
```

```sql
-- Phase 7③：契约注入 —— **条件**：§10.1 判定为「建议开」且已确认 verdict ≠ mismatch 的 KPI 覆盖得住
-- ⚠️ 写行即激活：所有命中 KPI 的 chat prompt 都会多出契约块 ⇒ 现有回答口径会变（变正确）
INSERT INTO system_config (key, value, description) VALUES
  ('KPI_CONTRACT_INJECTION_ENABLED', 'true', 'KPI 治理契约注入（1114 发版统一开闸）')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_time = NOW();
```

```sql
-- 回滚用（把某闸关掉，保留行便于审计；删行则回到「缺行默认」）
-- UPDATE system_config SET value='false', updated_time=now() WHERE key='KPI_MATCH_CONTAINMENT_ENABLED';
-- DELETE FROM system_config WHERE key='KPI_MATCH_CONTAINMENT_ENABLED';
```

> `system_config` 每请求读取 ⇒ **改行不用重启**；只有 env 杀闸需要重启进程。
> `KPI_MATCH_VECTOR_ENABLED` 想开必须 **env 显式 true + 行 true**（双串联闸）。
> `KPI_METRIC_GATE_ENABLED`（发布闸门）本批**不开**：P5 明确要求人工确认，且开着会让不合规指标 409 挡住现有维护流程。


---

## 11. Phase 8 冒烟（上线后必做）

```bash
# ① 迁移版本 + 三列
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ② 健康
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/api/v1/health

# ③ 前端新页可达（治理看板）
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/kpi-governance

# ④ ⚠️ 对账 verdict（决定契约块是否进 prompt —— §1.2 ⑦）
curl -sS -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:5173/api/v1/admin/kpi/diagnose?question=%E4%BE%9B%E5%BA%94%E5%95%86%E5%87%86%E6%97%B6%E4%BA%A4%E4%BB%98%E7%8E%87' \
  | python3 -m json.tool | head -40
# 关注：matches[].verdict（pass/mismatch/unknown）与 matches[].degraded
# 若出现 pass/unknown ⇒ 契约块将进 NL2SQL prompt ⇒ 现有回答口径会变（属预期收益，但需知悉）
# 若仍全 mismatch ⇒ 契约块仍为空 ⇒ chat 行为与上线前完全一致

# ⑤ 回归审计（近 7 天 degraded 对账行应为 0）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT count(*) FROM audit_log WHERE entity_type='\''kpi_metric_reconcile'\''
     AND created_at >= now() - interval '\''7 days'\''
     AND after_json->>'\''degraded'\'' = '\''true'\'';"'

# ⑥ 向量/图对账（KPI_SYNC_ENABLED 默认为开 ⇒ 存量 12 条应有补偿）
curl -sS -X POST http://127.0.0.1:5173/api/v1/kpi-catalog/embeddings/sync-missing \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool | head -30
# failedCount > 0 ⇒ 看 failures[]（多为 embedding 未配 / Milvus 不可达；不影响主流程）
```

业务冒烟（浏览器，硬刷新）：

1. 问一个 KPI 问题（如「供应商准时交付率是多少」）⇒ 记录答案与 `executionTier`；开 Tier A 后应显示 Tier A 且 tokensUsed=0；
2. 问一个带「最近 7 天」的问题 ⇒ 与上线前对比（**口径已修正，数字会变**，属预期）；
3. 治理看板 `/kpi-governance` 可见（admin）；非 admin 看不到菜单项；
4. 本体 → 指标编辑表单出现「时间粒度」项；
5. 非 admin 账号登录 ⇒ KPI 治理页不可见、指标写入口 403（**这是 1.2 ① 的预期表现**）。

---

## 12. 回滚

```bash
qa_compose stop backend frontend
docker tag qa-backend:pre-1114         qa-backend:offline
docker tag qa-system_frontend:pre-1114 qa-system_frontend
qa_compose up -d backend frontend
```

- 旧代码不认识 `time_grain` / `time_column` / `semantic_release` ⇒ **多出来的列与表可以保留**（旧代码不读，无副作用）；
- 若要彻底回退结构：`qa_compose run --rm backend alembic downgrade 0140`（**会删列**：0143 三列数据丢失、0142 表与数据丢失、0141 类型回退——`wiki_token_usage.compile_task_id` 若已有超 2^31 值会失败，属有意 fail-fast）；
- 数据回滚：Phase 2 的 `pg_dump` 备份（整库 + 0141 四张表）；
- 开关回滚：删 `system_config` 行即回到「缺行默认」；`KPI_MATCH_CONTAINMENT_ENABLED` 删行 ⇒ 回落 v1 关键字路径。

---

## 13. 待办 / 风险登记

1. **治理列对齐仍未做**（生产 12/12 mismatch）：本批不自动执行 `kpi_normalize_governance.py`（生产数据写入，需业务确认）。要做的话：先 dry-run 出 before/after 对照报告 → 人工确认 → apply。
2. **P2–P6（D12–D62）共 51 个工作日未开工**（1109 排程），门禁 G3–G7 全未过；本发版不涉及。
3. **语义发布链路在生产从未端到端跑过**（`semantic_release` 生产为空、开关默认关）；本发版只带代码与表结构。
4. 删 KPI 会残留 `id_mapping` 孤儿行（未修，跨批）。
5. `KPI_VECTOR_DISTANCE_MAX=0.5` 对 L2 相当于 cos≈0.875 ⇒ 向量匹配近乎打不中（故本批建议不开 `KPI_MATCH_VECTOR_ENABLED`）。
