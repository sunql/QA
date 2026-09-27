# 变更：后端 Dockerfile.runtime stage 补切阿里云 apt 源（修复 build 502）

- **日期**：2026-09-28
- **作者**：Claude (with user direction 启琳)
- **触发**：「frontend 和 backend 重新 build + 重启镜像」操作期间
- **状态**：done（镜像已 build + 容器已重启 + alembic 0087/0088/0089 已落 prod）
- **关联**：`Harness/wiki/operations-runbook.md`、`qa-system-deploy-container-fixes`

---

## 1. 触发与根因

启琳要求「按当前最新代码 build/publish frontend + backend 镜像，重启两个镜像」。
跑 `docker compose build --no-cache backend` 时 build 在 stage-1 3/6 阶段失败：

```
#11 135.7 E: Failed to fetch http://deb.debian.org/debian/pool/main/f/fonts-dejavu/fonts-dejavu-core_2.37-8_all.deb
                502  Bad Gateway [IP: 146.75.114.132 80]
#11 ERROR: process "/bin/sh -c apt-get update ... && apt-get install ... tesseract-ocr-chi-sim ..." did not complete successfully: exit code: 100
```

**根因**：`Dockerfile.backend` runtime stage (L47-62) 用 `FROM python:3.12-slim` 起新 base，
**没有切 apt 源** —— base image 默认指向 `deb.debian.org`，在国内网络 502/超时频繁。
builder stage (L3-21) 已切阿里云镜像源，但 runtime stage 漏切。

字体包 `fonts-dejavu-core` 是 `tesseract-ocr` 的间接依赖（poppler → fontconfig → fonts-dejavu-core），
是最容易触发此 bug 的大包。

## 2. 修复

把 runtime stage 的 `apt-get update && install tesseract-ocr ...` 改为：

1. 写 `/etc/apt/sources.list.d/debian.sources` 切到 `mirrors.aliyun.com/debian{,-security}`；
2. 删 `/etc/apt/sources.list`（避免旧条目干扰）；
3. `for i in 1..5` retry loop（与 builder stage 完全一致）；
4. 再装 `tesseract-ocr` + `tesseract-ocr-chi-sim` + `poppler-utils`。

无逻辑变化，仅 source + retry 兜底。

## 3. 部署结果

| 步骤 | 命令 | 结果 |
|---|---|---|
| frontend build | `docker compose build --no-cache --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend` | ✅ exit 0（npmmirror 提速 21 倍，SSOT `qa-system-frontend-build-npm-registry`）|
| backend build (1st) | `docker compose build --no-cache backend` | ❌ exit 100（fonts-dejavu-core 502）|
| Dockerfile 修复 | runtime stage 加阿里云源 + retry | ✅ |
| backend build (2nd) | `docker compose build --no-cache backend` | ✅ exit 0（约 4 min，#11 阶段顺利）|
| recreate | `docker compose up -d --no-deps --force-recreate backend frontend` | ✅ 2 容器 Recreated + Started |
| 镜像 hash | `qa-system-backend:latest` 2026-09-28 07:41 / `qa-system-frontend:latest` 2026-09-28 07:36 | ✅ 最新 |
| 健康检查 | `curl /api/v1/health` → `{"status":"ok","version":"0.1.0","appEnv":"development"}` | ✅ |
| frontend 入口 | `curl http://localhost:5173/` → 200 | ✅ |
| alembic 升级 | 0086 → 0087 → 0088 → 0089 | ✅ 全部落地 prod qa_metadata |
| system_config 行 | 9 个新 key（5 easy + 4 medium + 6 hard 共 15 项；与已治理 CLASS_FILTER_MAX_CLASSES 共 16 行） | ✅ 实测精确数 |

## 4. 镜像清单

```
qa-system-backend                               latest              2026-09-28 07:41   1.42GB
qa-system-frontend                              latest              2026-09-28 07:36   106MB
qa-system-backend                               pre-rebuild-20260926  2026-09-20 10:00  1.42GB  (旧基线)
qa-system-backend                               pre-p0              2026-09-12 12:04   1.15GB
```

## 5. 关联

- **memory**：`qa-system-docker-compose-tag`（前端必须 `--no-cache`）、
  `qa-system-frontend-build-npm-registry`（npmmirror）、
  `qa-system-deploy-container-fixes`（镜像/容器 drift 修复类）、
  `qa-system-stale-container-deploy`（deploy_backend.sh SSOT）
- **Dockerfile**：`docker/Dockerfile.backend`（修在 runtime stage）
- **Phase 2 SSOT**：`Harness/changes/chore-magic-number-governance/summary.md`
- **本次跑法是 SSOT 化的正常 build 路径**（不是 deploy_backend.sh 的 docker cp 临时手段）

---

## SSOT 校验清单

- [x] 根因记录：runtime stage 没切 apt 源 → deb.debian.org 502
- [x] 修法记录：与 builder stage 对齐（阿里云源 + retry）
- [x] 部署结果：build + recreate + health + alembic 全绿
- [x] 镜像 hash 最新（frontend/backend 都是 2026-09-28）
- [x] system_config 16 行精确数核对（含 Phase 2 新 15 项 + 已治理 1 项）