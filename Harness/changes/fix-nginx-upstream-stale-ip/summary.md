# 变更：fix-nginx-upstream-stale-ip

- **日期**：2026-09-12
- **作者**：启琳
- **Phase**：done
- **状态**：done

## 1. 需求

**现象**：侧边栏菜单「少了，而且没有分组，跟没有做分组一样」。**不报任何错**。

**根因**：`docker/nginx.conf` 里 `proxy_pass http://backend:8000;` 写死了主机名。
nginx 对 `proxy_pass` 里字面量主机名**只在启动时解析一次**并缓存 IP。后端容器一旦被重建
（`docker compose up -d backend` / `--force-recreate`，每次镜像重建部署都会发生）就拿到新 IP，
而 nginx 仍指向旧 IP → **所有 `/api/` 请求 502**。

**为什么表现成「菜单问题」**：`AppLayout.tsx` 的 `fetchMenuConfig()` 失败后走
`catch → setUseFallback(true)`，**静默**退回 `components/common/fallbackNav.ts` 的扁平
`FALLBACK_NAV`（22 项、无分组、**没有整个企业 Wiki 组**），只在 console 打一行警告。
菜单是唯一大面积依赖该 API 的首屏组件，所以 502 看起来只像菜单坏了，其实**所有**前端 API 都 502。

**验收标准**：重建前端镜像后修复仍生效（不能只靠 `docker cp`）；`/api/v1/menu-config`
经 `:5173` 返回 200 且分组完整。

**实测证据**：nginx error log 报 `connect() failed (111) ... upstream "http://172.18.0.4:8000/"`
而后端真实 IP 是 `172.18.0.10`；nginx 启动于 `06:07:13Z`，后端重建于 `13:12:37Z`。

## 2. 设计评审

| 方案 | 结论 |
|---|---|
| A. 每次后端重建后 `nginx -s reload` | 否。靠人记得，恰是会漏的环节，且不能自动化到 `compose up` |
| B. `docker cp` 新 conf 进容器 | 否。**临时**，`qa-frontend` 一重建就丢 |
| C. `resolver` + **变量**写法 `proxy_pass http://$backendUpstream;` | **采纳** |

选 C。**关键：两者必须配套** —— 只加 `resolver` 而不把主机名换成变量，nginx 依旧在启动时
钉死 IP，加了等于没加。变量形式不带 URI 部分，请求 URI 原样透传，与原先行为一致。

## 3. 数据模型变更

无。无迁移、不触碰 `alembic_version`，部署门禁（单一 head）不受影响。

## 4. 接口契约变更

无。前端调用路径与响应结构不变。

## 5. 实现要点

- `docker/nginx.conf`：新增 `resolver 127.0.0.11 valid=10s ipv6=off;`（Docker 内置 DNS），
  `/api/` 内改为 `set $backendUpstream backend:8000;` + `proxy_pass http://$backendUpstream;`
- `docker/Dockerfile.frontend`：新增 `ARG NPM_REGISTRY=https://registry.npmjs.org` 并
  `RUN npm ci --registry=${NPM_REGISTRY}`。**默认仍是官方源**，仓库保持可移植；受限环境构建时覆盖。
  用 `ARG` 而非 `ENV`：只在构建期可见，不会烘焙进镜像层。

**构建期额外发现的阻塞**：`--no-cache` 重建时 `npm ci` 失败于
`EIDLETIMEOUT — registry.npmjs.org:443`。根因是**吞吐量不是连通性**（单请求探测两边都「通」，
会误导排查方向）——同一个 4MB tarball：npmjs ~0.47 MB/s vs npmmirror ~10.3 MB/s（约 21 倍）。
前端依赖树约 900MB，`.dockerignore` 已排除 `node_modules/`/`dist/`，构建**必须**联网重装，
0.47 MB/s 跑不完就撞 idle timeout，**重试默认源无用**。

## 6. 测试

**已验证**：
- `docker exec qa-frontend grep -E "resolver|backendUpstream|proxy_pass" /etc/nginx/conf.d/default.conf`
  → 三项均在镜像内，确认**非** `docker cp` 残留
- `docker exec qa-frontend nginx -t` → syntax is ok / test is successful
- `curl localhost:5173/api/v1/menu-config` → **HTTP 200 / 7 sections / 37 children**；
  `section.enterpriseWiki` 在列（5 children）
- `curl localhost:5173/api/v1/health`、`/api/v1/menu-config/admin` → 均 200
- `docker logs qa-frontend | grep upstream` → 0 条报错

**动态重解析实测（2026-09-12 补，隔离环境）**：**未**重建生产 backend —— 排查中发现
重建会顺带把「未完成的 P0 部署」带上线（独立问题，不属本变更），故改用
**同一镜像 `qa-system-frontend:latest`** 在一次性网络 `172.30.0.0/16` 上起 nginx，
上游用 `python:3.12-slim` 小服务挂 `backend` 网络别名，读环境变量回显 token：

| 步骤 | nginx 进程 | 上游 IP | 结果 |
|---|---|---|---|
| 基线 | 启动于 `15:01:08Z` | `172.30.0.10` | `AAAA` |
| 换上游（**不碰 nginx**） | **同一进程，启动时间未变** | `172.30.0.20` | **`BBBB`** ✅ |

上游换了 IP、nginx 全程未 reload/restart，响应随之改变、无 upstream 报错 → **动态重解析成立**。

**反向对照（证明修复是承重的，不是碰巧）**：同样结构换用**旧写法** `proxy_pass http://backend:8000;`
（无 resolver、无变量，只挂 conf 到 `nginx:alpine`）：上游 `.10 → .20` 后返回
**502 Bad Gateway**，error log 明确仍连旧地址
`connect() failed ... upstream: "http://172.31.0.10:8000/api/x"` —— **与线上事故签名完全一致**，
反证根因诊断正确、且该修复确实承重。

> 方法论坑：首次对照失败是因为忘了清掉上一轮仍持有 `backend` 别名的容器，Docker DNS 对同名别名
> 多容器做轮询，导致结果张冠李戴。**别名在同一网络上必须确保唯一持有者**，否则实验无效。

## 7. 安全审查

- 不涉及认证/授权/用户输入/DB 查询，无 secrets 写入。未触发 security-reviewer 必查项。
- **供应链注意**：npmmirror 是第三方镜像。为控制该风险，**默认值保持官方源**，
  镜像仅由构建命令显式传入，因此仓库对任何环境仍是官方源，第三方只影响本机本次构建。
- `ARG` 不进入镜像层（非 `ENV`），无凭据或内部地址泄漏。

## 8. 部署验证

- 构建：`docker compose -f docker/docker-compose.yml build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend`
  → 成功，约 100s（改前卡死 ~7 分钟后失败）
- 镜像：`qa-system-frontend` `d6cfe069e560` → **`f7edf493a3ad`**
- 部署：`docker compose up -d frontend` → `qa-frontend` Recreated；**backend 未被重建**
- 冒烟：见 §6，全部通过

## 9. 关联

- Wiki：`Harness/wiki/`（部署/网关相关条目）
- 规则：`Harness/rules/开发流程规范.md`
- 相关变更：`feat-wiki-provenance`（P0 部署重建后端容器，正是本 bug 的触发场景）
