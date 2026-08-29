# 4. 初始化（迁移、种子、回填）

docker compose up 之后，**没有任何业务数据**，需要按顺序执行：

1. Alembic 数据库迁移（Postgres 结构）
2. Neo4j 约束 / 索引初始化
3. 模型种子（embedding provider registry）
4. 本体导入（Neo4j + Postgres）
5. 向量回填（Milvus embedding）

---

## 4.1 进入后端容器

```bash
docker compose -f docker/docker-compose.yml exec backend bash
```

容器内目录结构：

```
/app/
├── app/             # FastAPI 主代码
├── seed_*.py        # 种子脚本
├── alembic/         # 迁移
├── pyproject.toml
└── .env             # 由 mount 注入（docker/.env 当成 env_file 传入）
```

验证环境：

```bash
# 在容器内
env | grep -E '^(DATABASE_URL|NEO4J_|MILVUS_|SECRET_KEY)'
uv run python -c "from app.config import settings; print(settings.SECRET_KEY[:10])"
```

---

## 4.2 Alembic 迁移

```bash
# 当前最新版本
uv run alembic current

# 升级到 head
uv run alembic upgrade head

# 查看历次提交
uv run alembic history --verbose
```

⚠️ **坑位**：
- **必须用真实 PostgreSQL，不要 sqlite 内存库做测试**。ORM 行为在 sqlite / PG 上一致但 PG 独有的 timestamptz / JSONB 行为 sqlite 不会触发（如 `session_token_usage` 时区漂移）。
- **alembic 镜像版本与仓库代码不同步**：仓库内可能有 0010, 0011 等迁移，但 `ghcr.io/astral-sh/uv` 镜像只打包到 0001。这时 `docker compose exec backend uv run alembic upgrade head` 会报 `Can't locate revision identified by '0016_ontology_join'`。解决：
  - 重建镜像：`docker compose build --no-cache backend`
  - 或者改用本地 venv 直接跑 `uv run alembic upgrade head`（需 `cd backend`）

### 4.2.1 离线 SQL（无 alembic 工具时）

```bash
# 生成 SQL（不执行）
uv run alembic upgrade head --sql > /tmp/migration.sql
# 拿到 SQL 后手动 psql 喂
psql "$DATABASE_URL" -f /tmp/migration.sql
```

### 4.2.2 新增迁移

```bash
uv run alembic revision --autogenerate -m "add_my_table"
# 仔细 review backend/alembic/versions/xxxx_add_my_table.py，确认无破坏性改动
# 重点：modifying 已有列类型、删除列、drop index 等
```

⚠️ **坑位**：ORM 字段变更必须**同步写 alembic 迁移**且 apply 后端才能正常工作。test 数据库用 sqlite 不会发现 PG 专属行为（autogenerate 会丢 `server_default` / `timezone-aware` 等）。

---

## 4.3 Neo4j 初始化

迁移只创建 Postgres 元数据表，**Neo4j 的本体图**需要通过专门的脚本初始化：

```bash
uv run python -m app.scripts.init_neo4j
# 等价于
uv run python scripts/neo4j_init.py
```

会创建：
- 唯一约束（`CONSTRAINT ON (n:OntologyClass) ASSERT n.qualified_name IS UNIQUE`）
- 全文索引（`CREATE FULLTEXT INDEX ... IF NOT EXISTS`）
- 标签索引（按 `(label, name)` 加速名称查询）

---

## 4.4 模型种子

新建一个**空模型库**（任何 provider 都没激活）：

```bash
uv run python seed_models.py
```

种子会插入：

| id | name | provider | 备注 |
|---|---|---|---|
| 1 | `text-embedding-3-small` | `OPENAI` | 留空 apiKey，运营手动填 |
| 2 | `bge-m3-mlx-8bit` | `OPENAI_COMPATIBLE_PROXY` | 本地 oMLX，endpoint 占位 |
| 3 | `nomic-embed-text` | `OLLAMA` | 本地 |

种子脚本默认**isActive=False**，需要到 UI「模型配置」页面编辑填入真实 endpoint + apiKey 后激活。

> **加新模型永远走 UI / API**，不要直接 INSERT 表。embedding provider 有 mutex（同时只能一个 active），绕过 API 写库会破坏一致性。

---

## 4.5 本体导入

本体（Ontology）是 NLP/SQL 链路的核心元数据：类（Class）、属性（Property）、关系（Relationship）。

### 4.5.1 准备 ontology 数据

仓库 `docs/excel/` 目录有 10 个 xlsx：

| 文件 | 含义 |
|---|---|
| `ontology_classes.xlsx` | 顶层类（订单、采购、库存等） |
| `ontology_properties.xlsx` | 属性（订单号、收货数量等） |
| `ontology_relationships.xlsx` | 关系（订单 → 订单行） |
| `xx_关连.xlsx` 等 7 个 | 业务表 + 字段中文别名 |

### 4.5.2 生成 SQL 文档（可选）

`docs/excel/ontology_schema.sql` 是基于 xlsx 生成的 SELECT 别名语句（748 列），可用于人工核对。每张表的形式：

```sql
SELECT
  "PSHNUM_0" AS "请求号",
  "PSDLIN_0" AS "行",
  ...
FROM "与采购请求关连";
```

xlsx 的 `列` 字段做列名（统一加 `_0`），`长标题` 列做别名。

### 4.5.3 导入本体

```bash
# 解析 excel → 内部 JSON → 写入 Neo4j + Postgres
uv run python -m app.scripts.import_ontology --source docs/excel

# 或单独回填
uv run python -m app.scripts.import_ontology classes
uv run python -m app.scripts.import_ontology properties
uv run python -m app.scripts.import_ontology relationships
```

> ⚠️ **坑位**：xlsx 的 `不需要=1` 行需要清理，否则向量会污染。`to_camel` / `to_snake` 一致性也在这里校验。
>
> 修法步骤：
> 1. **原始 XML 解析**（绕过 openpyxl stylesheet 问题）：用 `zipfile` + 正则读 `xl/worksheets/sheet*.xml`，提取 `不需要=1` 行号。
> 2. **service.deleteProperty** 逐条删（确保级联删除 Postgres / Milvus 关联）。
> 3. **Milvus --cleanup**：用 `python -m app.scripts.backfill_milvus --cleanup` 重建集合。
> 4. **AST 删 seed**：检查 seed_ontology.py 里 `P("xxx", ...)` 引用。
> 5. **Neo4j 孤儿清理**：`MATCH (p:Property) WHERE NOT (p)-[:BELONGS_TO]->(c:Class) DETACH DELETE p`。

### 4.5.4 校验

```bash
# Postgres 计数
docker compose exec postgres psql -U qa_user -d qa_metadata \
  -c "SELECT COUNT(*) FROM ontology_class; SELECT COUNT(*) FROM ontology_property;"

# Neo4j
docker compose exec neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD \
  "MATCH (c:OntologyClass) RETURN COUNT(c) AS classes
   MATCH (p:OntologyProperty) RETURN COUNT(p) AS props"
```

---

## 4.6 向量回填（Milvus embedding）

每条 ontology（class / property）都需要一个 embedding 向量用于语义检索。

```bash
# 全量回填
uv run python -m app.scripts.backfill_milvus

# 指定 provider
uv run python -m app.scripts.backfill_milvus --provider-id 2

# 仅一类
uv run python -m app.scripts.backfill_milvus --type class

# 清理重灌（删集合后重建）
uv run python -m app.scripts.backfill_milvus --cleanup
```

> ⚠️ **坑位**：
> - **id 序列冲突**：ontology_class 和 ontology_property 共享 id（如 id=5 既是 class 又是 property），Milvus 删 / 查必须带 `type` 作用域。
> - **批量 delete 不可靠**：重负载下 `delete-then-insert` 会残留重复行。**收敛用 `--cleanup`** 删集重建，不要逐条增量删除。
> - **NL2SQL 类作用域**：`validatePlan` 按选定类校验属性，全库存在 ≠ 属于该类。如收货数量跨 3 类映射不同列，需补 `business_aliases`。

### 4.6.1 验证

```bash
# 列出 Milvus 集合
curl -sf http://localhost:9091/collections | jq

# 或进容器
docker compose exec milvus-standalone \
  milvus_cli --host localhost check
```

---

## 4.7 业务数据源初始化

QA System 演示用 MySQL（`wms_demo` 库）有自动启动 SQL：

```bash
docker compose exec mysql-demo ls /docker-entrypoint-initdb.d/
# 应看到 wms_demo.sql（约 100 MB，含 ruoyi-wms 模拟数据）
```

如果**没有自动导入**，手动：

```bash
docker compose exec -i mysql-demo mysql -uroot -p"$MYSQL_ROOT_PASSWORD" wms_demo < docker/mysql-demo/init/wms_demo.sql
```

---

## 4.8 初始化检查清单

```bash
# 1. 迁移
docker compose exec backend uv run alembic current | head -1
# 期望：001x_xxx (head)

# 2. Neo4j 节点
docker compose exec neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD \
  "MATCH (n) RETURN labels(n)[0] AS label, COUNT(n) ORDER BY label"

# 3. Postgres 表
docker compose exec postgres psql -U qa_user -d qa_metadata \
  -c "\dt"

# 4. Milvus 集合
curl -sf http://localhost:9091/collections | jq '.data[] | {name: .name, rowCount: .row_count}'

# 5. 模型
curl -sf http://localhost:8000/api/v1/models | jq

# 6. 业务数据源
curl -sf http://localhost:8000/api/v1/datasources | jq
```

**期望结果**：
- (1) 迁移已到 head
- (2) `OntologyClass`, `OntologyProperty`, `Relationship` 三类都有节点
- (3) 至少包含 `model_config`, `data_source`, `ontology_class`, `ontology_property`, `session_token_usage`, `alembic_version`
- (4) `ontology_class` 和 `ontology_property` 集合有 `row_count > 0`
- (5) 至少 3 个模型（默认种子）
- (6) 至少 1 个 datasource（MySQL demo）

如任何一步失败，先看 [07-operations.md](07-operations.md) 故障排查。

下一步：[05-reverse-proxy-https.md](05-reverse-proxy-https.md)。