# 生产发布方案：feat-data-domain-layer（数据域 + 数据层 + 工具页同源化）

- **日期**：2026-10-25
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`7e852b88`（main 之后 7 个提交：后端 ca02b9b5 / 前端 47a31fd9+1cc9addb / 菜单 28d419a8 / docs 其余）
- **迁移**：0132（生产当前）→ 0133 → 0134（后端容器 CMD 自带 `alembic upgrade head`）
- **操作分工**：以下所有生产服务器操作由你手工执行；交付物（镜像包、本文档）由开发机准备

---

## Phase 0 交付物（开发机准备，构建完成后填入）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1025-ddl.tar.gz` | `qa-system-backend:amd64-1025-ddl`（linux/amd64，已核验） | `56ed95895021961d962913bb6969d052ba7e346909868653ebce9830487818ba` | 276M |
| `/tmp/qa-frontend-amd64-1025-ddl.tar.gz` | `qa-system-frontend:amd64-1025-ddl`（linux/amd64，已核验，`VITE_API_BASE_URL=` 空同源相对路径） | `ade1cdf7ba2dc02816500d291d2593ba8bc452dafacfa5654e545d3ee2ff506c` | 26M |

> 生产上传输后先 `shasum -a 256` 比对再 load，防中途损坏。

> 两镜像必须都是 `--platform linux/amd64`（开发机是 arm64 Mac，不指定平台
> 生产 load 后 `exec format error`）。frontend 构建显式空 `VITE_API_BASE_URL`，
> 否则 bundle 烤入 localhost 绝对地址，非 localhost 访问全站 CORS 挂（1024 实踩）。

## Phase 1 生产预检（只读，不改动）

```bash
# ① 当前迁移版本（期望 0132）
docker exec <pg容器名> sh -c 'psql -U "$POSTGRES_USER" -d <业务库名> -tAc \
  "SELECT version_num FROM alembic_version;"'

# ② 0133 守卫预检（Harness/changes/feat-data-domain-layer/summary.md §9.1）
#    任意 count > 0 会阻断迁移，先发我结果再继续
docker exec <pg容器名> sh -c 'psql -U "$POSTGRES_USER" -d <业务库名> -tAc "
SELECT '\''null_header_bos'\'', code, name FROM business_object WHERE header_class_id IS NULL;"'
docker exec <pg容器名> sh -c 'psql -U "$POSTGRES_USER" -d <业务库名> -tAc "
SELECT '\''entity_mapping'\'', count(*) FROM entity_mapping WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''feature_definition'\'', count(*) FROM feature_definition WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''document_entity_relation'\'', count(*) FROM document_entity_relation WHERE entity_type IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL)
UNION ALL SELECT '\''feature_rule'\'', count(*) FROM feature_rule WHERE data_object IN
  (SELECT code FROM business_object WHERE header_class_id IS NULL);"'

# ③ 磁盘余量（镜像包 ~1-2GB + 加载后翻倍）
df -h /var/lib/docker
```

- ② 全 0 → 可直接升级；有引用 → 把输出发我，给补绑/清理 SQL 后再继续
- **不要动 `.env`**（SECRET_KEY 是 Fernet 主密钥，换了 5 数据源密码 + 6 LLM key 永久不可解）

## Phase 2 备份

```bash
# ① DB 逻辑备份（含 0132 全量，回滚兜底）
docker exec <pg容器名> sh -c 'pg_dump -U "$POSTGRES_USER" -d <业务库名>' \
  > ~/backup/qa_metadata_pre_ddl_$(date +%m%d).sql

# ② 现役镜像打备份 tag（镜像名以 docker ps 实际为准，见 Phase 3 探名）
docker tag <现役backend镜像> <同名>:pre-ddl-1025
docker tag <现役frontend镜像> <同名>:pre-ddl-1025
```

## Phase 3 传输 + 加载 + tag（⚠️ 镜像名下划线坑）

compose 的 backend/frontend 服务**没有显式 `image:`**，镜像名 = `<项目目录名>_backend`
派生——历史踩过「compose 引用 `qa_system_frontend` ≠ 构建名 `qa-system-frontend`」。
**先探名再 tag：**

```bash
# 传到生产（内网途径由你定，scp/U盘均可）
# 生产上：
docker load -i qa-backend-amd64-1025-ddl.tar.gz
docker load -i qa-frontend-amd64-1025-ddl.tar.gz

# 探名：compose 实际引用的镜像名（<compose目录> = docker compose 启动时所在目录）
cd <compose目录> && docker compose config | grep image

# 按 ① 输出的名字打 tag（示意，以实际为准）
docker tag qa-system-backend:amd64-1025-ddl  <探名1>
docker tag qa-system-frontend:amd64-1025-ddl <探名2>
```

## Phase 4 迁移（显式跑，不靠容器 CMD——守卫 raise 时看得见、不会 crash-loop）

```bash
cd <compose目录>
docker compose stop backend
docker compose run --rm backend alembic upgrade head
# 预期输出 0133 -> 0134 两行 running；若 RuntimeError「0133 阻断」→ 回 Phase 1② 处置

# 确认
docker exec <pg容器名> sh -c 'psql -U "$POSTGRES_USER" -d <业务库名> -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0134
```

## Phase 5 起容器 + seed 菜单

```bash
cd <compose目录>
docker compose up -d backend frontend
docker compose logs backend --since 3m | grep -E "Application startup|Traceback"   # 期望 startup complete 无 Traceback

# 菜单 seed（item.dataDomains + item.adminTools 两行靠这个进来；upsert 幂等）
docker exec -e PYTHONPATH=/app -w /app <backend容器名> python scripts/seed_menu_config.py
# 期望：seed_menu_config: 55 rows upserted（生产行数可能略有差异，>50 即正常）
```

## Phase 6 验收清单（逐项打勾）

```bash
# ① API 已挂载且鉴权正常（两个都应返回「请先登录」，不是 404）
curl -s http://localhost:<backend端口>/api/v1/data-domains | head -c 100
curl -s http://localhost:<backend端口>/api/v1/business-objects | head -c 100

# ② 前端 bundle 判据：记录这个 hash 发我
curl -s http://localhost:<frontend端口>/ | grep -o '/assets/index-[^"]*\.js'
```

UI（登录后）：
- [ ] 业务配置 → **数据域**：三域种子行（PROCUREMENT/QUALITY/LOGISTICS），CRUD、启停开关
- [ ] 业务对象：新建/编辑必选头表类 + 主数据域；附域不含主域
- [ ] 数据源 → 行「更多」→ **数据层分布**：探测过的源出层分布，未探测的出引导文案
- [ ] 系统配置 → **工具管理**：页面可达；数据层下拉出现 ODS_DICT / ODS_BUSINESS 等 7 项（不再是 3 项）

## Phase 7 回滚（仅在验收失败时）

```bash
cd <compose目录>
docker compose stop backend frontend
# DB 回滚：用 Phase 2① 的 dump 恢复（优先）；alembic downgrade 0132 会删
# data_domain/data_layer_rule 及挂域数据，不推荐
docker compose run --rm -T backend sh -c \
  'psql "$DATABASE_URL" -f -' < ~/backup/qa_metadata_pre_ddl_XXXX.sql   # 若容器内无 psql，改在 pg 容器执行恢复
docker tag <同名>:pre-ddl-1025 <探名>
docker compose up -d backend frontend
```
