# 变更：feat-wiki-search-endpoint（wiki 知识条目独立检索入口）

- **日期**：2026-09-16
- **Phase**：feature（业务用户把 wiki 当知识库用的查询入口）
- **状态**：done（后端已部署验证；前端待 docker compose build 持久化）
- **触发**：用户问「wiki 的知识条目能单独类似知识库一样查询吗？」
- **MEMORY**：qa-system-wiki-search-endpoint.md（本会话写入）

---

## 1. 需求

`WikiPageService.searchPages()`（ILIKE 标题+正文）早已实现，但只对 Agent 暴露
（`wiki_search` 工具），没有独立 HTTP 端点，前端管理页也没有搜索框。需要：
像知识库一样「输入关键词 → 检索 → 点进详情」。

## 3. 数据模型变更

无迁移。纯路由 + 前端。

## 4. 接口契约变更

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/v1/wiki/pages/search` | `query` 必填（strip 后非空），`dimension` 可选，`limit/offset` 分页；`total` = **命中数**（非总量，与 listPages 语义不同） |

## 5. 实现要点

- 后端 `wiki.py`：`/pages/search` **必须声明在 `/pages/{pageId}` 之前**（路径参数吞路由，老坑）；空白 query 在端点内 strip 校验后抛 `RequestValidationError` → 422（`min_length=1` 挡不住 `"  "`）
- 前端 `api/wikiPages.ts`：`searchWikiPages({query, dimension?, limit?, offset?})`
- 前端 `AdminWikiPagesPage.tsx`：`Input.Search`（回车/按钮提交）；检索模式与状态过滤**互斥**——检索端点不支持 status，检索生效时禁用状态下拉而非静默忽略
- `fetchList` 按 `searchQuery.trim()` 是否非空分流 `searchWikiPages` / `listWikiPages`

## 6. 测试

| 套件 | 结果 |
|---|---|
| 后端 integration `test_wiki_api.py` +6 用例（标题/正文命中、空白 422、dimension 过滤+非法 422、分页 total=命中数、无命中空数组） | ✅ 28/28 |
| 前端 vitest `wikiApi.test.ts` +2 用例（路径/query 形状、可选参数缺省） | ✅ 26/26 |
| `tsc --noEmit` | ✅ 0 errors |
| 真机 curl（容器内 prod 库） | ✅ 命中「采购供应商黑名单管理规范」；空白 query 422 |

### vitest 泄漏 once 队列的级联（本节要点）

首轮 RED 时 `searchWikiPages is not a function`，但 `wikiCoverage` 的 2 个无关
测试也挂了。根因：**失败的测试注册了 `mockResolvedValueOnce` 却没消费**，而
`vi.clearAllMocks()`（mockClear）不清 once 队列 → 队列错位，后续 describe 的
once 被前面的残留顶掉。实现函数后自然消失。教训：看到「不相关的测试突然挂」
先怀疑 mock once 队列泄漏，而不是去改那些测试。

## 7. 安全审查

只读端点，无写路径；`query` 经 `_escapeLike` 转义（service 层已有）；无 ACL 变更（与 listPages 同级）。

## 8. 部署验证

```bash
docker cp backend/app/api/v1/wiki.py qa-backend:/app/app/api/v1/wiki.py
docker restart qa-backend
# curl search → 命中 prod 数据；blank → 422
```

**docker cp 是临时的**：容器重建即回退。持久化需重建镜像（后端
`./scripts/deploy_backend.sh`；前端改了 bundle 必须
`docker compose build --no-cache frontend && up -d frontend`）。

## 9. 关联

- `backend/app/api/v1/wiki.py`（+searchPages 路由）
- `backend/app/services/wiki_page_service.py`（searchPages 复用，零改动）
- `backend/app/tests/integration/test_wiki_api.py`（+6）
- `frontend/src/api/wikiPages.ts`（+searchWikiPages）
- `frontend/src/pages/AdminWikiPagesPage.tsx`（+Input.Search + 模式互斥）
- `frontend/src/i18n/zh-CN.ts` / `en-US.ts`（+wikiPages.filters.search）
- `frontend/src/tests/wikiApi.test.ts`（+2）
- 关联变更：[feat-wiki-knowledge](../feat-wiki-knowledge/)（searchPages 的出处）

### 元教训

- **vi.clearAllMocks 不清 mockResolvedValueOnce 队列**：失败的测试留下未消费
  的 once，级联污染同文件后续测试。看到无关测试挂，先查队列泄漏。
