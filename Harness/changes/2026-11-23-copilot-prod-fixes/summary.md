# 采购 Copilot 生产三修：轨迹鉴权 / 流式收尾截断 / 终态文案

- 日期：2026-11-23　分支：`feat/ontology-evolution`（基线 = main `0d9f6a35`）
- 现场：生产 `ai-server`，采购 Copilot 页（`:5173`，前端 nginx 反代 `/api/` → `qa-backend:8000`）
- 结论：**三处独立缺陷，一处（①）是鉴权用法错、一处（②）是反代配置缺 SSE 必需指令、一处（③）是错误处理降级**

---

## ① 「查看执行轨迹」→ `{"success":false,"error":"请先登录"}`

**现象**：点击后弹新窗口，返回登录错误。

**根因**：按钮是 `<Button href={stepsUrl} target="_blank">`。`GET /api/v1/agent-runs/{runId}/steps`
要求 `Authorization: Bearer`，而新标签页的**裸导航带不上任何自定义头** ⇒ 401。

> 同族坑第二次出现：2026-11-22 文档原文预览也是 `window.open(裸URL)`（见记忆
> `qa-system-source-file-serving-traps`）。判据：**凡 `target="_blank"` / `window.open`
> 指向 `/api/` 都算嫌疑**。

**修法**：改为**页内 Drawer**：`getAgentRunSteps(runId)`（`httpClient`，自动带 Bearer）拉 JSON →
渲染步骤（编号 / kind·status / 工具名 / 计划 / 答案 / 错误 / tokens）。
轨迹是结构化数据，开新窗口展示原始 JSON 本身就是坏 UX，故不走 blob 方案。
顺带删掉 `CopilotTurn.stepsUrl`（已成写死状态）。

`frontend/src/pages/ProcurementCopilotPage.tsx` · i18n 新增
`procurement.copilot.traceTitle/traceEmpty/traceFailed`（zh-CN + en-US 同步）。

## ② 流式收尾 `net::ERR_INCOMPLETE_CHUNKED_ENCODING`

**现象**：一次运行超过 120s，前端收到「本次流式转发的等待时间已到……」（**按设计**：
`DEFAULT_MAX_WAIT_SECONDS = 120`，运行本身在后台继续，可看轨迹），同时 F12 报
`ERR_INCOMPLETE_CHUNKED_ENCODING`。

**根因**：`docker/nginx.conf` 的 `/api/` 段**缺 `proxy_http_version 1.1`**。nginx 默认用
HTTP/1.0 访问上游 —— 那是「无长度、靠关连接定界」的响应语义，收尾时客户端可能拿不到
chunked 终止块。仓库自己的部署文档 `docs/deployment/05-reverse-proxy-https.md` 的推荐块
里本来就有这两行，只有**镜像实际使用的 `docker/nginx.conf`** 漏了。

**修法**（`docker/nginx.conf`，`/api/` 段）：

```nginx
proxy_http_version 1.1;
proxy_set_header Connection "";
```

**防回退**：新增 `backend/app/tests/unit/test_nginx_sse_config.py`（3 例）——按花括号配平
取出 `location /api/` 段，断言 SSE 必需指令齐全、指令数量下限（防段改名后空集假通过）、
`proxy_read_timeout ≥ 240s`。已用旧配置反向验证：旧版恰好缺这两条。

> 不采用「心跳事件」缓解：一次 SSE 的等待预算 120s < `proxy_read_timeout 300s`，
> 空闲间隙永远够不到 nginx 读超时，加心跳是 YAGNI。

## ③ 超时文案被网络错误覆盖

**现象**：即便 ② 修好，收尾时的连接重置仍会让 `fetch` body reader 抛 `TypeError`，
`handleSend` 的 catch 会把已经显示的「等待时间已到」**覆盖成 `Failed to fetch`**。

**修法**：catch 只覆盖**仍在 RUNNING** 的轮次；已收到终态事件（`copilot_done` /
`copilot_error`）的轮次保留原文案。

```ts
prev.map((turn) =>
  turn.id === id && turn.status === "RUNNING"
    ? { ...turn, status: "ERROR", error: message }
    : turn,
)
```

---

## 验证

| 项 | 结果 |
|---|---|
| `ProcurementCopilotPage.test.tsx`（含新增 4 例：无裸链接、抽屉带鉴权拉取、拉取失败提示、终态文案不被覆盖） | **8 passed** |
| `tsc --noEmit` | 通过 |
| `test_nginx_sse_config.py`（mainline 测试库） | **3 passed** |
| 旧配置反向验证（`git show HEAD:docker/nginx.conf`） | 缺 `proxy_http_version 1.1` + `Connection ""` ⇒ 守卫会红 |
| `docker run nginx:alpine nginx -t`（挂载新 conf） | syntax is successful |
| 前端全量 coverage（改动前基线） | 82.89 / 83.52 / 80.11 / 82.89（≥80） |

## 部署影响

- **只需重建前端镜像**：本次改动 = `nginx.conf`（在 frontend 镜像内）+ 前端 bundle；
  后端**代码未变**（只多了个静态守卫测试）。
- 已产出：`/tmp/qa-frontend-amd64-1123.tar.gz`（sha256 `a6dbfa94…678b`，
  镜像 `sha256:675c52f5…`，bundle `index-C3eRPN1s.js`）—— 部署步骤见同目录 `runbook.md`。
- 生效后验收：
  1. Copilot 跑一次 >120s 的运行，F12 不再出现 `ERR_INCOMPLETE_CHUNKED_ENCODING`，
     页面保留「等待时间已到」文案；
  2. 点「查看执行轨迹」→ 页内抽屉出步骤，**不弹新窗口**；
  3. `docker exec qa-frontend nginx -T | grep -A2 proxy_http_version` 有 `1.1`。
- 回滚：换回旧 frontend 镜像即可（无 DDL、无数据变更）。

## 附：编排 Worker（生产首次拉起，同一批现场产物）

Copilot 的 run 由**独立 worker 进程**消费（`python -m app.workers.agent_orchestration_worker`），
compose 里是 `profiles: [agent-orchestration]` 门控。生产此前没有该容器 ⇒ 所有提问停在 `QUEUED`，
页面的「运行仍在后台继续」是假象。

首次拉起踩了三个「新容器未对齐 backend」的坑（详见 `runbook.md` §9）：

| 漏项 | 现象 | 修法 |
|---|---|---|
| `security_opt` | `pthread_create … Operation not permitted` / `can't start new thread`（exit 130） | `seccomp=unconfined` |
| `volumes` | 每个 run `PAUSED/INFRASTRUCTURE_ERROR`、tokens=0、`[Errno 2]` | 挂 `/opt/qa-system/docker/certs:/etc/qa-certs:ro` |
| `env_file` | 起不来/连不上库 | 与 backend 同款 |

实测闭环（2026-11-23）：`run 8 PAUSED/NEEDS_INPUT iter=1 tokens=895`，一步
`PLAN SKIPPED ASK_USER「缺少可检索的物料编码或供应商关键词」` —— 属**正常追问**
（`supplier_search` 只接受 `q` 或 `materialKey`，问品类词必然反问）。

附带发现两处已修（2026-11-23 同日）：

1. **`factory.getOrCreateClient` 埋雷已拆**：`_buildClient` 把 `model_name` 写死成
   `"cached-model"`、provider 写死 OPENAI。改为按配置传真实 `model_name` / `provider`
   （并纳入 LRU cache key），OLLAMA 走 `OllamaClient`，密文为空时回退环境变量 key。
   新增 2 个回归用例（模型名透传 / 改模型名后 cache miss）。
2. **供应商检索 Agent 补绑 `material_search`**：此前只绑 `supplier_search` + `supplier_360`，
   品类词（「外购标准件」）无法解析成物料 ⇒ 只能反问。种子里工具与指令同步更新，
   新增 `test_procurement_agent_profile_seeds.py`（4 例：工具必须已播种 / 数量下限 /
   供应商 Agent 必含 material_search / 指令须写「先定位物料」）。

> ⚠️ 生产数据面：seed 只**增量补工具**、不改已有 profile 的指令。
> 生产镜像不含新种子 ⇒ 需在「编排配置」页给该 Agent 勾选 `material_search` 并追加指令，
> 或 `docker cp` 新 seed 进容器后执行（见 §9.4）。

## 关联

- `Harness/changes/2026-11-22-prod-deploy/runbook.md` §5.5（同日补的 `.env` 开关顺序铁律）
- 记忆：`qa-system-source-file-serving-traps`（裸 `window.open` 第二次犯）
