# 生产发版 1124-fix02：Copilot 场景体验全量（复用/名称/描述/交互打磨）

- **代码版本**：`feat/ontology-evolution`（1895e3eb..HEAD，pending01 之后的全部修复；**未合 main 即部署**，同 pending01 先例）
- **部署日期**：2026-11-24（开发机产出镜像与本文档，宿主机逐条执行）
- **本次动**：**backend + agent-orchestration-worker + frontend 三个容器**（worker 同 backend 镜像）
- **迁移**：**零迁移**（alembic 保持 **0169**）
- **种子**：**无需**（工具 schema / profile 绑定 / ACL 在 pending01 已落库）
- **前置**：**1124-pending01 已部署**（本批不含 pending-delivery 工具首装与端点首注册的验收依赖其存在）
- **不动**：postgres / neo4j / milvus / ollama / mysql-demo；不改 `.env`

---

## 0. 一句话说清这次改了什么

| 主题 | 内容 |
|---|---|
| **同日复用（后台判断）** | ①工具层：三段查询结果按「过滤参数+数据源+当天」缓存至当日午夜；②run 级：同用户+同 Agent+同模型+同问题文本、当日、状态∈{QUEUED,RUNNING,SUCCEEDED} ⇒ 新 turn 直接指向既有 run（**零 LLM 成本**）；FAILED/CANCELLED 不复用；同会话**不同追问**仍带上下文新跑。**前台正常问话即可，无需任何操作** |
| **数据显示** | 供应商名称（LEFT JOIN BPSUPPLIER 带出 BPSNAM_0，如 J1=河北新泰鸿汽车零部件有限公司）；TOP 行表加**物料描述**列（LEFT JOIN ITMMASTER 带出 ITMDES1_0）；长文本完整换行展示 |
| **冲突优先级** | 对话框文字提到的维度（含**纯编码** B109/T3 等），高级参数对应槽位让位；尾注带仲裁指令「冲突以用户问题描述为准」 |
| **交互打磨** | 标题带场景名（采购 Copilot · 待到货 / 逾期未到）；chips 仅初次进入显示；结果卡三块 Collapse（概览默认展开）；轨迹抽屉改 Timeline 分段（Markdown 正文）；发送后进度弹窗；Tabs 纯切换器（只渲染激活项）；输入框边框加重带色；会话 id 隐藏 |
| **修复** | BySupplier 外层误引内层别名 Q（ORA-00904/500）；会话自动恢复跨账号 404（改为不自动恢复，侧栏恢复） |

**验收标准**：
1. `alembic_version` 仍为 **0169**；三容器 Image ID 与 §1 一致；health 200；
2. Copilot 进待到货：标题「采购 Copilot · 待到货 / 逾期未到」；业务现状卡出数；
3. 同一问题问两遍：第二次**秒回**（结果与第一次相同），后台 run 数不增加（§6 SQL）；
4. 汇总/明细表显示供应商**公司全名**与**物料描述**；导出 CSV 含名称/描述列；
5. 轨迹抽屉为时间线分段（Markdown 正文）；发送时有进度弹窗、出结果即消失；
6. 对话框指定供应商（如「分析B019…」）时，即使高级参数选了别的供应商，执行结果与文字一致。

---

## 1. 交付物（开发机已产出并自证）

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1124-fix02.tar.gz` | `qa-system-backend:amd64-1124-fix02` | linux/amd64 | `1bba940acface3d98b74c0374bb9f4d7eb8faae8b6d1b61cf29e22298d1ceb08` | 490M |
| `/tmp/qa-frontend-amd64-1124-fix02.tar.gz` | `qa-system-frontend:amd64-1124-fix02` | linux/amd64 | `6a6a61c616d812cdebcfa655d77cf21b92c01785e2fe67b72c9f4c91fc4356bf` | 26M |

镜像标识：backend `54b8bd651cbd`｜frontend `f9cad6e0e1d5`；前端 bundle `index-BHKWEk8R.js`

镜像内自证（开发机已跑，输出见交付记录）：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1124-fix02 -c '
  grep -c "supplierName" /app/app/services/agent_tools_procurement.py
  grep -c "BPSUPPLIER" /app/app/services/agent_tools_procurement.py
  grep -c "_sameDayReusableRunId" /app/app/services/procurement_copilot.py
  grep -c "_PENDING_RESULT_CACHE" /app/app/services/agent_tools_procurement.py'
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-fix02 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "分析内容" /usr/share/nginx/html/assets/index-*.js
  grep -c "正在分析，请稍候" /usr/share/nginx/html/assets/index-*.js
  grep -c "待到货 / 逾期未到" /usr/share/nginx/html/assets/index-*.js'
# 实测：backend supplierName=2 / bps=1 / reuse=2 / cache=3；
#       frontend bundle=index-BHKWEk8R.js / content_tag=1 / progress=1 / scenario_title=1
```

---

## 2. Phase 0 生产预检（只读）

```bash
# ⓪ compose 入口（label 权威）
docker inspect qa-backend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config | grep 'image:' | grep -E 'backend|frontend'

# ① 现役容器/镜像（pending01 后应为 amd64-1124-pending01 派生 tag）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|worker|frontend'

# ② 前置核对：pending01 已在（两端点存在 ⇒ 403）
curl -s -o /dev/null -w "summary=%{http_code}\n" http://localhost:8000/api/v1/procurement/pending-delivery/summary

# ③ alembic（非 0169 = 停）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# ④ 健康 + 磁盘
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
df -h /var/lib/docker | tail -1
```

**停下条件**：③ 非 **0169**；② summary=404（说明 pending01 未完整落地）；④ 任一非 200；磁盘 < 5G。

---

## 3. Phase 1 回滚锚点（零迁移 ⇒ 无需 pg_dump）

```bash
docker tag qa-backend:offline       qa-backend:pre-1124-fix02
docker tag qa-system_frontend       qa-system_frontend:pre-1124-fix02
docker images | grep pre-1124-fix02
```

> 回滚边界 = 现役 pending01 镜像；库不动。PRE_VER = 0169（本批无迁移）。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

```bash
sha256sum qa-backend-amd64-1124-fix02.tar.gz qa-frontend-amd64-1124-fix02.tar.gz
# 与 §1 开发机值比对，不一致 ⇒ 停

docker load -i qa-backend-amd64-1124-fix02.tar.gz
docker load -i qa-frontend-amd64-1124-fix02.tar.gz
docker tag qa-system-backend:amd64-1124-fix02   qa-backend:offline
docker tag qa-system-frontend:amd64-1124-fix02  qa-system_frontend
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E 'qa-backend:offline|qa-system_frontend'
# Image ID 与 §1 一致
```

---

## 5. Phase 3 切换（backend → worker → frontend）

```bash
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 | grep -E "Application startup complete|ERROR|Can't locate" | head -5

qa_compose up -d --no-build --no-deps --force-recreate agent-orchestration-worker
docker logs --tail 5 qa-agent-orchestration-worker 2>&1 | grep -E "启动|Error" | head -3

qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|worker|frontend'
```

**种子**：本批**无需任何种子**（工具 schema / profile 绑定 / ACL 沿用 pending01 已落库状态；
工具与 profile 均无变更，`git diff 1895e3eb..HEAD -- backend/scripts/` 为空）。
若 pending01 的 SOURCING 指令更新脚本当时未执行，补跑见 pending01 runbook §5（幂等）。

---

## 6. Phase 4 验收探针

```bash
# ① 后端
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                    # 0169
curl -s -o /dev/null -w "health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "summary=%{http_code}\n" http://localhost:8000/api/v1/procurement/pending-delivery/summary   # 403=已注册
curl -s -o /dev/null -w "sites=%{http_code}\n"   http://localhost:8000/api/v1/procurement/pending-delivery/sites     # 403

# ② 前端
docker exec qa-frontend sh -c '
  h=/usr/share/nginx/html
  echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
  echo content_tag=$(grep -c "分析内容" $h/assets/index-*.js)
  echo progress=$(grep -c "正在分析，请稍候" $h/assets/index-*.js)
  echo title_scene=$(grep -c "采购 Copilot · " $h/assets/index-*.js)'
```

**浏览器端到端（硬刷新）**：

1. 标题「采购 Copilot · 待到货 / 逾期未到」；业务现状卡出数（供应商带全名）；
2. 点 chip 跑一轮 → 进度弹窗出现 → 出结果后消失 → 结果卡三块折叠（概览展开）；
3. 汇总/明细表显示公司全名 + 物料描述；导出 CSV 含名称/描述列；
4. **同日复用**：同一问题再问一遍 → 秒回相同结果；随后查 run 数：
   ```bash
   docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
   select count(*) as runs from agent_orchestration_run r
   join procurement_copilot_turn t on t.run_id = r.run_id
   where r.agent_code='"'"'PROCUREMENT_SOURCING'"'"'
     and r.created_time >= date_trunc('"'"'day'"'"', now());"'
   # 两次相同提问后 runs 仍为 1（复用不新增 run）
   ```
5. 换个供应商问 → 正常新跑（run 数 +1）；
6. 轨迹抽屉 = 时间线分段 Markdown；发送时有进度弹窗；
7. 对话框指定供应商 vs 高级参数选别的供应商 → 执行与文字一致（冲突以文字为准）。

## 7. 回滚（零迁移，30 秒级）

```bash
# 仅回前端
docker tag qa-system_frontend:pre-1124-fix02 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend

# 全量回滚
docker tag qa-backend:pre-1124-fix02 qa-backend:offline
docker tag qa-system_frontend:pre-1124-fix02 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend agent-orchestration-worker frontend
docker logs --tail 3 qa-backend | grep "Application startup complete"
```

> 库保持 0169 ⇒ 无需 downgrade。复用产生的 turn 指向旧 run，回滚后旧代码
> 读取不受影响（数据结构无变更）。

## 8. 本批明确没做的事

- ❌ 不动数据容器与 `.env`；无迁移、无种子；
- ❌ 不合 main（分支部署先例；合并走 PR #178）；
- ❌ 不做欠交明细常驻页（P2）、侧栏搜索/分组（P3）、结果缓存跨天持久化；
- ❌ 不改工具 schema / profile / 指令（pending01 已落库）。
