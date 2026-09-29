# 变更：fix-frontend-sse-auth-header

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 前端接入层（Chat / 文档问答 / Wiki 问答）
- **状态**：done
- **关联变更**：[feat-user-auth](../feat-user-auth/summary.md)、[feat-wiki-chat](../feat-wiki-chat/summary.md)
- **迁移版本**：无
- **MEMORY**：[qa-system-frontend-sse-auth-header.md](../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-frontend-sse-auth-header.md)

---

## 1. 需求

三条 SSE 流式链路（`POST /chat/stream`、`POST /documents/qa`、`POST /wiki/chat`）都走裸 `fetch`（axios 不适合流式，因此绕过了 `httpClient` 的 axios 拦截器），各自的鉴权头靠手写：

- `chat.ts` / `document.ts`：从 `useAuthStore.getState().token` 取 token，拼 `Authorization: Bearer`（`feat-user-auth` 时期补的），**没有** `X-Tenant-Id`；
- `wikiChat.ts`：完全没有任何头 —— 线上 `POST /wiki/chat` 直接 403。

验收标准：三处 SSE 与 `httpClient` 拦截器走**同一个**鉴权头 SSOT（`authHeaders()`），一次注入 `Authorization` + `X-Tenant-Id`；未登录时不发 `Authorization`；三处用例能真实断言请求头被注入（尤其能抓住 wikiChat 的「完全没注入」回归）。

## 2. 设计评审

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 保持各处手写头 | 逐文件从 store 取 token 拼头 | ❌ 三份重复实现，已是既成事实的漂移源（wikiChat 漏注入就是这么漏的）；新增头（如 `X-Tenant-Id`）要改三处，永远修不齐 |
| B 统一调用 `authHeaders()`（**选定**） | 复用已存在的 SSOT `src/api/authHeaders.ts`（`httpClient` 拦截器用的同一个），三处裸 fetch 传 `authHeaders({ "Content-Type": "application/json" })` | ✅ 头集合单点定义；与拦截器行为天然一致；改动小、可回归（三处各有一个用例） |
| C 让 axios 支持流式 | 换 `fetch` 适配器 / `axios` onDownloadProgress | ❌ 需要自造 SSE 解析，等于重写三条链路；收益只是「不用裸 fetch」，与本次缺陷无关 |

**扩大范围的部分（有意为之）**：`wikiChat.ts` 从「无头」直接改为 SSOT 注入，而不是只补 `Authorization` —— 否则它与另外两处仍不一致，下一轮又会漂移。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无后端契约变更。前端请求头收敛为一处定义：

| 场景 | 请求头 |
|---|---|
| 已登录 | `Authorization: Bearer <token>` + `X-Tenant-Id: <tenantId>` + `Content-Type: application/json` |
| 未登录 | 只发 `X-Tenant-Id` + `Content-Type`（不发 `Authorization`） |

与 `httpClient` 拦截器（`frontend/src/api/client.ts`）及 `feat-user-auth` 的既有语义一致。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `frontend/src/api/chat.ts` | `sendMessageStream` 改 `headers: authHeaders({ "Content-Type": "application/json" })`；删手动 token 拼装与 `useAuthStore` 导入 |
| `frontend/src/api/document.ts` | `searchDocumentsQa` 同上 |
| `frontend/src/api/wikiChat.ts` | `sendWikiChat` 从「无头」改为 SSOT 注入（本次 403 的根因） |
| `frontend/src/api/authHeaders.ts` | **未改**（已存在的 SSOT，被三处复用） |

## 6. 测试

| 文件 | 用例 |
|---|---|
| `frontend/src/tests/chatApi.test.ts` | 断言 `fetch` 第二参数的头含 `Authorization: Bearer test-jwt` + `X-Tenant-Id` |
| `frontend/src/tests/documentApi.test.ts` | 同上（并补 `afterEach` 清理 stub） |
| `frontend/src/tests/wikiChatApi.test.ts` | 正向断言两个头 + **负向用例**：未登录（token 为 null）时不注入 `Authorization` |

用例用真实 `useAuthStore.setState(...)` 塞 token、`vi.stubGlobal("fetch")` 捕获 init，验证的是真实代码路径而非 mock 行为。

实测：`npx vitest run src/tests/chatApi.test.ts src/tests/documentApi.test.ts src/tests/wikiChatApi.test.ts` → **3 files / 36 tests passed**（2026-09-26）。

## 7. 安全审查

`security-reviewer` 已跑（触碰鉴权头注入路径），结论：**0 CRITICAL / 0 HIGH**。

| 级别 | 项 | 处置 |
|---|---|---|
| LOW | `authHeaders()` 的 `Object.assign(headers, extra)` 允许调用方用 `extra` 覆盖 `Authorization` / `X-Tenant-Id`（当前三处只传 `Content-Type`，无实际风险） | 记录，后续可改为「extra 不可覆盖安全头」 |
| LOW | `chatApi` / `documentApi` 未加「未登录不注入 Authorization」负向用例（该行为由 `authHeaders` 单点保证，wikiChat 已有负向覆盖） | 记录 |
| NOTE（相邻，非本次引入） | token 持久化在 localStorage（预存 XSS 取舍）；`api/chatHistory.ts` 仍发死值 `X-User-Id`；stub 认证模式下无 `Authorization` 的请求可能被后端兜底为 admin | 建议单独评估，不在本次范围 |

已核对：token 仅进 `Authorization` 头，不进 URL / 日志 / 错误消息；未登录时是「无凭据」而非「错误可信凭据」；原生 `fetch` 对头值做 CR/LF 校验，头注入面有兜底；`credentials: "include"` 为改动前已有且 JSON 预检已缓解 CSRF。

## 8. 部署验证

**未部署**（本次只做提交）。前端改动**必须**重建镜像才对线上生效：

```bash
docker compose build --no-cache frontend && docker compose up -d frontend
```

重建后冒烟（此前 403 的就是第一条）：

```bash
curl -i -X POST http://localhost/api/v1/wiki/chat \
  -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
  -d '{"question":"..."}' | head -1   # 期望 200，不是 403
```

> 注意：`docker cp` 只是临时覆盖，重建容器即回退（见 [qa-system-frontend-deploy-build-required]）。

## 9. 关联

- Wiki：`Harness/wiki/frontend.md`
- Rules：`Harness/rules/权限安全规范.md`
- Memory：`qa-system-frontend-sse-auth-header.md`（SSOT=authHeaders()；三处 SSE 已统一）
- 关联变更：`../feat-user-auth/summary.md`、`../feat-wiki-chat/summary.md`

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO 占位（部署一节如实标注「未部署」并给出重建命令）
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C 三案）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：已跑 security-reviewer，列 LOW ×2 + NOTE ×1，无 CRITICAL/HIGH
- [x] 第 8 段给出 docker compose 重建 + 端点冒烟命令（结果如实标注未执行）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki：`frontend.md` 已有 SSE 鉴权 SSOT 一节（本文件为其落地记录）
- [ ] 至少 1 条 MEMORY 索引已添加 —— 使用既有条目 `qa-system-frontend-sse-auth-header`（无需新增）
