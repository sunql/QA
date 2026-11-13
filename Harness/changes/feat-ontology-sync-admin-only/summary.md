# ontology 同步写面收 admin-only

- 分支：`feat/ontology-sync-admin`，base = main `e85f1ff8`（PR #123 合并点）
- 状态：代码完成、待评审/合并；未部署生产（生产仍 0140 + 旧代码）
- 关联：`feat-kpi-vector-graph-sync`（PR #123 把 `POST /ontology/graph/sync-missing` 的写面
  扩大到 KPI 的 `id_mapping` 与 `KpiCatalog` 节点/边，本批补权限收口）

## 背景

PR #123 后，ontology 的四个「同步」端点都会改共享基础设施：

| 端点 | 写什么 |
|---|---|
| `POST /ontology/embeddings/sync` | 覆盖单条 Milvus 向量 |
| `POST /ontology/embeddings/sync-missing` | 批量补/重建 class/property/metric 向量 |
| `POST /ontology/graph/sync-missing` | 批量补 Neo4j 节点/边（本批已扩到 Metric/KpiCatalog） |
| `POST /ontology/classes/{id}/embedding` | 重算单类向量并覆盖 |

它们此前只要求登录（默认角色桩 = user + admin），任何登录用户都能触发全量重建。
读侧（list/search）不受影响，仍对登录用户开放。

## 变更

### Backend

- `backend/app/api/v1/ontology.py`：四个同步端点补
  `user: CurrentUser = Depends(requireAdmin)`，与同文件 CRUD 写入口一致。
- `backend/app/tests/integration/test_ontology_sync_admin_only.py`（新增，8 用例）：
  - 非 admin（`X-User-Roles: user`）⇒ 403，且 **service 未被调用**（防止「先干活再被拦」）；
  - admin ⇒ 正常返回（200/204），且确实走到对应 service；
  - service 入口用 spy 替换，不触真实 Milvus/Neo4j，本地可裸跑。

### Frontend

- `frontend/src/components/ontology/ClassTab.tsx`：非 admin 隐藏三个同步入口
  （头部「补同步缺失向量」「补图信息」、行内「同步向量」），沿用 `MetricTab` 的
  `useAuthStore` + `roles.includes("admin")` 模式；刷新/新增等入口不受影响。
- `frontend/src/tests/ClassTabAdminSync.test.tsx`（新增，3 用例）：非 admin 三入口不可见、
  刷新/新增仍可用（反向守卫：不是把整条工具条藏掉）；admin 三入口全可见。

## 验收

- 后端定向套件（`test_ontology_sync_admin_only.py`）：8 passed。
- 前端 `ClassTabAdminSync.test.tsx` + `OntologyPage.test.tsx`：38 passed；`tsc --noEmit` exit 0。
- 邻近后端套件回归：`test_ontology_api.py` / `test_ontology_reconcile_sync.py` /
  `test_kpi_reconcile_graph.py` 的失败与 base `e85f1ff8` **逐一相同**（共享测试库
  `ontology_class` 有历史残留行，`testListClassesEmpty` 等在同基线也红）⇒ 净新增失败 0。

## 未做

- 不改读侧权限、不动 `kpi-catalog` 已有 admin 入口。
- 零 DDL、零迁移；不部署生产。
- 共享测试库残留数据未清理（独立测试卫生项）。
