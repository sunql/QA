#!/usr/bin/env bash
# 联网 x86_64 构建机：拉齐镜像 -> docker save -> 校验和。
# 产物 scp 到内网 CentOS 后 docker load。
# 依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md §6.1
set -euo pipefail

OUT_DIR="${OUT_DIR:-/opt/qa-transfer/images}"
# Mac 是 arm64、CentOS 是 x86_64 —— 平台不钉死会打出跑不了的镜像
PLATFORM="linux/amd64"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

# 与 docker-compose.yml + docker-compose.centos.yml 保持一致
# （backend/frontend 是本地构建，不在此列）
IMAGES=(
  "postgres:16-alpine"
  "neo4j:5.23-community"
  "quay.io/coreos/etcd:v3.5.5"
  "minio/minio:RELEASE.2023-03-20T20-16-18Z"
  "milvusdb/milvus:v2.4.6"
  "minio/minio:RELEASE.2024-06-13T22-53-53Z"
  "mysql:8.0"
  "alpine:3.20"
  "ollama/ollama:latest"
)

mkdir -p "$OUT_DIR"

echo "== 拉取镜像（$PLATFORM）=="
for img in "${IMAGES[@]}"; do
  echo "-- $img"
  docker pull --platform "$PLATFORM" "$img"
done

echo "== 构建 backend / frontend =="
# ⚠️ 必须在联网机做，且 NPM_REGISTRY 必须覆盖为国内源：
# docker/Dockerfile.frontend 默认走 registry.npmjs.org，内网构建会卡死
# （qa-system-frontend-build-npm-registry）。
# docker/Dockerfile.backend 已硬编码 aliyun Debian + aliyun PyPI 源，无需覆盖。
# ⚠️ compose 文件在 `docker/` 下，build context 是 `..`（= 仓库根）。
# 这里必须带 `docker/` 前缀：Step 5 从**仓库根**调用本脚本，脚本自己不 cd，
# 裸文件名会 no such file or directory。CentOS 侧那几处是 `cd /opt/qa/docker`
# 之后的裸名，那是对的，别照着它们抄。
# ⚠️ NPM_REGISTRY 必须用 `--build-arg` 传，不能只靠 `NPM_REGISTRY=xxx docker
# compose build` 的环境变量前缀：宿主环境变量只参与 compose 自身的 ${} 插值，
# buildkit 收不到（实测 build args 里只有 VITE_API_BASE_URL）。
# 所以这里**不写**那个环境变量前缀 —— 它除了被 --build-arg 覆盖之外没有任何作用，
# 留着还会让 shellcheck 报 SC2097/SC2098。
# ⚠️ 钉平台只有 DOCKER_DEFAULT_PLATFORM 一条路：`docker compose build` 没有
# `--platform` 这个 flag（compose v2.27.1 / v5.6.0 的 build 子命令都没有），
# 写成 `build --platform` 会直接报 unknown flag 而中断脚本。
# compose 的 applyPlatforms() 会读 DOCKER_DEFAULT_PLATFORM，给没写 platform
# 的服务补上 build.platforms，效果等价。
DOCKER_DEFAULT_PLATFORM="$PLATFORM" \
  docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml \
    build --build-arg NPM_REGISTRY="$NPM_REGISTRY" backend frontend

echo "== 打包 =="
TAG="$(date '+%Y%m%d-%H%M')"
docker save -o "$OUT_DIR/all-$TAG.tar" "${IMAGES[@]}"
# 只存本地构建的那两个。⚠️ 不要用 `docker compose images -q` —— 它列出**所有**服务的
# 镜像（含刚 pull 的 8 个外部镜像），会把 milvus / neo4j 之类再存一遍，
# 离线包凭空多几个 GB。`config --images` 给的是镜像**名**（docker save 也吃名），
# 减去 IMAGES 数组后剩下的正好是 backend / frontend。
# ⚠️ compose 文件在 `docker/` 下，build context 是 `..`（= 仓库根）。
# 这里必须带 `docker/` 前缀：Step 5 从**仓库根**调用本脚本，脚本自己不 cd，
# 裸文件名会 no such file or directory。CentOS 侧那几处是 `cd /opt/qa/docker`
# 之后的裸名，那是对的，别照着它们抄。
BUILT="$(docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config --images \
  | grep -vxF -f <(printf '%s\n' "${IMAGES[@]}"))"
# shellcheck disable=SC2086  # $BUILT 是多行镜像名列表，必须靠单词拆分成多个参数
docker save -o "$OUT_DIR/built-$TAG.tar" $BUILT

echo "== 校验和 =="
cd "$OUT_DIR"
sha256sum ./*.tar > images.manifest.sha256

# ⚠️ 顺手把每个镜像的**镜像 ID** 记下来。`ollama/ollama:latest` 是浮动 tag：
# 今天造的离线包和三个月后重建的同名 tag 未必是同一个镜像层，而 CentOS 是内网隔离的，
# 拉不到、也换不了 —— 届时没有任何手段确认装的到底是哪个版本。
# 用 `.ID`（镜像 config 的短 sha）而不是 `RepoDigest`：从 tar `docker load` 进来的镜像
# **没有** RepoDigests，比对它必然两边都空 ⇒ 恒过。
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' \
  | grep -v '<none>' | sort > images.digests.txt
cat images.digests.txt
# ⚠️ 必须硬断言，不要只 `wc -l` 打个数字：格式串写错 / 匹配全被 grep 掉时
# 这个文件会是空的，而下游所有按它做的比对都会「空空如也 ⇒ 通过」。
n=$(wc -l < images.digests.txt)
[ "$n" -ge 11 ] || { echo "ABORT: images.digests.txt 只有 $n 行，期望 >= 11"; exit 1; }
echo "镜像 ID 记录 $n 行"

echo "== 完成 =="
ls -lh "$OUT_DIR"
echo "把 images.manifest.sha256 和 images.digests.txt 一起带走，CentOS 侧要复核"
