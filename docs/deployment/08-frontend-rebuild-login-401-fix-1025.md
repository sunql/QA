# 生产前端镜像重建 Runbook · 1025 批次（登录/改密 401 修复）

- **日期**：2026-10-25
- **关联变更 SSOT**：`Harness/changes/fix-login-401-white-screen/summary.md`
- **代码**：分支 `fix/login-401-white-screen`，commit `0802894b` + `e85ff666`（前端源码以此两 commit 为准，后续 docs commit 不含前端代码）
- **操作人**：生产服务器所有操作由用户手工执行，本 runbook 提供命令与期望输出

## 0. 本批次修复内容

| 问题 | 修复后行为 |
|---|---|
| 登录输错用户名/密码 → 硬跳不存在的 `/auth/login` 路由，白屏无提示 | 留在登录页，弹「用户名或密码错误」；未知路径由 `*` 兜底路由重定向，不再白屏 |
| 改密码输错旧密码 → 拦截器误清登录态踢回 `/login`，提示丢失 | 留在改密页，弹「当前密码不正确」 |
| RequireAuth mustChangePassword 白名单指向不存在的 `/auth/*` 路由 | 统一改为真实路由 `/change-password`、`/login` |

纯前端改动，**后端无需任何变更**（与后端 8 个 .py 热修、graph-kit 业务图回填互不影响，先后随意）。

相对上一批前端 rebuild（`0f6c1cec`，1024 批次）新增内容：业务图输入框 placeholder（10105 口径）+ 本次登录/改密修复。**若 1024 批次尚未上线，本批次一次性全部包含，直接上本批即可。**

## 1. 交付物（本机 → 传生产）

```
/tmp/frontend-src-e85ff666.tar.gz   933KB（546 文件，无 node_modules / dist）
sha256: d2874c220a34b0637ebf0f199f0fc919f5da25077a1231e6f068c826a49564af
内容:   git archive（e85ff666）导出的 frontend/ 全量源码
        + docker/Dockerfile.frontend + docker/nginx.conf
```

## 2. ⚠️ 两条命门（历次事故沉淀，不可省略）

1. **`--build-arg VITE_API_BASE_URL=` 必须显式传空**。生产 `.env` 里的
   `VITE_API_BASE_URL` 若被烤进 bundle，非 localhost 访问会全站 CORS 挂
   （2026-10-24 实踩）。compose 的 `:-` 语法对空串会取默认值，所以必须走
   `docker build` 显式传空。
2. **上线必须 `--no-build`**。生产 compose 的 frontend 服务带 build 段，
   `docker compose up -d` 若触发重建会再用 `.env` 烤出错误值。

## 3. 前置检查与回滚锚点（生产）

```bash
docker --version                                # 预期 24.0.x
df -h /var/lib/docker                           # 余量 >2GB
# 回滚锚点（若 1024 批次已上，锚点即当前最新镜像）
docker tag qa-system-frontend:latest qa-system-frontend:pre-rebuild-20261025
```

## 4. 传输并解包到独立目录

```bash
# 本机:
scp /tmp/frontend-src-e85ff666.tar.gz <user>@192.168.205.82:/opt/
# 生产:
sudo mkdir -p /opt/qa-frontend-build-1025
sudo tar xzf /opt/frontend-src-e85ff666.tar.gz -C /opt/qa-frontend-build-1025
```

解压后结构：`/opt/qa-frontend-build-1025/{frontend/, docker/Dockerfile.frontend, docker/nginx.conf}`。
独立目录，不覆盖生产现有 checkout —— 零污染，删目录即完全清理。

## 5. 构建（临时 tag，不碰 latest）

```bash
cd /opt/qa-frontend-build-1025
docker build -f docker/Dockerfile.frontend \
  --build-arg VITE_API_BASE_URL= \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -t qa-system-frontend:tmp-20261025 .
```

- 预计 ~2 分钟（本机实测）。
- **npm 网络**：若 `package-lock.json` 与生产上次构建一致，`npm ci` 层直接
  `CACHED`，不需要外网。若 cache miss 且内网不通 npmmirror，build 会卡死在
  npm ci（历史踩过）→ 放弃本方案，改走 `docker save` 镜像 tar（本机先 build
  好再传 tar）。

## 6. 验证镜像（构建期探针——必做）

```bash
# 探针 1：bundle 绝不允许出现绝对地址（CORS 事故探针）
docker run --rm --entrypoint sh qa-system-frontend:tmp-20261025 -c \
  'grep -rl "localhost:5173/api/v1" /usr/share/nginx/html/assets/; echo exit=$?'
# 必须 exit=1（无匹配）

# 探针 2：记录 asset hash（第 8 步运行时验证要用）
docker run --rm --entrypoint sh qa-system-frontend:tmp-20261025 -c \
  'grep -o "index-[^\"]*\.js" /usr/share/nginx/html/index.html'

# 探针 3（本批次专属）：确认旧硬跳转代码已不在镜像里
docker run --rm --entrypoint sh qa-system-frontend:tmp-20261025 -c \
  'grep -rl "window.location.href=\"/auth/login\"" /usr/share/nginx/html/assets/ ; echo exit=$?'
# 必须 exit=1。js 压缩后字符串形态可能变化，若 exit=0，把 grep 换成
# grep -rl "auth/login" 人工目检命中处，确认只是无恶意的普通字符串。
```

## 7. 上线

```bash
> ⚠️ **2026-10-25 生产实踩坑（本步的真正命门）**：生产 compose 引用的镜像名
> 是**下划线**的 `qa-system_frontend`，与 build/tag 用的连字符
> `qa-system-frontend` 是**两个不同镜像**。tag 到连字符名上 build 得再对，
> 容器重建后仍跑 24h 前的旧镜像（当时证据：容器 Up 8 分钟但 bundle 还是旧
> hash）。**tag 前必须先确认 compose 实际引用的镜像名**：
>
> ```bash
> docker ps --filter name=frontend --format '{{.Image}}'   # 这才是 compose 认的名字
> ```

```bash
# tag 到 compose 实际引用的镜像名（生产实测是下划线 qa-system_frontend）
docker tag qa-system-frontend:tmp-20261025 qa-system_frontend:latest
# （若 compose 里写的确实是连字符名，才 tag qa-system-frontend:latest）

cd <生产 compose 目录>          # 有 docker-compose.yml + .env 的目录
docker compose up -d --no-deps --no-build --force-recreate frontend

# 兜底路线（生产 build 反复出问题时）：本机交叉构建 amd64 后 docker save 打包
# 传输，docker load + 同款 tag/force-recreate（2026-10-25 实际走通的路径，26MB）：
#   本机  docker build --platform linux/amd64 -f docker/Dockerfile.frontend \
#           --build-arg VITE_API_BASE_URL= -t qa-system-frontend:amd64-1025 .
#         docker save qa-system-frontend:amd64-1025 | gzip > /tmp/qa-frontend-amd64-1025.tar.gz
#   生产  docker load -i /opt/qa-frontend-amd64-1025.tar.gz
#         docker tag qa-system-frontend:amd64-1025 qa-system_frontend:latest
```
```




- `--no-deps`：不连带重启 backend。
- `--no-build`：命门 2，防 compose 按 build 段用 `.env` 重烤错误值。
- `--force-recreate`：即使 image ID 判断有偏差也强制重建容器（不加时 tag 新
  镜像后 compose 可能认为「无变化」而不重建）。
- 容器重建后，此前 docker cp 进去的 frontend html 自动消失——无所谓，镜像里已是新代码。


## 8. 运行时验证

```bash
curl -s http://localhost:5173/ | grep -o 'index-[^"]*\.js'
# 期望 = 第 6 步探针 2 的 hash
```

浏览器（从另一台机器访问，**强刷** Cmd+Shift+R）按四个场景验证：

| # | 操作 | 期望 |
|---|---|---|
| 1 | 登录页输错用户名或密码 | **留在登录页**，弹「用户名或密码错误」；不再白屏跳 auth/login |
| 2 | 登录成功 → 头像菜单改密码 → 故意输错「当前密码」 | **留在改密页**，弹「当前密码不正确」；不被踢回登录页 |
| 3 | 地址栏直访 `http://192.168.205.82:5173/auth/login` | 重定向到 /chat（已登录）或 /login（未登录），不再白屏 |
| 4 | 回归：正常登录、正常改密、退出登录、业务图 tab 遍历 10105 | 流程正常 |

## 9. 回滚

```bash
docker tag qa-system-frontend:pre-rebuild-20261025 qa-system-frontend:latest
cd <生产 compose 目录> && docker compose up -d --no-deps --no-build frontend
```

## 10. 清理（验证通过后）

```bash
docker rmi qa-system-frontend:tmp-20261025
sudo rm -rf /opt/qa-frontend-build-1025 /opt/frontend-src-e85ff666.tar.gz
```

## 11. 与其他在途变更的关系

| 事项 | 关系 |
|---|---|
| 后端 graph-kit（业务图 THBI 回填，`prod-graph-kit-1025`） | 独立，先后无所谓；本批次不碰后端 |
| 后端 8 个 .py 热修（1024 两批） | 维持现状，下次正式后端镜像部署统一收编 |
| 分支合并 | `fix/login-401-white-screen` 尚未合 main；生产验证通过后合并 |
