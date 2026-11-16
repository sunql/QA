# 生产部署：知识关联写入 422（裸 fetch 缺 Content-Type）

- **代码版本**：生产现役 = `main @ 10c79bb3`（1115 发版）；本批 = `504253cd`（分支 `fix/wiki-links-content-type`）
- **合入记录**：PR #138 squash ⇒ `main @ e8f49588`（2026-11-16T07:04:38Z）。
  本批前端内容与 main 一致 ⇒ **生产镜像不必重发**，等下次常规发版自然对齐
- **变更性质**：**纯前端**、**零 DDL**、**零业务数据写**、**零新环境变量**、**后端零改动**
- **本次只换前端容器**：backend / postgres / neo4j / milvus **一律不动**
- **生产命令由宿主机执行**：开发机**无法直连生产**（内网隔离、无 SSH 免密通道），
  本文件是交付给生产宿主机执行的 runbook

---

## 0. 一句话说清这次改了什么

`POST /api/v1/admin/wiki-links` 一直 422，原因是前端这个模块用**裸 fetch** 发 JSON
却**没声明 `Content-Type`** ⇒ 浏览器实际发 `text/plain;charset=UTF-8` ⇒
FastAPI **跳过 body 解析** ⇒ 请求体模型拿不到字段 ⇒ 422。
本批修前端请求头，并顺手堵上「后端 4xx/5xx 被当成成功」的静默假成功通道。

**验收标准**：知识关联页新建一条链接 ⇒ HTTP **201**（不再 422）且 **DB 里真的有这一行**。

---

## 1. 交付物（开发机已产出）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-frontend-amd64-1116-wikict.tar.gz` | `qa-system-frontend:amd64-1116-wikict`（linux/amd64，`VITE_API_BASE_URL=` 空 ⇒ 同源相对路径） | `fc04d6f8f89ea666d98b65687a92016f3068fe4b96f75e0a9f561a8ba427f4e5` | 26M |

> **只需要这一个包**。后端镜像 1115 那次已经上过生产，本次**后端不动**。

**镜像侧核验（开发机已实测通过）**：

| 项 | 新镜像 1116 | 旧镜像 1115（对照） |
|---|---|---|
| 架构 / 体积 | `linux/amd64` / 98.5MB（tar.gz 后 26M） | 同 |
| 前端 bundle | **`index-rBu8Smfz.js`** | `index-LixAJrlt.js` |
| wiki-links 模块内 `Content-Type` 出现次数 | **1** | **0** |
| 模块内 `HTTP ${...status}` 抛错（新的非 2xx 兜底） | **1** | **0** |
| 全包 `"Content-Type":"application/json"` 计数 | **9** | 8 |
| `http://localhost:5173` 硬编码引用 | **0** | 0 |
| `nginx.conf` 的 `resolver 127.0.0.11` / `proxy_pass http://$backendUpstream` | 1 / 1 | 1 / 1 |

> ⚠️ 之前试过的探针（`grep -o ".\{0,120\}/api/v1/admin/wiki-links.\{0,200\}"`）**对新旧镜像都返回 0**
> ⇒ 会对已部署的新镜像**误判成「没部署」**。原因是压缩后路径被提取成了变量
> （`const iR="/api/v1/admin/wiki-links"`），字面量与 fetch 调用**不再相邻**。
> 本文件 §6.1 用的是**已双向验证**的探针（取模块变量定义之后的 1600 字节再找 Content-Type）：
> 对新镜像必然出 `CT_in_mod=1`，对旧镜像必然出 `CT_in_mod=0`。

---

## 2. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口 —— **不要靠 `ls` 猜**，从现役容器的 label 直接读出实际文件（唯一权威）
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 2026-11-15 生产实测：指向**单独一个自包含文件**
#   /opt/qa-system/docker/docker-compose.centos7.yml
# 目录里另外那两个 docker-compose.yml / docker-compose.centos.yml 是仓库同步过来的副本，
# 不是生产实际使用的入口（前者缺 ollama / `:z`，后者的 `!override` 需要 v2 插件，v1 解析即炸）。
# ⇒ 生产全程只用 label 里读出的那一个文件。多条证据不符时以 label 为准。

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services    # 确认服务名（backend / frontend / …）
qa_compose config --images      # 确认镜像名（前端应为 qa-system_frontend）

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役容器与镜像（回滚锚点以这里的实际输出为准，别照抄）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'

# ② 现役前端 bundle（记录旧 hash，便于回滚后对照）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 后端健康（本次不动后端，但它必须活着）
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/

# ④ 磁盘余量（一个包 26M）
df -h /var/lib/docker
```

**停下条件**：③ 的 `frontend_health` 不是 200，或 `backend_health` 不是 200 ⇒ 停下发我
（说明 502 那条线还没解决，先别做本批）。

---

## 3. Phase 2 回滚锚点（镜像 tag，不碰数据库）

```bash
docker tag qa-system_frontend qa-system_frontend:pre-1116
docker images | grep qa-system_frontend
```

> 本批**零 DDL、零业务数据写** ⇒ **不需要 pg_dump**，也不需要动任何表。

---

## 4. Phase 3 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 /tmp/qa-frontend-amd64-1116-wikict.tar.gz              # 开发机
sha256sum  /tmp/qa-frontend-amd64-1116-wikict.tar.gz                 # 生产
# 两者都必须是 fc04d6f8f89ea666d98b65687a92016f3068fe4b96f75e0a9f561a8ba427f4e5
```

生产：

```bash
docker load -i /tmp/qa-frontend-amd64-1116-wikict.tar.gz
docker tag qa-system-frontend:amd64-1116-wikict qa-system_frontend

qa_compose config | grep image      # 确认解析到 qa-system_frontend
```

---

## 5. Phase 4 切换（只动前端）

```bash
qa_compose up -d --no-build --no-deps --force-recreate frontend
```

> ⚠️ **只切 frontend**，理由：
> - 后端代码**零改动** ⇒ 没有重启的必要；
> - backend 的 CMD 是 `alembic upgrade head && uvicorn ...` ⇒ 重启会白跑一次迁移 + seed，
>   生产上是无谓的风险；
> - `--no-build` 防止 compose 现场 build（会拉到错误架构的基础镜像）；
> - `--no-deps` 防止顺带重启 postgres / neo4j / milvus；
> - `--force-recreate` 保证容器真的换成新镜像（仅靠 `up -d` 在镜像 tag 变了时**可能不重建**）。

```bash
# 等 3~10 秒让 nginx 起来
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep frontend
docker logs qa-frontend --since 2m 2>&1 | tail -5
```

---

## 6. Phase 5 验收探针

### 6.1 代码探针（bundle 内自证，必须全部符合）

```bash
docker exec qa-frontend sh -c '
f=$(ls /usr/share/nginx/html/assets/index-*.js | head -1)
echo bundle=$(basename $f)
awk "{ i=index(\$0,\"admin/wiki-links\"); if(i>0){print substr(\$0,i,1600); exit} }" $f > /tmp/_mod.txt
echo CT_in_mod=$(grep -c "Content-Type" /tmp/_mod.txt)
echo httpThrow=$(grep -o "HTTP \${[a-zA-Z]*\.status}" /tmp/_mod.txt | wc -l)
echo CT_total=$(grep -o "\"Content-Type\":\"application/json\"" $f | wc -l)
echo localhost5173=$(grep -o "localhost:5173" $f | wc -l)
'
```

**期望值**：

| 键 | 期望 | 含义 |
|---|---|---|
| `bundle` | `index-rBu8Smfz.js` | 新前端已生效 |
| `CT_in_mod` | **1** | wiki-links 模块里**有** Content-Type（修复在） |
| `httpThrow` | **1** | 模块里有非 2xx 的抛错（错误不再被吞成成功） |
| `CT_total` | **9** | 全包 JSON content-type 计数 |
| `localhost5173` | **0** | 没烤进硬编码地址 |

> ⚠️ `CT_in_mod=0` ⇒ **部署没生效**（旧 bundle）。先看 `docker ps` 里前端容器的 Image 名，
> 再核对 `docker images | grep qa-system_frontend` 是否指向新 tag。

### 6.2 反代与后端链路

```bash
# nginx 配置完整性（反代还在）
docker exec qa-frontend sh -c '
grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf
grep -c "proxy_pass http://\$backendUpstream" /etc/nginx/conf.d/default.conf'
# 期望：1  1

# 经前端反代打后端（与浏览器同路径）
curl -s -o /dev/null -w "api_via_frontend=%{http_code}\n" http://localhost:5173/api/v1/health
curl -s -o /dev/null -w "frontend_root=%{http_code}\n" http://localhost:5173/
curl -s -o /dev/null -w "backend_direct=%{http_code}\n" http://localhost:8000/api/v1/health
# 期望：200 / 200 / 200
```

### 6.3 浏览器端到端（关键验收）

1. **硬刷新**（`Ctrl+Shift+R`）——否则会拿到旧的 `index-LixAJrlt.js`；
2. 进「管理 → 知识关联」页；
3. 新建一条链接（选一个本体类）⇒ **Network 面板**里 `POST /api/v1/admin/wiki-links` 应为 **201**；
4. 控制台**不应再有 422**；
5. 页面提示「添加成功」，且**列表里能立刻看到这一行**（旧版本这里是「假成功」：提示成功、库里没有）。

### 6.4 数据落库核对（只读 SELECT）

```bash
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT id, page_id, ontology_type, ontology_id, weight, created_time, revoked_time \
   FROM wiki_ontology_link ORDER BY id DESC LIMIT 5;"'
```

**期望**：能看到刚才新建的那一行（`revoked_time` 为空）。
若浏览器说成功但这里查不到 ⇒ 说明仍是旧前端没换掉，回查 §6.1。

---

## 7. Phase 6 回滚（任一探针不符即执行）

```bash
docker tag qa-system_frontend:pre-1116 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'
# 期望回到 index-LixAJrlt.js
```

回滚**不需要**动数据库：本批零 DDL、零业务写。
回滚后浏览器需**再次硬刷新**才能拿回旧 bundle。

> 注意：回滚只是把 422 恢复回来（写入继续失败），**不会**产生脏数据
> ——因为旧版本根本没写进过库（body 被跳过 ⇒ 422 ⇒ 没落库）。

---

## 8. 上线后行为变化（对用户可见）

| 位置 | 变化 |
|---|---|
| 知识关联 → 新建链接 | **从恒 422 变为成功**；失败时不再「假成功」，会抛出真实 HTTP 状态 |
| 知识关联 → 编辑权重（PATCH） | API 层已修好；⚠️ 该入口目前**页面上还没有**（`updateWikiLink` 无 UI 调用方），本批不补 |
| 其它页面 | **无变化**（本批只动 `adminWikiLinks.ts` 一个前端模块 + 两个测试文件） |

**不影响**：查询、撤销（DELETE 无 body）本就不受影响；后端、数据库、其它前端页面均未改动。
