# 生产热修 1124-fix03：研究列表「历史会话」按钮 i18n 键修复（仅前端）

- **代码版本**：`feat/ontology-evolution @ 29e9159e`（仅 1 个 i18n 文件）
- **本次动**：**仅 frontend 一个容器**；**零迁移、零种子、backend 不动**
- **缺陷来源**：`1124-fix02` 的前端包构建于 i18n 误删修复之前 ⇒ 研究列表页
  「历史会话」按钮渲染成 key 本身（`history research.list.history`）

## 0. 一句话说清这次改了什么

| 项 | 内容 |
|---|---|
| 缺陷 | `research.list.history` 键在 zh-CN / en-US 被误删 ⇒ 按钮文案显示 key |
| 修复 | 恢复两处 `history` 键（zh-CN: "历史会话" / en-US: "History"） |
| 影响面 | **仅**「研究分析 → 历史会话」按钮文案；功能不受影响（按钮仍可点） |
| 验收 | 研究列表页按钮显示「历史会话」而非 key |

## 1. 交付物

| 文件 | 镜像 | 平台 | sha256 | 大小 |
|---|---|---|---|---|
| `/tmp/qa-frontend-amd64-1124-fix03.tar.gz` | `qa-system-frontend:amd64-1124-fix03` | linux/amd64 | `ef9ab83e659468c18d294be99f690e4d9051d8e45a2e7cbfa7b23a5d1dada480` | 26M |

镜像标识：`da3afceb6854`；前端 bundle `index-22hb3Wgr.js`
（1124-fix02 的 bundle 是 `index-Dd2cXydV.js`）

镜像内自证（开发机已跑）：

```bash
docker run --rm --platform linux/amd64 qa-system-frontend:amd64-1124-fix03 sh -c '
  ls /usr/share/nginx/html/assets/index-*.js
  grep -c "research.list.history" /usr/share/nginx/html/assets/index-*.js'
# 期望：index-22hb3Wgr.js / 1
```

## 2. Phase 0 预检（只读）

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
docker inspect qa-frontend --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'   # 现役应为 index-Dd2cXydV.js
curl -s -o /dev/null -w "frontend=%{http_code}\n" http://localhost:5173/
```

**停下条件**：frontend 非 200；现役 bundle 不是 `index-Dd2cXydV.js`（说明生产已换过别的版本，先核对再切）。

## 3. Phase 1 回滚锚点

```bash
docker tag qa-system_frontend qa-system_frontend:pre-1124-fix03
docker images | grep pre-1124-fix03
```

## 4. Phase 2 传输 + 加载 + tag

```bash
sha256sum qa-frontend-amd64-1124-fix03.tar.gz
# 期望 ef9ab83e659468c18d294be99f690e4d9051d8e45a2e7cbfa7b23a5d1dada480

docker load -i qa-frontend-amd64-1124-fix03.tar.gz
docker tag qa-system-frontend:amd64-1124-fix03 qa-system_frontend
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep qa-system_frontend
# Image ID 期望 da3afceb6854
```

## 5. Phase 3 切换

```bash
qa_compose up -d --no-build --no-deps --force-recreate frontend
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep frontend
```

## 6. Phase 4 验收

```bash
docker exec qa-frontend sh -c 'ls /usr/share/nginx/html/assets/index-*.js'
# 期望 index-22hb3Wgr.js
```

浏览器（硬刷新）：「研究分析」列表页 → 右上角按钮显示 **「历史会话」**（不是 `history research.list.history`）⇒ 点得进去。

## 7. 回滚（30 秒）

```bash
docker tag qa-system_frontend:pre-1124-fix03 qa-system_frontend
qa_compose up -d --no-build --no-deps --force-recreate frontend
```
