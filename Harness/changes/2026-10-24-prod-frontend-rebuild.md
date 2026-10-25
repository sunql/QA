# 生产前端镜像重建方案（源码拷贝 + 生产 rebuild）

> **归档说明（2026-10-25）**：本文为 2026-10-24 生产前端重建的人工操作记录，正文一字未动。
> 历史口径与后续实证的差异，读时注意：
> ① 文中镜像名 `qa-system-frontend:latest` 是当时的假定名——2026-10-25 生产
> `docker ps` 实测现役名为 **`qa-system_frontend`**（`-p qa-system` 派生，下划线）；
> ② compose 目录当时未探明，2026-10-25 实证为 `/opt/qa-system/docker/`
> （`docker-compose.centos7.yml` + `-p qa-system`，统一入口 qa_compose，见
> `Harness/changes/feat-data-domain-layer/deploy-prod-1025.md`）；
> ③ 文中引用的 `prod-hotfix-kit-1024/`（后端 4 个 .py 热修包）已不在仓库中。
> 两条命门（VITE_API_BASE_URL 显式传空 / 上线 `--no-build`）仍为现行有效结论。

基于 commit `0f6c1cec`（含今日全部前端修复：属性页 N+1 / runOntologyBatch 300s / 编码映射服务端分页）。

## 交付物（本机 → 传生产）

```
/tmp/frontend-src-0f6c1cec.tar.gz   930KB
sha256: 31495dc0b64b2e8d0b2a268b8a36adf40632144bb47860d108e228ce1cf61661
内容: git archive 导出的 frontend/ 全量源码 + docker/Dockerfile.frontend + docker/nginx.conf
     （无 node_modules / dist，干净的构建上下文）
```

## ⚠️ 核心注意（本方案的两条命门）

1. **`--build-arg VITE_API_BASE_URL=` 必须显式传空**。生产 `.env` 里若有
   `VITE_API_BASE_URL=http://...`，compose build 会把绝对地址烤进 bundle →
   重演今天早上的 CORS 全挂（非 localhost 访问 XHR 打到浏览器自己机器）。
   本方案直接 `docker build`（不走 compose build），显式传空双保险。
2. **上线必须 `--no-build`**。生产 compose 文件的 frontend 服务带 build 段，
   `docker compose up -d` 若触发重建镜像会再吃一遍 `.env` 的错误值。

## 前提

- **后端 4 个 .py 热修必须已生效**（`{rows,total}` 是破坏性契约，前后端必须同批）。
  未做的话先按 `prod-hotfix-kit-1024/README.md` 把后端拷完、重启。
- 生产 Docker 24.0.x、磁盘余量 >2GB。

## 第 0 步：前置检查与回滚锚点（生产）

```bash
docker --version                                # 预期 24.0.x
df -h /var/lib/docker                           # 余量 >2GB
# 回滚锚点
docker tag qa-system-frontend:latest qa-system-frontend:pre-rebuild-20261024
# 顺手看一眼生产 Dockerfile 新旧（判断 compose 版本）
grep -n "VITE_API_BASE_URL" <生产仓库>/docker/Dockerfile.frontend
```

## 第 1 步：传输并解包到独立目录（生产）

```bash
# 本机:
scp /tmp/frontend-src-0f6c1cec.tar.gz <user>@192.168.205.82:/opt/
# 生产:
sudo mkdir -p /opt/qa-frontend-build-1024
sudo tar xzf /opt/frontend-src-0f6c1cec.tar.gz -C /opt/qa-frontend-build-1024
```

解压后结构：`/opt/qa-frontend-build-1024/{frontend/, docker/Dockerfile.frontend, docker/nginx.conf}`。
**独立目录，不覆盖生产现有 checkout —— 零污染，删目录即完全清理。**

## 第 2 步：构建（先打临时 tag，不碰 latest）

```bash
cd /opt/qa-frontend-build-1024
docker build -f docker/Dockerfile.frontend \
  --build-arg VITE_API_BASE_URL= \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com \
  -t qa-system-frontend:tmp-20261024 .
```

- 预计 ~2 分钟（本机实测）。
- **npm 网络**：若 `package-lock.json` 与生产上次构建一致，`npm ci` 层直接
  cache hit，**不需要外网**——看输出里该层是否 `CACHED`。若 cache miss 且内网
  不通 npmmirror，build 会卡死在 npm ci（历史踩过）→ 放弃本方案，改走
  `docker save` 镜像 tar（本机 `qa-system-frontend:latest` 已是修正版）。

## 第 3 步：验证镜像（构建期注入探针——必做）

```bash
# 探针 1：bundle 里绝不允许出现绝对地址（早上 CORS 事故的探针）
docker run --rm --entrypoint sh qa-system-frontend:tmp-20261024 -c \
  'grep -rl "localhost:5173/api/v1" /usr/share/nginx/html/assets/; echo exit=$?'
# 必须 exit=1（无匹配）

# 探针 2：记录 asset hash
docker run --rm --entrypoint sh qa-system-frontend:tmp-20261024 -c \
  'grep -o "index-[^\"]*\.js" /usr/share/nginx/html/index.html'
# 同 commit + 同 Dockerfile，理应与本机一致：index-B9xWLQOl.js
```

## 第 4 步：上线

```bash
docker tag qa-system-frontend:tmp-20261024 qa-system-frontend:latest
cd <生产 compose 目录>          # 有 docker-compose.yml + .env 的目录
docker compose up -d --no-deps --no-build frontend
```

- `--no-deps`：不连带重启 backend。
- `--no-build`：**命门 2**，防 compose 按 build 段用 `.env` 重烤错误值。
- 容器重建后，之前 docker cp 进去的 html 自动消失——无所谓，镜像里已是新代码。

## 第 5 步：运行时验证

```bash
curl -s http://localhost:5173/ | grep -o 'index-[^"]*\.js'
# 期望 = 第 3 步探针 2 的 hash
```

浏览器（从另一台机器，如 192.168.205.x）**强刷**（Cmd+Shift+R）后检查：

1. F12 Network：API 请求 URL 是 `http://192.168.205.82:5173/api/v1/...`（同源），
   不再出现 `localhost` → 退出登录等操作恢复
2. 编码映射页：出现分页条，total ≈ 35.4 万，翻页/切每页条数正常
3. 本体管理-属性页：一次请求拉全量属性（N+1 修复生效）

## 第 6 步：回滚

```bash
docker tag qa-system-frontend:pre-rebuild-20261024 qa-system-frontend:latest
cd <生产 compose 目录> && docker compose up -d --no-deps --no-build frontend
```

## 清理（验证通过后）

```bash
docker rmi qa-system-frontend:tmp-20261024
sudo rm -rf /opt/qa-frontend-build-1024 /opt/frontend-src-0f6c1cec.tar.gz
```

## 与 docker cp 热修的关系

镜像重建是**持久**方案（不怕容器重建回退）；此前热修包里 `frontend-html/` 的
docker cp 是止血。本方案完成后，前端侧不再需要任何 docker cp；后端 4 个 .py
仍是热修态，待下次正式镜像部署（docker save / 生产全量 build）收编。
