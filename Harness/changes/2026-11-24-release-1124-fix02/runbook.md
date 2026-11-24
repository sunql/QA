# 生产发版 1124-fix02：Copilot 场景体验全量（复用/名称/描述/交互打磨）

- **代码版本**：`feat/ontology-evolution`（1895e3eb..HEAD，pending01 之后的全部修复；**未合 main 即部署**，同 pending01 先例）
- **部署日期**：2026-11-24（开发机产出镜像与本文档，宿主机逐条执行）
- **本次动**：**仅 frontend 一个容器**（backend / worker 保持 pending01 镜像不动）
- **迁移**：**零迁移**（alembic 保持 **0169**）
- **种子**：**无需**
- **范围（2026-11-24 口径）**：本包只解决 **①首页问题**（打开 Copilot 自动恢复旧会话
  ⇒ 跨账号 detail 404 + 「默认进到待到货」；改为落在场景卡片墙，历史从侧栏恢复）
  与 **②隐藏历史对话**（侧栏默认收起，浮钮展开）。其余已本地验证的功能
  （run 级复用 / 供应商名称 / 物料描述 / MySQL 方言等）**不在本包**，随后端批次发布；
  侧栏「删除会话」按钮已做降级提示（旧后端无 DELETE 端点 ⇒ 点删除弹「暂不支持」）
- **前置**：**1124-pending01 已部署**（本批不含 pending-delivery 工具首装与端点首注册的验收依赖其存在）
- **不动**：postgres / neo4j / milvus / ollama / mysql-demo；不改 `.env`

---

## 0. 一句话说清这次改了什么

| 主题 | 内容 |
|---|---|
| **① 首页修复** | 移除「打开 Copilot 自动恢复上次会话」：跨账号 detail 404 消除；不再「默认进到待到货」，落在场景卡片墙；历史会话从侧栏一键恢复 |
| **② 隐藏历史对话** | 侧栏默认**收起**（浮钮展开）；删除按钮对旧后端降级提示 |
| **（随 bundle 的 UI 打磨）** | 以下已本地验证、随 bundle 生效：标题带场景名、欢迎态统计卡+chips、结果卡三块 Collapse、轨迹 Timeline、进度弹窗、Tabs 纯切换器、输入框边框、参数单行+重置、.pd-table 表格风格 |

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
| `/tmp/qa-frontend-amd64-1124-fix02.tar.gz` | `qa-system-frontend:amd64-1124-fix02` | linux/amd64 | `0e2a088a1516c7ab158677f7546a4421377533c23abc76818162137fa9f34067` | 26M |

镜像标识：frontend `3caa4e9db431a`；前端 bundle `index-Cynv99we.js`

镜像内自证（开发机已跑，输出见交付记录）：

```bash
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-fix02 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "分析内容" /usr/share/nginx/html/assets/index-*.js
  grep -c "历史会话" /usr/share/nginx/html/assets/index-*.js
  grep -c "暂不支持删除会话" /usr/share/nginx/html/assets/index-*.js'
# 实测：frontend bundle=index-Cynv99we.js / content_tag=1 / sidebar=1 / del_hint=1
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

docker load -i qa-frontend-amd64-1124-fix02.tar.gz
docker tag qa-system-frontend:amd64-1124-fix02  qa-system_frontend
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E 'qa-backend:offline|qa-system_frontend'
# Image ID 与 §1 一致
```

---

## 5. Phase 3 切换（backend → worker → frontend）

```bash
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep frontend
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
4. 打开 Copilot 不再出现 conversations/{id} 404；落在卡片墙；
5. 侧栏默认收起，浮钮展开；展开后可恢复历史（删除对旧后端弹「暂不支持」提示）。

## 7. 回滚（零迁移，30 秒级）

```bash
docker tag qa-system_frontend:pre-1124-fix02 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
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
