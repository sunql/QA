---
name: deploy-verify
description: Docker Compose 部署冒烟验证
---

# 部署验证技能

## 0. 生产发版命令（CentOS7 生产机，docker-compose v1）——发版任务必须用这套

生产机 `ai-server` 上是 **docker-compose v1**，且必须带固定的 project / env-file /
compose-file 三个参数。**创建发版/部署任务时，命令要按下面的写法生成，禁止裸写
`docker compose ...`（v2 语法，生产机上既有子命令不存在也会漏参数）**。

先在 shell 里定义包装函数（每次新会话都要重新定义）：

```bash
qa_compose() {
  docker-compose -p qa-system \
    --env-file /opt/qa-system/docker/.env \
    -f /opt/qa-system/docker/docker-compose.centos7.yml "$@"
}
```

常用命令（v1 兼容）：

```bash
# 校验配置合法 + env 插值无错（发版第一步）
qa_compose config --quiet && echo COMPOSE_OK

# 看每个服务实际解析出的镜像（注意：v1 没有 config --images，那是 v2 的）
qa_compose config | grep 'image:'

# 服务/卷清单（v1 支持 --services / --volumes，不支持 --images）
qa_compose config --services
qa_compose config --volumes

# 容器状态（重点看 postgres 是否 Up、有无 Restarting）
qa_compose ps

# 确认 app 实际使用的 DATABASE_URL（compose 插值视角，不用进容器）
qa_compose config | grep -n 'DATABASE_URL'

# 日志
qa_compose logs -f backend
```

**v1 vs v2 速查**：`config --images`、`compose ls` 等是 v2 才有的；v1 等效方案
用 `config | grep 'image:'`。拿不准时先 `qa_compose config --quiet` 校验，再看
`docker-compose --version`。

> 本文其余章节的 `docker compose ...` 命令是**本地开发（v2）**语法；
> 生产机上一律换成 `qa_compose ...`。

## 1. 数据库迁移同步（必查）
```bash
cp docker/.env.example docker/.env  # 填密钥
docker compose -f docker/docker-compose.yml --env-file docker/.env up -d
docker compose -f docker/docker-compose.yml ps  # 全部 healthy
```

## 0. 数据库迁移同步（必查）

对**每个**运行环境（测试库 / live 库 / 生产库）校验迁移版本，不一致即阻塞：

```bash
# 每个环境都要跑，不允许只跑一个
DATABASE_URL=postgresql+asyncpg://<user>:<pw>@<host>:<port>/<db> alembic upgrade head
# 校验版本号一致且为当前 head revision（各环境必须相同）：
SELECT version_num FROM alembic_version;
```

> docker compose 部署容器启动会自动 `upgrade head`，但本地/非容器化 DB 不会。
> 版本不一致按 [开发流程规范](../rules/开发流程规范.md)「数据库迁移同步（部署门禁）」
> 处理后再继续冒烟。

**撞名错误的修复分支**：`docker logs qa-backend` 若出现
`DuplicateColumnError` / `DuplicateTableError` / `RelationAlreadyExists` /
`... already exists` —— **不是迁移文件错，是 `alembic_version` 落后于库内
schema**（手工 DDL 跑过没 stamp / 分支切换 / 合版撞号）。**禁止改迁移文件加
`IF NOT EXISTS`**（治标不治本）。按
[数据库环境使用规范 §迁移元数据漂移治理](../rules/数据库环境使用规范.md#迁移元数据漂移治理crl2026-10-14-起)
走「停 backend → 对账 lineage → `alembic stamp head` → 验证 → 重启」标准流程。

## 冒烟检查
1. `curl http://localhost:8000/api/v1/health` -> `{"status":"ok",...}`
2. `POST /api/v1/models` 注册 OpenAI + Ollama 模型 -> 201
3. `POST /api/v1/datasource/register`（Phase 3）注册 demo MySQL -> 测试连接通过
4. `POST /api/v1/knowledge/define`（Phase 2）定义 `库存` 类
5. `POST /api/v1/chat`（Phase 4）提问 -> 返回回复 + SQL + 图表 + Token
6. `GET /api/v1/data-quality/rules`（若含数据质量特性）-> 200（非 5xx；表缺失会 500）

## 日志
```bash
docker compose logs -f backend
docker compose logs -f frontend
```

## 清理
```bash
docker compose -f docker/docker-compose.yml down -v  # 含卷
```
