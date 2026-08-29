---
created: 2026-08-19
updated: 2026-08-19
sources:
  - qa-system/docker/docker-compose.yml
  - qa-system/docker/Dockerfile.backend
  - qa-system/docker/Dockerfile.frontend
  - qa-system/backend/pyproject.toml
  - qa-system/frontend/package.json
  - qa-system/docker/.env.example
  - qa-system/docs/deployment/00-overview.md
  - qa-system/docs/deployment/02-environment.md
  - qa-system/docs/deployment/07-operations.md
tags: [部署, 离线, 迁移, Docker, qa-system]
---

# qa-system 离线迁移方案（无外网 Linux 目标机）

目标：把当前 `qa-system`（FastAPI + React + Postgres + Neo4j + Milvus + MinIO + etcd + MySQL demo）整体搬到**完全不能访问外网**的 Linux 服务器，全程不依赖在线拉镜像/装包/调外部模型。

本方案只描述「数据搬运 + 离线包制作 + 上线步骤 + 验证 + 回滚」，业务代码不动；具体参数含义一律回查 `docker/.env.example` 与 `docs/deployment/`。

---

## 一、先确认一件事：所谓"无外网"包含什么

迁移前**必须**与客户/目标机运维明确边界，决定后续每一步是否要离线化：

| 类别 | 是否对外 | 影响 |
| :--- | :--- | :--- |
| Docker Hub / ghcr.io / quay.io 镜像 | ❌ 离线 | 必须先在外网机 `docker save`，再 `docker load` |
| PyPI / npm Registry | ❌ 离线 | 后端 pip wheels、前端 npm tarballs 必须离线缓存 |
| GitHub Release（uv 二进制、Playwright 浏览器） | ❌ 离线 | Dockerfile.backend 用 `gh release` 下 uv；前端 e2e 用 Playwright 也要浏览器 |
| Debian apt 源（`debian.org` / 镜像站） | ❌ 离线 | builder 阶段需要 apt 包，`python:3.12-slim` / `node:20-alpine` 都装源 |
| LLM API（OpenAI / DeepSeek / Qwen…） | ⚠️ 视情况 | 模型运行时才调；激活前不强制要外网 |
| Embedding 服务（Ollama / oMLX / bge-m3） | ⚠️ 同上 | 推理机若在目标机本地提供，则无需外网；否则必须把模型权重带过去 |
| Let's Encrypt / 证书签发 | ⚠️ 视情况 | 改用内网自签证书或仅 80/443 直连 |
| 反向代理（HTTPS、Caddy） | ⚠️ 视情况 | 离线后 Caddy 自动签证书会失败 |

> **建议**先让目标机给一个测试域名 / 自签证书，**别**等部署完才补。LLM 与 embedding 在「首次跑通冒烟测试」前都不要激活——系统启动不依赖它们（见 `.env.example` 注释）。

---

## 二、需要搬运的物料清单（一次性出炉）

### 2.1 镜像（`docker-compose.yml` + Dockerfile 共 9 个）

| 镜像 / 构件 | 用途 | 来源 |
| :--- | :--- | :--- |
| `postgres:16-alpine` | 元数据库 | Docker Hub |
| `neo4j:5.23-community` | 本体图（带 apoc 插件） | Docker Hub |
| `quay.io/coreos/etcd:v3.5.5` | Milvus 元数据 | quay.io |
| `minio/minio:RELEASE.2023-03-20T20-16-18Z` | Milvus 对象存储 | Docker Hub |
| `milvusdb/milvus:v2.4.6` | 向量库（standalone） | Docker Hub |
| `mysql:8.0` | 演示库 `wms_demo` | Docker Hub |
| `python:3.12-slim` ×2（builder + runtime） | 后端镜像基础层 | Docker Hub |
| `node:20-alpine` + `nginx:alpine` | 前端镜像构建 / 运行 | Docker Hub |
| `local/qa-backend:<tag>` | 自建后端镜像 | `docker build` |
| `local/qa-frontend:<tag>` | 自建前端镜像 | `docker build` |

> 自建镜像里还有：uv 二进制（GitHub Releases）、ca-certificates、curl。这些也算「外网字节」，下面一起处理。

### 2.2 后端 Python 依赖（`backend/pyproject.toml`）

主依赖 20 个 + `dev` 可选（`transformers` 等）。其中含二进制扩展的有：

- `asyncpg`、`aiomysql`、`pymilvus`、`oracledb`：C 扩展，需在 builder 阶段 gcc 编译（已在 `Dockerfile.backend` 装好 `build-essential`）
- `cryptography`：Rust 扩展，但官方多数平台有 wheel
- `pydantic` / `sqlalchemy` / `fastapi` / `uvicorn[standard]`：纯 Python + 部分 C
- 其余 wheels / sdist 均需离线

### 2.3 前端 Node 依赖（`frontend/package.json`）

- 运行时 13 个包（`antd`/`echarts`/`monaco-editor`/`react`/…）
- dev 11 个包（`vite`/`vitest`/`playwright`/`typescript`/…）

Playwright 还要再下浏览器二进制；离线迁移若不需要 e2e，可省。生产部署仅需 `npm run build`，**不用**装 dev 依赖。

### 2.4 Embedding 模型权重（按需）

| 方案 | 来源 | 体积估算 |
| :--- | :--- | :--- |
| Ollama 拉 `bge-m3`（默认） | Ollama registry | ~600 MB |
| oMLX + 自定义模型 | 本地模型仓 | 视模型而定 |
| TEI 镜像（`ghcr.io/huggingface/text-embeddings-inference`） | GHCR + HF Hub | 模型权重 + 镜像（合计 几 GB） |

⚠️ Milvus 集合维度已经被 `bge-m3` 的 1024 维硬约束（`docs/deployment/02-environment.md` 第 2.5 节）；如果换 embedding，必须重建 Milvus 集合或带同维度模型。

### 2.5 数据卷（若只迁空系统，可省；带数据则必须带）

| Volume | 数据类型 | 导出方式 |
| :--- | :--- | :--- |
| `pg_data` | 元数据 + session_token_usage | `pg_dump -F c` |
| `neo4j_data` | 本体图 | `neo4j-admin dump`（社区版需停机） |
| `milvus_data` / `milvus_etcd` / `milvus_minio` | 向量 + 集合元数据 | `milvus-backup` |
| `mysql_data` | `wms_demo` 业务库 | `mysqldump` |
| 配置：前端 `dist` | 由 build 产物 | 重新 `docker build` |

### 2.6 配置与密钥

- `docker/.env`：POSTGRES_PASSWORD / NEO4J_PASSWORD / MYSQL_PASSWORD / SECRET_KEY（Fernet 44B）/ MINIO_KEY / 各 LLM key
- `docker/init/`：MySQL 初始化 SQL（自动跑）
- `backend/app/seed_models.py`、`seed_ontology.py` 输出的种子行（如要复现历史种子）

> **必知原则**：SECRET_KEY 一旦更换，库里所有 `datasource.api_key` 和 `model_config.api_key_encrypted` 都解不开。详见 `docs/deployment/07-operations.md §7.5.3`。

---

## 三、离线打包流水线（外网机 → 离线包）

> 强烈建议把这一步在**当前能上外网的机器**上完成，产物是 1 个目录 + 若干 tar，可以刻 U 盘 / 内部共享文件柜 / 内部镜像仓库搬运。

### 3.1 准备外网中转机

要求：装好 Docker Engine ≥ 24、Docker Compose v2、能 `docker login` 公开镜像仓库。可以是当前 dev 机，也可以另起一台。

### 3.2 拉全部镜像

```bash
mkdir -p offline/{images, wheels, npm, models, configs}
cd <qa-system-repo-root>

# 1. 公开镜像
docker pull postgres:16-alpine
docker pull neo4j:5.23-community
docker pull quay.io/coreos/etcd:v3.5.5
docker pull minio/minio:RELEASE.2023-03-20T20-16-18Z
docker pull milvusdb/milvus:v2.4.6
docker pull mysql:8.0

# 2. 构建后端 / 前端镜像（这一步会下载 apt 源 + uv 二进制 + npm 包）
docker compose -f docker/docker-compose.yml --env-file docker/.env.example build backend frontend

# 镜像改名方便 load
docker tag qa-system-backend   local/qa-backend:offline-$(date +%Y%m%d)
docker tag qa-system-frontend  local/qa-frontend:offline-$(date +%Y%m%d)
```

### 3.3 把镜像打包成 tar

```bash
cd offline/images
IMAGES=(
  postgres:16-alpine
  neo4j:5.23-community
  quay.io/coreos/etcd:v3.5.5
  minio/minio:RELEASE.2023-03-20T20-16-18Z
  milvusdb/milvus:v2.4.6
  mysql:8.0
  local/qa-backend:offline-<DATE>
  local/qa-frontend:offline-<DATE>
)
for img in "${IMAGES[@]}"; do
  safe=$(echo "$img" | tr '/: ' '___')
  docker save "$img" -o "${safe}.tar"
done
ls -lh offline/images
```

### 3.4 离线 wheels（Python）

```bash
# 用现成的 dev venv 导出；没有就先 uv sync
cd backend
uv export --no-hashes --format requirements-txt > ../offline/wheels/backend.txt
uv pip download \
  -d ../offline/wheels/pkg \
  -r ../offline/wheels/backend.txt \
  --python-version 3.12 --python-platform manylinux2014_x86_64
```

> 如果目标机是 `aarch64`，把 `manylinux2014_x86_64` 换成 `manylinux2014_aarch64`。混部需要两个平台都下一遍。

### 3.5 离线 npm 包（前端构建期）

```bash
cd frontend
# 1. 只打运行时 + 构建期（不装 dev 的 playwright 浏览器）
npm ci --omit=dev --ignore-scripts    # 先确保前端能 build
# 2. 把 node_modules 跟 package-lock.json 一起搬走
tar czf ../offline/npm/frontend-modules.tgz node_modules package-lock.json
```

如果目标机**要跑测试 / e2e**，再单独缓存 dev 依赖：

```bash
# 完整 dev 依赖（含 playwright）
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm ci
npx playwright install    # 这一步需要外网下载浏览器二进制；离线时把浏览
                          # 器目录也按 tar 搬运（~/.cache/ms-playwright）
tar czf ../offline/npm/frontend-full.tgz node_modules package-lock.json
tar czf ../offline/npm/playwright-browsers.tgz "$HOME/.cache/ms-playwright"
```

### 3.6 Embedding 模型权重（按需）

```bash
# 方案 A：Ollama 跑 bge-m3
ollama pull bge-m3
# Ollama 模型在 ~/.ollama/models，按目录压缩即可
tar czf offline/models/ollama-bge-m3.tgz -C ~/.ollama models

# 方案 B：用 TEI，自带镜像
docker pull ghcr.io/huggingface/text-embeddings-inference:1.5
docker save ghcr.io/huggingface/text-embeddings-inference:1.5 -o offline/images/tei-1.5.tar
# HF 模型权重用 huggingface-cli 拉到本地，再 huggingface-cli upload-all-arch 不可；
# 直接把模型目录（多文件）也按 tar 带过去：
huggingface-cli download BAAI/bge-m3 --local-dir offline/models/bge-m3
```

### 3.7 把代码、配置、数据卷产物打包

```bash
# 代码
tar --exclude='**/node_modules' --exclude='**/.venv' \
    -czf offline/code.tgz -C ../.. MyWiki/wiki/aicode/qa-system

# （可选）数据迁出
docker compose -f docker/docker-compose.yml up -d postgres neo4j mysql-demo milvus-standalone
# 停 backend → 备份 → 停机 → dump
# 详见 §五

# 环境变量模板（去掉真实 secret）
cp docker/.env.example offline/configs/.env.example
```

### 3.8 校验包完整性

```bash
cd offline
sha256sum images/*.tar wheels/pkg/*.whl npm/*.tgz models/*.tgz configs/.env > MANIFEST.sha256
echo "Total:"
du -sh images wheels npm models configs
```

> 经验值：全栈约 8-12 GB（含 Milvus/Neo4j/前端依赖），如带 embedding 权重再 +1-3 GB。

---

## 四、目标机离线导入（无外网 Linux）

### 4.1 装机与基础环境

要求：Ubuntu 22.04 LTS / Debian 12 / Rocky 9 之一；防火墙关闭或仅放 80/443。

```bash
# 必须只能从离线包安装；apt 源走内网或本地 deb 仓库，没有就走 §4.2 提前做好
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER  # 重新登录生效

# 时区建议 UTC
sudo timedatectl set-timezone UTC
```

如果内网没有 apt 源，需要：

```bash
# 把 dpkg 包也带过去
sudo apt-get download docker.io docker-compose-plugin containerd runc
# 或者用 apt-mirror 整库预下载
```

### 4.2 解包 + load 镜像

```bash
cd /opt
tar xf /path/to/offline/code.tgz
mkdir -p qa-system && mv MyWiki/wiki/aicode/qa-system/* qa-system/  # 视包结构而定

cd /opt/qa-system/offline/images
for f in *.tar; do
  docker load -i "$f"
done
docker images | grep -E 'qa-|postgres|neo4j|milvus|minio|etcd|mysql'
```

### 4.3 安装 Python / Node（如目标机没装）

镜像里其实**带了** python 与 node，只在 compose 内部用，所以目标机**不必**装 Python/Node，只需要 Docker。

唯一例外：如果种子脚本要在目标机**外部**跑，先把 Python 跟 `cryptography` wheels 装好——但更推荐直接进 backend 容器里跑（见 §四.4）。

### 4.4 写 .env 并启动

```bash
cd /opt/qa-system/docker
cp .env.example .env

# 必须修改：
#   POSTGRES_PASSWORD / NEO4J_PASSWORD / MYSQL_ROOT_PASSWORD / MINIO 密钥
#   SECRET_KEY（44B Fernet，固定后永不更换！）
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 启动
docker compose -f docker-compose.yml --env-file .env up -d
```

> EMBEDDING_HOST_OVERRIDE 默认 `host.docker.internal`：目标机若是纯 Linux（无 Docker Desktop），需确认 `docker-compose.yml` 里的 `extra_hosts` 仍然解析到宿主；`extra_hosts: "host.docker.internal:host-gateway"` 在新版 Docker 已自带，无需改。

### 4.5 初始化数据

```bash
# 等所有 healthy
docker compose ps
# 等 30s 后 alembic 迁移（容器启动已自动 upgrade head，但仍可手动补跑）
docker compose exec backend uv run alembic upgrade head

# 种子（仅空库首次）
docker compose exec backend uv run python seed_models.py
docker compose exec backend uv run python seed_ontology.py
```

如果是带数据的迁移，不要在 compose 里跑 `up -d`，先恢复 dump（见 §五）。

---

## 五、带数据迁移的额外步骤

> 若首次上线的目标机**已经有**或**要带过去**历史数据：

### 5.1 导出（在原机器）

```bash
# Postgres
docker compose exec -T postgres pg_dump -U qa_user -d qa_metadata -F c \
  > /backup/pg-$(date +%Y%m%d).dump

# Neo4j（社区版需停后端）
docker compose stop backend
docker compose exec -T neo4j neo4j-admin dump --database=neo4j --to=/tmp/neo4j.dump
docker compose cp neo4j:/tmp/neo4j.dump /backup/neo4j.dump
docker compose start backend

# Milvus：用 milvus-backup（推荐）
# 见 https://milvus.io/docs/backup.md
docker compose exec milvus-standalone milvus_cli --host localhost backup create -n full
# 或挂 milvus-backup 容器：
#   docker run --rm -v milvus_data:/milvus_data milvusdb/milvus-backup \
#     backup create -n full -o /backup

# MySQL demo
docker compose exec -T mysql-demo mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" wms_demo \
  | gzip > /backup/mysql-$(date +%Y%m%d).sql.gz
```

把 `/backup/*` 一并带过去。

### 5.2 导入（在目标机）

```bash
# Postgres
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U qa_user -d qa_metadata --clean --if-exists \
  < /backup/pg-XXX.dump || true  # 用 -c 单事务见 ops 文档

# Neo4j（需要目标机停 backend）
docker compose up -d neo4j
docker compose exec -T neo4j neo4j-admin load --from=/tmp/neo4j.dump --database=neo4j --force

# Milvus：用 milvus-backup 容器或 cli import
# MySQL
gunzip -c mysql-XXX.sql.gz | docker compose exec -T mysql-demo mysql -uroot -p"$MYSQL_ROOT_PASSWORD" wms_demo
```

---

## 六、上线后验证 Checklist（按顺序）

1. **容器健康**：`docker compose ps` 全部 `healthy` / `Up`。
2. **DB 连接**：浏览器打开 `http://<host>:7474/`（Neo4j Browser）能登录。
3. **前端静态**：`curl http://<host>/` 拿到 `index.html`。
4. **API doc**：`curl http://<host>:8000/docs` 返回 Swagger UI。
5. **`/health`**：`curl http://<host>:8000/health` 返回 200。
6. **模型激活**：UI → 模型配置，随便配 1 个本地 Ollama / OpenAI 兼容的 provider，点激活。
7. **NL2SQL 冒烟**：用模型管理里现成的 demo question（或用 `seed_models.py` 自带 demo）验证整链路：自然语言问题 → SQL → 数据 → 图表 option。
8. **回写校验**：`session_token_usage` 表能看到新行；Milvus `ontology_embeddings` collection 在 `attu`/cli 里能查到。
9. **看日志**：`docker compose logs --tail=200 backend` 没有 stack trace。
10. **离线验证**：用 `nc -zv <host> 443 <host> 80` 确认仅 80/443 对外可达（生产网络隔离）。

---

## 七、回滚方案

> **必做**：上线前在旧机器保留 7 天的 `pg/neo4j/milvus` 备份；新机器尽量用**新端口 / 新 volume 名**（如 `qa_pg_data_v2`）做并行部署，验证通过再切换流量。

| 故障 | 处理 |
| :--- | :--- |
| 容器起不来 | `docker compose logs <svc>`；常见是 `.env` 语法错或端口冲突 |
| Alembic 失败 | 备份 `pg.dump` 在；`alembic downgrade -1` 单步回退；最差 `pg_restore` |
| Milvus 大版本不兼容 | 升级路径 v2.4→v2.5+ 需先全量导出再装新版再导入；保留 v2.4 镜像作为回退 |
| 镜像损坏 / load 失败 | SHA256 校验；用 §三.3 重打 |
| 客户端禁用外网规则变更 | 需配合甲方走变更申请（变更到「禁止所有外联」之前先把流程走完） |

整回退：

```bash
# 停新机 → 切流量回旧机 → 旧机无需重启（保留原数据）
ssh old-host "docker compose -f /opt/qa-system/docker/docker-compose.yml ps"
# 旧机用旧 SECRET_KEY / 旧密钥 → 客户端无感知
```

---

## 八、风险与注意事项（容易踩坑）

1. **`docker-compose.yml` 的 `extra_hosts: host.docker.internal:host-gateway`**：
   - macOS / Win Docker Desktop 自带解析；纯 Linux 须 Docker ≥ 20.10。遇到容器里 `host.docker.internal` 不可达，先 `docker network inspect` 看默认 bridge。
2. **`host-gateway`** 不是所有 Docker 版本都支持，详见 `docker compose` 文档；如失败改用 `network_mode: host` 跑后端。
3. **Neo4j apoc 插件**：`NEO4J_PLUGINS: '["apoc"]'` 镜像启动时会下载；离线要么预先 docker save 时把镜像里的 `/var/lib/neo4j/plugins` 覆盖好，要么离线移除该插件（评估是否真的用到）。
4. **Milvus / etcd / minio 三件套**：分三个容器；任一缺失都会让 milvus-standalone 起不来。**全部 docker save**。
5. **MySQL demo 的 `init/` 目录**：原机器若是首次启动后产生的 schema，需另导一次 `mysqldump`；如果目标机希望完全重建 demo，可以留 `init/` 让它重新跑。
6. **前端 `dist` 在镜像里**：离线后浏览器访问时检查 chunk 文件能正常加载（nginx 默认 gzip 是否带取决于 `nginx.conf`，当前已配 `try_files`，通常 OK）。
7. **证书**：离线后 Let's Encrypt 失败，准备自签证书挂到 Caddy/Nginx；或干脆不上 HTTPS，纯内网 HTTP（**风险**+1：审计要求 TLS 时必须补）。
8. **审计 / 合规**：离线部署后，**所有第三方依赖（含 npm/pip wheels）必须随包归档**，便于日后的 license 审计（`pip-licenses` / `npm ls`）。
9. **备份**：上线后**第一周**每天全量；之后按 `docs/deployment/07-operations.md §7.3` 的 cron。
10. **SECRET_KEY 不要先在 dev 机器生成再带到目标机**：必须在目标机本机生成；保留安全的副本到 1Password/Vault。

---

## 九、简化版"先做最少迁移"路径

如果你只想要**先有可用的、最小可工作的 qa-system**，按下面顺序做（每一步独立可回滚）：

1. 后端镜像 + Postgres 镜像 + 前端镜像：约 1-2 GB，先把服务起跑、空 `.env` 跑通。
2. Neo4j 镜像：本体为空也能起，验证后再灌数据。
3. Milvus + etcd + MinIO 三件套：让向量检索生效。
4. MySQL demo：如暂时不用，可以从 compose 注释掉。
5. Embedding 服务：放到最后再激活（不在 docker-compose 里跑，便于模型权重的独立版本管理）。

---

## 相关条目
- [[多步查询"执行计划"缺失的回归排查]]
- `qa-system/docs/deployment/00-overview.md`
- `qa-system/docs/deployment/02-environment.md`
- `qa-system/docs/deployment/07-operations.md`
