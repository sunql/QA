# 生产热修 3：采购 Copilot 轨迹鉴权 + SSE 收尾截断（仅前端）

- **代码版本**：`feat/ontology-evolution` 工作树（基线 `0d9f6a35` = main；**本次改动尚未提交**，镜像即按此工作树构建）
- **本次动**：**只换前端容器**；**零 DDL、零迁移、零后端改动**
- **不动**：postgres / neo4j / milvus / ollama / backend 容器；不改 `/opt/qa-system/docker/.env`
- **性质**：热修，**要求 1122 批次已在生产**（前端镜像基线）

---

## 0. 一句话说清这次改了什么

| # | 问题 | 修复 |
|---|---|---|
| ① | 采购 Copilot「查看执行轨迹」→ 新窗口 `{"success":false,"error":"请先登录"}` | 裸链接（`target="_blank"` 带不上 Bearer）改**页内抽屉**：`getAgentRunSteps(runId)` 走 httpClient 带 token 拉 JSON 渲染 |
| ② | 流式收尾 F12 报 `net::ERR_INCOMPLETE_CHUNKED_ENCODING` | `docker/nginx.conf` `/api/` 段补 `proxy_http_version 1.1;` + `proxy_set_header Connection "";`（HTTP/1.0 上游 = 无长度响应，收尾缺终止块） |
| ③ | 超时文案被网络错误覆盖成 `Failed to fetch` | `handleSend` 的 catch 只覆盖仍在 RUNNING 的轮次，已收到终态事件的保留原文案 |

> 关于「本次流式转发的等待时间已到」：**这是设计行为**（`DEFAULT_MAX_WAIT_SECONDS=120s`），
> 运行本身在后台继续，可点轨迹跟进。本次修的是它后面那条网络错误 + 轨迹打不开。

**验收标准**：
1. `docker exec qa-frontend nginx -T | grep -E "proxy_http_version|Connection"` 有 `1.1` 与空 Connection；
2. 前端 bundle = `index-C3eRPN1s.js`；
3. Copilot 跑一次 >120s 的运行：页面保留「等待时间已到」文案，F12 **无** `ERR_INCOMPLETE_CHUNKED_ENCODING`；
4. 点「查看执行轨迹」→ **页内抽屉**出步骤，不弹新窗口、不报「请先登录」。

---

## 1. 交付物（开发机已产出并自证）

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-frontend-amd64-1123.tar.gz` | `qa-system-frontend:amd64-1123` | linux/amd64 | `a6dbfa94219a11e9e5432b8b6d2701dccd055353c69ffcf7c67dd4ffd519678b` | 26M |

镜像标识：`sha256:675c52f53ebad28484925ab4d81bb2fcff9cf110b23d8c230c8f70ae6791ad16`
前端 bundle：`index-C3eRPN1s.js`

镜像内自证（开发机已跑，输出如下）：

```bash
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1123 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "查看执行轨迹" /usr/share/nginx/html/assets/index-*.js
  grep -c "这次运行没有记录步骤" /usr/share/nginx/html/assets/index-*.js
  grep -c "proxy_http_version 1.1" /etc/nginx/conf.d/default.conf
  grep -c "proxy_set_header Connection" /etc/nginx/conf.d/default.conf'
# 实测：index-C3eRPN1s.js / 2 / 1 / 1 / 1
```

---

## 2. Phase 0 预检（只读）

```bash
# ⓪ compose 入口（从现役容器 label 读出，唯一权威）
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --images      # 回填前端实际引用名（1122 实测：qa-system_frontend）

# ① 现役容器与镜像
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'

# ② 现役前端 bundle（回滚对照）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 健康
curl -s -o /dev/null -w "backend=%{http_code}\n"  http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
```

**停下条件**：backend health 非 200；前端 bundle 记录不下来。

---

## 3. Phase 1 回滚锚点（只有镜像，无需 pg_dump）

```bash
docker tag qa-system_frontend qa-system_frontend:pre-hotfix3
docker images | grep pre-hotfix3
```

> 本批零 DDL / 零数据变更 ⇒ 回滚只需换回镜像，不需要 `alembic downgrade`。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

```bash
# 传完先比对 §1 的 sha256
sha256sum qa-frontend-amd64-1123.tar.gz    # 期望 a6dbfa94...678b

docker load -i qa-frontend-amd64-1123.tar.gz
docker tag qa-system-frontend:amd64-1123 qa-system_frontend
docker images | grep -E 'qa-system_frontend|amd64-1123'
```

---

## 5. Phase 3 切换（只动前端）

```bash
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep frontend

# nginx 配置已生效
docker exec qa-frontend nginx -T 2>/dev/null | grep -E "proxy_http_version|proxy_set_header Connection"
# 期望：proxy_http_version 1.1;  与  proxy_set_header Connection "";
```

> `--no-deps` 保证不碰 backend/postgres/neo4j/milvus；`--no-build` 防止现场构建。

---

## 6. Phase 4 验收探针

```bash
# ① bundle
docker exec qa-frontend sh -c '
  h=/usr/share/nginx/html
  echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
  echo trace_ui=$(grep -c "查看执行轨迹" $h/assets/index-*.js)'
# 期望 bundle=index-C3eRPN1s.js / trace_ui>0

# ② nginx SSE 指令
docker exec qa-frontend nginx -T 2>/dev/null | grep -c "proxy_http_version 1.1"   # 1

# ③ 前端可达
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/

# ④ 后端未受影响
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health   # 200
```

**人工验收（浏览器，强刷 Ctrl/Cmd+Shift+R）**：

1. 采购 Copilot 发一句需要较长时间的问题 → 收尾时页面显示「等待时间已到……」，F12 无红色 `ERR_INCOMPLETE_CHUNKED_ENCODING`；
2. 点「查看执行轨迹」→ 右侧抽屉出现步骤列表；关掉抽屉再开另一轮，内容不串。

## 7. 回滚（30 秒）

```bash
docker tag qa-system_frontend:pre-hotfix3 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'   # 回到旧 bundle
```

---

## 8. 本次没做的事

- ❌ 不动后端（无后端代码改动；新加的 `test_nginx_sse_config.py` 只是守卫测试）；
- ❌ 不提高 120s 流式等待预算（超时即转后台是本产品口径，轨迹可继续跟进）；
- ❌ 不加 SSE 心跳（120s < `proxy_read_timeout 300s`，够不到空闲超时，YAGNI）。

---

## 9. 附：编排 Worker 首次拉起（生产，2026-11-23 实测）

> 本节不属于前端热修，但同一批现场排障产物 —— **不做这一步，Copilot 的 run 永远停在 `QUEUED`**。

### 9.1 现象与判据

Copilot 提问后前端一直「运行中」，`agent_orchestration_run` 长期 `QUEUED`、`iteration_count=0`、
`agent_orchestration_step` 0 行；120s 后页面提示「等待时间已到，运行仍在后台继续」——**那句是假的**，
后台没有任何进程。

```bash
docker ps -a --format '{{.Names}}\t{{.Image}}' | grep -i orchestration || echo "NO_WORKER"
```

Worker 在 compose 里是 `profiles: [agent-orchestration]` 门控，默认 `up -d` 不会起。

### 9.2 compose 服务块（三件套缺一不可）

```yaml
  agent-orchestration-worker:
    image: qa-backend:offline            # 与 backend 同镜像，只换 CMD
    container_name: qa-agent-orchestration-worker
    restart: unless-stopped
    env_file:
      - .env                             # ① 与 backend 同款环境
    security_opt:
      - seccomp=unconfined               # ② CentOS7 3.10 + Docker 19.03：不放开则建不了任何线程
    volumes:
      - /opt/qa-system/docker/certs:/etc/qa-certs:ro   # ③ SSL_CERT_FILE 指向这里
    environment:
      ENABLE_AGENT_ORCHESTRATION: "true"
    command: ["python", "-m", "app.workers.agent_orchestration_worker"]
```

**为什么必须逐项对齐 backend**：backend 是「老容器」，当年显式设过 `seccomp:unconfined` 和证书挂载；
**新服务不会自动继承**。漏任何一项的报错都很不直观：

| 漏了 | 现象 | 关键日志 |
|---|---|---|
| `security_opt` | 进程起不来 | `OpenBLAS … pthread_create failed … Operation not permitted` 或 `RuntimeError: can't start new thread`（`exit 130`，尾巴是误导性的 `KeyboardInterrupt`） |
| `volumes` | 能起、但每个 run 都 `PAUSED/INFRASTRUCTURE_ERROR`、`tokens=0` | `Planner 调用失败，重试：[Errno 2] No such file or directory` |
| `env_file` | 起不来或连不上库 | 配置校验/连接失败 |

### 9.3 启动与验收

```bash
qa_compose config --quiet && echo COMPOSE_OK
qa_compose --profile agent-orchestration up -d --no-build agent-orchestration-worker
docker logs --tail 20 qa-agent-orchestration-worker
# 期望：AgentToolConfigRegistry warmed up / FeatureRuleRegistry warmed up / agent orchestration worker 启动
```

```bash
# 前端提问后
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select id,status,pause_code,iteration_count,tool_call_count,
       prompt_tokens+completion_tokens as tokens from agent_orchestration_run order by id desc limit 3;"'
# 期望：tokens>0；终态 SUCCEEDED 或 PAUSED（PAUSED 是正常终态：多候选/缺参数追问）
```

2026-11-23 实测样例（采购供应商检索 Agent）：

```
run 8  PAUSED  NEEDS_INPUT  iter=1  tools=0  tokens=895
step 0  PLAN  SKIPPED  ASK_USER  「缺少可检索的物料编码或供应商关键词，无法定位数据」
```

`ASK_USER` 不是故障：`supplier_search` 只接受 `q`（供应商名称/编码）或 `materialKey`（物料编码），
问品类词（如「外购标准件」）时反问是设计行为。给具体编码即可继续（同一 run 会被 resume）。

### 9.4 注意事项

- **历史 QUEUED 会被一次性消费**：起 worker 前先看 `agent_orchestration_run` 里积压条数，不需要的先取消
  （`POST /api/v1/agent-runs/{runId}/cancel`，QUEUED 立即终止）；每条都是一次真实 LLM 花费。
- **发版后 worker 不会自动换镜像**（`image:` 是显式引用）：换 backend 镜像时要一并
  `--force-recreate agent-orchestration-worker`，否则 worker 跑的还是旧代码。
- **证书轮换**：`/etc/qa-certs` 是目录级只读挂载，backend/worker 同时生效，换文件后确认两个容器都能读到。
- 回滚：`docker stop qa-agent-orchestration-worker`（不影响 backend/frontend）。

### 9.5 失败排查速查（本次实际踩到）

| 日志 | 根因 | 修法 |
|---|---|---|
| `pthread_create failed … Operation not permitted` / `can't start new thread` | 默认 seccomp 拒绝 `clone3`（内核 3.10） | `security_opt: [seccomp=unconfined]` |
| `[Errno 2] No such file or directory` | `SSL_CERT_FILE` 指向的证书不在容器里 | 挂 `/opt/qa-system/docker/certs:/etc/qa-certs:ro` |
| `400 … you passed cached-model` | 调了**废弃**的 `factory.getOrCreateClient`（内部把 `model_name` 写死成 `cached-model`，全仓只有测试在用） | 业务链路用 `createClient(config)`；该函数建议删除或改签名 |
| `httpx.UnsupportedProtocol: Request URL is missing protocol` | `llm_config.api_endpoint` 有前导空格（实测 id=3 为 `' https://…'`） | 前台模型配置里去掉空格，或 `update llm_config set api_endpoint=trim(api_endpoint)` |
