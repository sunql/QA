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
