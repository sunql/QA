# 知识关联写入 422：裸 fetch 缺 Content-Type

- **发现时间**：2026-11-16（生产）
- **生产运行的代码版本**：`main @ 10c79bb3`（= 1115 发版）
- **本分支基线**：`origin/main @ 3a27dc96`
- **变更性质**：**纯前端**、零后端改动、**零 DDL**、**零业务写**、**零新环境变量**
- **合并**：PR #138 ⇒ `main @ e8f49588`（2026-11-16，CI 净新增失败 0）
- **影响面**：`/api/v1/admin/wiki-links` 的 **创建（POST）与更新（PATCH）100% 失败**；
  查询类端点（GET）与撤销（DELETE，无 body）不受影响

---

## 1. 现象

生产浏览器控制台：

```
POST http://192.168.205.82:5173/api/v1/admin/wiki-links 422 (Unprocessable Entity)
```

同一时段此前出现的 `502 Bad Gateway` 是另一条线（nginx → backend 链路），与本条无关：
**422 本身证明 nginx 已经能通到后端**（响应来自 FastAPI，不是 nginx）。

## 2. 根因（两段都已实测，不是推测）

### 2.1 浏览器侧：`fetch` 字符串 body 的默认 Content-Type

`frontend/src/api/adminWikiLinks.ts` 用**裸 fetch**（不是 axios `httpClient`）发 JSON：

```ts
const resp = await fetch(BASE, {
  method: "POST",
  headers: authHeaders(),              // ← 只有 X-Tenant-Id / Authorization
  body: JSON.stringify(body),          // ← 字符串 body
});
```

Chromium 实测（Playwright 拦截请求头）：

| 写法 | 浏览器实际发出的 Content-Type |
|---|---|
| `body: JSON.stringify(x)`，不设 Content-Type | **`text/plain;charset=UTF-8`** |
| 显式 `headers: { "Content-Type": "application/json" }` | `application/json` |

### 2.2 服务端侧：FastAPI 只在 JSON content-type 下解析 body

在**生产同版本 FastAPI 0.141.1**（`qa-backend` 容器内）用**与生产完全相同的请求模型**
`CreateWikiLinkRequest` 实测：

| Content-Type | 结果 |
|---|---|
| `application/json` | **200**，body 被解析 |
| `text/plain;charset=UTF-8` | **422**，`detail: [{"loc":["body"], "msg":"Input should be a valid dictionary or object to extract fields from"}]` |

⇒ 浏览器发 `text/plain` ⇒ FastAPI **跳过 body 解析** ⇒ 请求体模型拿不到任何字段 ⇒ **422**。

### 2.3 为什么全仓库只有这一个文件中招

本仓库裸 fetch 调用共 5 个文件；带 JSON body 的 4 处里，3 处都显式声明了 JSON：

| 文件 | 写法 | 状态 |
|---|---|---|
| `api/chat.ts:294` / `:497` | `authHeaders({ "Content-Type": "application/json" })` | ✅ |
| `api/document.ts:184` | `authHeaders({ "Content-Type": "application/json" })` | ✅ |
| `api/wikiChat.ts:41` | `authHeaders({ "Content-Type": "application/json" })` | ✅ |
| **`api/adminWikiLinks.ts:38` / `:58`** | **`authHeaders()`（无参）** | ❌ **本批修复** |

### 2.4 为什么单测全绿（假绿机制）

`frontend/src/tests/adminWikiLinksApi.test.ts` **mock 了 global fetch**，只断言
`url` / `method` / `body` / 返回值 —— **从不检查请求头**。
`frontend/src/pages/__tests__/WikiLinksPage.test.tsx` 则把整个 api 模块 mock 掉。
⇒ 漏写 Content-Type 时**测试永远绿**，只有生产会炸。

### 2.5 附带发现：错误被吞成「假成功」（本次一并修）

FastAPI 的 4xx/5xx 响应体**也是 JSON**（`{"detail": ...}`）⇒ 原代码直接
`return resp.json()` 会 **resolve 而不是 reject** ⇒ `WikiLinksPage.tsx` 的 `catch`
永不触发 ⇒ 页面照样弹「添加成功」，**但库里没有这条记录**。
这正是本次事故**难定位**的原因：前端没有任何错误痕迹。
（仓库其它裸 fetch 模块 `chat.ts` / `document.ts` / `wikiChat.ts` 都有 `if (!resp.ok) throw`。）

## 3. 本批变更

| 文件 | 变更 |
|---|---|
| `frontend/src/api/adminWikiLinks.ts` | ① `createWikiLink`（POST）与 `updateWikiLink`（PATCH）显式声明 `authHeaders({ "Content-Type": "application/json" })`；② 新增 `readJson` 统一判 `resp.ok`，**非 2xx 一律 reject**（5 个函数统一走它） |
| `frontend/src/tests/adminWikiLinksApi.test.ts` | ① 2 条 Content-Type 回归锚点；② 1 条「写入路径仍带鉴权头」断言；③ 3 条「后端 4xx/5xx ⇒ 必须 reject」用例；④ mock 补 `ok`/`status` |
| `frontend/src/tests/apiJsonContentTypeGuard.test.ts`（新建） | **反向守卫**：源码级扫描 `src/` 下所有 fetch 调用，找「`body` 是 `JSON.stringify` 却没声明 content-type」；含 10 条守卫自检 |
| `Harness/wiki/frontend.md` | 新增「请求头约定：JSON body 必须显式 Content-Type」一节 |

### 3.1 守卫的判定口径（实现与注释一致）

对每个 `fetch(...)` 调用，取 init 对象按**顶层成员**解析：

- `body` 成员的值以 `JSON.stringify` 开头 ⇒ 该调用在范围内；
- `headers` 成员**不存在** ⇒ 违规（浏览器仍发 text/plain）；
- `headers` 值是对象字面量 ⇒ 其中无 content-type 键 ⇒ 违规；
- `headers` 值是函数调用（如 `authHeaders(...)`）⇒ 实参里无 content-type 键 ⇒ 违规；
- `headers` 值是**变量** ⇒ 静态判不出 ⇒ **跳过，不误报**。

content-type 按**键位**匹配且**大小写不敏感**（HTTP 头名本身大小写不敏感），
字符串值里的 "Content-Type: ..." 文本不算键。

### 3.2 守卫已知边界（刻意取舍）

- headers 传变量（`const h = authHeaders(...); ... headers,`）与 init 整体传变量
  （`fetch(url, init)`）静态判不出 ⇒ 跳过。**仓库主流写法（`chat.ts`）正是变量形态**
  ⇒ 这两类只能靠人工与 code review 兜底。
- 扫描范围仅 `frontend/src`：**不要扩到 `e2e/`**，那里是 Playwright 的 `route.fulfill`
  （不是 fetch 请求，且已显式带 `contentType: "application/json"`），扩了立刻假阳性。
- 扫描器已处理：注释（`//` `/* */`）、模板字符串里的 `${...}`、ES6 简写属性（`headers,`）、
  符号链接（跳过）、`__tests__` / `tests` 目录（跳过）。

## 4. 测试证据

| 项 | 结果 |
|---|---|
| `src/tests/adminWikiLinksApi.test.ts` + `src/tests/apiJsonContentTypeGuard.test.ts` | **25 passed**（10 守卫 + 15 api） |
| `src/pages/__tests__/WikiLinksPage.test.tsx` | **13 passed** |
| 全量前端套件 | 见 §4.1 |
| `npx tsc -b` | **exit 0** |
| 守卫**非空转**验证 | 把守卫喂**修复前**的 `adminWikiLinks.ts`（`git show origin/main:...`）⇒ 准确报出 **2 处**（第 40 行 POST、第 60 行 PATCH）⇒ 回退修复必红 |
| 仓库**无 eslint 配置** | `npx eslint` 报 `couldn't find an eslint.config.* file` ⇒ 前端唯一可用门禁是 `tsc -b` + vitest |

### 4.1 全量前端套件

`npx vitest run` ⇒ **235 files / 2406 tests 全过**（评审前的基线是 2396 ⇒ 本批净增 10 条，
无净新增失败）。

## 5. 评审处置

两轮评审已跑完（`code-reviewer` + `security-reviewer`，均**独立复现**了根因与守卫行为）：

| 来源 | 级别 | 结论 | 处置 |
|---|---|---|---|
| code-reviewer | HIGH H1 | api 层不检查 `resp.ok` ⇒ 422 在 UI 上是「假成功」 | ✅ **本批已修**（`readJson` + 3 条 reject 用例） |
| code-reviewer | HIGH H2 | 守卫锚点用「headers 字面量」⇒ 属性顺序 / 无 headers 键 / 手写头对象三类**静默漏报** | ✅ **本批已修**（锚点改为 fetch 调用 + init 对象顶层成员解析） |
| code-reviewer | MEDIUM M1/M2 | 守卫误命中注释里的 `headers`、裸子串匹配不可靠 | ✅ 随 H2 一并重写 |
| code-reviewer | MEDIUM M3 | `adminWikiLinks` 是唯一用裸 fetch 做普通 CRUD 的模块（`httpClient` 默认已带 Content-Type） | ⏸ **不本批做**：迁移会牵动 `WikiLinksPage` 取值方式与全局错误 toast 的双弹，爆炸半径远大于热修。已立为独立后续项，写进 §6 与 wiki |
| code-reviewer | MEDIUM M4 | 缺 `Harness/changes/<feature>/summary.md` / MEMORY / wiki | ✅ **本批已补齐** |
| code-reviewer | LOW L1/L2/L3 | `__tests__` 排除放错分支、`statSync` 跟随 symlink、空扫描假绿 | ✅ **本批已修**（目录分支排除 + `withFileTypes` 跳过 symlink + 断言扫描文件数 > 0） |
| code-reviewer | LOW L4 | `updateWikiLink` **无 UI 调用方** ⇒ PATCH 路径当前无端到端覆盖 | ⏸ 记录在案：影响面小于 POST；建议后续给页面补「编辑权重」入口 |
| security-reviewer | — | **无 CRITICAL / HIGH**。鉴权未削弱（`authHeaders` 注入逻辑未动）、无头注入/走私/跨域风险（CORS `allow_headers=["*"]`，同源反代）、无 secret 泄露 | — |
| security-reviewer | MEDIUM M1 | 守卫覆盖面窄于其声明 | ✅ 随 H2 一并重写 |
| security-reviewer | MEDIUM M2 | 大小写敏感匹配会误杀 `content-type` 小写写法 | ✅ **本批已修**（键位 + 大小写不敏感） |
| security-reviewer | MEDIUM M3 | 写入路径不检查 `resp.ok` | ✅ **本批已修**（同 H1） |
| security-reviewer | LOW L3 | 失败信息回显源码行 | ✅ **本批已修**（只输出 `file:line`） |
| security-reviewer | LOW L4 | 新用例未断言鉴权头仍在 | ✅ **本批已修**（补断言 + 用例） |
| security-reviewer | LOW L5 | `@types/node` 只靠传递依赖 | ⏸ 记录在案：`package-lock.json` 已锁定 26.2.0，`npm ci` 后 `tsc -b` 不红；仓库已有同款先例 |
| security-reviewer | LOW L6 | fallback 分支依赖 cwd | ⏸ 记录在案：`import.meta.dirname` 主路径可靠（Node ≥ 20.11），fallback 仅在主路径失效时启用 |

**评审未提出的问题（本批自查已覆盖）**：`adminWikiLinks.ts` 的 `fetch(\`${BASE}/${id}\`, {` 用模板字符串做 URL
⇒ 守卫的 init 对象定位必须跳过 `${...}` 里的 `{`（否则 PATCH 调用会被整体跳过 ⇒ 漏报）。
已改为「跳过字符串/模板/注释 + 只取 fetch 参数列表顶层的 `{`」，并用修复前的文件验证 PATCH 确实被抓到。

## 6. 明确未做的事

- ❌ 不改后端（后端契约本身正确：它按 FastAPI 惯例要求 JSON content-type）
- ❌ 不把该模块整体改用 axios `httpClient`（独立重构，见 §5 M3）
- ❌ 不动生产数据库、不动 `.env`、不重新传后端镜像（**后端不需要变**）
- ❌ 不给页面补「编辑权重」入口（`updateWikiLink` 仍无 UI 调用方，见 §5 L4）
- ❌ 不把守卫扩到 `e2e/`（见 §3.2）

## 7. 部署

见同目录 `deploy-prod-1116.md`：**只换前端镜像**，后端容器不动。

### 7.1 生产验收实测（2026-11-16）

镜像 `qa-system-frontend:amd64-1116-wikict` 上生产后，知识关联页新建两条链接，
再对 `wiki_ontology_link` 跑只读 SELECT：

```
 id |      page_id       | ontology_type | ontology_id | weight |         created_time          | revoked_time
----+--------------------+---------------+-------------+--------+-------------------------------+--------------
  2 | PAGE-OP30-5505D633 | property      |           7 |   1.00 | 2026-11-16 04:28:07.610899+00 |
  1 | PAGE-OP30-5505D633 | class         |           2 |   1.00 | 2026-11-16 04:27:59.111919+00 |
(2 rows)
```

- 两行 `revoked_time` 均为空 ⇒ 真实提交，不是「提示成功、库里没有」；
- **id 从 1 开始** ⇒ 该表此前从未有过任何写入，反向印证 §2.5 的静默假成功诊断。
