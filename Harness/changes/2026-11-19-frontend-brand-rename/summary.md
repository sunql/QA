# 变更记录：系统改名「AI服务平台」→「泰鸿智脑」（前端专用）

- **日期**：2026-11-19
- **分支**：线 B `feat/brand-rename`（→ `main`）；线 A `prod/brand-rename-1119`（→ `e8f49588`，只用于出镜像）
- **性质**：**纯前端**、零 DDL、零后端改动、零业务数据写
- **生产状态**：本批**先只发前端改名**；main 上的改名**合并但不部署**
- **生产 runbook**：[`deploy-prod-1119.md`](./deploy-prod-1119.md)

---

## 1. 背景与约束

用户需求三句话：**改名字**、**只改前端**、**在生产发版**，但同时**最近新开发的功能不发布到生产**，且**不影响正在开发的工作**。

三条约束互相拉扯：生产现役前端比 main 落后 3 个 commit（#150 P5 工作流前端 / #151 本体五 Tab 分页 /
#154 401 清理），而这些正是「最近新开发的功能」；若直接从 main 构建镜像发版，这 3 个 commit 会一起上线，
违反「新功能不发布」。

另外，未来还会走一次「功能全完成后的统一发版」⇒ 改名**必须同时落进 main**，
否则那次发版会把名字改回去（静默回退）。

### 采用的策略：双线同改

| 线 | 基线 | 改动 | 用途 |
|---|---|---|---|
| **线 A**（出镜像） | `e8f49588`（生产现役） | cherry-pick 改名 commit → `42c2a22a` | 构建**发生产**的前端镜像；不含 #150/#151/#154 |
| **线 B**（进 main） | `origin/main` @ `279204f2` | 改名 commit → `b1f348b6` | 提 PR 合进 main，**合并但不部署**；保证下次统一发版不会改回旧名 |

**两条线都用 fresh worktree**（`/private/tmp/qa-brand-prod` / `/private/tmp/qa-brand-main`），
主工作区 `feat/procurement-m0` 的 19 个未提交文件（M1-1.3 采购域编排）**全程未被触碰** ——
无 stash、无 checkout、无分支切换。

---

## 2. 改名清单（5 处）

| # | 文件 | 旧 | 新 |
|---|---|---|---|
| ① | `frontend/index.html` `<title>` | `AI服务平台` | `泰鸿智脑` |
| ② | `frontend/index.html` 内联 favicon SVG | 字母 `Q` | 字母 `T` |
| ③ | `frontend/src/i18n/zh-CN.ts` `appLayout.brand` | `AI服务平台` | `泰鸿智脑` |
| ③' | `frontend/src/i18n/en-US.ts` `appLayout.brand` | `AI Service Platform` | `Taihong ZhiNao` |
| ④ | `frontend/src/components/common/AppLayout.tsx:135` | `collapsed ? "QA"` | `collapsed ? "TH"` |
| ⑤ | `frontend/src/i18n/zh-CN.ts` `auth.login.title` | `登录 qa-system` | `登录 · 泰鸿智脑` |
| ⑤' | `frontend/src/i18n/en-US.ts` `auth.login.title` | `Sign in to qa-system` | `Sign in to Taihong ZhiNao` |

配套测试断言更新：`frontend/src/tests/App.test.tsx:98`、`frontend/src/tests/pages.test.tsx:130`。

### 用户决策记录

| 决策 | 内容 |
|---|---|
| 决策 1 | en-US 品牌名用 **Taihong ZhiNao**（而非逐字直译） |
| 决策 2 | 侧边栏折叠态缩写 **QA → TH**（而非「泰」） |
| 决策 3 | favicon 字母 **Q → T** |
| 决策 4 | 登录页标题格式 **「登录 · 泰鸿智脑」** |
| 菜单文案 | **用户人工调整**，不在本批范围 |

### 刻意不改的两处

- `qa-system-theme` / `qa-system-auth` 两个 localStorage key：它们是**内部存储键**，
  改名会导致所有用户被登出并破坏 localStorage 契约 ⇒ 保持原样。
- 菜单文案：DB 驱动，用户自行人工调整。

---

## 3. 新增守卫

`frontend/src/tests/brandRenameGuard.test.ts`（新建，5 条用例）—— 点名 5 个改名位置 + 新名字，
**只做正向断言**（`^\s*key:\s*"值",$` 行首锚定正则，注释免疫）。

为什么需要守卫：改名的失败模式不是「改错」，而是**只改了几处**（改了 i18n 忘了 `<title>`，
改了 zh-CN 忘了 en-US）。这类「同一语义散落在多个文件、漏一处就自相矛盾」的形态，本仓已反复出现。

**变异反证 ×6**：逐个破坏 ① title / ② favicon / ③ zh-CN brand / ④ en-US brand / ⑤ 折叠态 TH /
⑥ 登录页标题，每次恰好 1 条用例失败，还原后 5 条全绿 ⇒ 守卫**确实在守**，不是空集假绿。

---

## 4. 取证：证明「只改了名字」

### 4.1 产物级对比（静态文件树）

用**生产现役代码（`e8f49588` 原样）另建一个镜像**，与本批镜像逐一对比 `/usr/share/nginx/html`：

- 文件清单**只差 2 个文件**：`index.html`（708 → 706 B）、`assets/index-*.js`（bundle 文件名随内容哈希变化）；
- `50x.html`、`assets/index-WW9dT-wP.css` **逐字节相同**；
- nginx `resolver 127.0.0.11` / `proxy_pass http://$backendUpstream` **两边都是 1**；
- 硬编码 `localhost:5173` 计数 **两边都是 0**。

### 4.2 UI 文案清单对比

提取两侧产物里的**中文连续字串多重集**并做差分：

- 中文：两侧各 **2717 种**，差异仅 `-「服务平台」×1`、`+「泰鸿智脑」×2`；
- ASCII：`AI Service Platform` 1→0、`Taihong ZhiNao` 0→1、
  `Sign in to qa-system` 1→0、`Sign in to Taihong ZhiNao` 0→1、`"QA"` 1→0、`"TH"` 0→1。

⇒ **没有任何其它 UI 文案、路由、请求路径、反代配置发生变化。**

> 取证方法论备注：曾尝试对两个 3.6MB bundle 做 `difflib.SequenceMatcher`（超时）、正则串字面量提取
> （误命中跨越压缩代码的巨型串）、`git diff --word-diff`（minifier 会 re-mangle 短标识符，
> 如 `p1e`→`h1e`，单行词级 diff 结构上不可能）—— 最终**改用「UI 文案清单 + 文件清单」对比**，
> 这才是对压缩产物唯一有效的产物级证据。

### 4.3 验收探针双向验证

runbook §6.1 的探针已对**两个镜像**分别跑过（正/负对照）：

| 键 | 新镜像 | 现役镜像（负对照） |
|---|---|---|
| `bundle` | `index-BrJZOJUV.js` | `index-rBu8Smfz.js` |
| `title_new` / `bundle_new` | 1 / 1 | 0 / 0 |
| `title_old` / `bundle_old` | 0 / 0 | 1 / 1 |
| `en_old` / `login_old` | 0 / 0 | 1 / 1 |
| `favicon_T` / `favicon_Q` | 1 / 0 | 0 / 1 |
| `resolver` / `proxy_pass` | 1 / 1 | 1 / 1 |

⇒ 探针**对新旧镜像都能区分**，不会出现 1116 runbook 里那版「对新旧都返回 0 ⇒ 误判没部署」的问题。

---

## 5. 测试证据

| 线 | 基线 | 套件 | 覆盖率（statements/branches/functions/lines） | `tsc -b` |
|---|---|---|---|---|
| 线 B（main） | `origin/main` @ `279204f2` | **241 文件 / 2444 用例全绿** | 84.25 / 84.22 / 82.54 / 84.25 | EXIT=0 |
| 线 A（prod） | `e8f49588` | **236 文件 / 2411 用例全绿** | 84.13 / 84.67 / 83.10 / 84.13 | — |

四指标覆盖率均 **≥ 80%**（仓库门禁线）。
线 A 用例数少 5 文件 / 33 用例 —— 这正是「生产基线不含 #150/#151/#154」的**预期表现**，是证据不是缺陷。

---

## 6. 交付物

| 项 | 值 |
|---|---|
| 镜像 | `qa-system-frontend:amd64-1119-brand`（linux/amd64） |
| 镜像 Id | `sha256:dc866ead2472ef87193b6f448eb0500b3df1ab2a543f2552520c6d4ca2de1a40` |
| config digest | `sha256:e4751315a9a006ecb46cf1f5cd8aceef43f02dc95c6368e8bbebb7578aa802bb` |
| tar 包 | `/tmp/qa-frontend-amd64-1119-brand.tar.gz`（26M） |
| sha256 | `7dbe648f6274f43a99ebe9149052b23745e99ad7440bd9b304709309b25296d6` |
| 构建参数 | `VITE_API_BASE_URL=`（空 ⇒ 同源相对路径，与现役一致） |

tar 为**经典 `docker load` 兼容**格式（`manifest.json` + `oci-layout` + `blobs/`）。

---

## 7. 明确没做的事

- ❌ **不带** #150 / #151 / #154 三个已合 main 但未上生产的前端 commit（用户要求：新功能不发布）；
- ❌ 不动后端镜像（backend / worker / postgres / neo4j / milvus 一律不动，**不跑迁移**）；
- ❌ 不做任何 DDL、不写任何业务数据；
- ❌ 不改生产 `docker/.env`（`SECRET_KEY` 是 Fernet 主密钥，改了旧 token 全失效）；
- ❌ 不改菜单文案（用户人工调整）；
- ❌ 不改 localStorage 内部键（避免全员登出）。

---

## 8. 后续

1. 生产按 [`deploy-prod-1119.md`](./deploy-prod-1119.md) 逐条执行（**生产命令由用户在宿主机执行**）；
2. 线 B 合进 main（**不部署**）⇒ 下次「统一发版」时名字保持「泰鸿智脑」；
3. 本批合并后清理三个临时 worktree 与本地分支。
