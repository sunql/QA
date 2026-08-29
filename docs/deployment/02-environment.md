# 2. 环境变量与密钥

整个系统所有运行时配置都通过 `docker/.env`（容器内）和 `backend/.env`（本地直跑时）两个文件注入。本文档解释每个字段的语义、生成方式和生产建议。

---

## 2.1 文件位置

| 路径 | 作用 |
|---|---|
| `docker/.env` | docker-compose 变量替换、前端构建期变量、镜像内通用变量 |
| `backend/.env` | 后端 Pydantic Settings 读取（仅本地直跑 `uvicorn` 时用） |
| `frontend/.env` | 前端 Vite 环境变量（可选，覆盖 docker compose 构建参数） |

> ⚠️ **坑位**：`backend/app/config.py` 用 `env_file=".env"`（相对路径），必须从 `backend/` 目录启动 uvicorn，或在容器内运行。直接在前端目录 cd 后跑 uvicorn 会读到错误的环境。

---

## 2.2 必填字段

### 关键密钥

```bash
# 必须 44 字节的 URL-safe base64 字符串（Fernet 协议）
SECRET_KEY=KO9d4_ynyXQEHhg2_NB9QPdc9IG5ACnvrdUB9s4LYeM=
```

生成方式（任选其一）：

```bash
# 1. Python cryptography
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. OpenSSL（输出需要 url-safe base64）
openssl rand -base64 32

# 3. 容器内
docker compose run --rm backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

⚠️ **关键原则**：
- 一旦生成，**永远不要中途更换**。.env 中加新字段时旧的也保留，否则历史 datasource / apikey 全部解密失败。
- 备份到密钥管理（1Password / Vault），不要只 commit 在 git。
- **生产** SECRET_KEY 失效等同全库清密文，需提前录入应急。

### 数据库密码

| 变量 | 默认 | 说明 |
|---|---|---|
| `POSTGRES_USER` | `qa_user` | Postgres 用户 |
| `POSTGRES_PASSWORD` | `qa_pg_dev_2026` | **生产必须改** |
| `POSTGRES_DB` | `qa_metadata` | 库名 |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `qa_neo4j_dev_2026` | **生产必须改** |
| `NEO4J_AUTH` | `neo4j/qa_neo4j_dev_2026` | 与上一行同步 |
| `MINIO_ROOT_USER` | `minioadmin` | Milvus 对象存储 |
| `MINIO_ROOT_PASSWORD` | `minioadmin` | **生产必须改** |
| `MYSQL_ROOT_PASSWORD` | `root` | 演示用 MySQL |
| `MYSQL_DATABASE` | `wms_demo` | 演示库 |

生产环境生成一组随机密码：

```bash
PG_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)
NEO4J_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)
MINIO_PASS=$(openssl rand -base64 18 | tr -d '/+=' | head -c 24)
```

---

## 2.3 内部连接（主机名 = docker 服务名）

容器间互相访问，**用 docker 服务名**而非 localhost：

```bash
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@postgres:5432/qa_metadata
NEO4J_URI=bolt://neo4j:7687
MILVUS_URI=http://milvus-standalone:19530
DATASOURCE_MYSQL_HOST=mysql-demo
```

主机直跑后端（`uvicorn`），需要改为 `localhost`：

```bash
DATABASE_URL=postgresql+asyncpg://qa_user:qa_pg_dev_2026@localhost:5432/qa_metadata
NEO4J_URI=bolt://localhost:7687
MILVUS_URI=http://localhost:19530
```

**完整 .env 模板**（生产）：

```bash
# docker/.env
SECRET_KEY=<44字节Fernetkey>

# Postgres
POSTGRES_USER=qa_user
POSTGRES_PASSWORD=<pg-pass>
POSTGRES_DB=qa_metadata

# Neo4j
NEO4J_USER=neo4j
NEO4J_PASSWORD=<neo4j-pass>
NEO4J_AUTH=neo4j/<neo4j-pass>

# MinIO
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=<minio-pass>

# MySQL demo
MYSQL_ROOT_PASSWORD=<mysql-root-pass>
MYSQL_DATABASE=wms_demo

# 后端读取
DATABASE_URL=postgresql+asyncpg://qa_user:<pg-pass>@postgres:5432/qa_metadata
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<neo4j-pass>
MILVUS_URI=http://milvus-standalone:19530
DATASOURCE_MYSQL_HOST=mysql-demo
DATASOURCE_MYSQL_PORT=3306
DATASOURCE_MYSQL_USER=root
DATASOURCE_MYSQL_PASSWORD=<mysql-root-pass>

# 业务开关
SESSION_BUDGET=10.0
USE_ES=true
CORS_ORIGINS=https://qa.example.com
```

---

## 2.4 Embedding / LLM 模型密钥

模型密钥（OpenAI、DeepSeek、Azure 等）**不存 .env**，而是运行时通过前端「模型管理」页面写入，Fernet 加密后存 Postgres `model_config.api_key_encrypted` 字段。

激活后端不需要任何 LLM key 即可启动——embedding 自带本地 fallback（sentence-transformers），但仅供开发测试；生产**必须配置**至少一个 LLM provider。

写入方式：

```bash
# 推荐：UI 页面（模型配置）→ 新增模型
# 或后端 API
curl -X POST http://localhost:8000/api/v1/models \
  -H "Content-Type: application/json" \
  -d '{
    "modelName": "deepseek-chat",
    "provider": "OPENAI_COMPATIBLE_PROXY",
    "apiEndpoint": "https://api.deepseek.com/v1",
    "apiKey": "sk-...",
    "costPer1KInput": 0.0014,
    "costPer1KOutput": 0.0028,
    "maxInputTokens": 64000,
    "weight": 2,
    "isActive": true
  }'
```

> Pydantic `to_camel` 把 `cost_per_1k_input` 序列化为 `costPer1KInput`（**K 大写**）。前端错了大小写就会出现 `Cannot read properties of undefined (reading 'toFixed')` 之类的崩溃，已在 issue 中标记。

### 已知 provider 枚举

| provider | 适用 |
|---|---|
| `OPENAI` | OpenAI 官方 |
| `OPENAI_COMPATIBLE_PROXY` | OpenAI 兼容（DeepSeek、SiliconFlow、智谱 GLM、oMLX 等） |
| `OLLAMA` | 本地 Ollama |
| `AZURE_OPENAI` | Azure 部署 |
| `ANTHROPIC` | Claude（NL2SQL 暂不直接消费，仅 embedding/prompts） |

---

## 2.5 Embedding 服务

向量库用 Milvus，但**embedding 本身**由后端调用外部服务。

| 场景 | 推荐 | 配置 |
|---|---|---|
| 生产（中文本体） | 自建 bge-m3 / bge-large-zh-v1.5 | 走 OpenAI 兼容协议（tei/text-embeddings-inference 或 oMLX） |
| 内部 demo | oMLX（apple silicon）或 Ollama 跑 `nomic-embed-text` | `provider=OPENAI_COMPATIBLE_PROXY` + `apiEndpoint=http://host.docker.internal:8888/v1` |
| 不想本地跑 | OpenAI `text-embedding-3-small` | 直接 `apiKey=sk-...` |

⚠️ **坑位**：
- **Mac dev 走 oMLX**：`apiEndpoint=http://host.docker.internal:8888/v1`（docker 访问宿主机），开启鉴权后 `apiKey` 不能留空，本地无 key 服务用 `placeholder` 占位也行（详见 `backend/app/services/embedding/`），但生产一定要真实鉴权。
- **keyless 本地 embedding**：`AsyncOpenAI` 拒绝空 api_key，本地无 key 服务须用占位 key（如 `EmbeddingClient._KEYLESS_PLACEHOLDER`）。

---

## 2.6 端口冲突排查

生产部署前先确认宿主机端口干净：

```bash
# 列出占用关键端口的进程
sudo lsof -iTCP -sTCP:LISTEN -P | grep -E ':(80|443|5432|7687|19530|3306) '

# 停掉冲突服务（按具体情况）
sudo systemctl stop postgresql
sudo systemctl stop nginx
brew services stop mysql  # 宿主机 mysql
```

---

## 2.7 .env 模板文件

git 仓库已自带 `docker/.env.example`（如果找不到就以本节内容为准）。**绝对不要**把它当作真实部署的 `.env` 直接使用——SECRET_KEY 必须重新生成。

```bash
cd docker
cp .env.example .env
# 编辑 .env，替换所有密码与 SECRET_KEY
```

---

完成后，进入 [03-docker-compose.md](03-docker-compose.md)。