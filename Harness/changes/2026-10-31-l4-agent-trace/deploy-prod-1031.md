# 生产发布方案：L4 执行轨迹卡（1031）

- **日期**：2026-10-31
- **目标**：192.168.205.82（CentOS 7，内网隔离，Docker Compose）
- **代码基线**：`feat/l4-agent-trace` @ `f3a30462`（发版源 worktree `/private/tmp/qa-deploy-1031-l4trace`，detached）
- **迁移**：0138 → **0139**（`session_message.agent_trace` JSONB 可空，无回填；若生产仍在 0137，upgrade head 会连带跑 0138）
- **操作分工**：开发机已产出镜像包与本文档；生产服务器命令由你执行（内网无 SSH 免密通道）

## 0. 交付物（开发机，已构建并核验）

| 文件 | 内容 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1031-l4trace.tar.gz` | `qa-system-backend:amd64-1031-l4trace`（linux/amd64） | `f281d2f909b0e81fab402d0e31978d1803c7e9f4c71329b20bff423eb2a38552` | 217M |
| `/tmp/qa-frontend-amd64-1031-l4trace.tar.gz` | `qa-system-frontend:amd64-1031-l4trace`（linux/amd64，`VITE_API_BASE_URL=` 空同源） | `6852afc87befbce755aad44efdcd83ee8f29d57a9617fed610d87e343e75ff48` | 26M |

镜像侧核验（开发机）：

- backend：`0139` 迁移在镜像内；`/app/app/services/l4_trace.py` 存在且 sha256 与发版树一致；`EVENT_AGENT_TRACE` 2 处；镜像内 alembic heads = `['0139']`；`/app/.env` 不存在
- frontend：`http://localhost:5173` 引用 **0**；bundle `index-udpQCxcR.js`；`agentTrace` 4 处、`查看执行轨迹` 1 处

## 1. qa_compose 入口（每个新 shell 先定义）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
```

## 2. Phase 1 预检（只读）

```bash
# ① 容器实际引用的镜像名（tag 前以它为准，别照抄仓库名）
docker ps --format '{{.Names}}\t{{.Image}}' | grep -E 'backend|frontend'

# ② 当前迁移版本（预期 0138；若为 0137 也 OK，upgrade head 连带 0138）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# ③ 磁盘余量（两包 ~250M，load 后镜像层翻倍）
df -h /var/lib/docker
```

**若 ② 不是 0137/0138，先停下发我结果。**

## 3. Phase 2 备份 + 回滚锚点

```bash
mkdir -p /root/backup
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > /root/backup/qa_metadata_pre_1031_l4trace_$(date +%m%d).sql

docker tag qa-backend:offline      qa-backend:pre-1031-l4trace
docker tag qa-system_frontend      qa-system_frontend:pre-1031-l4trace
```

## 4. Phase 3 传输 + 加载 + tag

开发机 → 生产（scp/U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 qa-backend-amd64-1031-l4trace.tar.gz qa-frontend-amd64-1031-l4trace.tar.gz   # 开发机
sha256sum  qa-backend-amd64-1031-l4trace.tar.gz qa-frontend-amd64-1031-l4trace.tar.gz      # 生产
```

生产：

```bash
docker load -i qa-backend-amd64-1031-l4trace.tar.gz
docker load -i qa-frontend-amd64-1031-l4trace.tar.gz

docker tag qa-system-backend:amd64-1031-l4trace   qa-backend:offline
docker tag qa-system-frontend:amd64-1031-l4trace  qa-system_frontend

qa_compose config | grep image     # 确认解析到新 tag
```

## 5. Phase 4 迁移（显式跑，不靠容器 CMD）

```bash
qa_compose stop backend
qa_compose run --rm backend alembic upgrade head
# 预期输出：0139_session_message_agent_trace running
# （若从 0137 起，会先跑 0138_session_message_replay_plan_cards）

docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'   # 期望 0139
```

`0139` 只 `add_column`（可空），失败可直接停在这里：backend 仍按旧镜像回滚启动即可。

## 6. Phase 5 起服务 + 冒烟

```bash
qa_compose up -d backend frontend
docker logs --tail 30 qa-backend | grep -E "Application startup complete|Error|Traceback"
```

只读冒烟：

```bash
# 迁移版本
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
  "SELECT version_num FROM alembic_version;"'

# 后端新代码哨兵（期望 2）
docker exec qa-backend sh -c 'grep -c EVENT_AGENT_TRACE /app/app/services/chat_stream_entry.py || true'

# 前端 bundle（期望 index-udpQCxcR.js）+ 新卡文案（期望 ≥1）
curl -sS http://127.0.0.1:5173/ | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -1
docker exec qa-frontend sh -c 'grep -rc "查看执行轨迹" /usr/share/nginx/html/assets/ | awk -F: "{s+=\$2} END {print s+0}"'

# 健康
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/api/v1/health
```

浏览器验证（浏览器需硬刷新 Cmd/Ctrl+Shift+R）：

1. 登录后提一个需要查数的复杂问题，走 L4；
2. 回答下方应出现可收起的「查看执行轨迹」卡（摘要：N 步 · M 轮 · 耗时），展开后按「第 N 轮」分组列出 list_tables / describe_table / execute_sql 等步骤；
3. 刷新页面重开会话 → 历史回放里同一张卡应与即时展示一致（`agent_trace` 已落库）；
4. 普通 L2 回答不应出现该卡（无工具调用即无卡）。

## 7. 回滚

```bash
qa_compose stop backend frontend
docker tag qa-backend:pre-1031-l4trace      qa-backend:offline
docker tag qa-system_frontend:pre-1031-l4trace qa-system_frontend
qa_compose up -d backend frontend
```

- `session_message.agent_trace` 列可保留（旧代码不 SELECT 它，无副作用）；如需彻底回退：
  `qa_compose run --rm backend alembic downgrade 0138`（会删列，已落库的轨迹随之丢失）。
