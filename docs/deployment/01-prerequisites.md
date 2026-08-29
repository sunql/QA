# 1. 服务器与依赖要求

## 1.1 操作系统

| 系统 | 支持 | 备注 |
|---|---|---|
| Ubuntu 22.04 LTS / 24.04 LTS | ✅ 推荐 | apt 安装 Docker 方便 |
| Debian 12 (Bookworm) | ✅ | 同 Ubuntu 路线 |
| CentOS Stream 9 / Rocky 9 | ✅ | dnf 装 docker；注意 selinux 上下文 |
| macOS 14+ (Apple Silicon) | ✅（dev 用途） | arm64 镜像兼容，注意 MySQL 用 `mysql:8.0` 不用 `-debian` |
| Windows 11 + WSL2 | ✅（仅开发） | 不建议生产 |

生产**强烈建议 Linux**，避免 WSL/虚拟化层引入的性能与稳定性风险。

---

## 1.2 硬件最低/推荐配置

| 角色 | CPU | 内存 | 磁盘 | 网络 |
|---|---|---|---|---|
| **最小可行**（PoC / 单机 demo） | 4 vCPU | 8 GB | 40 GB SSD | 100 Mbps |
| **小规模**（≤10 并发用户） | 8 vCPU | 16 GB | 100 GB SSD | 500 Mbps |
| **生产**（50+ 并发，多副本） | 16+ vCPU | 32+ GB | 200 GB+ NVMe | 1 Gbps |

> **内存大头**：Milvus 容器默认 4 GB，Neo4j 容器默认 2 GB，Postgres + 后端各 1 GB，加上系统与 buffer。16 GB 是体感流畅的下限。
>
> **磁盘**：Milvus（`milvus_data`）和 Neo4j（`neo4j_data`）是增长主体；向量库随本体规模线性增长。每 1 万条 ontology embedding 约 40 MB，按业务复杂度预留 50 GB+。

---

## 1.3 端口清单

如果服务器有防火墙（`ufw` / `firewalld` / 云厂商安全组），需要放行以下端口：

| 端口 | 服务 | 是否对外 | 说明 |
|---|---|---|---|
| **443** | Caddy/Nginx (HTTPS) | ✅ 对外 | 唯一对外端口（推荐） |
| 80 | Caddy/Nginx (HTTP) | ✅ 对外 | 自动跳转 HTTPS |
| 5173 | qa-frontend (nginx:80) | ⚠️ 可选对外 | 直接走前端时可暴露 |
| 8000 | qa-backend | ⚠️ 可选对外 | 仅调试时暴露 |
| 7474 | Neo4j HTTP | ❌ 内网 | 仅运维/开发使用 |
| 7687 | Neo4j Bolt | ❌ 内网 | 后端连接 |
| 5432 | PostgreSQL | ❌ 内网 | 元数据 |
| 19530 | Milvus gRPC | ❌ 内网 | 向量检索 |
| 9091 | Milvus metrics | ❌ 内网 | Prometheus 拉取 |
| 3306 | MySQL demo | ❌ 内网 | 演示业务库 |
| 9000/9001 | MinIO | ❌ 内网 | Milvus 对象存储 |

**生产只对外暴露 80/443**，其余用 docker network 隔离，仅主机或 docker-compose 默认网络可达。

---

## 1.4 软件依赖

### 必需

| 组件 | 版本 | 安装 |
|---|---|---|
| Docker Engine | ≥ 24.0 | `apt install docker.io` 或官网脚本 |
| Docker Compose | ≥ v2.20（v2 插件版） | Ubuntu 22+ 默认自带 |
| Git | ≥ 2.30 | `apt install git` |
| OpenSSL | ≥ 1.1 | 生成 Fernet key |
| Python 3.11+ | 仅首次种子脚本需要 | 跑 `seed_*.py` 时用 |

### 可选

- **Caddy 2** 或 **Nginx 1.20+**：反向代理 + 自动 HTTPS
- **cron**：备份 / 证书续期
- **logrotate**：日志滚动
- **Prometheus + Grafana**：指标监控（Milvus 9091、Neo4j metrics）

---

## 1.5 域名 / 证书

- 准备一个域名解析到服务器 IP（A 记录）
- 若用 Caddy：自动申请 Let's Encrypt 证书，**零配置**
- 若用 Nginx：可用 `certbot --nginx` 申请；或上传自有证书到 `/etc/nginx/ssl/`

---

## 1.6 预检清单（部署前自测）

```bash
# Docker 引擎
docker info | grep -E "Server Version|Storage Driver"
# 期望：Server Version: 24.0+，Storage Driver: overlay2

# Compose 插件
docker compose version
# 期望：Docker Compose version v2.x+

# 端口可用
sudo ss -tlnp | grep -E ':(80|443|5173|8000) ' || echo "ports free"

# 磁盘
df -h /var/lib/docker
# 期望：至少 30 GB 可用

# 内存
free -h
# 期望：至少 8 GB

# 时区
timedatectl
# 期望：UTC 或 Asia/Shanghai，建议设 UTC 避免日志错乱
sudo timedatectl set-timezone UTC
```

确认全部通过后，进入 [02-environment.md](02-environment.md)。