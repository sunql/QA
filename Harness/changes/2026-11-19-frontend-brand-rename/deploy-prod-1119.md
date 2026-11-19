# 生产部署：系统改名「AI服务平台」→「泰鸿智脑」

- **代码版本**：生产现役前端 = `main @ e8f49588`（1116 wikict 发版，bundle `index-rBu8Smfz.js`）；
  本批 = **`e8f49588` + 改名 commit**（cherry-pick 自 `b1f348b6`，落在 `prod/brand-rename-1119` @ `42c2a22a`）
- **变更性质**：**纯前端**、**零 DDL**、**零业务数据写**、**零新环境变量**、**后端零改动**
- **本次只换前端容器**：backend / postgres / neo4j / milvus **一律不动**
- **生产命令由宿主机执行**：开发机**无法直连生产**（192.168.x 内网隔离、无 SSH 免密通道），
  本文件 + 镜像包由开发机交付，**你在宿主机逐条执行**

---

## 0. 一句话说清这次改了什么

产品名从「AI服务平台」改为「泰鸿智脑」，共 **5 处**：

| # | 位置 | 旧 | 新 |
|---|---|---|---|
| ① | 浏览器标签页标题（`index.html` `<title>`） | `AI服务平台` | `泰鸿智脑` |
| ② | 浏览器 favicon（`index.html` 内联 SVG 字母） | `Q` | `T` |
| ③ | 侧边栏顶部品牌（i18n `appLayout.brand`） | `AI服务平台` / `AI Service Platform` | `泰鸿智脑` / `Taihong ZhiNao` |
| ④ | 侧边栏折叠态缩写（`AppLayout.tsx` 硬编码） | `QA` | `TH` |
| ⑤ | 登录页标题（i18n `auth.login.title`） | `登录 qa-system` / `Sign in to qa-system` | `登录 · 泰鸿智脑` / `Sign in to Taihong ZhiNao` |

**本批刻意不带**已在 main 但未上生产的 3 个前端 commit（#150 P5 工作流前端、#151 本体五 Tab 分页、
#154 401 清理）⇒ **生产前端 = 现役前端 + 改名，其余逐字节一致**（证据见 §1.2）。

**验收标准**：浏览器标签页标题与侧边栏顶部显示「泰鸿智脑」，favicon 字母为 T，折叠态缩写为 TH。

---

## 1. 交付物（开发机已产出）

| 文件 | 镜像 | sha256 | 大小 |
|---|---|---|---|
| `/tmp/qa-frontend-amd64-1119-brand.tar.gz` | `qa-system-frontend:amd64-1119-brand`（linux/amd64，`VITE_API_BASE_URL=` 空 ⇒ 同源相对路径） | `7dbe648f6274f43a99ebe9149052b23745e99ad7440bd9b304709309b25296d6` | 26M |

> **只需要这一个包**。后端不动 ⇒ 不需要 backend 包；零 DDL ⇒ 不需要 `pg_dump`，不动任何表。

### 1.1 镜像标识（生产核对用）

| 项 | 值 |
|---|---|
| manifest list digest（开发机 `docker image inspect .Id`） | `sha256:dc866ead2472ef87193b6f448eb0500b3df1ab2a543f2552520c6d4ca2de1a40` |
| config digest（tar 内 `manifest.json` 的 `Config`） | `sha256:e4751315a9a006ecb46cf1f5cd8aceef43f02dc95c6368e8bbebb7578aa802bb` |
| 平台 / 体积 | `linux/amd64` / 98.5MB（tar.gz 后 26M） |
| 新前端 bundle | **`index-BrJZOJUV.js`** |
| 旧（现役）前端 bundle | `index-rBu8Smfz.js` |

### 1.2 「只改了名字」的证据（开发机已实测）

用生产现役代码（`e8f49588` 原样）**另建一个镜像**逐项对比：

| 检查项 | 现役镜像 | 本批镜像 |
|---|---|---|
| 静态产物**文件清单** | — | **只差 2 个文件**：`index.html`（708→706 B）与 `assets/index-*.js`（bundle 改名） |
| `50x.html` / `assets/index-WW9dT-wP.css` | — | **逐字节相同** |
| **中文 UI 文案清单** | 2717 种 | 2717 种，差异仅：`-「服务平台」×1`、`+「泰鸿智脑」×2` |
| `AI Service Platform` | 1 | **0** |
| `Taihong ZhiNao` | 0 | **1** |
| `Sign in to qa-system` | 1 | **0** |
| `Sign in to Taihong ZhiNao` | 0 | **1** |
| `"QA"` | 1 | **0** |
| `"TH"` | 0 | **1** |
| `localhost:5173` 硬编码 | 0 | **0** |
| nginx `resolver 127.0.0.11` / `proxy_pass http://$backendUpstream` | 1 / 1 | 1 / 1 |

⇒ **没有任何其它 UI 文案、路由、请求路径、反代配置发生变化。**

---

## 2. Phase 1 生产预检（只读，不改动）

```bash
# ⓪ compose 入口 —— **不要靠 `ls` 猜**，从现役容器的 label 直接读出实际文件（唯一权威）
docker inspect qa-frontend qa-backend \
  --format '{{.Name}} => {{index .Config.Labels "com.docker.compose.project.config_files"}}'
# 2026-11-15 生产实测：指向**单独一个自包含文件**
#   /opt/qa-system/docker/docker-compose.centos7.yml
# 目录里另外那两个 docker-compose.yml / docker-compose.centos.yml 是仓库同步过来的副本，
# 不是生产实际使用的入口（前者缺 ollama / `:z`，后者的 `!override` 需要 v2 插件，v1 解析即炸）。
# ⇒ 生产全程只用 label 里读出的那一个文件。多条证据不符时以 label 为准。

qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
qa_compose config --quiet && echo COMPOSE_OK
qa_compose config --services    # 确认服务名（backend / frontend / …）
qa_compose config --images      # 确认镜像名（前端应为 qa-system_frontend）

# ⚠️ 不要改 /opt/qa-system/docker/.env（SECRET_KEY 是 Fernet 主密钥，改了旧 token 全失效）

# ① 现役容器与镜像（回滚锚点以这里的实际输出为准，别照抄）
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep -E 'backend|frontend'

# ② 现役前端 bundle（必须是 index-rBu8Smfz.js；不是就停下发我）
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'

# ③ 健康检查（本次不动后端，但它必须活着）
curl -s -o /dev/null -w "backend_health=%{http_code}\n" http://localhost:8000/api/v1/health
curl -s -o /dev/null -w "frontend_health=%{http_code}\n" http://localhost:5173/

# ④ 磁盘余量（一个包 26M）
df -h /var/lib/docker
```

**停下条件**：

- ② 的 bundle **不是** `index-rBu8Smfz.js` ⇒ **停下发我**（生产前端已被别人换过 ⇒ 基线变了，
  本镜像不能直接套用）；
- ③ 的 `frontend_health` 不是 200，或 `backend_health` 不是 200 ⇒ 停下发我。

---

## 3. Phase 2 回滚锚点（镜像 tag，不碰数据库）

```bash
docker tag qa-system_frontend qa-system_frontend:pre-1119-brand
docker images | grep qa-system_frontend
```

> 本批**零 DDL、零业务数据写** ⇒ **不需要 pg_dump**，也不需要动任何表。
> 锚点应指向 `a041235bd8a6`（2026-11-17 的 1116 wikict 镜像）。

---

## 4. Phase 3 传输 + 校验 + 加载 + tag

开发机 → 生产（scp / U 盘均可）。**传完先比对 sha256**：

```bash
shasum -a 256 /tmp/qa-frontend-amd64-1119-brand.tar.gz              # 开发机
sha256sum  /tmp/qa-frontend-amd64-1119-brand.tar.gz                 # 生产
# 两者都必须是 7dbe648f6274f43a99ebe9149052b23745e99ad7440bd9b304709309b25296d6
```

生产：

```bash
docker load -i /tmp/qa-frontend-amd64-1119-brand.tar.gz
docker tag qa-system-frontend:amd64-1119-brand qa-system_frontend

qa_compose config | grep image      # 确认解析到 qa-system_frontend
```

---

## 5. Phase 4 切换（只动前端）

```bash
qa_compose up -d --no-build --no-deps --force-recreate frontend
```

> ⚠️ **只切 frontend**，理由：
> - 后端代码**零改动** ⇒ 没有重启的必要；
> - backend 的 CMD 是 `alembic upgrade head && uvicorn ...` ⇒ 重启会白跑一次迁移 + seed，
>   生产上是无谓的风险；
> - `--no-build` 防止 compose 现场 build（会拉到错误架构的基础镜像）；
> - `--no-deps` 防止顺带重启 postgres / neo4j / milvus；
> - `--force-recreate` 保证容器真的换成新镜像（仅靠 `up -d` 在镜像 tag 变了时**可能不重建**）。

```bash
# 等 3~10 秒让 nginx 起来
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep frontend
docker logs qa-frontend --since 2m 2>&1 | tail -5
```

---

## 6. Phase 5 验收探针

### 6.1 代码探针（bundle 内自证）

> ⚠️ 本探针**已双向验证**：对**新镜像**必然出 `title_new=1 / bundle_new=1 / *_old=0 / favicon_T=1`，
> 对**现役镜像**必然出 `title_new=0 / title_old=1 / favicon_Q=1`。
> ⇒ 不会像 1116 runbook 里那版探针一样「对新旧都返回 0」而误判「没部署」。

```bash
docker exec qa-frontend sh -c '
h=/usr/share/nginx/html
echo bundle=$(ls $h/assets/index-*.js | head -1 | xargs basename)
echo title_new=$(grep -c "泰鸿智脑" $h/index.html)
echo bundle_new=$(grep -c "泰鸿智脑" $h/assets/index-*.js)
echo title_old=$(grep -c "AI服务平台" $h/index.html)
echo bundle_old=$(grep -c "AI服务平台" $h/assets/index-*.js)
echo en_old=$(grep -c "AI Service Platform" $h/assets/index-*.js)
echo login_old=$(grep -c "Sign in to qa-system" $h/assets/index-*.js)
echo favicon_T=$(grep -c "%3ET%3C/text%3E" $h/index.html)
echo favicon_Q=$(grep -c "%3EQ%3C/text%3E" $h/index.html)
echo resolver=$(grep -c "resolver 127.0.0.11" /etc/nginx/conf.d/default.conf)
echo proxy_pass=$(grep -c "proxy_pass http://\$backendUpstream" /etc/nginx/conf.d/default.conf)
'
```

**期望值**：

| 键 | 期望 | 含义 |
|---|---|---|
| `bundle` | `index-BrJZOJUV.js` | 新前端已生效 |
| `title_new` / `bundle_new` | **1 / 1** | 标题与侧边栏品牌都是新名 |
| `title_old` / `bundle_old` / `en_old` / `login_old` | **0 / 0 / 0 / 0** | **旧名彻底消失**（负对照） |
| `favicon_T` / `favicon_Q` | **1 / 0** | favicon 字母已是 T |
| `resolver` / `proxy_pass` | 1 / 1 | 反代未被破坏 |

> 若 `bundle` 仍是 `index-rBu8Smfz.js` ⇒ **部署没生效**（容器没重建或 nginx 缓存了旧文件）。
> 先看 `docker ps` 里前端容器的 Image 名，再核对 `docker images | grep qa-system_frontend`。

### 6.2 反代与后端链路

```bash
curl -s -o /dev/null -w "api_via_frontend=%{http_code}\n" http://localhost:5173/api/v1/health
curl -s -o /dev/null -w "frontend_root=%{http_code}\n" http://localhost:5173/
curl -s -o /dev/null -w "backend_direct=%{http_code}\n" http://localhost:8000/api/v1/health
# 期望：200 / 200 / 200
```

### 6.3 浏览器端到端（关键验收）

1. **硬刷新**（`Ctrl+Shift+R`）——否则会拿到旧的 `index-rBu8Smfz.js`；
2. 浏览器标签页标题应为 **泰鸿智脑**（favicon 字母为 **T**）；
3. 登录页标题应为 **登录 · 泰鸿智脑**；
4. 登录后侧边栏顶部应显示 **泰鸿智脑**；折叠侧边栏后缩写应为 **TH**；
5. 随手点两个菜单页，确认页面正常（**改名不应影响任何功能**）。

---

## 7. 回滚

```bash
docker tag qa-system_frontend:pre-1119-brand qa-system_frontend
qa_compose stop frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
```

回滚后 `docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'`
应重新出现 `index-rBu8Smfz.js`，且 §6.1 的 `title_old` / `favicon_Q` 回到 1。

> **回滚只动前端镜像，不碰后端、不碰库** ⇒ 无数据风险、无迁移回滚问题。

---

## 8. 本批明确没做的事

- ❌ 不带 #150 / #151 / #154 三个已合 main 但未上生产的前端 commit（用户要求：新功能不发布）；
- ❌ 不动后端镜像（backend / worker / postgres / neo4j / milvus 一律不动）；
- ❌ 不跑任何迁移、不做任何 DDL、不写任何业务数据；
- ❌ 不改生产 `.env`（`SECRET_KEY` 是 Fernet 主密钥）；
- ❌ 不改菜单文案（用户自行人工调整；本批只改名称相关的 5 处）。
