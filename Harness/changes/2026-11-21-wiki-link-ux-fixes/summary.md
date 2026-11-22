# Wiki 链接管理 —— 4 项可用性死路修复

- **日期**：2026-11-21
- **分支**：`feat/wiki-link-ux-fixes`（base `origin/main` @ `2536cea2`）
- **改动面**：后端 2 个 service + 1 个 API + 1 个新 util；前端 1 个页面 + i18n；测试 3 个文件
- **边界**：零 DDL、零迁移、零生产写；不改变召回/注入语义

## 背景

「Wiki 链接管理」（`/admin/wiki-links`）是 wiki 知识进入 NL2SQL 的唯一入口。使用中卡住用户的 4 个问题：

1. **本体对象下拉搜不到**：后端 `?q=` 早已支持，但前端从不发送 `q`；`showSearch` 是客户端过滤已加载的 50 条 ⇒ 对象多时目标不在那 50 条里就永远搜不到。
2. **停用类仍可选**：`listLinkableTargets` 的 class 分支没有 `valid_to IS NULL` 过滤，而召回只取活类 ⇒ 选中墓碑类「建链成功但永远不生效」。
3. **左树硬编码 200 页**：`listWikiPages({limit:200})` 且无检索 ⇒ 超过 200 的页面在树上直接消失。
4. **权重/备注改不了**：`PATCH /admin/wiki-links/{id}` 与 `updateWikiLink` 都已存在，但页面没有编辑入口，只能撤销重加。

## 改动清单

### 后端

| 文件 | 改动 |
|---|---|
| `app/services/wiki_link_service.py` | class 分支加 `valid_to IS NULL`；三分支统一 `ORDER BY name, id`（limit 在排序之后）；**列投影**替代实体查询（避免 `lazy="selectin"` 级联拉全量属性/指标）；`q` 走 `escapeLike` + `ilike(..., escape=)` |
| `app/api/v1/admin_wiki_links.py` | `type: Literal["class","property","metric"]`（非法值 422，此前静默 200 `[]`）；`limit: Query(ge=1, le=200)`；`q: Query(max_length=100)` |
| `app/utils/like_escape.py`（新增） | ILIKE 通配符转义（`%`/`_`/`\`），`wiki_page_service` 与 `wiki_link_service` 共用 |
| `app/services/wiki_page_service.py` | 删除本地 `_escapeLike` 实现，改从共享 util 导入（调用点零改动） |

### 前端

| 文件 | 改动 |
|---|---|
| `pages/WikiLinksPage.tsx` | ① 本体对象 Select 改**服务端检索**（300ms 防抖 + `latestQRef` 丢弃乱序响应 + 选中项保留在 options 首位，防 label 退化成裸 id）；② 左树默认 **50** 页 + 总数提示 + 页面搜索（复用 `/wiki/pages/search`，结果标注分类名）；③ 链接行新增**编辑**入口（PATCH weight/note，清空备注传 `""`）；④ `refresh` 增加切页竞态守卫 |
| `i18n/zh-CN.ts` / `en-US.ts` | 新增 11 个键；顺带补回 en-US 缺失的 `treeLoadFailed` |

> ⚠️ 本仓 i18n 的插值前缀被配置成**单花括号**（`i18n.ts:19-21` `prefix: "{"`），不是 i18next 默认的 `{{ }}`。新增带变量的 key 必须写 `{shown}` 而不是 `{{shown}}`，否则会原样渲染占位符。

## 测试证据

| 套件 | 结果 |
|---|---|
| `backend/app/tests/unit/test_wiki_link_service.py` | **23 passed** |
| `backend/app/tests/integration/test_wiki_link_admin_api.py` | **18 passed** |
| `backend/app/tests/integration/test_wiki_api.py`（回归：`/pages` 与 `/pages/search`） | **38 passed** |
| `frontend/src/pages/__tests__/WikiLinksPage.test.tsx` | **26 passed** |
| `frontend/src/tests/adminWikiLinksApi.test.ts` | **15 passed** |
| `npx tsc -b` | 无错误 |

关键回归用例（均为「去掉修复必红」验证过的）：
- 墓碑类在 service 与 HTTP 两层都被排除；
- 三分支排序正确、`limit` 在排序之后生效；
- `q` 能命中默认 50 条之外的第 51 条；
- `%` / `_` 被当字面量（不再退化成全表匹配）；
- 非法 `type` / 越界 `limit` 返回 422；`linkables` 与 `PATCH` 非 admin 返回 403；
- 前端：默认只加载 50 页、全量搜索替换树、空白搜索不发请求、搜索结果标注分类、防抖只发最终关键词、清空后回默认列表、选项替换后 label 保留、编辑预填/提交/取消/清空备注、**关闭并重开弹窗后遗留防抖请求不覆盖默认列表**（该条做了变异验证：移除 `clearTimeout` 即红）。

## 明确未做（挂账）

- `/wiki/pages/search` 仍是「任意已登录用户」可达（既有设计，非本批引入）；若产品要求 Wiki 检索仅 admin 可见，需另批在后端加守卫。
- `updateLink` / `revokeLink` 不写 `audit_log`（既有缺口）；本批的编辑入口沿用同一模式，未扩面。
- `WikiLinksPage.tsx` 现 700+ 行、组件近 550 行，超过项目「函数 <50 行 / 文件 200–400 行」建议；拆分 `PageSearchPanel` / `EditLinkModal` / `useLinkablesSearch` 留待后续。
- 分类级「批量关联」仍无 UI（数据模型无该维度），需要脚本。

---

## 追加（2026-11-22）：CI 归因与补修

PR #172 于 2026-11-21 15:04:02Z squash 合入 main（`053b5de0`），**合并时 9 个 CI 检查全部 pending**
（分支保护 `enforce_admins=false` 放行）⇒ 属「在 CI 之前合入」，不是绿灯后合的。
合并后的 main push CI run `44917177808`（16:11 完成）⇒ 按「失败集合对差**净新增 0**」判据取证：

```
基线 run 44901528802（7994da68，11:57Z）：失败 126 条   ← 此前最近一次非 cancelled 的 main push run
PR  run 44917177808（053b5de0，15:04Z）：失败 125 条
⇒ 净新增 3 条 / 净丢失 4 条 ⇒ 工具判 NEW_FAILURES
```

### 净新增 3 条的逐条归因（`ci_failure_set` 的「基线陈旧」提示指向此处）

| # | 失败用例 | 根因 | 归属 |
|---|---|---|---|
| 1 | `test_ci_codeowners.py::test_every_ci_surface_file_in_the_pre_collection_closure_has_a_codeowner` | **本批引入**：本批新增 `backend/app/utils/like_escape.py` 并被 `wiki_link_service` / `wiki_page_service` import ⇒ `app/utils/__init__.py`（`ci_surface.py:171` 显式钉住的包 init）**第一次**进入「收集前闭包」⇒ 而 CODEOWNERS 里没有它（其余包 init `app/`·`app/domain/`·`app/models/`·`app/services/`·`app/infrastructure/` 都有）⇒ 派生守卫红 | **本批（#172）** |
| 2 | `test_agent_tool_assembly.py::TestHandlerRegistries::test_builtin_has_expected_keys` | 采购线 I3 批次把 `contract_document_search` / `quotation_compare` 加进 `BUILTIN_HANDLERS`，但本用例是**集合相等型**断言 ⇒ 期望集没同步 | 采购线（`5899a189` / `72c9b957`） |
| 3 | `test_agent_tool_assembly.py::TestHandlerRegistries::test_arg_extractors_expected_keys` | 同上（`ARG_EXTRACTORS` 同期新增两个键） | 采购线（同上） |

**归因取证（可复现）**：以 `ci_conftest_chain.transitivePreCollectionPaths` 派生闭包，逐 commit 二分：

| commit | 闭包规模 | 含 `app/utils/__init__.py` |
|---|---|---|
| `7994da68`（基线） | 108 | ✗ |
| `b3e12b4b`（#173） | 109 | ✗ |
| `053b5de0`（**#172 本 PR**） | 111 | **✓** |

⇒ 闭包从 108→111 由**本批**贡献（新增 `app/utils/__init__.py` + `app/utils/like_escape.py`），
`5899a189` / `72c9b957` 未贡献闭包变化。

### 净丢失 4 条的确认（**不是**「把失败藏起来了」）

`test_seed_menu_config.py` 的 4 条用例在基线红、现在绿 ⇒ 是被 `72c9b957` **修好**的：
该用例写死了 `count == 53` / `sections == 8` / `total_items == 44`，而菜单 seed 早已增长到 64（加 Copilot 后 65）
⇒ 属**存量红基线**，该 PR 把计数改为从 `SECTIONS`/`ITEMS` 常量推导 ⇒ 绿是真绿。

### 补修内容（本批）

1. `.github/CODEOWNERS` 补 `backend/app/utils/__init__.py @sunql`（与其它包 init 条目同段）。
2. **新增先验守卫** `test_every_ci_surface_package_init_has_a_codeowner`
   —— 上一条闭包守卫是**事后**的（只有文件**已经进闭包**才红）；本条改成先验：
   凡被 CI 相关面 pattern 命中的**包 init**，无论在不在闭包内，都必须有 owner
   ⇒ 下次再往 `app/utils/` 之类目录加模块时，在**进闭包之前**就被挡住。
   （含反假绿断言 `assert pinned` ⇒ 清单被清空时红。）
3. `test_agent_tool_assembly.py` 两个**集合相等型**期望集补上 `contract_document_search` / `quotation_compare`
   ⇒ main 上不再带这两个真红。

### 验证

| 套件 | 结果 |
|---|---|
| `app/tests/unit/test_ci_codeowners.py` | 9 passed（改前 1 failed / 7 passed，**RED 已目视**） |
| `app/tests/unit/test_agent_tool_assembly.py` | 12 passed |
| 全部 CI 守卫单测（`-k "ci_surface or ci_codeowners or ci_conftest or ci_shard or ci_failure or ci_flake or ci_tool or ci_mutat"`） | **524 passed** |

> ⚠️ 改 `.github/CODEOWNERS` 与 `test_ci_*.py` **本身就是 CI 相关面**（`ci_surface.py` 显式收录）
> ⇒ 本 PR 的 merge-gate 会判 **exit 6（CI_SURFACE）** ⇒ 属**预期的**人工仲裁面，不是失败。
