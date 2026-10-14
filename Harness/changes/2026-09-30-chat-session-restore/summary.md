# 变更：2026-09-30-chat-session-restore

- **日期**：2026-09-30
- **作者**：Claude / 启琳
- **Phase**：v3.1 B6 补完（会话归属守卫铺开）+ 前端会话恢复（原 B 方案）
- **状态**：in-review（代码 / 测试 / 部署 / e2e 全绿；7 项收尾清单见 §10，其中 5 项已做、2 项**明确不做**并留痕）
- **关联变更**：[feat-chat-history-panel](../feat-chat-history-panel/summary.md)（`/chat-history` 与会话端点）
  > 注：守卫的前身 `_assertChatSessionOwnership`（v3.1 B6）**没有**对应变更记录（`grep` 全库 `Harness/changes/` 无命中）——
  > 属既有文档缺口，本变更把它的语义完整写进了 `session_guard.py` 与本文档。 
- **迁移版本**：无（守卫用现有列，分键是纯客户端行为；alembic head 仍 `0105`）
- **MEMORY**：[../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-chat-session-restore.md](../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-chat-session-restore.md)

---

## 1. 需求

**报障**：刷新页面（或新开标签页）后回到 `/chat`，屏幕是空的 —— 上一场对话消失，历史面板里任何一行也没有「当前」标记。

**验收标准**（用户 2026-09-30 选定 B 方案时拍板的三条前置）：

1. 刷新后**不点任何东西**，屏幕上是**最近**那批对话，输入框接着它继续；
2. 「新对话」按钮仍然可用（清指针 → 不再恢复）；
3. **换用户后不得带出别人的会话**（渠道维度分键 + 登出/换人清键 + 后端归属校验）。

> 顺带修掉两个既有缺陷：`channel` 从不被持久化（进过一次文档问答后永久停在 `doc_qa`）、
> `setChannel` 当场生成新 id 却不清 `messages`（跨渠道串台）。

---

## 2. 设计评审

### 根因（已亲自读到 file:line）

`frontend/src/stores/persistChatUiState.ts` 的 `qa:chat:lastSessionId` 头注释写着「用于刷新恢复」，
`chatStore.ts` 初始化也读它，但实测三个时点该键**恒为 null**：

- 4 个写入点（`setChannel` / `resetSession` / `loadSessionMessages` / `deleteSession`）里，
  **发送路径一次都不写**，而其中 3 个写的是「刚生成的**空**会话 id」—— 唯一让「恢复」有意义的时机从来不写；
- `ChatPage` 挂载**不调用**回放（唯一 effect 依赖 `[historyPanelOpen]`），回放只在**点击**历史项时发生。

### 为什么不能只补「写」这一行（方案取舍）

前端发给后端的上下文是 `history: toHistory(messages)`（**屏幕上可见的那批**），而追问锚点
（`last_plan` / `last_sql` / `last_data`）由**服务端按 `session_id` 存在 `session_query_state`**
（`chat_recall.py:200-206` 只按 session_id 读，从不查 `session_message` 判断可见性）。

| 候选 | 屏幕 | 服务端锚点 | 结论 |
|---|---|---|---|
| A 维持现状（不写键） | 空 | 新 session ⇒ 空 | 只丢对话，不会出现看不见的上下文 |
| B′ **只写键、不回放** | 空 | **沿用旧锚点** | 问「那 4 月份呢？」会得到**关于一场看不见的对话**的回答 —— 比现状更坏 |
| **B 写 + 回放（采纳）** | 有历史 | 与屏幕一致 | 正确 |

⇒ **写与回放必须同批**，且回放窗口必须取**最新**（`tail: true`）。写与回放拆开上线是**回退**而非前进。

### 两条被核实推翻的前提（记录以免重犯）

- 「chat 渠道完全没有归属线」→ **错**：`chat.py:40-56` 早有 `_assertChatSessionOwnership`（v3.1 B6），
  只是**只有一个调用者**（`/hypotheses`）；`POST /chat`、`/chat/stream` 与 `session.py` 全篇都没挂。
- 「`/chat-history` 有归属过滤」→ **错**：只按 `channel` 过滤，列出**所有人**的会话。

### 审查发现的两处必修（本变更内闭环）

| 审查结论 | 事实 | 修法 |
|---|---|---|
| **CRITICAL**：守卫的归属事实源写死 `channel='chat'`，而 `/messages`、`DELETE`、`export.pdf` 服务三个渠道 ⇒ doc_qa / wiki_qa 会话**静默 fail-open**（删除是破坏性操作） | `evidence_query_service.py:36-40`；doc_qa 行由 `rag_qa_service.py:103-118` 打标、wiki_qa 由 wiki 侧打标 | `getSessionOwnerUserIds(..., channel=None)` 取**任意渠道并集**；守卫传 `channel=None`；补 doc_qa/wiki_qa 双向用例 |
| **HIGH**：登出/登录只清 localStorage，**不清内存 store** ⇒ 同一 tab 换人后上一个用户的对话仍在屏幕上（`enterChannel` 的「同渠道且有消息」早退） | `authStore.ts:41-52` 不触碰 chatStore | 新增叶子模块 `stores/userSwitch.ts`（注册-通知，避免 `authStore ← chatStore ← api/client ← authStore` 循环）+ `chatStore.clearForUserSwitch()`；login / logout / 401 三处触发 |
| **HIGH**（代码审查）：把 `setChannel` 由同步改为异步回放后，回放**无代际校验** ⇒ 晚到的响应覆盖当前渠道/消息（跨渠道串台、冲掉刚发的消息、把 `loading` 置 false 使发送守卫失效） | `loadSessionMessages` await 后无条件 `set` | 加回放代际 `sessionLoadSeq`（`invalidatePendingLoads()`），返回值改 `loaded / failed / stale`；切渠道、点历史、新建、删除、发送、换人六处作废在途回放 |
| **MEDIUM**：`DocumentQaPanel` 缺 StrictMode ref 守卫（首次回放时幂等条件不成立）⇒ 发两遍 `GET /messages` | `ChatPage` 有 ref，面板没有 | 面板补 ref 守卫；并把 `ChatPage` 那句「store 侧也有幂等保护」改成与实现相符的说明 |

---

## 3. 数据模型变更

**无迁移**。守卫复用 `session_message.user_id`（既有列）。alembic head 仍 `0105`。

---

## 4. 接口契约变更

| 端点 | 变更 |
|---|---|
| `GET /sessions/{sid}/messages` | 新增归属守卫（已打标且非本人 → 403）；`tail` / `before_id` 上一变更已就位 |
| `DELETE /sessions/{sid}` | 新增归属守卫（守卫**先于**删除执行，拒绝时行仍在） |
| `POST /sessions/{sid}/export.pdf` | 新增归属守卫（**先于** 404 判定，别人的会话一律停在 403） |
| `POST /chat`、`POST /chat/stream` | 新增归属守卫 ⇒ **关闭追问锚点继承**（`/chat/stream` 的守卫在 `eventSource` 之前，仍能回 HTTP 状态码） |
| `GET /chat/sessions/{sid}/hypotheses` | 行为不变，改为 import 共享守卫（删私有副本） |

- 状态码与文案**沿用**既有 403 `MSG_HYPOTHESIS_SESSION_NOT_OWNED`（为一个「守卫搬家」改状态码 = 同时改契约与测试）。
- 前端：`loadSessionMessages` 返回值 `boolean` → `SessionLoadOutcome`（`loaded` / `failed` / `stale`）；
  `chatStore` 新增 `clearForUserSwitch()`；`setChannel` → `enterChannel(channel)`。

---

## 5. 实现要点

- **后端**：新建 `app/api/v1/session_guard.py:assertSessionOwnership(session, sessionId, user)`
  （admin 放行 → 有归属且不含本人 → 403 且 detail 不回显归属者 → 无归属 fail-open），
  挂到 5 个端点；`chat.py` 删私有副本。
- **前端分键**：`qa:chat:lastSessionId:<channel>` + `qa:chat:lastChannel`；清键**按前缀遍历**，不硬编码渠道列表。
- **前端生命周期**：`enterChannel`（面板声明渠道：ChatPage → `chat`，DocumentQaPanel → `doc_qa`）
  → 有指针则 `loadSessionMessages(id, { tail: true })` → 成功写指针；**失败清指针 + 换新会话**
  （否则每次刷新重放同一个失败，且失败只在面板展开时可见 ⇒ 表现为「刷新后永远空白」）。
- 发送路径（`sendMessage` / `sendDocQa`）补写指针 —— 这是原缺陷的核心。
- `toChatMessage` 补 `dbMessageId`（恢复出来的消息才有「导出此条」按钮）。

---

## 6. 测试

| 文件 | 用例 | 覆盖 |
|---|---|---|
| `backend/tests/integration/test_session_guard_api.py` | **21** | 5 个端点 × 双向（别人 403 / 自己放行 / 存量 NULL 放行 / admin 旁路）+ doc_qa·wiki_qa 参数化 + 「拒绝后行仍在」+ detail 不回显归属者 |
| `frontend/src/tests/persistChatUiState.test.ts` | 10 | 分键互不干扰、按前缀清键（不误删 panel 键）、localStorage 异常回退 |
| `frontend/src/tests/chatStoreHydration.test.ts` | **18** | 模块级 hydration（`vi.resetModules()` + 动态 import）、`tail:true`、失败清指针换会话、换渠道取各自指针、回放竞态（2）、换人清内存（2）、写→重启→回放闭环 |
| `frontend/src/tests/authStore.test.ts` | **8** | login/logout 清指针 + **清内存对话**（用真 chatStore）+ 口令错不清 |
| `frontend/src/tests/chatStore.test.ts` / `chatStoreDocQa.test.ts` / `ChatPage.test.tsx` | 37 / 2 / 20 | 既有行为 + 挂载回放接线 |

**RED 证据**（守卫类断言必须双向，且要证明会咬人）：

- 摘掉 `getSessionMessages` 的守卫 → 阻断用例返 200 而非 403（RED），还原后 21/21 绿；
- 把归属查询退回 `channel="chat"` → **恰好 4 条**非 chat 用例红、其余 17 条不动（证明那条「静默 fail-open」真实存在）；
- 去掉回放代际校验 → 2 条竞态用例红；摘掉换人监听 → 4 条换人用例红。

**回归**：后端 8 个集成文件 89 passed（守卫 + session + chat_history + history_replay + export_charts + chat_stream + evidence_api + evidence_acl，真实 PG `qa-pg-a1:5434`，串行）；
前端 `npx vitest run src/stores src/pages src/tests` = **1327 passed / 1 failed** —— 唯一失败是既有基线红 `EntityMappingPage`（与本次无导入图交集）。

---

## 7. 安全审查

触发条件：认证 / 授权 / 用户输入。已跑 `security-reviewer`（opus）与 `code-reviewer`（opus），两条审查独立命中同一批问题，处置如下：

| 级别 | 结论 | 处置 |
|---|---|---|
| CRITICAL | 守卫归属事实源写死 `channel='chat'` ⇒ doc_qa / wiki_qa 的读与删静默 fail-open | **本变更内已修**（`channel=None` 并集）+ 4 条新用例 |
| HIGH | 换人只清 localStorage 不清内存 ⇒ 上一个用户的对话留在屏幕上 | **本变更内已修**（`userSwitch` + `clearForUserSwitch`） |
| HIGH | 回放无代际校验 ⇒ 晚到响应覆盖当前状态（本次改动引入） | **本变更内已修** + 2 条竞态用例 |
| MEDIUM | `DocumentQaPanel` 无 StrictMode 守卫 ⇒ 双请求 | **本变更内已修** |
| MEDIUM | `/sessions/{sid}/usage`、`/usage/list` 未纳入归属（成本台账弱敏感） | **已补挂守卫**（见 §10-2）+ 4 条新用例（含 RED 验证） |
| LOW（**未修，转「待拍板」**） | `/chat-history` 列全站会话（方案明确不做，依赖回填口径） | 见 §9 待拍板 1、2 |
| LOW | 403 文案偏窄（复用「分析假设」） | **已中性化**（见 §10-7） |
| LOW | `scripts/e2e_smoke/login_e2e.mjs`、`wiki_chat_e2e.mjs` 里**硬编码了口令字面量**（未跟踪文件，一旦提交即入库） | **已改为只读环境变量**（见 §10-3） |
| LOW（接受） | `persistChatUiState` 的 localStorage 读写失败一律静默回退 | 刻意 best-effort（隐私模式不该炸 UI），已在模块头注释写明理由 |

正向结论：身份派生链路可信（JWT + nginx 剥 `X-User-*` + `AUTH_MODE=real`），
守卫执行顺序正确（无「先副作用后校验」），router 级依赖与端点级依赖不重复不冲突。

---

## 8. 部署验证

- 后端：`./scripts/deploy_backend.sh` → 「✅ 启动成功」（快照在 `backups/container/20260930_*`）
- 前端：`docker compose build --no-cache frontend` + `up -d frontend`
- 守卫冒烟（真实 auth，非 stub）：登录拿 Bearer → `GET /sessions/{sid}/messages?tail=true`
  - 无 token → **403**；带 token → **200**，`tail` 窗口返回最新 130 条（旧会话无 `chartType`，符合预期）
- **e2e：8 步全过（0 失败）**。`scripts/e2e_smoke/chart_report_e2e.mjs` 第 7 步（本次改写）输出：
  `无操作恢复会话 s-muo2d989-sl08xx：2 条消息，其中 1 条带 chartType=hbar，canvas 已渲染`
  —— **刷新后不点任何东西**会话与图自动回来；并断言了回放请求带 `tail=true` 且 URL 含指针指向的会话 id。
  第 4/5/6/8 步同时复验 0105 契约（`chartType=hbar`、canvas=1、导出 PDF 101 KB 含 `/Subtype /Image`、
  `/messages?tail=true` 的 assistant 行带 chartType）。
  > 首次重跑时第 3 步曾超时 180s：真因是业务库 **THBI Oracle `192.168.205.70:1521` 不可达**
  > （`nc -z` 失败；非流式 `/chat` 同样 2 分钟后 500，日志 `business_db_pool.py:403 TimeoutError`，
  > 另有 Milvus ORM `ConnectionNotExistException` 仅致召回降级）。与本次改动无关 ——
  > `/chat/stream` 当时返回 200，守卫未拦。**归因前先 `nc -z` 探活业务库。**
- **二次部署（§10 处置后）**：`./scripts/deploy_backend.sh` → 「✅ 启动成功」
  （快照 `backups/container/20260930_201442`）。核验容器内 5 处 `assertSessionOwnership`
  全在（session.py:142/154/182/203/251）、守卫用 `MSG_SESSION_NOT_OWNED` 且 `channel=None`、
  旧常量已从 `messages_zh.py` 删除（全后端无残留 import）、`GET /api/v1/health` → 200。
  > 首次核验时容器**仍是旧代码**（仅 3 处守卫、docstring 还写着 usage「未纳入归属」）——
  > 说明 §10-2 的改动只在磁盘上。**改完后端必须重新灌容器，再核验容器内的 file:line，别只看磁盘。**

### C-1 在真实数据上的影响面（修复前后对照）

`SELECT channel, count(*), count(user_id), count(DISTINCT session_id), count(DISTINCT user_id) FROM session_message GROUP BY 1`（prod，2026-09-30）：

| channel | 行数 | 已打标 | 会话数 | 归属人数 |
|---|---|---|---|---|
| chat | 1036 | 116 | 239 | 1 |
| **wiki_qa** | 22 | **22** | **8** | **3** |

⇒ 修复前：这 **8 个 wiki_qa 会话（3 个归属人）** 对任意登录用户开放（`/messages` 可读、`DELETE` 可删、
`export.pdf` 可导出）—— 因为守卫只查 `channel='chat'`、恒返空集。chat 渠道那 920 行未打标的存量会话仍 fail-open（见 §9 待拍板 1）。

---

## 9. 关联

- 设计稿 / 方案：`~/.claude/plans/piped-beaming-possum.md`（B 方案，含「明确不做」清单）
- Wiki：`Harness/wiki/chat-service-capabilities.md`（§4 会话上下文、§8 安全与边界）
- Rules：`Harness/rules/测试规范.md`（真实 PG + 全链路）、`Harness/rules/权限与安全规范.md`
- Memory：`qa-system-chat-session-restore`、相关既有 `qa-system-chat-session-restore-broken`（本变更修的就是它）
- 关联变更：`../feat-chat-history-panel/summary.md`（`/chat-history` 与三个会话端点）

### 待拍板（不阻塞合入，但需记录）

1. **存量未打标行**（chat 渠道 1032 行里 920 行 `user_id IS NULL`）仍 fail-open ⇒ 历史面板对所有人可见、
   存量会话的锚点仍可被继承。回填属 **prod 数据变更**（`UPDATE ... WHERE user_id IS NULL AND channel='chat'`），
   须先确认这些确实都是同一个人的历史。**本变更不顺手做。**
2. **`/chat-history` 是否加 owner 过滤**：依赖 1 的答案；不加则「任何人能看到所有会话标题与首尾预览」留着。
3. ~~**403 文案**偏窄~~ —— **已处置**，见 §10-7。
4. ~~**`/sessions/{sid}/usage`、`/usage/list`** 未纳入归属~~ —— **已处置**，见 §10-2。
5. **多 tab 共享同一指针**：本版不做隔离（换 sessionStorage 的代价是浏览器重启丢恢复）。**保持原状。**
6. **`/messages` 不按 channel 过滤**（`loadFullMessages` 仅按 session_id）：跨渠道同 id 会把行混在一起，本版不动。**保持原状。**

---

## 10. 本轮处置（用户 2026-09-30 批准「先按你的建议来吧」）

原始是一份 7 项收尾清单。逐项结论如下 —— **做与不做都要留痕**，不做的那两项同属决策，不是遗漏。

| # | 项 | 结论 | 落点 |
|---|---|---|---|
| 1 | 三个逻辑提交 | **已做** | 前端恢复 / 后端守卫 / 文档，分支 `feat/chat-session-restore`；只 stage 本次相关文件 |
| 2 | `/usage`、`/usage/list` 补归属守卫 | **已做** | `session.py` 两端点加 `Depends(getCurrentUser)` + `assertSessionOwnership`；新增 `TestUsageEndpointOwnership` 4 条（含 admin/NULL 双向）。RED 验证：摘掉守卫 → `test_foreign_session_blocked_on_summary` 变红（1 failed / 3 passed），已还原 |
| 3 | e2e 脚本硬编码口令 | **已做** | 两个脚本口令改 `process.env.E2E_PASSWORD`，**无默认值**，缺失即 `process.exit(1)`；账号/BASE 走 `E2E_USER`/`E2E_BASE_URL`；登录断言里硬编码的 `"admin"` 改 `USERNAME`。验证：`node --check` 双过、无变量时 exit=1、全仓无 `Admin@` 字面量残留 |
| 4 | 删 2 个 prod 验证残留会话 | **已做** | 见下方「删除记录」 |
| 5 | 回填 920 行 `user_id IS NULL` | **明确不做**（原建议即为不做） | 属 prod 数据变更；须先确认这些确实同属一人。见 §9 待拍板 1 |
| 6 | `/chat-history` 加 owner 过滤 | **明确不做**（依赖 5） | 见 §9 待拍板 2 |
| 7 | 403 文案中性化 | **已做** | `MSG_SESSION_NOT_OWNED = "会话不存在或不属于当前用户"` 成为守卫统一文案；删除 `MSG_HYPOTHESIS_SESSION_NOT_OWNED`（留注释说明为何删）。选「会话不存在**或**不属于」而非单纯的「不属于」：不区分二者即不泄露「这个 id 存在」 |

保持原状（评估后认为利弊不比现状好）：`/messages` 不按 channel 过滤（§9-6）、多 tab 共享指针（§9-5）。

### 删除记录（item 4）

删除前逐条核对（**先看清再删**），确认与 e2e 两轮验证一一对应：

| session_id | 行数 | 归属 | 首帧 | 对应 |
|---|---|---|---|---|
| `s-muo0x36z-e5a4ul` | 2 条 message + 3 条 usage | admin | 11:32:19 | **失败那轮**（Oracle 不可达，`nl2sql`+`step_plan`） |
| `s-muo2d989-sl08xx` | 2 条 message + 3 条 usage + 1 条 query_state | admin | 12:10:55 | **成功那轮**（`nl2sql`+`chart`+`answer`） |

- 先 `pg_dump --data-only` 三张表全量备份到 `/tmp/session-cleanup/all_three_tables.dump`（1.7 MB，已核实含两个目标 id）
- 单事务删除三表：`4 messages / 6 usage / 1 query_state`，与删除前计数**精确相符**；删后复查三表均 0
- 代价：这 6 行成本台账（合计 **$0.0403**）随会话一并消失 —— 即本次自测开销，删除即目的
- **未动**更早的 e2e 残留 `e2e-threestep-1790743461`（04:44，2 条，不在本批批准清单内）

> 踩坑：`docker exec` **不带 `-i` 不转发 stdin**，heredoc 形式的 psql 会静默不执行任何语句
> （表现为「无输出 + 数据还在」）。判断是否真删，一律以**复查查询**为准，不以命令退出码为准。


---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位（§8 的 e2e 未过是**如实记录**，不是占位）
- [x] 第 2 段 ≥ 2 个候选方案对比（A / B′ / B 三行取舍表）
- [x] 第 3 段迁移文件名 ≤ 32 字符（无迁移）
- [x] 第 7 段 security-reviewer 结果已分级（CRITICAL / HIGH / MEDIUM / LOW）
- [x] 第 8 段部署冒烟命令与输出贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新（`chat-service-capabilities.md`）
- [x] MEMORY 索引已在 `~/.claude/projects/.../memory/MEMORY.md` 添加
- [x] 涉及真实 SQL/DB 改动时 `scripts/<feature>_realdata.py` 已跑通 —— **不适用**（无 SQL/DB 结构改动）
- [x] **部署后 e2e 闭环** —— 8 步全过，见 §8
- [x] **提交** —— 用户 2026-09-30 批准（「先按你的建议来吧」），3 个逻辑提交见 §10-1
