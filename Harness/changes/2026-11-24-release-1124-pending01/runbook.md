# 生产发版 1124-pending01：待到货场景（欢迎态 + 高级过滤 + 导出）+ 会话侧栏/删除

- **代码版本**：`feat/ontology-evolution @ 125aeebc`（PR #178 后半段 4 commits，9c8b5701..125aeebc；**未合 main 即部署，与 1122 热修同先例**）
- **部署日期**：2026-11-24（开发机产出镜像与本文档，宿主机逐条执行）
- **本次动**：**backend + agent-orchestration-worker + frontend 三个容器**（worker 与 backend 同镜像，一起 recreate）
- **迁移**：**零迁移**（alembic 保持 **0169**；本批 `backend/alembic/` 无变更）
- **不动**：postgres / neo4j / milvus / ollama 数据容器；不改 `.env`（本批无开关变化）

---

## 0. 一句话说清这次改了什么

| PR/commit | 内容 |
|---|---|
| `709b68f2` | Copilot 场景卡片墙 + 场景钉 Agent；待到货欢迎态（业务现状卡零 LLM 成本 + 推荐问题 + 底部输入栏） |
| `deb4c03c` | 待到货高级过滤：供应商/物料/基地**多选**（编码精确 IN + 中文名模糊 LIKE / MDM 解析）+ 时间窗「按月\|按时间段」+ 明细导出 CSV |
| `125aeebc` | 修复 BySupplier SQL 外层误引内层别名 Q（ORA-00904 ⇒ 统计卡 500） |
| `be9b1294` | （纯 Harness 文档，随包） |

**新端点**：`GET /api/v1/procurement/pending-delivery/summary`（登录即可）、`GET /api/v1/procurement/pending-delivery/sites`。

**验收标准**：
1. `alembic_version` 仍为 **0169**；
2. 三个容器均为新镜像（Image ID 与 §1 一致），health 200；
3. Copilot 首页场景卡片墙；进「待到货」见欢迎态：**业务现状卡出数**（欠交 9,828 行量级）、4 个推荐问题可点击运行；
4. 高级参数：供应商/物料搜索多选、基地多选（滨海总厂等中文名）、时间窗按月/按时间段切换；
5. 跑一轮后结果卡出现「待到货分析结果」（统计+两表）+「解读与建议」Markdown 卡 +「导出明细(CSV)」；
6. 「查看执行轨迹」抽屉步骤为终态（SUCCEEDED 等）；会话侧栏可切换/删除历史会话。

---

## 1. 交付物（开发机已产出并自证）

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1124-pending01.tar.gz` | `qa-system-backend:amd64-1124-pending01` | linux/amd64 | `a2fda568d4a7bd18a92c71a932f382ca2ba03b54ac3f7c46eb9bd6366ce0bc18` | 490M |
| `/tmp/qa-frontend-amd64-1124-pending01.tar.gz` | `qa-system-frontend:amd64-1124-pending01` | linux/amd64 | `d54b528226925947f33c19d1808c91a3d4bf90fe76ffd4ce48b26f0e7bc974f3` | 26M |

镜像标识：backend `sha256:fa9854888fc5`（fa9854888fc5…，Image ID）｜frontend `sha256:196b5d64d250`
前端 bundle：`index-DEyxTT6t.js`

镜像内自证（开发机已跑，输出见 §summary）：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1124-pending01 -c '
  grep -c "pending_delivery_query" /app/app/services/agent_tools.py
  grep -c "PendingDeliveryFilters" /app/app/services/agent_tools_procurement.py
  grep -c "pending-delivery/summary" /app/app/api/v1/procurement_analytics.py'
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-pending01 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "你好，我是采购待到货分析助手" /usr/share/nginx/html/assets/index-*.js
  grep -c "导出明细" /usr/share/nginx/html/assets/index-*.js'
# 实测：backend tool_reg=2 / filters=9 / summary_api=1 / heal=6 / instr=1；
#       frontend bundle=index-DEyxTT6t.js / welcome=1 / export=2 / cardwall=1
```

---

## 2. Phase 0 生产预检（只读）

```bash
# ⓪ compose 入口（从现役容器 label 读出，唯一权威）
docker inspect qa-backend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config | grep 'image:' | grep -E 'backend|frontend'      # 回填引用名核对（应为 qa-backend:offline / qa-system_frontend）

# ① 现役容器与镜像（回滚锚点对照）
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|worker|frontend|mysql'

# ② 现役前端 bundle（记录备用）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ alembic 版本（不是 0169 = 停下发我）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'

# ④ 健康与磁盘
curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
df -h /var/lib/docker | tail -1
```

**停下条件**：③ 非 **0169**；④ backend/frontend 任一非 200；磁盘剩余 < 5G。

---

## 3. Phase 1 回滚锚点（零迁移 ⇒ 无需 pg_dump，但保留以防万一）

```bash
# ① 镜像锚（引用名自带 tag，不能再拼一层冒号）
docker tag qa-backend:offline       qa-backend:pre-1124-pending01
docker tag qa-system_frontend       qa-system_frontend:pre-1124-pending01
docker images | grep pre-1124-pending01

# ② 数据锚（零迁移本可省；有 DDL 习惯的批次才必须。本批保留为可选）
docker exec qa-postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > /opt/qa-system/backups/pg_pre_1124-pending01_$(date +%Y%m%d_%H%M).sql
ls -lh /opt/qa-system/backups/ | tail -2
```

> 三锚（pre- 镜像 / pg_dump / PRE_VER=0169）齐备才进 Phase 2。

---

## 4. Phase 2 传输 + 校验 + 加载 + tag

```bash
sha256sum qa-backend-amd64-1124-pending01.tar.gz qa-frontend-amd64-1124-pending01.tar.gz
# 与 §1 开发机值比对，不一致 ⇒ 停

docker load -i qa-backend-amd64-1124-pending01.tar.gz
docker load -i qa-frontend-amd64-1124-pending01.tar.gz
docker tag qa-system-backend:amd64-1124-pending01   qa-backend:offline
docker tag qa-system-frontend:amd64-1124-pending01  qa-system_frontend
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E 'qa-backend:offline|qa-system_frontend'
# Image ID 必须与 §1 一致
```

---

## 5. Phase 3 切换（backend → worker → frontend；worker 同镜像随 backend，显式列出防漏）

```bash
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 | grep -E "Application startup complete|ERROR|Can't locate" | head -5
# 期望：Application startup complete；无 Can't locate（库仍 0169，新镜像兼容）

qa_compose up -d --no-build --no-deps --force-recreate agent-orchestration-worker
docker logs --tail 5 qa-agent-orchestration-worker 2>&1 | grep -E "启动|Error" | head -3
# 期望：agent orchestration worker 启动（poll=… lease=…）

qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|worker|frontend'
```

**种子（三步，均幂等）**：

```bash
# ① 工具 schema：pending_delivery_query 新槽位（dateFrom/dateTo/三组多选）进 DB
qa_compose exec backend python -m scripts.seed_agent_tool_configs

# ② Agent ACL 愈合：SOURCING 自动补 PURCHASE_ORDER/ODS 授权（漏了 ⇒ 场景 PERMISSION_DENIED）
qa_compose exec backend python -m scripts.seed_agents

# ③ 编排绑定：把 pending_delivery_query 补进 SOURCING profile（漏了 ⇒
#    「请求的工具不在工具目录」——2026-11-24 生产实测，①②不含此步）
qa_compose exec backend python -m scripts.seed_procurement_agent_profiles
```

**SOURCING 编排指令更新（播种器只补工具不改指令，必须手动执行一次）**：

```bash
docker exec -i qa-backend python - <<'PY'
import asyncio
from scripts.seed_procurement_agent_profiles import PROFILE_SEEDS
from sqlalchemy import select
from app.domain.agent_orchestration_models import AgentOrchestrationProfile
from app.domain.models import AgentDefinition
from app.infrastructure.database import getSessionFactory

async def main():
    factory = getSessionFactory()
    async with factory() as session:
        rows = (await session.execute(
            select(AgentOrchestrationProfile, AgentDefinition.agent_code)
            .join(AgentDefinition, AgentDefinition.id == AgentOrchestrationProfile.agent_id)
            .where(AgentDefinition.agent_code == "PROCUREMENT_SOURCING")
        )).all()
        for profile, code in rows:
            instructions, _tools, _it, _tc = PROFILE_SEEDS[code]
            if profile.instructions != instructions:
                profile.instructions = instructions
                profile.revision += 1
                await session.commit()
                print(f"{code}: instructions updated (rev={profile.revision})")
            else:
                print(f"{code}: instructions already current")

asyncio.run(main())
PY
# 期望：PROCUREMENT_SOURCING: instructions updated（幂等，重复跑显示 already current）
```

> 指令变更含「输出纪律」（LLM 不复述数字，前端结构化卡承载）与「待到货分析」（工具调用引导）。不更新 ⇒ LLM 依旧输出数据墙（可读性回退），但功能不受影响。

---

## 6. Phase 4 验收探针

```bash
# ① 后端
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'                     # 0169
curl -s -o /dev/null -w "health=%{http_code}\n" http://localhost:8000/api/v1/health   # 200
# 新端点：403=已注册（登录守卫）；404=未注册
curl -s -o /dev/null -w "summary=%{http_code}\n" http://localhost:8000/api/v1/procurement/pending-delivery/summary
curl -s -o /dev/null -w "sites=%{http_code}\n"   http://localhost:8000/api/v1/procurement/pending-delivery/sites

# ② 前端
docker exec qa-frontend sh -c '
  h=/usr/share/nginx/html
  echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
  echo welcome=$(grep -c "你好，我是采购待到货分析助手" $h/assets/index-*.js)
  echo export=$(grep -c "导出明细" $h/assets/index-*.js)
  echo cardwall=$(grep -c "自由问答" $h/assets/index-*.js)'

# ③ 工具 schema 新槽位已入 DB
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select name, input_schema::text::jsonb ->> 0 is not null as has_schema
from agent_tool_config where name='"'"'pending_delivery_query'"'"';"'
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select jsonb_object_keys(input_schema::jsonb -> '"'"'properties'"'"') as prop
from agent_tool_config where name='"'"'pending_delivery_query'"'"';"'
# 期望含 dateFrom/dateTo/supplierKeys/materialKeys/siteKeys

# ④ SOURCING 授权（缺 ODS ⇒ 场景 PERMISSION_DENIED）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
select a.agent_code, p.data_object, p.data_layer
from agent_access_policy p join agent_definition a on a.id=p.agent_id
where a.agent_code='"'"'PROCUREMENT_SOURCING'"'"' order by 2,3;"'
# 期望含 PURCHASE_ORDER/ODS
```

**浏览器端到端（硬刷新 Ctrl/Cmd+Shift+R）**：

1. Copilot 首页 = 场景卡片墙（待到货/逾期未到 + 自由问答）；
2. 进入「待到货」→ 欢迎态：业务现状卡**出数**（J1/B125 级别的 TOP 供应商）、4 个推荐问题；
3. 点 chip「全部供应商概览」→ 轨迹出现 `pending_delivery_query` → 结果卡（统计+两表）+「解读与建议」+「导出明细(CSV)」可下载；
4. 高级参数：供应商搜中文名多选、基地勾中文名、时间窗切「按时间段」→ 发送后消息尾注带 `supplierKeys=[…]` 等；
5. 侧栏：历史会话切换/删除（删当前会话线程同时清空）。

## 7. 回滚（零迁移，30 秒级）

```bash
# 仅回前端（不动库，快速止血 UI 问题）
docker tag qa-system_frontend:pre-1124-pending01 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend

# 全量回滚（backend+worker+frontend）
docker tag qa-backend:offline  qa-backend:offline_pre 2>/dev/null || true
docker tag qa-backend:pre-1124-pending01 qa-backend:offline
docker tag qa-system_frontend:pre-1124-pending01 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend agent-orchestration-worker frontend
docker logs --tail 3 qa-backend | grep "Application startup complete"
```

> 库保持 0169 ⇒ 无需 downgrade；旧镜像起旧代码直接可用。

## 8. 本批明确没做的事

- ❌ 不动 postgres / neo4j / milvus / ollama / mysql-demo（Exit 1 已知，忽略）；
- ❌ 不改 `.env`（本批无开关变化）；
- ❌ 不做欠交明细常驻页（P2）与侧栏搜索/分组（P3）——待欢迎态验收后排期；
- ❌ 不合 main（分支部署，与 1122 热修同先例；合并走 PR #178 流程）。
