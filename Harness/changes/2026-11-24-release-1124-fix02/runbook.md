# 生产发版 1124-fix02：Copilot 场景体验全量（复用/名称/描述/交互打磨）

- **代码版本**：`feat/ontology-evolution`（1895e3eb..HEAD，pending01 之后的全部修复；**未合 main 即部署**，同 pending01 先例）
- **部署日期**：2026-11-24（开发机产出镜像与本文档，宿主机逐条执行）
- **本次动**：**backend + agent-orchestration-worker + frontend 三个容器**
  （worker 与 backend 同镜像；backend 含方言适配 + 友好错误修复）
- **迁移**：**零迁移**（alembic 保持 **0169**）
- **种子**：**无需**
- **范围（2026-11-24 口径）**：
  ① **首页问题**（打开 Copilot 自动恢复旧会话 ⇒ 跨账号 detail 404 + 「默认进到待到货」；
     改为落在场景卡片墙，历史从侧栏恢复）；
  ② **隐藏历史对话**（侧栏默认收起，浮钮展开）；
  ③ **随 backend 一并带上**（同一后端镜像发布，成本为零）：MySQL 方言适配
     （生产 1248 'Every derived table must have its own alias'）、数据源失败友好化
     （502 + 中文 detail，不再显示「网络异常」）、**DELETE 会话接口**
     （侧栏删除按钮本次即可用）、run 级同日复用、供应商名称 + 物料描述列。
- **前置**：**1124-pending01 已部署**（本批不含 pending-delivery 工具首装与端点首注册的验收依赖其存在）
- **不动**：postgres / neo4j / milvus / ollama / mysql-demo；不改 `.env`

---

## 0. 一句话说清这次改了什么

| 主题 | 内容 |
|---|---|
| **① 首页修复** | 移除「打开 Copilot 自动恢复上次会话」：跨账号 detail 404 消除；不再「默认进到待到货」，落在场景卡片墙；历史会话从侧栏一键恢复 |
| **② 隐藏历史对话** | 侧栏默认**收起**（浮钮展开） |
| **③ 后端随包** | MySQL 方言适配（1248）；数据源失败 ⇒ 502 + 中文 detail；**DELETE 会话接口**（删除按钮本次可用）；run 级同日复用；供应商名称 + 物料描述 |
| **（随 bundle 的 UI 打磨）** | 标题带场景名、欢迎态统计卡+chips、结果卡三块 Collapse、轨迹 Timeline、进度弹窗、Tabs 纯切换器、输入框边框、参数单行+重置、.pd-table 表格风格 |

**验收标准**：
1. `alembic_version` 仍为 **0169**；三容器 Image ID 与 §1 一致；health 200；
2. Copilot 进待到货：标题「采购 Copilot · 待到货 / 逾期未到」；业务现状卡出数；
3. 同一问题问两遍：第二次**秒回**（结果与第一次相同），后台 run 数不增加（§6 SQL）；
4. 汇总/明细表显示供应商**公司全名**与**物料描述**；导出 CSV 含名称/描述列；
5. 轨迹抽屉为时间线分段（Markdown 正文）；发送时有进度弹窗、出结果即消失；
6. 对话框指定供应商（如「分析B019…」）时，即使高级参数选了别的供应商，执行结果与文字一致。

---

## 1. 交付物（开发机已产出并自证）

> **构建方式（2026-11-24 起）**：后端拆出**基础镜像**（系统包层预构建）
> —— `docker/Dockerfile.backend.base` → `qa-system-backend-base:2026-11-amd64`，
> 业务镜像引用它。构建命令：
> ```bash
> ./scripts/build_backend_images.sh --platform linux/amd64 --tag amd64-1124-fix02
> ./scripts/build_frontend_image.sh --platform linux/amd64 --tag amd64-1124-fix02
> ```
> 业务层重建 **1–3 分钟**（此前每次 15–25 分钟：apt 装 ~200 个 deb 且随业务代码
> 变动缓存失效）。基础镜像**每 platform 只建一次**；`docker save` 业务镜像自带
> 父层 ⇒ 生产仍只需传一个 tar。

| 文件                                         | 镜像                                    | 平台          | sha256                                                             | 大小  |
| ------------------------------------------ | ------------------------------------- | ----------- | ------------------------------------------------------------------ | --- |
| `/tmp/qa-backend-amd64-1124-fix02.tar.gz`  | `qa-system-backend:amd64-1124-fix02`  | linux/amd64 | `a9bc686a2864f01ff3cd1bb8b8f37164b7da26874b7d2bf1202e53ea926a385c` | 490M |
| `/tmp/qa-frontend-amd64-1124-fix02.tar.gz` | `qa-system-frontend:amd64-1124-fix02` | linux/amd64 | `b4df9f6c4215a15cd5e87a3fbfcd30c6d53e96ca975d80c97a756bbaa6f935e2` | 26M |

镜像标识：backend `e7d03546e2d8`｜frontend `276bf0e1447a`；前端 bundle `index-Dd2cXydV.js`
（基础镜像 `qa-system-backend-base:2026-11-amd64` = `6359bbf4d394`，已含在后端 tar 内）

镜像内自证（开发机已跑，输出见交付记录）：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1124-fix02 -c '
  grep -c "_PendingDialect" /app/app/services/agent_tools_procurement.py
  grep -c "该数据源执行待到货查询失败" /app/app/api/v1/procurement_analytics.py
  grep -c "deleteCopilotConversation" /app/app/api/v1/procurement_copilot.py
  which tesseract; which libreoffice'
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-fix02 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "分析内容" /usr/share/nginx/html/assets/index-*.js
  grep -c "历史会话" /usr/share/nginx/html/assets/index-*.js'
# 实测：backend dialect=13 / friendly=1 / delete_api=1 / tesseract+libreoffice 均在；
#       frontend bundle=index-Dd2cXydV.js / content_tag=1 / sidebar=1
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
# Image ID 与 §1 一致（backend e7d03546e2d8 / frontend 276bf0e1447a）
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

**种子**：本批**无需新增种子**（工具 schema / profile 绑定 / ACL 沿用 pending01 已落库状态）。
若 pending01 的 SOURCING 指令更新脚本当时未执行，补跑 pending01 runbook §5 的指令脚本（幂等）——
本批后端带 DELETE 会话接口，删除按钮随之可用。

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
4. 打开 Copilot 不再出现 conversations/{id} 404；落在卡片墙；
5. 侧栏默认收起，浮钮展开；展开后可恢复历史（删除对旧后端弹「暂不支持」提示）。

## 7. 回滚（零迁移，30 秒级）

```bash
# 仅回前端
docker tag qa-system_frontend:pre-1124-fix02 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend

# 全量回滚（库不动，本批零迁移）
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
- ❌ **不含**：run 级同日复用 / 供应商名称 / 物料描述 / MySQL 方言（后端批次单独发布；
  方言适配已 revert 待重做）；本包 = 纯前端；
- ❌ 不改工具 schema / profile / 指令（pending01 已落库）。
