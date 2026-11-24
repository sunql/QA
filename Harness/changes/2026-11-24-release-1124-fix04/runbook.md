# 生产发版 1124-fix04：待到货明细页 + 侧栏增强 + 菜单

- **代码版本**：`feat/ontology-evolution @ 8787509d`
- **本次动**：**backend + frontend 两个容器**（worker 沿用现状，不动）
- **迁移**：**零**（alembic 仍 0169）
- **种子**：**必须跑 `seed_menu_config`**（新增菜单行 ⇒ 不跑 seed 就看不到菜单，这是本批最容易漏的一步）

---

## 0. 一句话说清这次改了什么

| # | 内容 |
|---|---|
| 1 | **P2 欠交明细页**：新路由 `/procurement/pending-delivery` + 采购菜单「待到货明细」；筛选（数据源/时间窗/供应商·物料·基地多选/最低逾期天数）+ 表格 + 服务端分页；新端点 `GET /procurement/pending-delivery/lines` |
| 2 | **P3 侧栏增强**：搜索框、按场景分组、置顶（localStorage） |
| 3 | 顺带修：axios 数组参数序列化成 `key[]=` 与后端逗号分隔对不上 ⇒ 过滤静默失效（两个 api 模块统一逗号分隔） |

**验收标准**：
1. 左侧「采购协同」下出现「**待到货明细**」菜单项（admin 可见）；
2. 点进去：表格出行（订单号 / 物料描述+编码 / 供应商名+码 / 欠交量 / 逾期天数 / 预计到货），筛选与分页可用；
3. Copilot 侧栏出现搜索框、场景分组标题、图钉（置顶）；
4. 结果卡新增「查看欠交明细」按钮 ⇒ 跳明细页。

---

## 1. 交付物

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-backend-amd64-1124-fix04.tar.gz` | `qa-system-backend:amd64-1124-fix04` | linux/amd64 | `09cafe44108f0fe746997bc88810951c7e688942d057163cd5ee6733a7c3b5d1` | 490M |
| `/tmp/qa-frontend-amd64-1124-fix04.tar.gz` | `qa-system-frontend:amd64-1124-fix04` | linux/amd64 | `7f0ab30f7e8788b2ea90c025e606ff33ab0d368d07ea0ab3c16795a5c1fc47cc` | 26M |

镜像标识：backend `f83bf5619368`｜frontend `bdd980c685e7`；前端 bundle `index-DUUFrAFG.js`

镜像内自证（开发机已跑）：

```bash
docker run --rm --platform linux/amd64 --entrypoint sh qa-system-backend:amd64-1124-fix04 -c '
  grep -c "pending-delivery/lines" /app/app/api/v1/procurement_analytics.py
  grep -c "item.procurementPendingDelivery" /app/scripts/seed_menu_config.py'
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-fix04 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "待到货明细" /usr/share/nginx/html/assets/index-*.js'
# 实测：backend 1 / 1；frontend index-DUUFrAFG.js / 3
```

---

## 2. Phase 0 生产预检（只读）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK

docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend|worker'
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select version_num from alembic_version"'          # 期望 0169

# 菜单现状（本批新增行应尚不存在）
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select code, path from menu_config where code like '"'"'item.procurement%'"'"';"'

curl -s -o /dev/null -w "backend=%{http_code}\n" http://localhost:8000/api/v1/health
```

**停下条件**：alembic 非 0169；backend health 非 200。

---

## 3. Phase 1 回滚锚点

```bash
docker tag qa-backend:offline       qa-backend:pre-1124-fix04
docker tag qa-system_frontend       qa-system_frontend:pre-1124-fix04
docker images | grep pre-1124-fix04
```

## 4. Phase 2 传输 + 加载 + tag

```bash
sha256sum qa-backend-amd64-1124-fix04.tar.gz qa-frontend-amd64-1124-fix04.tar.gz
# 与 §1 比对

docker load -i qa-backend-amd64-1124-fix04.tar.gz
docker load -i qa-frontend-amd64-1124-fix04.tar.gz
docker tag qa-system-backend:amd64-1124-fix04   qa-backend:offline
docker tag qa-system-frontend:amd64-1124-fix04  qa-system_frontend
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E 'qa-backend:offline|qa-system_frontend'
```

## 5. Phase 3 切换 + **菜单种子（关键）**

```bash
qa_compose up -d --no-build --no-deps --force-recreate backend
docker logs -f qa-backend 2>&1 | grep -E "Application startup complete|ERROR|Can't locate" | head -5

qa_compose up -d --no-build --no-deps --force-recreate frontend

# ★ 菜单种子：不跑这步，菜单里看不到「待到货明细」
qa_compose exec backend python -m scripts.seed_menu_config
# 期望输出含 seed 成功与总行数（比之前多 1 行）

# 复核
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "select code, path, sort_order from menu_config where code like '"'"'item.procurement%'"'"' order by sort_order;"'
# 期望多出 item.procurementPendingDelivery | /procurement/pending-delivery | 2745
```

> `seed_menu_config` 用 `on_conflict_do_nothing`：**只插入缺失行，不覆盖已有行**（不回滚 UI 改动），可安全重跑。

## 6. Phase 4 验收

```bash
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'   # index-DUUFrAFG.js

# 新端点（登录守卫 ⇒ 401/403/409 均表示已注册）
curl -s -o /dev/null -w "lines=%{http_code}\n" \
  "http://localhost:8000/api/v1/procurement/pending-delivery/lines?limit=20&offset=0"
```

浏览器（硬刷新）：
1. 左侧「采购协同」下出现「**待到货明细**」⇒ 点开有表格、有筛选、能翻页；
2. Copilot 侧栏：搜索框 + 场景分组标题 + 图钉（点图钉置顶，再点取消）；
3. Copilot 结果卡有「查看欠交明细」⇒ 跳到明细页。

## 7. 回滚（30 秒，零迁移）

```bash
docker tag qa-backend:pre-1124-fix04 qa-backend:offline
docker tag qa-system_frontend:pre-1124-fix04 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate backend frontend
# 菜单行留着无害（指向已回退的路由 ⇒ 404）；要彻底移除：
docker exec qa-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "delete from menu_config where code = '"'"'item.procurementPendingDelivery'"'"';"'
```

## 8. 已知边界

- 采购菜单项**未给采购部门授权**（与 `item.procurementCopilot` 同口径）⇒ 非 admin 看不到；
  业务人员要用需追加一条 `permission_grant`（另批）。
- 置顶状态存在**本机 localStorage**，换设备/换浏览器不跟随（跨设备同步需加迁移，另批）。
