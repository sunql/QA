# CentOS 7 生产部署与数据前移 — 设计稿

> **状态**：🟡 OPEN（设计已评审通过，待写实施计划）
> **日期**：2026-10-22
> **现状冻结**：`origin/main` `0ea463b3`（Merge PR #66 fix/aichat-audit-batch） / macOS 生产 `alembic_version = 0132`
> **路线**：方案 A —— 全容器 Docker Compose + 联网 x86_64 机器离线造镜像 + 一次性单向数据导数
> **决策记录**：见 §4「关键设计决策」

---

## 1. 目的

把 qa-system 从 macOS Docker Desktop 环境前移到 **CentOS 7.9 / x86_64 / 16C32G / 内网隔离** 的生产服务器，保留全部现有业务数据，并让 macOS 继续作为开发机长期双活。

本文档是**部署设计**，不是实施计划。实施计划由 writing-plans 阶段产出。

### 1.1 目标环境约束（用户确认）

| 项 | 值 | 备注 |
|---|---|---|
| OS | CentOS 7.9 | **硬约束，无法更改**。已 EOL（2024-06-30） |
| 架构 | x86_64 | 与 macOS 开发机（arm64）不同 ⇒ 镜像不可从 Mac 直接导出 |
| 资源 | 纯 CPU，16C / 32G+，无 GPU | 本地跑 Ollama 承担 embedding + chat |
| 权限 | 有 root | 可装 Docker CE、改 SELinux/firewalld |
| 网络 | **内网隔离**（无公网） | 可达 `192.168.205.70`（THBI Oracle）；**不可达** Docker Hub / PyPI / npm / 云端 LLM |
| 出口 | 另有一台**联网 x86_64 机器** | 唯一的镜像/依赖/模型拉取出口 |
| 数据策略 | **一次性单向导数** | 导数后两边各自演化，不做持续同步 |
| 双活 | Mac 长期当开发机 | 接受 schema 漂移风险（§10） |

---

## 2. 范围与非范围

### 2.1 In scope

1. CentOS 7 宿主准备（Docker CE、SELinux、firewalld、XFS、时区）
2. 10 个容器在 CentOS 上的编排（现有 9 个 + 新增 ollama）
3. 离线镜像交付管线（联网机构建 → `docker save` → scp → `docker load`）
4. 4 类数据的一次性导数：PostgreSQL / Neo4j / qa-objects / MySQL demo
5. Milvus **重建 + 回填**（不搬卷）
6. Embedding provider 从 oMLX（Mac）切到 Ollama（CentOS 本地）
7. `docker/.env` 的 CentOS 侧取值清单
8. 数据导入后的业务配置修正（data_source / llm_config）
9. 冒烟验收清单
10. 双活漂移的已知缺口登记

### 2.2 Out of scope

- **持续数据同步机制**（PG 逻辑复制 / 定期 dump-restore）：用户已选一次性单向导数
- **HTTPS / 正式域名 / 证书签发**：内网隔离场景，先走 LAN IP + 5173。后续需要时再补宿主机 nginx
- **CI/CD 流水线搭建**：仓库当前无 `.github/workflows`（见 `qa-system-no-ci-main-can-go-red`），本次不引入
- **CentOS 宿主直接跑中间件**（裸装 PG/Neo4j/Milvus）：见 §4.3 已否决方案 C
- **K8s / 分布式部署**：`docs/deployment/06-distributed.md` 的方案本次不启用
- **对话模型选型**：由用户在管理页自行配置，本 spec 只保证 Ollama 端点可达 + 离线模型预置通道打通（§7.5）

---

## 3. 现状冻结（as of 2026-10-22，`origin/main` `0ea463b3`）

### 3.1 服务拓扑

`docker/docker-compose.yml`（`name: qa-system`）定义 9 个服务：

| 服务 | 容器名 | 镜像 | 宿主端口 | 数据卷 |
|---|---|---|---|---|
| postgres | `qa-postgres` | `postgres:16-alpine` | 5433→5432 | `pg_data` |
| neo4j | `qa-neo4j` | `neo4j:5.23-community` | 7474, 7687 | `neo4j_data` |
| milvus-etcd | `qa-milvus-etcd` | `quay.io/coreos/etcd:v3.5.5` | — | `milvus_etcd` |
| milvus-minio | `qa-milvus-minio` | `minio/minio:RELEASE.2023-03-20T20-16-18Z` | — | `milvus_minio` |
| milvus-standalone | `qa-milvus` | `milvusdb/milvus:v2.4.6` | 19530, 9091 | `milvus_data` |
| qa-objects | `qa-objects` | `minio/minio:RELEASE.2024-06-13T22-53-53Z` | 9000, 9001 | `objects_data` |
| mysql-demo | `qa-mysql-demo` | `mysql:8.0` | 3306 | `mysql_data` |
| backend | `qa-backend` | 构建自 `docker/Dockerfile.backend` | 8000 | — |
| frontend | `qa-frontend` | 构建自 `docker/Dockerfile.frontend` | 5173→80 | — |

### 3.2 数据体量（实测）

| 卷 | 占用 | 实际业务体量 |
|---|---|---|
| `pg_data` | 1.6 GB | 95 张 public 表，`ontology_class` 54 行，`data_source` 5 行，`system_config` 28 行 |
| `milvus_data` | 903 MB | **实际实体仅 4953 条**（见下表），SST 绝大部分是预分配空空间 |
| `milvus_etcd` | 440 MB | 元数据 |
| `neo4j_data` | 518 MB | **3753 节点 / 3277 关系** |
| `mysql_data` | 228 MB | demo 业务库 |
| `objects_data` | 13 MB | 知识源文件（pdf/docx/…） |
| `milvus_minio` | 31 MB | Milvus 对象存储 |
| **合计** | **≈ 3.7 GB** | |

Milvus 集合实体数实测（`pymilvus` 查询）：

| 集合 | 实体数 | 迁移方式 |
|---|---|---|
| `ontology_property_embeddings` | 3742 | 回填 |
| `query_embeddings` | 1061 | 回填（历史查询向量，可选丢弃） |
| `ontology_class_embeddings` | 50 | 回填 |
| `wiki_page_embeddings` | 64 | 回填 |
| `document_embeddings` | 36 | **重新 ingest**（无回填脚本，见 §6.5） |
| `ontology_metric_embeddings` | 0 | — |
| **合计** | **4953** | |

### 3.3 迁移敏感配置（本次必须处理）

#### 3.3.1 `SECRET_KEY` 是 Fernet 主密钥（致命项）

`backend/app/config.py:82` 的 `secretKey` 即 Fernet 主密钥，`backend/app/infrastructure/security/crypto.py:24-33` 的 `_getFernet()` 用它构造 `Fernet` 实例。以下三处数据用它加密，密文前缀已实测确认为 `gAAAAA`（Fernet v0x80 token）：

| 表.列 | 行数 | 后果 |
|---|---|---|
| `data_source.password_encrypted` | 5 | 含 **THBI Oracle 19c 生产库密码** ⇒ NL2SQL 全线不可用 |
| `llm_config.api_key_encrypted` | 6（3 条激活） | 所有模型调用鉴权失败 |
| `embedding_provider.api_key_encrypted` | 1 | embedding 鉴权失败 |

> ⚠️ **`docs/deployment/00-overview.md:49` 与 `02-environment.md:32` 教的是"生成新 key"**：
> ```bash
> python -c "from cryptography.fernet import Fernet; print('SECRET_KEY='+Fernet.generate_key().decode())" >> docker/.env
> ```
> 这条命令在**空环境首次部署**时正确，在**带数据迁移**时是破坏性的。迁移场景必须逐字复制源环境 `SECRET_KEY`。

#### 3.3.2 Embedding 现状

`embedding_provider` 3 行，激活行是 `id=2 oMLX bge-m3 8bit`，`provider_type=omlx`，`base_url=http://127.0.0.1:8888/v1`，`dimension=1024`。该服务运行在 **macOS 宿主机**（Apple Silicon MLX），容器经 `host.docker.internal:host-gateway` 访问。

解析链（读码确认，非推测）：
- `backend/app/infrastructure/llm/embedding_provider_factory.py:51-88` `getActiveEmbeddingClient()` —— **DB 激活行优先于 env**；env 仅在无激活行或 DB `OperationalError` 时回退
- `backend/app/infrastructure/llm/embedding_provider_factory.py:44-46` `KNOWN_PROVIDER_TYPES = ("ollama", "omlx", "openai_compatible")`
- 三种类型**共用同一个 OpenAI 兼容客户端**，走 `/v1/embeddings`，**无 Ollama 原生 `/api/embeddings` 分支**
- `backend/app/infrastructure/llm/embedding_client.py:35-52` `_applyHostOverride()` —— 仅当 base_url 主机名是 `localhost` / `127.0.0.1` 时替换为 `EMBEDDING_HOST_OVERRIDE`；**override 为空则静默 no-op**（`:43-44`）
- 维度守卫 `embedding_provider_factory.py:160-177` 比对 `embedding_provider.dimension` 与 `backend/app/infrastructure/milvus_client.py:38` 的 `_DIM = 1024` 常量，不一致抛 `ConfigError`（HTTP 400）
- `docker/docker-compose.yml:166-169` **已配置** `extra_hosts: ["host.docker.internal:host-gateway"]`，注释明确说明兼容 Linux Docker

**结论**：`bge-m3` 在 Ollama 与 oMLX 上同族同 1024 维 ⇒ `_DIM` 不变、集合无需重建、维度守卫照常通过、已有向量在维度上仍有效。

#### 3.3.3 LLM 现状

`llm_config` 6 行：

| id | provider | model_name | 激活 | api_endpoint |
|---|---|---|---|---|
| 1 | openai_compatible_proxy | deepseek-chat | ✅ | `https://api.deepseek.com/v1`（**内网不可达**） |
| 3 | openai_compatible_proxy | Qwen3.8-27B-4bit | ✅ | `http://host.docker.internal:8888/v1`（**指向 Mac oMLX**） |
| 9 | openai_compatible_proxy | MiniMax-M3 | ✅ | `https://api.minimax.cn/v1`（**内网不可达**） |
| 8 | moonshot | kimi-k3 | ❌ | `https://api.minimax.cn/v1/chat/completions` |
| 10 / 11 | openai_compatible_proxy | gemma-4-31b-it-8bit / DeepSeek-V4-Pro… | ❌ | `http://host.docker.internal:8888/v1`（指向 Mac oMLX） |

**3 条激活模型在 CentOS 上全部不可用**（2 条云端不可达 + 1 条指向 Mac）。

#### 3.3.4 数据源现状

`data_source` 5 行，仅 1 条是真业务依赖：

| id | name | type | host:port | db | CentOS 上的可用性 |
|---|---|---|---|---|---|
| 1 | THBI Oracle | oracle 19c | `192.168.205.70:1521` | X3V71ORA | ✅ **唯一必须保留** |
| 2 | localmysql | mysql | `host.docker.internal:3306` | `srm` | ❌ CentOS 该端口是 compose 自带 `mysql-demo`（库名 `wms_demo`），库名不匹配 |
| 3 | smoke | postgresql | `h` | `d` | ❌ 烟囱探针 |
| 4 | recall-probe | postgresql | `localhost:5432` | qa_metadata | ❌ 探针 |
| 5 | phase13_realdata_pg | postgresql | `localhost:5433` | qa_metadata_test | ❌ 探针 |

#### 3.3.5 容器启动契约

`docker/Dockerfile.backend` 的 `CMD` 是 `sh -c "alembic upgrade head && uvicorn ..."`。`alembic upgrade head` 失败会 `&&` 短路导致 uvicorn 根本不起 —— 这就是 `qa-system-deploy-source-must-be-fresh-worktree` 记录的事故成因（主仓缺 `0132` 而 prod 已在 `0132` ⇒ `Can't locate revision` ⇒ 后端整体挂掉）。

当前 `origin/main` 的迁移链含 `0132_kpi_catalog_datasource_fk_restrict.py`，与生产 `alembic_version=0132` 一致 ⇒ 迁移数据后 `alembic upgrade head` 为 no-op。

---

## 4. 关键设计决策

### 4.1 D1：全部中间件保留在容器内（方案 A）

**决策**：9 个中间件 + backend + frontend 全部跑 Docker 容器，CentOS 宿主不装任何应用级中间件。

**理由**：容器自带完整 userspace（glibc、库、二进制）。CentOS 宿主的 **glibc 2.17 / systemd 219 完全不影响容器内部**。这是 CentOS 7 这条路上最关键的一条 —— 把所有东西容器化，就把"宿主机太老"这个约束消解掉了。

### 4.2 D2：Ollama 跑成容器，而非宿主机二进制

**决策**：对话与 embedding 模型都在 `ollama/ollama` 容器内提供。

**理由**：Ollama 官方 Linux 二进制要求 **glibc ≥ 2.28**，CentOS 7 是 2.17。直接安装会**静默失败** —— 安装脚本跑完、`/usr/local/bin/ollama` 存在，但执行报 `GLIBC_2.28 not found`，`ollama` 命令永远不注册到 PATH。容器化后 glibc 来自镜像，问题消失。

**副作用**：模型 blob 存放在容器 volume `/root/.ollama`，需在离线环境预置（§6.3）。

### 4.3 D3：否决「宿主机装中间件」（方案 C）

**否决理由**（三条独立的硬阻断）：
- **PostgreSQL 16**：PGDG 官方不提供 RHEL 7 / CentOS 7 的 RPM，PG16 最低要求 RHEL 8
- **Neo4j 5.x**：要求 glibc ≥ 2.28，宿主 2.17 直接跑不了
- **Milvus / etcd**：同样存在 RHEL 7 安装包缺口

方案 C 把容器已经解决的问题重新引回来，且没有收益。

### 4.4 D4：镜像从联网 x86_64 机器出，不从 Mac 出

**决策**：新增一台（或复用现有的）**联网 x86_64 机器**作为构建与拉取出口，`docker save` 后 scp 到 CentOS。

**理由**：Mac 是 **arm64（Apple Silicon）**。`docker save` 出的是 arm64 镜像，在 x86_64 CentOS 上 `docker load` 虽成功但**无法运行**，且错误信息不直观。必须用 `--platform linux/amd64` 在 x86 机器上拉取/构建。

### 4.5 D5：Milvus 重建 + 回填，不搬卷

**决策**：CentOS 上新建空集合，用现有脚本回填向量；不复制 `milvus_data` / `milvus_etcd` / `milvus_minio` 三个卷。

**理由**：
1. 实际实体仅 **4953 条**（§3.2），搬 1.37 GB 卷（其中绝大部分是预分配空空间）收益为零
2. 搬卷要求 etcd / WAL / binlog / segment 版本严格对齐，脆且回滚困难
3. `bge-m3` 换运行时（MLX → GGUF）后向量不逐位相同，重建比搬运更符合语义
4. 现成脚本已覆盖本体与 wiki 两类（§6.5），无需新写工具

**代价**：`document_embeddings`（36 条）没有回填脚本，只能重新 ingest —— 用户已确认接受并在本次执行。

### 4.6 D6：一次性单向导数，不做持续同步

用户已选定。导数完成后 Mac 与 CentOS 各自演化。代价见 §10。

---

## 5. 已核实的 CentOS 7 事实

### 5.1 Docker CE 在 CentOS 7 的官方包（第一手核实）

Docker 官方仓库 `https://download.docker.com/linux/centos/7/x86_64/stable/` 的 `repodata`（`<revision>1717607461` = **2024-06-04 UTC**）**已冻结**（Docker 停止为 CentOS 7 发包的时刻）。经代理拉取 `primary.xml.gz` 解析（337 包；`docker-ce` 99 个版本 / `docker-ce-cli` 82 / `docker-compose-plugin` 26 / `containerd.io` 52），**再绕过元数据直接 HTTP HEAD 探测 RPM 本体交叉验证**：

| 包 | 最高 el7 x86_64 | 本方案选用 | HEAD 验证 |
|---|---|---|---|
| `docker-ce` | 26.1.4-1.el7 | **24.0.9-1.el7** | ✅ 200 |
| `docker-ce-cli` | 26.1.4-1.el7 | **24.0.9-1.el7** | ✅ 200 |
| `containerd.io` | 1.6.33-3.1.el7 | 1.6.33-3.1.el7 | — |
| `docker-compose-plugin` | 2.27.1 | 2.27.1 | ✅ 200 |

`docker-ce` 版本族分布：`24.0.x`(至 **9**) / `25.0.x`(至 5) / `26.1.x`(至 4)。**24.0.9 是 24.0 系列最后一个补丁版**，本方案取它而非 24.0.7。

#### 为什么选 24.0.9 而不是可得的最高版 26.1.4

> ⚠️ **"RPM 存在" ≠ "守护进程能在 kernel 3.10 上起来"**。仓库冻结后无人再对 CentOS 7 做持续验证，**不存在任何一手来源确认或否认这些版本能否在 kernel 3.10 + systemd 219 上启动**。

需要说清楚的是：**24.0.9 的运行时同样未经任何一手来源验证**。选它不是"这个已验证那个没验证"，而是**在两个都未验证的选项里取更保守的那个**：

| | 24.0.9（选用） | 26.1.4（可得最高） |
|---|---|---|
| CentOS 7 上的运行时验证 | 无一手来源 | 无一手来源 |
| runc 世代风险 | 更早，低于社区已在 CentOS 7 上报障的 `runc create failed: EOF: unknown` 世代 | 更新，正落在该风险区间内 |
| 反方论据（**记录在案，供将来推翻**） | — | 它是 Docker 为 el7 发布的**最后一版**。按"平台停止支持前发布的版本通常被测得最充分"推断，它反而可能是 el7 上验证最充分的那个 |

选定 24.0.9，是为了让**happy path 上不残留任何未验证前提**。若 P1 实测 24.0.9 一切正常且希望换更新的，`26.1.4-1.el7` 就在同一仓库里，改版本号即可——但届时本节的未验证标记要一并转移过去，不能因为"换新版本所以标记失效"。

支持该系列可用的间接证据（均非"有人跑通过"）：
1. `docker-ce-*.el7` 是 **el7 原生构建**，符号依赖 `GLIBC_2.14` / `LIBSYSTEMD_209`，CentOS 7 的 glibc 2.17 / systemd 219 均满足 ⇒ **确实是链接到 CentOS 7 的**。但"链接得上" ≠ "起得来"。
2. runc 对 systemd 版本是**特性门控而非硬性最低要求**：读到版本 < 242 / < 244 时走 `logrus.Debugf` 并降级到 cgroupfs，找不到拒绝启动的代码路径。（⚠️ 该结论基于阅读 runc **1.1.14** 源码；若捆绑的是 runc 1.2.x 则不适用——P1 用 `rpm -q runc` 确认。）
3. `containerd.io` 虽声明对 `runc` 的无版本依赖、而 CentOS 7.9 extras 里只有 2018 年的 `runc-1.0.0-70.rc10`，但 **`containerd.io` 自带 runc**（filelist 含 `/usr/bin/runc`、`/usr/bin/containerd-shim-runc-v2`），该隐患不成立。

#### P1 必跑的实测命令（这才是判定，不是推断）

```bash
rpm -q runc containerd.io docker-ce docker-ce-cli   # 确认用的是自带的 runc，不是 extras 的 1.0.0-rc10
systemctl start docker && docker info                # 守护进程能否起来
docker run --rm hello-world                         # 容器能否真跑
docker compose version                              # compose 插件能否与该 engine 通信
```

#### 版本阶梯

| 级别 | 版本 | 何时用 | 代价 |
|---|---|---|---|
| **选用** | **`24.0.9-1.el7`** | 默认路径 | — |
| 可选升 | `26.1.4-1.el7` | 24.0.9 一切正常、且愿意承担上表的未验证标记 | 同上，仅换版本号 |
| 降级 | `20.10.24-1.el7` | 24.0.9 起不来（`failed to start daemon` / `runc create failed: EOF: unknown`） | compose 插件降到 2.20 档，失去 `--wait` 等新特性；方案其余部分不变 |

> 调研过程中多个中文技术站（phpnode.cn / yisu.com / CSDN 等）声称「Docker CE 23.x+ 要求 kernel ≥ 4.18，CentOS 7 最高只能 20.10.x」。**该结论无任何一手来源**——检索到的全部是 SEO 内容农场，其中还夹着可证伪的伪事实（某页称「Docker 24+ 默认要求 cgroup v2」）。而官方 repodata 直接证明 26.1.4 是 Docker 为 CentOS 7 发布的最后一个版本。**不要采信中文站的这个说法。**

### 5.2 镜像在 kernel 3.10 上的兼容性判断

| 镜像 | 判断 | 备注 |
|---|---|---|
| `postgres:16-alpine` | ✅ 安全 | musl 自带，无宿主 glibc 依赖 |
| `milvusdb/milvus:v2.4.6` | ✅ 安全 | compose 已配 `seccomp:unconfined` |
| `quay.io/coreos/etcd:v3.5.5` | ✅ 安全 | |
| `minio/minio` ×2 | ✅ 安全 | |
| `neo4j:5.23-community` | ✅ 安全 | Ubuntu 22.04 base，glibc 自带 |
| `python:3.12-slim`（backend） | ✅ 安全 | 当前是 Debian 13 trixie；glibc 声明的最低内核需求自 glibc 2.24 起一直未提升（`LIBC_LINUX_VERSION` 在 2.34→2.41 间逐字节一致），kernel 3.10 在规格内 |
| `node:20-alpine` / `nginx:alpine` | ✅ 安全 | 仅构建期/轻量运行时 |
| `mysql:8.0` | ⚠️ **需 CPU 检查** | Oracle Linux base 使用 x86-64-v2 基线。CPU 缺 SSE4.2/POPCNT/CX16 时降级到 `mysql:8.0.32` |

**唯一真实的内核 syscall 缺口**：`getrandom()` 于 kernel 3.17 引入，glibc 2.41 的封装**没有 ENOSYS 回退**。影响面是 CSPRNG 播种路径。本栈中 Postgres / Neo4j / Milvus / etcd / MinIO / Python 后端的熵源都是 `/dev/urandom` 设备节点（内核 2.6 即有），不是 `getrandom()` 系统调用 ⇒ **预期无影响**。若未来引入用 `getrandom` 播种的 Go/Rust 二进制并报 `ENOSYS`，查这里。

其余新版系统调用（`clone3` 5.3、`statx` 4.11、`faccessat2` 5.8、`rseq` 4.18）在 3.10 上均有内核回退路径或仅是非致命优化。

---

## 6. 实施阶段与门禁

每个阶段有**硬门禁**，不通过不进入下一阶段。失败即停，不"先往下走回头补"。

### P0 · 宿主勘察

**目的**：探明 CentOS 7 真实能力，决定后续分支。

```bash
uname -r                      # 需 ≥ 3.10.0-514（overlay2 的 RH 回移点）
grep -o 'sse4_2\|avx2\|popcnt\|cx16' /proc/cpuinfo | sort -u   # x86-64-v2 指令集（判 mysql:8.0）
xfs_info /var/lib/docker 2>/dev/null | grep ftype             # 必须是 ftype=1
getenforce                                                  # SELinux 状态
systemctl --version | head -1
free -h && nproc && df -h /var/lib/docker
ls -Zd /data /var/lib/docker                                 # SELinux 上下文
```

**门禁**：全部有输出且 `ftype=1`、`uname -r ≥ 3.10.0-514`。
**分支决策**：
- Docker 24.0.9 起不来 → 降级 **20.10.24**（见 §5.1 版本阶梯）
- CPU 缺 AVX2 → `mysql:8.0` 钉 `8.0.32`

### P1 · Docker 装机

```bash
# 联网机：把下列 4 个 RPM 下载到 /opt/qa-transfer/，连同依赖一起带进内网
#   https://download.docker.com/linux/centos/7/x86_64/stable/Packages/
yum install -y yum-utils device-mapper-persistent-data lvm2

# 内网 CentOS：离线安装（版本取 §5.1 版本阶梯的「选用」档）
yum install -y /opt/qa-transfer/docker-ce-24.0.9-1.el7.x86_64.rpm \
               /opt/qa-transfer/docker-ce-cli-24.0.9-1.el7.x86_64.rpm \
               /opt/qa-transfer/containerd.io-1.6.33-3.1.el7.x86_64.rpm \
               /opt/qa-transfer/docker-compose-plugin-2.27.1-1.el7.x86_64.rpm
systemctl enable --now docker
```
> `docker-ce-cli` **不可省**：它才提供 `docker` 命令本身，漏装则后续所有 `docker` 调用 command not found。
> 不要用 `curl https://get.docker.com | sh` —— 该脚本面向当前发行版，对 EOL 的 el7 会解析出错误的仓库配置。

**daemon.json**（`/etc/docker/daemon.json`）：

```json
{
  "storage-driver": "overlay2",
  "log-driver": "json-file",
  "log-opts": { "max-size": "50m", "max-file": "3" },
  "registry-mirrors": ["<内网 harbor 或加速器>"],
  "insecure-registries": []
}
```
> `log-opts` 不可省：默认 `json-file` 无上限，长跑会把 `/var/lib/docker` 撑爆。

**门禁**：`docker info` 正常、`docker run --rm hello-world` 成功、**9 个目标镜像 + `ollama/ollama` 各能真正 `run` 起来并退出**（仅 `docker load` 成功不算）。

### P2 · 离线镜像交付

见 §6.3。

### P3 · 源端数据导出（macOS）

**门禁**：4 类导出全部成功，且**能校验**（PG 可 `pg_restore --list`；Neo4j dump 文件非零且大小合理）。

### P4 · CentOS 空环境起容器

**门禁**：`docker compose ps` 10 个服务全部 healthy（ollama 除外）；`GET /api/v1/health` 返回 200。

### P5 · 数据导入

**门禁**：逐表行数与源端一致（至少 `ontology_class` / `data_source` / `system_config` 三表），Neo4j `MATCH (n) RETURN count(n)` 等于源端。

### P6 · Embedding provider 切换（**必须在 P7 回填之前**）

**门禁**：`GET /api/v1/embedding-providers/active` 返回 `provider_type=ollama`、`dimension=1024`，且 baseUrl 解析后指向 CentOS 宿主上的 Ollama。详见 §6.6。

### P7 · Milvus 重建回填 + LLM 切换

**门禁**：集合实体数达到附录 A 快照值（用 `diag_milvus_embedding_state.py` 核，不是看脚本 exit 0）。详见 §6.5。

> ⚠️ **编号顺序 ≠ 执行顺序**：P7 的回填依赖 P6 的 provider 切换，因为回填要真实调用 embedding，而迁移过来的 provider 行指向 Mac 上才存在的后端。

### P8 · 冒烟验收

见 §8。

---

### 6.1 P2 · 离线镜像交付管线（详细）

```bash
# ═══ 联网 x86_64 构建机 ═══
for img in postgres:16-alpine neo4j:5.23-community milvusdb/milvus:v2.4.6 \
           quay.io/coreos/etcd:v3.5.5 \
           minio/minio:RELEASE.2024-06-13T22-53-53Z \
           minio/minio:RELEASE.2023-03-20T20-16-18Z \
           mysql:8.0 nginx:alpine ollama/ollama:latest ; do
  docker pull --platform linux/amd64 "$img"
done

# 在 origin/main 的全新 worktree 上构建（构建纪律见下）
git worktree prune
git worktree add --detach /tmp/qa-deploy-$(date +%Y%m%d) origin/main
cd /tmp/qa-deploy-*/docker
docker compose --env-file .env build \
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com frontend
docker save -o /srv/images.tar.gz \
  postgres:16-alpine neo4j:5.23-community milvusdb/milvus:v2.4.6 \
  quay.io/coreos/etcd:v3.5.5 \
  minio/minio:RELEASE.2024-06-13T22-53-53Z \
  minio/minio:RELEASE.2023-03-20T20-16-18Z mysql:8.0 \
  nginx:alpine ollama/ollama:latest \
  qa-system-backend qa-system-frontend

scp /srv/images.tar.gz root@<centos>:/opt/qa-transfer/

# ═══ CentOS ═══
docker load -i /opt/qa-transfer/images.tar.gz
docker image ls   # 核对架构与版本
```

**构建纪律（强制）**：
1. 必须在 **origin/main 的全新 worktree** 里构建。主仓常停在陈旧 detached HEAD —— 2026-10-22 实测主仓停在 `9cdd934c`，缺 `0132` 迁移而生产已在 `0132`，用它构建后重启会直接崩后端。离岸构建风险更高，**每次都 `git worktree prune` 后新建**。
2. 构建前核对迁移链包含生产的 `alembic_version`：
   ```bash
   docker run --rm --entrypoint python -v <worktree>/backend:/src:ro -w /app \
     qa-system-backend:latest -c "
   from alembic.config import Config; from alembic.script import ScriptDirectory
   cfg=Config('/src/alembic.ini'); cfg.set_main_option('script_location','/src/alembic')
   print(ScriptDirectory.from_config(cfg).get_heads())"
   ```
3. `NPM_REGISTRY` **必须覆盖**为国内源。`docker/Dockerfile.frontend` 默认走 `registry.npmjs.org`，内网构建会卡死（见 `qa-system-frontend-build-npm-registry`）。
4. `docker/Dockerfile.backend` **已硬编码** aliyun Debian 源 + aliyun PyPI 源，无需覆盖。
5. `docker/.env` 是 gitignored，新 worktree 必须手动拷入。

### 6.2 P3 · 源端数据导出（macOS）

```bash
# PostgreSQL —— 现成脚本
./scripts/backup_pg.sh                      # → backups/pg/qa_metadata_*.dump（-Fc custom format）

# Neo4j —— 现成脚本（会自动停 qa-neo4j 再 dump，失败用 trap 重启）
./scripts/backup_neo4j.sh                   # → backups/neo4j/neo4j_*.dump

# qa-objects —— 现成脚本
./scripts/backup_objects.sh                 # → backups/objects/objects_data_*.tar.gz

# MySQL demo —— 无现成脚本，手工
docker exec qa-mysql-demo sh -c \
  'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines --triggers wms_demo' \
  > backups/mysql/wms_demo.sql
```

> **`backup_milvus.sh` 不执行。** 它是裸 tar 卷备份，Milvus 按 §4.5 走重建路径。

#### 护栏：导出后先自检，再往下走

四份产物在**导出完成、尚未拷贝**这个时点就应当校验。跳过这步的话，损坏要到 CentOS 上导入失败时才暴露，那时排查成本高得多。

```bash
cd backups
# 1) 四份产物都在，且非空
for f in pg/*.dump neo4j/*.dump objects/*.tar.gz mysql/*.sql; do
  [ -s "$f" ] && echo "OK   $f  $(du -h "$f" | cut -f1)" || echo "EMPTY/MISSING  $f"
done

# 2) PG dump 结构完整（-Fc custom format 可被解析）
docker run --rm -v "$PWD/pg:/b:ro" postgres:16-alpine \
  pg_restore --list /b/qa_metadata_*.dump | tail -5
#    期望：列出 TABLE DATA / DATABASE 等条目，末尾无 "error" 字样

# 3) MySQL dump 有内容且不是只有注释
grep -vcE '^(--|/\\*|!|$)' mysql/*.sql   # 期望 > 0

# 4) 记录校验和，拷贝到 CentOS 后在那边复核（传输损坏的唯一低成本检测手段）
find pg neo4j objects mysql -type f \( -name '*.dump' -o -name '*.tar.gz' -o -name '*.sql' \) \
  | sort | xargs sha256sum | tee ../transfer-manifest.sha256
```

> **PG dump 完整性补充**：`pg_restore --list` 只验证**目录结构**可解析，不验证数据块 CRC。若要更强的保证，在 CentOS 侧 `pg_restore` 时**不加 `--list`**、跑真实恢复，让它自己校验——真实恢复失败会明确报 `invalid compressed data` 之类。

### 6.3 Ollama 模型离线预置

CentOS 无法 `ollama pull`。模型 blob 是内容寻址文件，整目录带走即可。

```bash
# ═══ 联网 x86_64 机器 ═══
docker run -d --name ollama-pre -v /srv/ollama:/root/.ollama ollama/ollama
docker exec ollama-pre ollama pull bge-m3          # embedding，约 1.2 GB
docker exec ollama-pre ollama pull <对话模型>       # 见 §7.4，模型名由使用方定
docker rm -f ollama-pre
tar czf /srv/ollama-models.tar.gz -C /srv ollama
scp /srv/ollama-models.tar.gz root@<centos>:/opt/qa-transfer/

# ═══ CentOS ═══
tar xzf /opt/qa-transfer/ollama-models.tar.gz -C /srv/
```

> **必须在 x86_64 上 pull。** 跨架构（arm64 → x86_64）的 Ollama manifest 兼容性不保证。
> `bge-m3` 是**必需**的（Milvus 回填依赖它）；对话模型由使用方在 §7.4 确定。

### 6.4 P5 · CentOS 数据导入

#### 护栏：本段全部是破坏性操作，三条前置断言必须先过

以下命令带 `--clean --if-exists`、覆盖卷 tar、直接 load 到目标库。**在任何一条命令之前**，逐条确认：

```bash
# 断言 1：backend 绝不能已经在跑（它会跑 seed，污染半空库）
docker ps --format '{{.Names}}' | grep -qx qa-backend \
  && { echo "ABORT: backend 在跑，先 docker compose stop backend"; exit 1; }

# 断言 2：目标卷确实是空的。若非空说明之前有人导过，不要叠加导入。
for v in qa-system_pg_data qa-system_neo4j_data qa-system_objects_data; do
  n=$(docker run --rm -v "$v:/d" alpine:3.20 sh -c 'ls -A /d | wc -l')
  echo "$v  entries=$n"; [ "$n" -eq 0 ] || echo "  ⚠️ 非空，确认后再继续"
done

# 断言 3：传输完整性（对照 §6.2 生成的清单）
sha256sum -c transfer-manifest.sha256
```

#### 导入（严格按序，每步验完再下一步）

```bash
# --- PostgreSQL ---
docker compose up -d postgres
# ⚠️ `-i` 不可省：docker exec 默认不接 stdin。缺了它，`<` 重定向的内容根本进不去。
#    实测（postgres:16-alpine）：此时 pg_restore 读空流会以 exit 1 响亮失败——
#    `pg_restore: error: input file is too short (read 0, expected 5)`。
#    这是好事（不会假绿），但前提是**有人真的读了那条报错**。
#    紧跟其后的 "立即验" 就是防这个：只看容器 healthy 会漏掉恢复根本没发生。
docker exec -i qa-postgres pg_restore -U qa_user -d qa_metadata --clean --if-exists < backup/pg/qa_metadata_*.dump
# 立即验：版本号 + 关键表行数（对比附录 A）
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc "select version_num from alembic_version;"
#   期望 0132 —— 与 backend 镜像的 head 对齐
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc "select count(*) from audit_log;"
#   期望 17100（不是 pg_stat 的估算值，见附录 A）

# --- Neo4j（server 必须停着，load 才允许） ---
docker compose up -d neo4j && docker compose stop neo4j
docker run --rm -v qa-system_neo4j_data:/data -v $PWD/backup:/backup \
  neo4j:5.23-community neo4j-admin database load neo4j --from-path=/backup --overwrite-destination=true
docker compose start neo4j
# 立即验
docker exec -i qa-neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" \
  "MATCH (n) RETURN count(n);"     # 期望 ≈ 3753

# --- qa-objects ---
docker run --rm -v qa-system_objects_data:/data -v $PWD/backup:/backup \
  alpine:3.20 sh -c 'cd /data && tar xzf /backup/objects_data_*.tar.gz'
# 立即验：目录非空
docker run --rm -v qa-system_objects_data:/data alpine:3.20 sh -c 'ls -A /data | head'

# --- MySQL demo ---
docker compose up -d mysql-demo
docker exec -i qa-mysql-demo sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" wms_demo' < backup/mysql/wms_demo.sql
# 立即验
docker exec -i qa-mysql-demo sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -NBe "select count(*) from information_schema.tables where table_schema=\"wms_demo\";"'
```

> **每一步的"立即验"不可省。** 跳过验证一路走到底的话，故障会在 P7 embedding 回填甚至 P8 冒烟时才浮现，那时已经无法区分"迁移丢了"还是"回填失败"。

**导入顺序铁律**：PostgreSQL 与 Neo4j 必须**先于 backend 首次启动**完成恢复。否则 backend 启动时会跑一堆 seed / schema drift 校验，作用在半空库上，产生脏数据。backend 的 `depends_on` 只保证容器顺序，不保证恢复完成 —— 恢复必须手工串行。

### 6.5 P7 · Milvus 重建回填

#### 前置条件：embedding provider 必须**已经**指向 CentOS 的 Ollama

回填要真实调用 embedding。DB 里迁移过来的是 Mac 上的 provider 行（`omlx` / MLX 之类），在 CentOS 上那个后端不存在。**因此 §6.6 的 provider 切换必须先于本节的回填执行**，不能按 P6→P7 的编号顺序走。

```bash
# 0. 先做 §6.6 的 embedding provider 切换，确认 /active 返回的是 centos ollama 那一行
curl http://localhost:5173/api/v1/embedding-providers/active -H "Authorization: Bearer $JWT"
#    确认 baseUrl 解析后指向 host.docker.internal:11434，且 dimension=1024

# 1. 起 Milvus 全家桶，等健康（backend 启动不需要它，但回填需要）
docker compose up -d milvus-etcd milvus-minio milvus-standalone
#    集合无需手工建表：milvus_client._ensureCollection() 在**首次写入路径**上惰性创建
#    （ensureQueryCollection / ensureDocumentCollection / ensureWikiPageCollection）。
#    即"第一条 insert/search 时集合才出现"，故 Milvus 必须先于回填脚本就绪。

# 1. 【强烈建议先跑】--dry-run 验证全链路，不写 Milvus
#    ⚠️ dry-run 仍会真实调用 embedding API 产生费用，但不会写入任何向量
docker exec qa-backend python scripts/backfill_milvus_embeddings.py --dry-run
#    通过标准：能连库、能取到类/属性、embedding 生成无维度错误（必须是 1024）

# 2. 本体向量（3742 property + 50 class）
docker exec qa-backend python scripts/backfill_milvus_embeddings.py

# 3. Wiki 向量（64 页，需 admin 身份）
curl -X POST http://localhost:5173/api/v1/wiki/vector-sync -H "Authorization: Bearer $JWT"

# 4. RAG 文档（36 条）—— 无回填脚本，走一次完整 ingest
#    源文件已在 qa-objects volume 中恢复，重新走文档上传/解析流程生成 chunk 与向量
```

#### 关于 `--cleanup`：CentOS 上**默认不需要**，且历史上正是它出过事故

`backfill_milvus_embeddings.py` 的 docstring 记着一件已修复的事故（2026-09-30）：

> 此前 cleanup 调的是 `dropCollection()` / `ensureCollection()` —— 在 Task 14 丢弃旧 `ontology_embeddings` 之后**二者已成 no-op**，于是 cleanup **静默退化为纯 append**：每跑一次就多一整套向量（32 类被堆成 98 行、3642 属性被堆成 10926 行），检索 topK 窗口被重复项吃光。

三点推论：

1. **CentOS 上 Milvus 卷是重建的、集合为空**，普通回填走 `syncEmbedding`（按 `ontology_id` 先删后插，**幂等**）就够了。**不要因为脚本支持 `--cleanup` 就顺手加上** —— 在干净空库上跑 cleanup 只会徒增风险。
2. `--cleanup` 的用途是**收敛一个已经重复的集合**。若回填需要重跑、或从 Mac 侧导过脏数据，才用它；它与 `--sources` / `--skip-classes` **互斥**（同时给会 `SystemExit`）。
3. 无论走哪条路，**回填后必须核对行数**，因为历史故障的特征正是"脚本 exit 0 但行数翻倍"：

```bash
# 期望：class≈54、property≈3742、query_embeddings≈1061、wiki≈64
docker exec qa-backend python scripts/diag_milvus_embedding_state.py
```
对比附录 A 的快照。若任一项**接近整数倍翻倍**（例如 3742 → 7484），就是踩了上面那个坑，改用 `--cleanup` 收敛后重验。

> 另有 `scripts/reset_milvus_collections.py` 会 drop 三个集合但**保留 `query_embeddings`**。本方案不需要它——CentOS 上从空集合开始，不需要预清理。只有在确认需要保留 query 而重置其余时才用。

### 6.6 P6 · Embedding provider 切换（须先于 §6.5 回填）

```bash
# 通过管理 API 切换（走 CRUD，不要手改库 —— 手改会绕过缓存失效）
curl -X POST http://<centos>:5173/api/v1/embedding-providers \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"Ollama bge-m3 (centos)","providerType":"ollama",
       "baseUrl":"http://localhost:11434/v1","modelName":"bge-m3:latest",
       "dimension":1024,"isActive":true}'

curl http://<centos>:5173/api/v1/embedding-providers/active -H "Authorization: Bearer $JWT"
```

要点：
- `baseUrl` **故意写 `localhost`** —— resolver 按 `EMBEDDING_HOST_OVERRIDE` 改写成 `host.docker.internal`，与 macOS 上完全同一套机制（`embedding_client.py:35-52`）
- `apiKey` 留空：Ollama 无鉴权，`api_key_encrypted` 存 NULL；客户端在 OpenAI SDK 要求非空 key 时注入占位串（`embedding_client.py:127-131`）
- `isActive: true` 会在 create 时自动停用其他所有行（`embedding_provider_service.py:54-55`），并由 DB 的部分唯一索引 `uq_embedding_provider_active` 兜底并发
- 服务层会调 `invalidateEmbeddingClientCache()`，下次 embedding 重新解析

> ⚠️ **绝不让后端按内容自动改派模型。** 切换只能由人在管理页显式操作（铁律：`qa-system-no-model-override`）。

### 6.7 P7 · LLM 切换

```sql
-- 停用指向 Mac oMLX 的三条（内网不可达，留着只会让路由选到死端点）
UPDATE llm_config SET is_active = false WHERE api_endpoint LIKE '%host.docker.internal:8888%';
-- 停用两条云端（内网不可达）
UPDATE llm_config SET is_active = false WHERE api_endpoint LIKE 'https://api.deepseek.com%'
                                                 OR api_endpoint LIKE 'https://api.minimax.cn%';
```

之后**由使用方在管理页（模型配置）自行登记并激活**指向 CentOS 本地 Ollama 的模型条目 —— 本 spec 不钉死对话模型。

**Ollama 必须监听 `0.0.0.0`**：

```yaml
ollama:
  image: ollama/ollama:latest
  container_name: qa-ollama
  restart: unless-stopped
  environment:
    OLLAMA_HOST: 0.0.0.0:11434        # 默认只听 127.0.0.1，容器经 host-gateway 够不着
    OLLAMA_KEEP_ALIVE: 5m             # 释放内存，避免与 Milvus/Neo4j 抢
    OLLAMA_NUM_PARALLEL: 1            # 32G 内存下的保守值
    OLLAMA_MODELS: /root/.ollama
  volumes:
    - ollama_models:/root/.ollama
  ports:
    - "11434:11434"
```

> 仓库中**没有任何 `OLLAMA_HOST` 配置**（已 grep 确认），这是纯运维项，遗漏会导致「容器内 localhost 连不上宿主 Ollama」且错误信息不直观。

---

## 7. 配置改动清单

### 7.1 CentOS 侧 `docker/.env`

| 键 | 值 | 理由 |
|---|---|---|
| `SECRET_KEY` | **逐字复制 macOS 现值** | ⚠️ 致命。重新生成 ⇒ 5 个数据源密码 + 6 个 LLM key + embedding key 永久不可解。**跳过 `docs/deployment/00-overview.md:49` 的生成命令** |
| `POSTGRES_PASSWORD` | 可换新 | 全新建容器，无历史依赖；改了要同步改 `DATABASE_URL` |
| `JWT_SECRET` | 可换新 | 换新 = 所有会话失效，符合迁移预期 |
| `NEO4J_PASSWORD` | **复用 macOS 现值** | Neo4j 认证信息存在 `neo4j_data` 数据目录内 |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | **复用 macOS 现值** | 凭据在 `objects_data/.minio.sys/config.json` |
| `NEO4J_URI` / `NEO4J_USER` | `bolt://neo4j:7687` / `neo4j` | 不变（容器 service 名） |
| `AUTH_MODE` | `stub` | 沿用现状；改 `real` 需另配用户体系，不在本次范围 |
| `DATASOURCE_HOST_ALLOWLIST` | 需包含 `192.168.205.70` 或留空 | 空 = 不限制（`docker/.env.example:44`） |
| `EMBEDDING_HOST_OVERRIDE` | `host.docker.internal` | compose 已配 `host-gateway`（`docker-compose.yml:166-169`） |
| `EMBEDDING_MODEL` / `EMBEDDING_API_BASE` | `bge-m3:latest` / `http://host.docker.internal:11434/v1` | env 回退路径；非 loopback，override 对它 no-op，正确 |
| `EMBEDDING_DIMENSION` | `1024` | 与 `milvus_client.py:38` 的 `_DIM` 一致 |
| `OPENAI_API_KEY` / `AZURE_*` / `DEEPSEEK_*` / `QWEN_*` | **全部清空** | 内网隔离，全部不可达；留着会被路由选中导致失败 |
| `LLM_TRUST_ENV` | `false` | 无代理；且容器内无 macOS 系统代理 |
| `VITE_API_BASE_URL` | 留空（同源 `/api/v1`） | 符合 `Harness/rules/部署与访问规范.md` §一；LAN IP 访问自动适配 |
| `MINIO_ENDPOINT` | `qa-objects:9000` | compose 里已硬编码，不需在 .env |
| `NL2SQL_NO_SCOPE_ROW_LIMIT` | `1000` | 不变 |
| `APP_ENV` | `production` | 从 `development` 改 |

#### 逐字复制项的校验方法（**导入前**做，不要等导入后报 400）

"逐字复制"最常见的失败不是忘了填，而是**抄错一个字符**——肉眼在 44 位 base64 上是发现不了的。以下校验**全部在 P4 起容器之前、在源端和 CentOS 侧各跑一次**，比对的是字节而不是肉眼：

```bash
# 变量名清单（这 5 个必须逐字复用，其余按上表处理）
REUSE_KEYS="SECRET_KEY NEO4J_PASSWORD MINIO_ROOT_USER MINIO_ROOT_PASSWORD"

# 1) 生成指纹：只输出 SHA-256，不回显密钥本身
#    贴到日志/工单里是安全的，密钥本身不进任何输出
printenv | grep -E "^(${REUSE_KEYS// /|})=" | sort \
  | sha256sum | cut -d' ' -f1
```
**判定**：源端与 CentOS 侧两次输出**必须完全相同**。

若不相同，逐项定位（同样不回显明文）：
```bash
for k in $REUSE_KEYS; do
  a=$(grep -m1 "^$k=" /path/to/source/docker/.env | sha256sum | cut -c1-12)
  b=$(grep -m1 "^$k=" /path/to/centos/docker/.env | sha256sum | cut -c1-12)
  [ "$a" = "$b" ] && echo "OK   $k" || echo "DIFF $k  src=$a dst=$b"
done
```

**两个容易踩的坑**：
1. `.env` 里值带引号 / 尾部有 `\r`（Windows 换行）时 `sha256sum` 就不一致了。CentOS 侧从 Windows 中转文件时用 `dos2unix docker/.env` 先清掉 CR。
2. `docker/.env` 若已 gitignore，**不要用 `git show` 取值**——那会拿到仓库里的占位符而非现网值。现网值只在源端那份工作副本的 `docker/.env` 里。

**导入后仍需二次验证**（指纹一致只证明密钥没抄错，不证明密文与密钥配对正确）：
见 §8.1 第 3 条 —— 用**迁移过来的**那条 THBI Oracle 数据源做连接测试，必须真的建连成功。

### 7.2 数据导入后的业务配置修正

```sql
-- 只留真业务依赖
UPDATE data_source SET is_active = false
 WHERE name IN ('localmysql','smoke','recall-probe','phase13_realdata_pg');
-- THBI Oracle (id=1, 192.168.205.70:1521) 保持 is_active=true
```

`localmysql` 指向的 `host.docker.internal:3306` + 库名 `srm`，与 CentOS 上 compose 自带的 `mysql-demo`（库名 `wms_demo`）**库名不匹配**，必然连不上。

### 7.3 CentOS 宿主侧特有处理

| 项 | 处理 |
|---|---|
| **SELinux** | 唯一的 bind mount 是 `./init:/docker-entrypoint-initdb.d`（`docker-compose.yml` mysql-demo 段）。该目录**当前为空** ⇒ **直接删掉这个挂载**，避免 `:Z` 标签问题。其余 7 个均为 named volume，不受 SELinux 影响 |
| **firewalld** | 只放行 5173（+ 后续需要时的 80/443）。5433 / 8000 / 7474 / 7687 / 19530 / 9091 / 9000 / 9001 / 3306 / 11434 **一律不外放**，走容器网络 |
| **XFS ftype** | `/var/lib/docker` 所在文件系统必须 `ftype=1`，否则 overlay2 报 `failed to mount overlay: invalid argument` |
| **存储驱动** | 优先 overlay2；内核 < 3.10.0-514 才退 `overlay` |
| **时区** | `timedatectl set-timezone Asia/Shanghai`，否则日志时间戳错乱 |
| **日志轮转** | daemon.json 配 `log-opts`（§6.1 P1），默认无上限会撑爆磁盘 |

### 7.4 内存预算（32G）

| 组件 | 估算 |
|---|---|
| Milvus | ~4 G |
| Neo4j | ~2 G |
| PostgreSQL | ~1 G |
| backend (uvicorn) | ~2 G |
| etcd | ~1 G |
| MinIO ×2 | ~1 G |
| MySQL demo | ~1 G |
| **Ollama** | **~12 G**（对话模型权重 + bge-m3 + KV cache） |
| 系统 + 缓冲 | ~8 G |
| **合计** | **≈ 32 G（偏紧）** |

缓解：`OLLAMA_KEEP_ALIVE=5m` 释放空闲模型内存；`OLLAMA_NUM_PARALLEL=1` 限制并发；对话模型优先选 7B 级量化（~4.7 GB）而非 14B（~9 GB）。**若内存吃紧，本体回填与对话不要并发跑。**

### 7.5 对话模型选型（由使用方在管理页配置）

本 spec **不钉死模型名**。需要在离线阶段预置的只有：

- `bge-m3` —— **必需**，Milvus 回填与线上 embedding 都依赖
- 对话模型 —— 由使用方选定后，在联网 x86_64 机器上 `ollama pull` 并随 §6.3 一同交付

**选型参考**（16 核纯 CPU，无 GPU）：7B 级 Q4 量化模型中文与 SQL 能力足够，NL2SQL 生成几百 token 的 SQL 约需 1-2 分钟 —— 可用但慢。14B 级质量更好但显存/内存更紧且更慢，若 §7.4 内存预算吃紧则不推荐。

---

## 8. 验收清单

### 8.1 通用冒烟（`Harness/skills/deploy-verify`）

1. `curl http://<centos>:5173/api/v1/health` → `{"status":"ok"}`
2. 注册模型 → 201
3. **验证迁移过来的** THBI Oracle 数据源（`id=1`，`192.168.205.70:1521`）→ 连接测试通过。
   ⚠️ **不要重新注册一条来测**：新注册的数据源不经过 `password_encrypted` 解密路径，测通了也不证明 SECRET_KEY 用对了。必须用导入后库里那一条。
   **解密失败的特征**（已核实）：`data_source.password_encrypted` 由 `business_db_pool.py:708` 走 `decryptApiKey()` 解密；`crypto.py:50-51` 捕获 `InvalidToken` → 抛 `ConfigError`；`ConfigError` 无专门状态码分支，在 `exceptions.py:206 statusForError` 落到 **`return 400`**。响应体文案固定为 `API Key 密文无法解密，可能密钥已变更`。
   ⚠️ 该文案里的 "API Key" 有误导性——`decryptApiKey` 是通用加密助手，数据源密码走同一函数。看到这句文案时**不要去查 `llm_config`**，先查 `data_source.password_encrypted`。
4. 定义知识类 `库存` → 成功
5. **提问 → 返回回复 + SQL + 图表 + Token 计量**
6. `GET /api/v1/data-quality/rules` → 200（表缺失会 500）

### 8.2 迁移专属冒烟

7. **语义检索召回正常** —— 证明 embedding 切换到 Ollama 成功（最易失败项）
8. **Neo4j 本体图可见** —— `MATCH (n) RETURN count(n)` ≈ 3753
9. **PG 行数抽查** —— `ontology_class` = 54、`data_source` 有效条数、`system_config` = 28
10. **RAG 文档检索** —— 重新 ingest 后 `document_embeddings` 有实体且能召回
11. **重启存活** —— `docker compose restart` 后 10 个容器全回 healthy
12. **迁移链一致** —— `SELECT version_num FROM alembic_version` = 0132，且镜像 head 包含它

### 8.3 访问面验收

13. **必须用 LAN IP 访问一次**（不是 localhost），确认同源反代、登录、菜单加载、一次 API 调用全通 —— `Harness/rules/部署与访问规范.md` §四 要求。
    > 静默失败模式：nginx 缓存后端容器 IP → `/api/` 全 502 → 前端 `fetchMenuConfig` 抛错后**静默退回 `FALLBACK_NAV`** → 表现为"菜单少了、没有分组"而非报错。`docker/nginx.conf` 已用 `resolver 127.0.0.11 valid=10s` + 变量写法规避，验收时需专门确认。

---

## 9. 风险登记册

| ID | 风险 | 等级 | 缓解 | 触发时 |
|---|---|---|---|---|
| R1 | **`SECRET_KEY` 被重新生成** | 🔴 致命 | §7.1 逐字复制；**导入后立刻验**：`curl /datasources` + 逐个试连 THBI Oracle | 解密抛 `ConfigError`，HTTP 400 |
| R2 | **任何** el7 版 Docker 在 kernel 3.10 起不来（**可获得 ≠ 能启动**，所有候选版本均无一手来源） | 🟠 高 | 选用 24.0.9（较保守，见 §5.1）；P1 跑 `docker info` + `hello-world` + `docker compose version` 实测；起不来降级 20.10.24 | `failed to start daemon` / `runc create failed: EOF: unknown` |
| R3 | **`data_source` 指向不可达端点** | 🟠 高 | §7.2 停用 4 条 | NL2SQL 连接超时 |
| R4 | Ollama 仍只听 `127.0.0.1` | 🟠 高 | §6.7 `OLLAMA_HOST=0.0.0.0` | 容器内 embedding 连接被拒 |
| R5 | 对话模型指向 Mac / 云端死端点 | 🟠 高 | §6.7 停用 5 条 | chat 503 |
| R6 | CPU 缺 x86-64-v2 → `mysql:8.0` 崩溃 | 🟡 中 | P0 探针；降级 `mysql:8.0.32` | `Illegal instruction` |
| R7 | SELinux 拦 bind mount | 🟡 中 | 删掉空的 `./init` 挂载（§7.3） | `Permission denied` |
| R8 | XFS `ftype=0` → overlay2 挂不上 | 🟡 中 | P0 探针；必要时 `mkfs.xfs -f -m ftype=1` | `failed to mount overlay: invalid argument` |
| R9 | 内存吃紧（§7.4 偏紧） | 🟡 中 | `OLLAMA_KEEP_ALIVE` / `NUM_PARALLEL=1`；7B 级模型；不并发跑 | OOM kill |
| R10 | 构建源用了陈旧 worktree | 🟠 高 | §6.1 强制新建 worktree + 验 head | `Can't locate revision` ⇒ 后端整体挂 |
| R11 | 前端 build 走官方 npm 源卡死 | 🟡 中 | §6.1 覆盖 `NPM_REGISTRY` | 构建超时 |
| R12 | 数据恢复未完成就起 backend | 🟠 高 | §6.4 手工串行，不依赖 `depends_on` | seed 作用于半空库，产生脏数据 |
| R13 | **双活 schema 漂移**（§10） | 🟡 中 | 用户已接受 | CentOS 报错在 Mac 复现不了 |

---

## 10. 已知缺口：双活漂移（用户已确认接受）

### 10.1 先厘清一件事：漂移有两个维度，性质完全不同

| | **schema 漂移**（表结构 / alembic 版本） | **数据漂移**（行内容） |
|---|---|---|
| 会不会自己收敛 | **会** | **永不** |
| 为什么 | `Dockerfile.backend` 的 `CMD` 是 `alembic upgrade head && uvicorn`（见 §3.3.5）——**两边都是容器启动时自动迁移**，没有"谁决定升不升"这一步 | 各自写入，无同步机制（§2.1 已定为一次性导数） |
| 风险 | 低但有：存在**中间态窗口**，且破坏性迁移不可逆 | 中：新会话 / 新数据源 / 新 ontology 只在一侧存在 |

**这个区别决定了下面的判断**：schema 不需要"同步机制"，只需要"别在错误的时间启动容器"；数据才是真正需要人管的部分。

### 10.2 触发条件与具体场景

**场景 A —— 谁先跑 migration，其实不是人决定的**

由于两边都是启动时 `alembic upgrade head`，"谁先升"取决于**哪个 backend 容器先被拉起**。实际后果：

- CentOS 先起、Mac 后起 ⇒ 两边最终都在 0133，**自动收敛，无需人工干预**
- Mac 先起、CentOS 后起 ⇒ 同上
- **危险的不是顺序，而是中间态**：在 CentOS 升到 0133 之后、Mac 还没 pull 新镜像的这段时间里，Mac 上跑的是**0132 的代码 + 0133 的 schema**。如果 0133 是破坏性的（drop 列 / 改类型），Mac 的旧代码可能在下次启动时行为异常。

**场景 B —— 破坏性迁移在 Mac 上先落地**

Mac 上是开发数据，通常无所谓。但如果某次迁移的 `downgrade()` 不可用或数据已不可回滚，而 Mac 与 CentOS 有**业务相关的数据差异**，Mac 那边的自动升级就是一次不可逆的本地数据丢失。缓解靠约定：涉及 `drop` / 数据改写的迁移，**先在 CentOS 验一遍再 pull 到 Mac**。

**场景 C —— 缺陷只在单侧可复现**

Mac 上复现不了 CentOS 的数据形态（反过来也是）。这是双活的固有代价，不靠流程消除，只能靠**在 CentOS 上跑验收**来覆盖（§8 就是在 CentOS 上跑的）。

**场景 D —— 4 条开发探针数据源长期分叉**

§7.2 只在 CentOS 侧停用 `localmysql` / `smoke` / `recall-probe` / `phase13_realdata_pg`，Mac 侧保留。**两边该表内容会长期不同，属预期**，不要试图"对齐"。

### 10.3 冲突时怎么判

给一条明确的裁决规则，避免每次都重新讨论：

> **业务数据以 CentOS 为准；Mac 是开发草稿，不承担权威性。**

具体到冲突：
- **schema 冲突**（同名迁移在两侧产生不同结果）⇒ 以 `main` 上的迁移文件为准，两边都回到该文件重新 `upgrade`。**不要**在任一侧手工改 `alembic_version` 对齐（铁律 `qa-system-never-fake-alembic-version-number`）。
- **数据冲突**（同一实体两侧内容不同）⇒ CentOS 赢。需要拿 Mac 的数据过去时，走一次性导出导入，不要建双向同步。
- **不确定谁对** ⇒ 去查 `audit_log`（CentOS 有 17100 行，见附录 A），那边有操作留痕。

### 10.4 未采纳的缓解措施（用户已选择接受风险）

以下是**建议但本次不做**的事，记录在此以免将来重复讨论：

1. 部署 runbook 改为**双目标清单式**：每次部署产出两条记录（Mac + CentOS 各自的镜像 tag 与 alembic 版本）
2. CentOS 侧加一个**只读版本探针**（脚本或接口），随时能回答"CentOS 现在在 0132 还是 0133"
3. 仓库无 CI（`qa-system-no-ci-main-can-go-red`），两边都不会自动提醒

第 2 条的**临时替代**：随时可以手工查
```bash
docker exec -i qa-postgres psql -U qa_user -d qa_metadata -tAc "select version_num from alembic_version;"
```

---

## 11. 文档回写清单（实施时同步）

本 spec 定稿后，以下文档需要更新（本次**不改**，仅登记）：

| 文档 | 需改内容 |
|---|---|
| `docs/deployment/01-prerequisites.md` | 已有 CentOS Stream 9 / Rocky 9 条目；需补 **CentOS 7 的受限路径**（Docker 版本上限、glibc 2.17 下 Ollama 需容器化） |
| `docs/deployment/02-environment.md:32` 与 `00-overview.md:49` | 加醒目警告：**带数据迁移时禁止重新生成 `SECRET_KEY`**，必须逐字复制源环境 |
| `docs/deployment/07-operations.md` | 补 CentOS 侧运维差异（SELinux / firewalld / 无 cron launchd 契约） |
| `Harness/rules/部署与访问规范.md` | 补 CentOS 生产访问面约定（LAN IP 5173，暂不启用 HTTPS） |
| `Harness/wiki/operations-runbook.md` | 补双目标部署流程与版本探针 |

---

## 12. 未决项

| # | 项 | 状态 |
|---|---|---|
| 1 | **对话模型具体选型** | 由使用方在管理页配置；离线阶段只需确定要预置的模型名（§7.5） |
| 2 | `data_source` 中 4 条开发探针在 Mac 侧是否保留 | 本 spec：保留（开发便利），CentOS 侧停用 |
| 3 | 是否为 CentOS 侧加只读 alembic 版本探针 | 建议但不在本次范围（用户已接受 R13） |
| 4 | 联网 x86_64 构建机由谁提供、何时交付 | 待定（§6.1 是 P2 的硬前置） |

---

## 附录 A · 源端当前数据快照（2026-10-22）

用于导入后逐表比对。

```
alembic_version: 0132
public 表数: 95
ontology_class: 54
data_source: 5（有效：THBI Oracle 1 条）
system_config: 28
embedding_provider: 3（激活：id=2 oMLX bge-m3 8bit）
llm_config: 6（激活：id=1 deepseek-chat, id=3 Qwen3.8-27B-4bit, id=9 MiniMax-M3）
session_query_state: 320
session_token_usage: 2391
evidence: 1729
audit_log: 17100
multi_step_step: 27
user_sessions: 544
session_message: 1420
multi_step_run: 9

Neo4j: 3753 节点 / 3277 关系
Milvus 集合实体: 4953（详见 §3.2）
卷总占用: ≈ 3.7 GB
```

> 行数均为 `SELECT count(*)` **实测值**。⚠️ 不要用 `pg_stat_user_tables.n_live_tup` 做比对基准 —— 那是统计估算值，在本库上偏差可达 3 个数量级（实测 `audit_log` 统计值 6 / 真实 17100）。

## 附录 B · 关键代码位置索引

| 主题 | 位置 |
|---|---|
| Fernet 主密钥来源 | `backend/app/config.py:82` → `backend/app/infrastructure/security/crypto.py:24-33` |
| Embedding provider 解析 | `backend/app/infrastructure/llm/embedding_provider_factory.py:51-88` |
| `KNOWN_PROVIDER_TYPES` | `backend/app/infrastructure/llm/embedding_provider_factory.py:44-46` |
| 维度守卫 | `backend/app/infrastructure/llm/embedding_provider_factory.py:160-177` |
| loopback 改写 | `backend/app/infrastructure/llm/embedding_client.py:35-52`（空 override no-op 在 `:43-44`） |
| Milvus 维度常量 | `backend/app/infrastructure/milvus_client.py:38`（`_DIM = 1024`） |
| Embedding CRUD 路由 | `backend/app/api/v1/embedding_provider.py`，挂载于 `backend/app/main.py:447-452` |
| 单激活 DB 约束 | `backend/alembic/versions/0014_embedding_provider_active_unique.py` |
| 初始 seed | `backend/alembic/versions/0013_seed_embedding_providers.py:19-48` |
| 容器启动契约 | `docker/Dockerfile.backend` 的 `CMD`（`alembic upgrade head && uvicorn`） |
| `host-gateway` 映射 | `docker/docker-compose.yml:166-169` |
| nginx 反代 + resolver | `docker/nginx.conf` |
| 同源部署铁律 | `Harness/rules/部署与访问规范.md` §一 |
| 冒烟技能 | `Harness/skills/deploy-verify/SKILL.md` |