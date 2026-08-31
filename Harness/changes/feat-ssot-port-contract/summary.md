# 变更：feat-ssot-port-contract

- **日期**：2026-08-31
- **作者**：AI Assistant
- **Phase**：运维硬化（横切）
- **状态**：done
- **Commit**：`6ea63e3`

## 1. 需求

docker-compose 把 PG 宿主端口映射到 5433（避让本机 openmetadata 的 5432），但代码 + `.env` 都默认写 5432，导致容器外跑 uvicorn 必然 `ConnectionRefused`。过去靠口口相传，新人踩坑率高。

验收标准：
- 改端口只改一处（SSOT）
- 双层 fail-fast 给出精确提示
- 容器外 / 容器内 入口统一

## 2. 设计评审

SSOT 设计：把容器内 / 容器外的端口契约分离到两个独立 `.env`：

| 场景 | 配置文件 | URL 写法 | 注入方式 |
|---|---|---|---|
| 容器内 | `docker/.env` | `postgres:5432`（容器 service + 容器端口） | compose `env_file: .env` |
| 容器外 | `backend/.env` | `localhost:5433`（宿主端口） | `scripts/run_local.sh` 自动 export |

关键决策：**两套 .env 各自单一事实源**，不再靠"复制一份改端口"或文档口头提醒。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无业务接口变更。新增运维脚本入口：`./scripts/run_local.sh <command>`。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `docker/.env.example` | 加 SSOT 端口契约说明段（容器内端口是 SSOT） |
| `docker/docker-compose.yml` | backend service 改 `env_file: .env`，删除硬编码 `environment: DATABASE_URL: ...` 段 |
| `backend/.env.example`（新） | 容器外端口契约模板（含 5433 不是 5432 提示、容器内/外端口对照表） |
| `scripts/run_local.sh`（新） | 容器外统一入口：容器内拒绝运行、端口可达性预检、SSOT 防御、自动 export |
| `backend/app/main.py` lifespan | 连 PG 失败且 URL 含 5432 时打印精确端口契约提示（双层 fail-fast 第二层） |
| `Harness/wiki/operations-runbook.md` | 加「SSOT 端口契约」段 |
| `~/.claude/.../memory/qa-system-runbook-quirks.md` | #6 标记已 SSOT 根因修复 |

## 6. 测试

- 脚本 help：✅
- SSOT 防御：故意把 `.env` 改回 5432 → 脚本拒绝运行 + 提示 ✅
- 端口预检：PG/Milvus/Neo4j 三个 ✓ ✅
- 端到端：通过 wrapper 启动 uvicorn 8002 → 6 文档路由 + POST 201 ✅
- lifespan 预检（5432 错配）：fail-fast + 精确端口契约提示 ✅
- lifespan 预检（5433 正确）：进入 yield ✅

## 7. 安全审查

- 无 auth / 权限变更
- 无新外部依赖
- 脚本内的 `nc -z -G 2` 仅做 TCP 连通性探测，不传输数据
- **未触发 security-reviewer**（运维硬化非敏感）

## 8. 部署验证

| 验证项 | 结果 |
|---|---|
| docker compose up 后端 | 容器内 DATABASE_URL 自动从 docker/.env 注入 = postgres:5432 |
| 本地 uvicorn | 通过 scripts/run_local.sh 自动注入 localhost:5433 |
| 端口可达性预检 | PG/Milvus/Neo4j 三个 ✓ |
| 故意错配（.env 含 5432） | 双层 fail-fast 触发 + 精确提示 |

## 9. 关联

- 根因：runbook quirks #6（"Postgres 端口是 5433 不是 5432"）
- 关联事故：之前 uvicorn 启动报 ConnectionRefused（2026-08-30 跑过 9 次，每次都要看 hints）
- 修复来源：用户问题「这个怎么保证以后不会出现」

## 10. 未来维护口径

| 场景 | 改哪里 |
|---|---|
| 改 PG 宿主端口（如 5433 → 5434） | `docker-compose.yml` 端口映射段 + `backend/.env` + `run_local.sh` 提示文字 |
| 改容器内 PG 端口（如 5432 → 5433） | `docker-compose.yml` 容器端口段 + `docker/.env` 的 `postgres:<新端口>` |
| 新增依赖端口（如 Redis） | 在 `.env.example` 两份都加 + `run_local.sh` 加 `check_port` 行 |

## 11. 同类问题预防清单

本次发现的"环境契约不一致"是开发常见陷阱。已识别同类型风险：

1. ~~PG 宿主端口 5433~~ ✅ 已修
2. Milvus URI 写法：`localhost:19530` vs `http://localhost:19530`（pymilvus 2.x 要求完整 URI）—— 见 `run_local.sh` 提示文字
3. EMBEDDING_HOST_OVERRIDE：容器内 `host.docker.internal` / 容器外留空 —— `.env.example` 已明确
4. ~~neo4j URI `bolt://` 前缀~~ —— compose 已用 `bolt://neo4j:7687`，宿主用 `bolt://localhost:7687`

---

**该 SSOT 把"口口相传"的环境契约固化为代码契约 + 自动化预检 + 双层 fail-fast。后续新人 onboarding 不再依赖人工记忆。**