# 2026-11-19 生产部署 main @ c959b77f（#154 + #155 + #156 一揽子）

## 背景与用户指令

用户裁决（2026-11-19）：**把目前最新的代码（含两个 bug 修复）部署到生产**；
「本地部署按合并后 main、生产热修单独部署」中的生产侧本次落地。
- main `c959b77f` = 现役生产基线 `86ff4a1f`（1116 热修）之后合入的全部：
  **#150** P5 工作流前端、**#151** 本体五 Tab 分页、**#154** 401 清理、
  **#155** 改名泰鸿智脑、**#156** Milvus HNSW ef/limit 修复。
- 本 runbook 取代 `prod/brand-rename-1119`（base `e8f49588` + 仅改名）的计划——
  本次 main 一揽子已包含改名，前端镜像直接从 `c959b77f` 构建。
- **采购线不发生产**：已核实 `c959b77f` 库内迁移最高 **0152**（无 0154–0158）、
  无采购 binding/域发布代码；`procurement_knowledge.py` 是 main 既有的 V0.2 采购知识查询路由，非本线。

## 交付物（均已构建并自证）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-backend-amd64-1119-main.tar.gz` | `qa-system-backend:amd64-1119-main` | `8ae7a41acfff6ee0c9187cc2dc07bab6d98a92a08211c10e9f06f08a2d76d91f` | 219M |
| `/tmp/qa-frontend-amd64-1119-main.tar.gz` | `qa-system-frontend:amd64-1119-main`（bundle `index-BVutQwOS.js`） | `851c796c8c310087a4cc08a11e60839bf8a0dc6a8c2b62aa57528bf2198201ae` | 26M |

- 构建方式：`git worktree`（detach `c959b77f`）+ `docker build --platform linux/amd64`
  （frontend 带 `--build-arg NPM_REGISTRY=https://registry.npmmirror.com`）。
- 前端自证：title/bundle/favicon 探针全绿（改名 5 处生效、旧名归零、css 与现役同源）。
- 后端自证：迁移 ≤0152、`hnswSearchParams` ×4、helper import 可用。

## Runbook

`deploy-prod-main-1119.md`（同目录）——沿用 1119 改名 runbook 的 Phase 结构，差异点：

1. **后端也换** ⇒ 新增 pg_dump 备份锚点（1116 以来首次带 DDL 上生产）。
2. **迁移 0148→0152**：4 个纯加法迁移（P5 workflow 6 表 + audit_log CHECK 放宽），全部有 downgrade。
3. **★回滚陷阱**：旧 backend 镜像 CMD `alembic upgrade head` 在库=0152 时报
   「Can't locate revision」起不来 ⇒ **回滚必须先用新镜像一次性 `alembic downgrade 0148`
   再换回旧 tag**（downgrade 会 drop 6 张 P5 表，窗口内审批数据丢失、有 pg_dump 兜底）。
   前端可单独回滚，无 DB 依赖。
4. 生产无 worker ⇒ P5 超时/补偿后台巡检不生效（API 可用），本批不新增 worker 容器。
5. ef 修复验收：容器 grep `hnswSearchParams` + 跑一轮 wiki 注入 chat 后日志零
   `should be larger than k` / `65535`。

## 状态

- 开发机侧：**完成**（镜像 + runbook 已产出，等用户在生产宿主机执行）。
- 生产侧：待用户执行 Phase 0–6；执行结果回填本目录。

## 2026-11-19 生产预检实测

- ✅ 前端现役 bundle = `index-rBu8Smfz.js`（基线未漂移）——用户已确认预检通过到 ③。
- ⚠️ **生产库 `alembic_version` = 0143，不是预期的 0148**：0144–0148（WP-ONT-AI A1 线）
  当时标「不部署」，1116 热修未跑迁移。**裁决：放行升级**——9 个迁移（0144–0152）逐个
  核过内容，全部加法/可逆/零数据写入；且新 ORM 已声明这些表，`missing_table` blocking，
  不升库新镜像起不来。runbook 预期值已全量改写（0148→0143，迁移表扩为 9 行，
  回滚目标 0143，downgrade 会 drop 8 张表 + 删 4 个可空列）。
- runbook 占位符坑：`<POSTGRES_USER>` 被用户原样执行撞 bash 重定向 ⇒ 已全部改为
  `docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" ...'`（容器内展开环境变量，
  不占宿主 shell、不碰 .env）。
