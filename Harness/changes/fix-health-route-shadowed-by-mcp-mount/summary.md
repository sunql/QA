# 变更：fix-health-route-shadowed-by-mcp-mount

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 6 MCP × 应用装配（`app/main.py`）
- **状态**：done
- **关联变更**：[feat-mcp-server](../feat-mcp-server/summary.md)、[fix-nginx-upstream-stale-ip](../fix-nginx-upstream-stale-ip/summary.md)
- **迁移版本**：无
- **SSOT 出处**：真机探活（2026-09-26 重建后端镜像后按 `数据备份策略.md` §4 做健康检查，全 404）

---

## 1. 需求

`GET /api/v1/health` **返回 404**，后端直连与经 nginx 两侧一致。

该端点是运维手册（`Harness/rules/数据备份策略.md` §5.1 步骤 4「启动 + 健康检查」）与
日常探活使用的地址，404 意味着**恢复演练的最后一步是坏的**——且它不会报错，只会打印
`Not Found`，容易被当成「服务没起来」而去做多余的排查。

真机证据（重建镜像后）：

```
$ curl -s -D- -o /dev/null http://localhost:8000/api/v1/health
HTTP/1.1 404 Not Found
server: uvicorn
content-type: text/plain; charset=utf-8     # Starlette 默认 404，不是 nginx 的、也不是 DomainError 的 JSON 404
```

验收标准：真实 app（`createApp()`）能对外提供 `/api/v1/health` → 200 `{"status":"ok"}`；
并且**这类「路由被静态吞掉」的缺陷今后必须被测试挡住**，而不是靠真机探活发现。

## 2. 设计评审

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 只挪路由（最小改动） | 把 `@app.get("/api/v1/health")` 从 MCP 挂载之后移到之前 | ❌ 消症不消因：下一个人再往 `createApp()` 末尾加一条路由，同样被吞。而且**现有 180 个集成测试对此完全无感**，说明缺的是「守卫」不是「那一行」 |
| B 挪路由 + 对 `_testapp` 补一条 health 断言（**曾经的思路**） | 在既有的 `_testapp` 上加测试 | ❌ 无效：`_testapp.py` 是**平行手工维护**的另一套 wiring，它**不挂 MCP**、health 也独立声明在最前面 —— 在它上面断言恒为绿，永远看不见这个缺陷 |
| C 挪路由 + 对**真实 app** 建路由表不变量守卫（**选定**） | 新增 `tests/integration/test_app_wiring.py`：①catch-all Mount 之后不得存在任何路由；②真实 app 上 `GET /api/v1/health` → 200 | ✅ ①把「MCP mount 必须是最后一条」钉成不变量，任何未来路由加错位置立刻红；②补上「跑真实 app」这一此前完全缺失的视角。代价：多一个测试文件（≈90 行） |

选 C 的理由：本缺陷的根因是**测试基础设施的结构性盲区**——集成测试统一走
`_testapp.buildTestApp`，它是 `main.py` 的手工副本，因此 `main.py` 里的装配缺陷
对全部测试不可见。只挪一行修不好盲区，下次换个端点还会重演（同类教训：feat-in-app-message
「4 根线缺一，测试全绿而端点 404」）。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无对外契约变更——本次是**把已声明的契约恢复为可达**：

| 端点 | 修复前 | 修复后 |
|---|---|---|
| `GET /api/v1/health` | 404（被 MCP catch-all Mount 遮蔽） | 200 `{"status":"ok","version":"0.1.0","appEnv":"..."}` |

副作用（已知、可接受）：`POST /api/v1/health` 仍是 404 而非 405 —— 方法不匹配的请求在
遍历完所有路由后落到末尾的 catch-all Mount，由 MCP 子 app 回 404。这是 catch-all
设计的固有结果，非本次引入；GET 已可达，不另做处理。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/main.py` | `@app.get("/api/v1/health")` 从 MCP 挂载块**之后**移到**之前**（紧跟最后一个 `include_router`）；原位补注释写明「必须注册在 MCP 挂载之前」及原因，并指向守卫文件 |
| `app/tests/integration/test_app_wiring.py`（新增） | ①真实 app 路由表不变量：catch-all Mount 之后不得有任何路由；②真实 app 上 health → 200 |

根因（Starlette 匹配语义）：`Mount("", app=_mcpApp)` 的 `path=""` 是**全能前缀匹配**，
而 Starlette 按**注册顺序**逐条匹配，命中即停。因此挂载点之后注册的路由永远不会被匹配到
——请求全部落进 MCP 子 app，由它回一个纯文本 404。`app.router.routes` 实测：

```
route count: 51 | catch-all mount at 50 | last index 50   ← 修复后：mount 已是最后一条
```

## 6. 测试

用例先行（RED → GREEN）：

```
RED（修复前）：
  test_no_route_registered_after_catch_all_mount
    E  catch-all Mount（第 49 条）之后仍有 1 条路由，它们会被永久遮蔽（请求一律落到 MCP 子 app）：['/api/v1/health']
  test_health_endpoint_reachable
    E  /api/v1/health 在真实 app 上不可达（404）—— 响应体：Not Found

GREEN（修复后）：
  app/tests/integration/test_app_wiring.py                      2 passed
  集成回归切片（app_wiring / auth_endpoints / in_app_message
      / rate_limit_chat_route / menu_config_api / chat_api）
                                                               43 passed
  单元回归（unit/test_main.py —— 唯一 import app.main 的既有测试）  2 passed
```

隔离性证据（说明回归面为何是上面这些）：`grep -rn "app\.main" app/tests/` 仅命中
`unit/test_main.py`（只测 `shutdownCleanup`，不建 app、不断言路由）与新增守卫本身；
其余 179 个集成文件全部走 `_testapp`。**这既是回归面小的原因，也正是缺陷能逃逸的原因**
——已在本文 §2/§7 记录。

## 7. 安全审查

`code-reviewer` 已跑（触碰应用装配与 ASGI 挂载顺序），结论 **APPROVE**：
**0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW**。

| 级别 | 项 | 处置 |
|---|---|---|
| LOW | 守卫文件 docstring 写「`_testapp` 里 health 排在最前面」——**事实错误**：`_testapp` 里 health 也在约 40 个 `include_router` 之后；真正让它不被遮蔽的原因是 `_testapp` **根本不挂 MCP**（无任何 `Mount`）。原表述会误导后续维护者 | **当场改**为「`_testapp` 不挂 MCP、health 无 catch-all 遮蔽，故恒可达」 |
| LOW | 注释「`client` fixture 只为副作用（真实 PG + 全局会话工厂替换）」暗示请求经过该 client 的会话链路，实际请求走自建的 `ASGITransport` | **当场改**：注释写明「仅用于让本测试跑在同一真实 PG 环境下」，并补一句「若 health 日后引入 DB 依赖，本测试需改为复用该 client」 |

reviewer 的核验方式（逐项查证 + 反证实验，非抽样）：

- 打印真实 app 的 `router.routes`：51 条，第 49 条为 health、第 50 条（末条）为 `Mount ''`，
  mount 之后路由数为 0；全文件仅一处 `routes.append(Mount(...))`，无同类遮蔽。
- **反证实验**：构造「Mount 先于路由注册」的最小复现，`GET /api/v1/health` 确实返回 404
  ⇒ 回退生产改动两条断言必红（非假阳性）。
- 鉴权面：`main.py:522` 与 `_testapp.py:210` 两处 health 声明签名一致、均**无 `Depends`**，
  本是公开探活端点；返回体字段未变；`appEnv` 属既有行为（MCP 挂载前一直可达并返回该字段），
  非本次新增暴露，且非密钥。
- 规范一致性：测试落在 integration（受 autouse 真实 PG 链路约束），无 sqlite、无直连 service；
  health 只读无写入，无失败构造/清理需求；已用真实 PG（qa_metadata_test）跑通。

人工核验要点：

- **无鉴权面变化**：`/api/v1/health` 原本就是无鉴权公开端点（`_testapp` 亦如此），本次只改
  它在路由表中的位置，未改其依赖注入或返回内容；MCP 挂载的鉴权（stub auth）不受影响。
- **无信息泄露增量**：返回体与修复前声明的 `HealthResponse` 逐字段一致（`status`/`version`/`appEnv`）。
- **未引入新的遮蔽**：修复后 catch-all Mount 是路由表最后一条（实测 index 50 = last），
  既有 50 条路由全部排在它之前，顺序未被改动（只做插入/删除，未重排）。

预存 lint（**非本次引入**，未顺手改以免污染本批 diff）：`ruff check app/main.py` 报 6 处
`I001`/`SIM117`，位于 `:231`/`:246`/`:259`/`:368`/`:535`/`:546`（函数内 import 排序、
`_mergedLifespan` 的嵌套 `with`）。本次新增行零 lint 报告。

## 8. 部署验证

已部署（用户要求「重新打包并启动」）：

```bash
cd docker && docker compose build backend && docker compose up -d backend
```

真机验证：

| 检查 | 结果 |
|---|---|
| `curl -s localhost:8000/api/v1/health`（后端直连） | **200** `{"status":"ok","version":"0.1.0","appEnv":"development"}` |
| `curl -s localhost:5173/api/v1/health`（经 nginx） | 200 |
| 端点抽样（真实 app 内进程探测） | `auth/password-policy` 200、`menu-config` 200、`ontology/health/joins` 200、`chat/models` 404（该路径本就不存在，非遮蔽）、`/mcp` 400（子 app 活着） |
| 容器代码一致性 | `chat_service.py` / `main.py` repo md5 == 容器 md5 |

> 无前端改动 ⇒ 不重建前端镜像。

**修复前后同一个探活的对照**是本次最有价值的一条证据：同一句 `curl` 从
`404 Not Found` 变为 `200`，且 `route count 51` 未变（只挪位置、无增删路由）。

## 9. 关联

- 规则：`Harness/rules/数据备份策略.md` §5.1 步骤 4 —— 该处 `curl /api/v1/health`
  从本次起恢复有效（此前自 2026-09-25 起一直 404）
- 关联变更：`../feat-mcp-server/summary.md` §3.1（已补「挂载顺序不变量」条目）、
  `../fix-nginx-upstream-stale-ip/summary.md`（同属「探活/反代路径」一类）
- 代码守卫：`backend/app/tests/integration/test_app_wiring.py`（唯一跑真实 app 的测试）
- Memory：`qa-system-testapp-wiring-blindspot.md`（新增）+ `qa-system-stale-container-deploy.md`（同属「部署后必真机验」）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C 三案，含「曾考虑的 B 方案为何无效」）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：已跑 code-reviewer（APPROVE，0 C/H/M + 2 LOW，两条均已当场修）+ 反证实验记录 + 预存 lint 如实标注
- [x] 第 8 段给出部署命令 + 真机验证结果（已执行，结果如实）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki / 规则文档：`数据备份策略.md` §5.1 的健康检查恢复有效已记录
- [x] 至少 1 条 MEMORY 索引已添加（`qa-system-testapp-wiring-blindspot.md`）
