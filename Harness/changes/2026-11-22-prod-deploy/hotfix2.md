# 生产热修 2：文档上传元数据 + 批量上传 + 密级可见性（2026-11-22 晚）

- **代码版本**：`feat/ontology-evolution @ c30a5cec`（部署后 #177 合入 main 树与此一致）
- **本次动**：**前端 + 后端两个容器**；**零 DDL、零迁移**（库保持 **0169**）
- **不动**：postgres / neo4j / milvus / ollama；不改 `.env`
- **性质**：热修（今天 1122 批次之后发现的上传/可见性缺陷），**必须先有 1122 批次已在生产**

---

## 0. 一句话说清这次改了什么

| # | 问题 | 修复 |
|---|---|---|
| ① | 上传选的 文档类型/安全级别/Owner/版本/生效日期 **不生效**（恒 OTHER / L1 / 空） | 前端把元数据从 multipart 表单改到 **Query 参数**（后端只读 Query） |
| ② | 上传弹窗只有单文件、且**没有生效日期**（新建有、编辑有） | 上传弹窗改**多选批量** + 共用元数据 + 逐条结果 + 失败重试 + **生效日期** |
| ③ | admin 上传 **L2** 文档后列表看不到 | `buildClearanceFilter`：**admin → 全密级 L0–L3** |
| ④ | 新上传文档排在列表最后一页 | `listDocuments` 改 **`id DESC`**（最新在前） |
| ⑤ | **删除文档后向量还在**（RAG 仍能检索到已删文档） | `deleteDocument` 调 `deleteDocumentChunks`（Milvus 故障不阻断删除，记 ERROR/审计） |
| ⑥ | 历史删除留下的**孤儿向量** | 新增 `scripts/purge_orphan_document_vectors`（默认只报告，`--apply` 真删） |

**验收标准**：
1. 上传选 `SOP / L2 / v1.1 / owner / 生效日期` ⇒ 列表原样显示；
2. 上传弹窗可多选、逐条结果、可重试失败项；
3. admin 列表能看 L0–L3；新上传的排在最前；
4. `alembic_version` 仍为 **0169**（本批无迁移）；
5. 删除文档后：`python -m scripts.purge_orphan_document_vectors` 不应再把它列为孤儿（新删的立即清）；历史孤儿用同一脚本 `--apply` 清理。

---

## 1. 交付物（开发机已产出）

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1122-hotfix2.tar.gz` | `qa-system-backend:amd64-1122-hotfix2` | linux/amd64 | `85a2f084a2101fd6b1db3a6aa3a1e2e09407392dfcc07d4d46538a082aa34ad6` | 490M |
| `/tmp/qa-frontend-amd64-1122-hotfix2.tar.gz` | `qa-system-frontend:amd64-1122-hotfix2` | linux/amd64 | `35ce2646797cf0501fb36553ed4eb636c27ead2918c552188b7661d9897d103e` | 26M |

镜像标识：backend `sha256:0edd8700edf026ed4942863001694031edd1e132abf2c5fe6f755fccb231c7cc`｜frontend `sha256:0f4f3c7086f337f79f0aa90e712377881cb729728242ad65547bb6dfb564cbf8`
前端 bundle：`index-uUeXdHJq.js`（css `index-WW9dT-wP.css`）

镜像内自证：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1122-hotfix2 -c '
  grep -c "DocumentCatalog.id.desc" /app/app/services/document_service.py
  grep -c "_hasRole(user, \"admin\")" /app/app/services/document_clearance.py
  grep -c deleteDocumentChunks /app/app/services/document_service.py
  ls /app/scripts/purge_orphan_document_vectors.py'
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1122-hotfix2 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "重试失败项" /usr/share/nginx/html/assets/index-*.js'
# 期望：1 / 1；bundle = index-uUeXdHJq.js；batch = 1
```

---

## 2. Phase 0 预检（只读）

```bash
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK

docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'          # 预期 0169
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health
```

**停下条件**：迁移不是 0169（说明 1122 批次未完整落地）；health 非 200。

---

## 3. Phase 1 回滚锚点（镜像 tag；**无需 pg_dump**）

```bash
# 现役引用名（以你的生产为准；1122 批次后通常是）
docker tag qa-backend:offline        qa-backend:pre-hotfix2
docker tag qa-system_frontend        qa-system_frontend:pre-hotfix2
docker images | grep pre-hotfix2
```

> 本批**零 DDL**，不需要数据库备份；回滚只需换回镜像（不用 `alembic downgrade`）。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

传完先比对 §1 的 sha256。

```bash
docker load -i qa-backend-amd64-1122-hotfix2.tar.gz
docker load -i qa-frontend-amd64-1122-hotfix2.tar.gz
docker tag qa-system-backend:amd64-1122-hotfix2  qa-backend:offline
docker tag qa-system-frontend:amd64-1122-hotfix2 qa-system_frontend
docker images | grep -E 'qa-backend:offline|qa-system_frontend'
```

---

## 5. Phase 3 切换（先后端，再前端）

```bash
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 | grep -E "Running upgrade|Application startup complete|ERROR" | head -10
# 期望：无 Running upgrade（库已 0169）；有 Application startup complete.

qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'
```

---

## 6. Phase 4 验收探针

```bash
# 后端：版本 + 两处代码 + 健康
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                     # 0169
docker exec qa-backend sh -c 'grep -c "DocumentCatalog.id.desc" /app/app/services/document_service.py'        # 1
docker exec qa-backend sh -c 'grep -c "_hasRole(user, \"admin\")" /app/app/services/document_clearance.py'    # 1
docker exec qa-backend sh -c 'grep -c deleteDocumentChunks /app/app/services/document_service.py'             # ≥1
docker exec qa-backend ls /app/scripts/purge_orphan_document_vectors.py                                      # 存在
# 历史孤儿向量清理（先报告，确认后 --apply；建议在维护窗口执行）
qa_compose exec backend python -m scripts.purge_orphan_document_vectors
qa_compose exec backend python -m scripts.purge_orphan_document_vectors --apply
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health

# 前端：bundle + 批量上传文案
docker exec qa-frontend sh -c '
  h=/usr/share/nginx/html
  echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
  echo batch=$(grep -c "重试失败项" $h/assets/index-*.js)'
# 期望：bundle = index-uUeXdHJq.js；batch = 1
```

浏览器硬刷新后：
1. 文档中心 → 上传文档 → **多选** 多个文件 → 选 `SOP / L2 / v1.1 / Owner / 生效日期` → 逐条成功；
2. 列表第一页即可看到新文档，且类型/密级/Owner/生效日期与所选一致；
3. admin 能看到 L2 文档。

---

## 7. 回滚

```bash
docker tag qa-backend:pre-hotfix2        qa-backend:offline
docker tag qa-system_frontend:pre-hotfix2 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend frontend
```

> 无 DDL、无数据迁移 ⇒ 回滚只换镜像；库保持 0169 不影响旧镜像
> （旧镜像 head=0169 与库一致）。

---

## 8. 本批没做

- ❌ 不改数据库（无迁移）；
- ❌ 不改 `.env` 开关；
- ❌ 不新增 worker；
- ❌ 历史已上传但元数据错误的文档**不会自动修正**，需编辑或重传。
