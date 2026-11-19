# 生产发版 runbook — 召回根治三件套（#157 / #158 / #159）

- **代码版本**：`main @ d1c20de9`（PR #159 squash 合并）
  - 该 main 依次包含：**PR #157** `d58bb1ad`（数据源过滤 + 关键词锚定）、
    **PR #158** `f07ae867`（Gap B：向量路径失效时锚定接管）、
    **PR #159** `d1c20de9`（Gap A：向量检索窗口收敛到本地类 id）。
  - ⚠️ 生产只跑 main ⇒ 镜像**必须**由合并后的 main 构建（本批**直接**从 `main @ d1c20de9`
    检出构建 ⇒ 不存在「分支头 ≠ 合并 commit」的等价性争议）。
- **本次动**：**前端 + 后端两个容器**；数据库**加法迁移 9 个**（0144–0152，main 既有，本批自身零迁移）
- **不动**：postgres / neo4j / milvus / ollama 容器一律不动；不改 `/opt/qa-system/docker/.env`
- **执行者**：生产命令全部由用户在宿主机执行（开发机无法直连生产）

> 本 runbook **取代** `Harness/changes/2026-11-19-recall-datasource-filter-anchor/deploy-prod.md`
> 的「构建 / 交付 / 切换 / 验收」段（那份是 #157 单批的稿，镜像已过期：它的构建源 `970fc39f`
> 早于 #158/#159 两批合并）。**探针段（§2 A1/A2/B1/C1）与墓碑复活段（§7）以那份为准，本文件已全文并入。**

---

## 0. ★关键依赖：本批自身零迁移，但**必须与 0144–0152 迁移同窗口**

本批三件套**零 alembic 迁移**（`alembic/versions/` 无新增）⇒ **别照抄 1119 runbook 的 downgrade 步骤**
（本批自身无需 downgrade 0144–0152，那 9 个迁移是 main 上既有的）。

但本批的 base 是 `main @ d1c20de9`，其 ORM **已声明** 0144–0152 引入的表
（`workflow_*` / `approval_task` / `discovery_*` 等）⇒ schema drift 的 `missing_table` 是 **blocking** 级
（`backend/app/infrastructure/schema_drift.py`）⇒ **生产库不到 0152，新 backend 起不来**。

生产库现状（2026-11-19 预检实测）：`alembic_version = 0143`（0144–0148 当时标「不部署」）。
⇒ **本批不能单独发**：一次部署同时完成 0143→0152 迁移 + 召回根治三件套。

> ⚠️ 迁移链的既有判据：`git diff` 迁移目录为空 ⇒ 本批零迁移；
> `alembic heads` 必须是**单一 head `0152`**（若出现第二个 head ⇒ 迁移链分叉 ⇒ 停下）。

---

## 1. Phase 0 生产预检（只读）

```bash
# ⓪ compose 入口 —— 从现役容器 label 读出（唯一权威），不要靠 ls 猜
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
#   预期：/opt/qa-system/docker/docker-compose.centos7.yml（单文件自包含）
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --images    # 确认解析出的镜像名（回填 §2 对照）

# ⚠️ 生产只有 v1 docker-compose（`docker compose` 未装插件）；centos7.yml 不含 !override，v1 可解析。
# ⚠️ v1 下不要加 --remove-orphans。
# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役容器与镜像（回滚锚点以实际输出为准）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
#   预期：backend = qa-backend:offline（1116 热修镜像 513b4287ff58）
#         frontend = qa-system_frontend（改名镜像）

# ② 现役前端 bundle（基线锚点）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'
#   预期：index-rBu8Smfz.js（1119 未部署时）；若已是 index-BVutQwOS.js ⇒ 1119 已上线，走 §7 分支判断

# ③ 现役库 alembic 版本（决定迁移段要不要跑）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'
#   0143 ⇒ 必须带 0144–0152 迁移（本 runbook 默认路径）
#   0152 ⇒ 迁移已在库，本批纯代码更新（跳过 §6.1 的迁移观察即可）
#   其它值 ⇒ 停下发我（迁移链对不上）

# ④ 健康检查
curl -s -o /dev/null -w "backend_health=%{http_code}\n"  http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/

# ⑤ 磁盘余量（两个包 ~250M + 新镜像 + 备份）
df -h /var/lib/docker
```

**停下条件**：② 的 bundle 与预期不符（说明 1119 已上线或基线漂移）；③ 的版本既非 0143 也非 0152；④ 任一非 200。

---

## 2. Phase 0.5 上线前置探针（只读 SQL，**不通过不得上线**）

本批有一个**代码不可解**的风险：过滤用 `schema_cache`（按 `(datasource_id, schema_name)` 唯一，Oracle 一源多 owner）
做「类→源」判定，而体检制本体导入**不校验** `schema_cache` ⇒「类已登记、owner 未 introspect」可达
⇒ 该 owner 的类会被硬过滤**静默剔除**（不是报错，是「看不见」）。

先取数据源 id 清单：

```bash
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select id, name, type, is_active from data_source order by id"'
```

对**每个将被使用的数据源**各跑一次（把 `:datasource_id` 替换成实际 id）：

```sql
-- A1：本源有哪些 owner 进了 schema_cache、每个 owner 缓存了多少张表
SELECT sc.schema_name AS owner,
       count(*)       AS cache_rows,
       jsonb_array_length(sc.schema_data) AS table_cnt
FROM schema_cache sc
WHERE sc.datasource_id = :datasource_id
GROUP BY sc.schema_name
ORDER BY sc.schema_name;

-- A2：本源登记的活类里，有多少在本源缓存清单中找不到（这些会被静默剔除）
SELECT upper(oc.source_table) AS source_table,
       count(*)               AS class_cnt
FROM ontology_class oc
WHERE oc.valid_to IS NULL
  AND coalesce(oc.source_table, '') <> ''
  AND upper(oc.source_table) NOT IN (
      SELECT upper(t ->> 'table_name')
      FROM schema_cache sc, jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  )
  AND upper(oc.source_table) NOT IN (
      SELECT upper(t ->> 'table_name' || '.' || coalesce(t ->> 'owner', ''))
      FROM schema_cache sc, jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  )
GROUP BY upper(oc.source_table)
ORDER BY class_cnt DESC;

-- B1：除本源外，其它数据源的 DIM_* 类有多少（维度词触发时走 DIM 全量拉取路径）
SELECT count(*) AS cross_source_dim_classes
FROM ontology_class oc
WHERE oc.valid_to IS NULL
  AND upper(coalesce(oc.source_table, '')) LIKE 'DIM\_%'
  AND upper(coalesce(oc.source_table, '')) NOT IN (
      SELECT upper(t ->> 'table_name')
      FROM schema_cache sc, jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  );

-- C1：可能进研究 BO 臂的「跨源活类」（BO 臂仍是 Milvus 全局检索，无数据源维度）
SELECT count(*) AS cross_source_alive_classes
FROM ontology_class oc
WHERE oc.valid_to IS NULL
  AND coalesce(oc.source_table, '') <> ''
  AND upper(oc.source_table) NOT IN (
      SELECT upper(t ->> 'table_name')
      FROM schema_cache sc, jsonb_array_elements(sc.schema_data) AS t
      WHERE sc.datasource_id = :datasource_id
  );
```

**判读**（与代码口径一致：`source_table` 全文 `.upper()` ∈ 裸名 ∪ owner 限定名并集）：

| 探针 | 结果 | 处置 |
|---|---|---|
| **A1** | 空（该源一行缓存都没有） | **该数据源不得上线** ⇒ 过滤整体 fail-open（披露 `no_cache`），跨源幽灵表仍会进 ⇒ 先补 introspect |
| **A2** | 非空 | 这些表「本体登记了但本源没 introspect」⇒ 会被**静默剔除** ⇒ **该数据源不得上线**，先补该 owner 的 introspect（或人工确认这些类确实不该出现） |
| A2 | 只含跨源表名 | 覆盖度正常，可上线 |
| **B1** | > 0 | 上线后**必须**用含维度词的问句（「名称」「属性」「分类」…）回归一次 |
| **C1** | > 0 | 上线后**必须**用**研究入口**重问「公司目前有多少供应商」，确认无跨源值域采样空转 |

> ⚠️ 爆炸半径提示：硬过滤现覆盖 **chat 三调用点 + 维度词 DIM 全量拉取路径 + 研究执行面**（共 5 处）。
> ⚠️ **Gap A（#159）的作用域另算**：向量检索窗口的收敛只作用在**对话链路的类召回**接线上，
> **不覆盖研究 BO 臂**（BO 臂仍是 Milvus 全局检索）⇒ C1 > 0 时研究入口仍是残余风险面。

---

## 3. Phase 1 交付物（开发机已构建并自证）

> ✅ **本批直接从合并后的 main 构建** ⇒ 无「分支头 vs 合并 commit」的等价性论证负担。
> 构建源 = `/tmp/build-1120`（`git worktree add --detach /tmp/build-1120 origin/main`）
> ⇒ `HEAD = d1c20de9`，`git status --porcelain` 为空（无未跟踪文件进入构建上下文）。
> 复核命令（任何时候可重跑）：
>
> ```bash
> git -C /tmp/build-1120 rev-parse HEAD          # d1c20de9...
> git -C /tmp/build-1120 status --porcelain       # 空
> ```

```bash
# 开发机（macOS arm64 → linux/amd64 交叉构建）
git fetch origin main && git rev-parse origin/main
git worktree add --detach /tmp/build-$(date +%m%d) origin/main
cd /tmp/build-$(date +%m%d)

docker build --platform linux/amd64 \
  -f docker/Dockerfile.backend \
  -t qa-system-backend:amd64-1120-recall .

docker build --platform linux/amd64 \
  -f docker/Dockerfile.frontend \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -t qa-system-frontend:amd64-1120-recall .

docker save qa-system-backend:amd64-1120-recall  | gzip > /tmp/qa-backend-amd64-1120-recall.tar.gz
docker save qa-system-frontend:amd64-1120-recall | gzip > /tmp/qa-frontend-amd64-1120-recall.tar.gz
shasum -a 256 /tmp/qa-*-amd64-1120-recall.tar.gz
```

> 上下文是**仓库根**（Dockerfile 内 `COPY backend/ ./` / `COPY frontend/ ./`）。
> 前端 `--build-arg NPM_REGISTRY` 必带（默认官方源，国内构建会卡）。
> `VITE_API_BASE_URL` 留空 ⇒ 同源相对路径 `/api/v1`（经 nginx 反代）。

**构建后自证（不绿不出包）**：

```bash
# 后端：本批两个新模块在镜像内 + 五个调用点都接上 + 迁移最高 0152（无 0153+）
docker run --rm --entrypoint sh qa-system-backend:amd64-1120-recall -c '
  ls -1 /app/app/services/recall_datasource_filter.py /app/app/services/recall_keyword_anchor.py
  grep -c applyDatasourceRecallFilter \
    /app/app/services/chat_recall.py /app/app/services/chat_service.py \
    /app/app/services/chat_multistep.py /app/app/services/research_agent_execution.py
  ls -1 /app/alembic/versions/ | sort | tail -3
'
# 期望：两个文件都在；四个 grep 均 ≥1；迁移末位 0152（**不得出现 0153+，采购线 0154–0158 也不得出现**）

# 前端：新文案进 bundle + bundle 名与现役不同
docker run --rm --entrypoint sh qa-system-frontend:amd64-1120-recall -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "已按当前数据源过滤" /usr/share/nginx/html/assets/index-*.js
  grep -c "已按关键词锚定"     /usr/share/nginx/html/assets/index-*.js
  grep -c "泰鸿智脑"           /usr/share/nginx/html/index.html
'
# 期望：bundle 名 ≠ index-rBu8Smfz.js（现役）且 ≠ index-BVutQwOS.js（1119 待发版那次）、
#       ≠ index-DizP7VQ_.js（#157 单批那次，均未上线过）；
#       两个新文案 grep = 1；泰鸿智脑 = 1（1119 改名已含）
```

**交付物**：

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1120-recall.tar.gz` | `qa-system-backend:amd64-1120-recall`（→ tag `qa-backend:offline`） | `23b775d2e43bec621fe63070be505ee668f801c023f221d5137115c07fae1aa2` | 219M |
| `/tmp/qa-frontend-amd64-1120-recall.tar.gz` | `qa-system-frontend:amd64-1120-recall`（→ tag `qa-system_frontend`） | `47c12ccb97c64b4a528841a3f25ce5407017ba87af70a018ff46e6e5b102c949` | 26M |

**自证结果（开发机实测，2026-11-20）**：

| 探针 | 实测 |
|---|---|
| 后端 `alembic heads` | **`0152 (head)`**（单一 head；版本目录末位 `0152_audit_workflow_actions.py`，**无 0153+、无采购 0154–0158**） |
| 后端 `recall_datasource_filter.py` / `recall_keyword_anchor.py` | 均在镜像内 |
| 后端 `grep -c applyDatasourceRecallFilter` | chat_recall **2** / chat_service **2** / chat_multistep **2** / research_agent_execution **3**（均 ≥1） |
| 后端 Gap A 探针 | `classOntologyIds` = 1、`class_scope_bypass` = 1、`pinned_scope_skipped` = 1（`chat_recall.py`）；`buildClassIdScopeExpr` = 1（`milvus_client.py`） |
| 前端 bundle | **`index-t1K9T8s7.js`**（≠ 现役 `index-rBu8Smfz.js`，≠ 1119 那次 `index-BVutQwOS.js`，≠ #157 单批那次 `index-DizP7VQ_.js` ⇒ 三个 bundle 名都不同，可确认确实换了） |
| 前端文案 | `已按当前数据源过滤` = 1、`已按关键词锚定` = 1、`泰鸿智脑` = 1 |
| 平台 | `docker run` 报 `platform (linux/amd64) does not match host (linux/arm64/v8)` 是**预期**（交叉构建），不是错误 |

---

## 4. Phase 2 回滚锚点（镜像 tag + **pg_dump**）

```bash
# 镜像锚点（⚠️ backend 现役引用名是 qa-backend:offline，frontend 是 qa-system_frontend）
docker tag qa-backend:offline      qa-backend:pre-1120-recall
docker tag qa-system_frontend       qa-system_frontend:pre-1120-recall
docker images | grep -E 'pre-1120-recall'

# 数据库锚点（本次带 DDL ⇒ 必须备份）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/qa-system/backups/pg_pre_1120_recall_$(date +%Y%m%d_%H%M).sql
ls -lh /opt/qa-system/backups/ | tail -3
# 备份非空校验：文件应为几百 KB～数百 MB；0 字节 ⇒ 重跑并检查上面报错
```

> ⚠️ **别在库里建 `<表>_bak` 影子表做备份** ⇒ 下次 backend 启动会被 schema drift 判「多余表」直接拒启动。
> 必须 `pg_dump` 到宿主文件。

---

## 5. Phase 3 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可），**传完先比对 sha256**（§3 表）。

```bash
docker load -i qa-backend-amd64-1120-recall.tar.gz
docker load -i qa-frontend-amd64-1120-recall.tar.gz
# ⚠️ tag 成**现役容器引用的名字**
docker tag qa-system-backend:amd64-1120-recall  qa-backend:offline
docker tag qa-system-frontend:amd64-1120-recall qa-system_frontend
docker images | grep -E 'qa-backend:offline|qa-system_frontend:latest'
```

---

## 6. Phase 4 切换 + 验收（前端 + 后端，不碰数据容器）

### 6.1 切换

```bash
# ① 先后端（CMD 会自动 `alembic upgrade head`：库 0143 ⇒ 升到 0152，共 9 条加法迁移）
qa_compose up -d --no-build --no-deps --force-recreate backend

# 盯启动日志：9 条 Running upgrade + "Application startup complete." 必须出现
docker logs -f qa-backend 2>&1 | grep -E "Running upgrade|014[4-9]|015[0-2]|Application startup complete|ERROR|blocking" | head -40

# ② 再前端（换 bundle；同时刷新 nginx 的 upstream IP —— 必须在 backend recreate 之后）
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
```

> - `--no-build` 防现场 build；`--no-deps` 防顺带重启 postgres/neo4j/milvus；`--force-recreate` 保证真换镜像。
> - **顺序不能反**：先 backend 后 frontend（nginx 会缓存 upstream IP，见 [[qa-system-nginx-upstream-ip-stale]]）。
> - 生产无 worker 容器 ⇒ P5 工作流的后台超时/补偿巡检**不会跑**（API 可用）；本批不新增 worker。

### 6.2 验收探针

```bash
# ① 启动 + 迁移 + 健康
docker logs qa-backend --since 10m 2>&1 | grep -cE "Application startup complete"   # ≥1
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                                        # 0152
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health   # 200
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/                  # 200
curl -s -o /dev/null -w "api_via_frontend=%{http_code}\n" http://localhost:5173/api/v1/health  # 200

# ② 本批代码落地（代码探针）
docker exec qa-backend ls -1 /app/app/services/recall_datasource_filter.py \
                            /app/app/services/recall_keyword_anchor.py
docker exec qa-backend sh -c 'grep -c applyDatasourceRecallFilter \
  /app/app/services/chat_recall.py /app/app/services/chat_service.py \
  /app/app/services/chat_multistep.py /app/app/services/research_agent_execution.py'   # 各 ≥1
#   Gap A（#159）作用域接线探针：
docker exec qa-backend sh -c 'grep -c "classOntologyIds" /app/app/services/chat_recall.py \
  && grep -c "class_scope_bypass" /app/app/services/chat_recall.py'                    # 各 ≥1

# ③ 过滤真的生效（服务端日志探针）
docker logs qa-backend --since 10m 2>&1 | grep -c "召回候选跨源过滤"          # ≥0；>0 ⇒ 有类被滤掉（过滤在跑）
docker logs qa-backend --since 10m 2>&1 | grep -c "schema_cache 无可用表清单"  # 期望 0（非 0 ⇒ fail-open 在生效 ⇒ 停）
docker logs qa-backend --since 10m 2>&1 | grep -c "ORA-00942"                 # 期望 0（硬红线）
docker logs qa-backend --since 10m 2>&1 | grep -c "main_data_supplier"        # 期望 0（270 仍是墓碑时；复活后它可能被记为「已滤掉」）
#    注：「召回候选跨源过滤」这行日志**会列出被滤掉的表名**（前 10 个）⇒ 看到别的跨源表名是正常的，
#    看到 main_data_supplier 才说明它被召回了（270 复活后且源不对时才会出现）。

# ④ Gap A 三个新观测点（缺任一项 ⇒ 修复静默失效且没人知道）
docker logs qa-backend --since 10m 2>&1 | grep -c "scene=class_scope_bypass"     # 期望 0；>0 ⇒ 本地类没有向量 / expr 过严 ⇒ 要查
docker logs qa-backend --since 10m 2>&1 | grep -c "scene=pinned_scope_skipped"   # >0 ⇒ 该会话钉住了语义发布 ⇒ 该路径**不**收窄（预期行为，非故障）
docker logs qa-backend --since 10m 2>&1 | grep -c "reason=no_match"              # 期望 ≈0（作用域生效后命中应全部落在本地类）
docker logs qa-backend --since 10m 2>&1 | grep -c "scene=class_scope_too_large"  # 期望 0；>0 ⇒ 作用域 id > 2000 ⇒ 降级为不限

# ⑤ 前端 bundle
docker exec qa-frontend sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "已按当前数据源过滤" /usr/share/nginx/html/assets/index-*.js
  grep -c "已按关键词锚定"     /usr/share/nginx/html/assets/index-*.js
  grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf
'
```

### 6.3 浏览器端到端（**硬刷新 Ctrl+Shift+R**）

1. 聊天页问 **「公司目前有多少供应商」** ⇒ **必须出数**（不再礼貌拒答：这正是锚定要治的「问法敏感漏选」）；
2. 该条消息下方出现 info 级提示：**「已按当前数据源过滤 N 张跨源数据表。」**（N > 0）与/或
   **「已按关键词锚定 N 张数据表。」**（N > 0）⇒ 披露面证明两个机制都在跑；
3. 服务端日志零 `ORA-00942`；
4. 若 §2 的 **B1 > 0** ⇒ 追加回归一个含维度词的问句（如「列出所有名称维度」）⇒ 跨源 `DIM_*` 类不再进候选集；
5. 若 §2 的 **C1 > 0** ⇒ 追加走**研究入口**重问「公司目前有多少供应商」⇒ 无跨源值域采样空转；
6. **再问一个不含维度词的泛化问句**（如「公司经营情况怎么样」）⇒ 验证 Gap A 的作用域窗口不是把召回收得过窄
   （作用域过窄的表征是「本来能答、现在答不出」⇒ 与「问法敏感漏选」是相反方向的回归）。

---

## 7. Phase 5 墓碑 id 270 原地复活（**验收通过后**才做）

```sql
-- 前提：§6.2 / §6.3 已全绿，且已在 **SRM 数据源会话**下确认 main_data_supplier 被正常召回且不引发 ORA-00942
UPDATE ontology_class SET valid_to = NULL WHERE id = 270;
```

- ⚠️ **必须原地**：换行（新建一行）= 换 `class_id` = **Milvus 向量悬空**（旧向量指向已不存在的 id）= 白做。
- ⚠️ **未验证前保持墓碑**（宁可漏召，不可幽灵表）。
- ⚠️ **id 271 未探针核实前一律不动**（本地测试库无该 id 种子，不得据本地推断）。核实方式：
  查其 `source_table` 是否落在当前会话数据源的 `schema_cache` 清单内 ⇒ 跨源则与 270 同策（保持墓碑），
  本源则可原地复活。

---

## 8. 回滚

```bash
# ① 前端可单独回滚（无 DB 依赖）——最常用
docker tag qa-system_frontend:pre-1120-recall qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend

# ② 后端回滚：⚠️ 旧镜像 CMD 带 `alembic upgrade head`，库=0152 时会报「Can't locate revision」起不来
#    ⇒ 必须先降级。降级目标**读出来**，不要凭记忆写 0143/0148：
docker run --rm --entrypoint sh qa-backend:pre-1120-recall -c 'alembic heads'   # 旧镜像的 head
qa_compose stop backend
docker run --rm --network qa-system_default \
  -v /opt/qa-system/docker/.env:/app/.env:ro \
  qa-system-backend:pre-1120-recall \
  alembic downgrade <上面读到的 head>
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'
docker tag qa-backend:pre-1120-recall qa-backend:offline
qa_compose up -d --no-build --no-deps --force-recreate backend
```

> downgrade 会 drop 0144–0152 建的表（workflow 6 张 + discovery 2 张）并删 4 个可空列
> ⇒ 窗口内产生的审批/工作流数据丢失（§4 的 pg_dump 兜底）。`audit_log` 既有数据不受影响。
> `extra_table` 在 schema drift 里只是 warning（不 blocking）⇒ 表留库不挡旧代码。

---

## 9. 本批明确没做的事

- ❌ 不带采购线（`feat/procurement-m0` 的 0154–0158 与采购代码）；
- ❌ 本批**自身**零迁移 ⇒ 不需要为本批写 downgrade 步骤（0144–0152 是 main 既有）；
- ❌ 不改生产 `.env`（含 SECRET_KEY）、不动 ENABLE_* 开关；
- ❌ 不动 postgres / neo4j / milvus / ollama 容器与数据；
- ❌ 不新增 worker 容器（生产保持无 worker 拓扑）；
- ❌ **不做**研究 BO 臂的数据源收窄（残余路径，**另批**；本批只把墓碑类/悬空向量剔出 BO 臂）；
- ❌ **不做** Milvus 写路径修复（墓碑向量未删 + 2.47× 重复向量）—— Gap A 只在 class 检索窗口内绕开；
- ❌ **不做**钉住语义发布路径的 id 空间根治（快照合成 id ≠ Milvus 主键）—— 该路径**不施加作用域**，
  `scene=pinned_scope_skipped` 日志会显式暴露它的存在；
- ❌ 不复活 id 271（未探针核实）。

---

## 10. 相关 SSOT

- 设计：`Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md`（#157）
- 实施汇总：`Harness/changes/2026-11-19-recall-datasource-filter-anchor/summary.md`（#157）
- Gap B 汇总：`Harness/changes/2026-11-19-recall-anchor-fallback-rescue/summary.md`（#158）
- Gap A 汇总：`Harness/changes/2026-11-19-recall-milvus-local-scope/summary.md`（#159）
- Gap A 设计：`Harness/changes/2026-11-19-recall-milvus-local-scope/design.md`
- 1119 改名 runbook（前端 bundle 锚点同源）：`Harness/changes/2026-11-19-frontend-brand-rename/deploy-prod-1119.md`
- Wiki：`Harness/wiki/nl2sql-engine.md`「召回链前置过滤 + 关键词锚定」节 +「第 3 级·向量检索窗口收敛到本地类 id」
