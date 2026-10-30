# 通用 Agent 编排：灰度上线手册（feat-agent-orchestration / Task 11）

> **本文件是手册，不是脚本。** 所有生产操作（迁移、部署、起停 Worker、播种菜单）
> 由人手动执行；本仓只交付「工件 + 手册」，不代跑任何生产命令。
>
> 关联变更：[feat-agent-orchestration](summary.md)（本目录；变更 SSOT）
> · 任务书：`.superpowers/sdd/2026-10-26-general-agent-orchestration/task-11-brief.md`
> · 设计稿：`docs/superpowers/specs/2026-10-26-general-agent-orchestration-design.md`
> · 迁移：`backend/alembic/versions/0135_agent_orchestration.py`（revision `0135`）
> · 部署契约：`docker/docker-compose.yml` 里的 `agent-orchestration-worker` 服务
> · 菜单脚本：`backend/scripts/seed_agent_orchestration_menu.py`
> · 验证报告：`docs/superpowers/plans/2026-10-26-general-agent-orchestration-verification.md`

---

## 0. 一句话现状（先看这里）

**今天没有任何工具可以被编排绑定。** 白名单里的 7 个只读工具
`input_schema` 全是 `{}`，而编排的工具合法性判定
（`app/services/agent_orchestration_validation.py::validateObjectSchema`）
要求「根 `type="object"` + `properties` 是对象 + `additionalProperties` 为 false 或缺省」
⇒ `{}` 不合法 ⇒ 7 个工具全部 `isOrchestrationToolValid == false`
⇒ 创建编排配置时绑定工具会直接 422。

所以**阶段 0（补 schema）必须先做完**，后面的阶段才有意义。

> **第一波只补 schema，不翻 `enabled`** —— 在 4 个 wiki 工具**实测为 `false`** 的环境里，
> 翻 `enabled` 会改变 4 个既有 legacy Agent 的可运行性，属于需要单独审批的后续波次
> （§2.5、§9 坑 12）。

---

## 1. 硬约束（任何阶段都不得违反）

| # | 约束 | 落在哪 |
|---|---|---|
| 1 | `ENABLE_AGENT_ORCHESTRATION` 默认 `false`；关闭 = 不注册新路由 **且** 不起 Worker | `backend/app/config.py`、`backend/app/main.py` |
| 2 | Worker 只在显式 `--profile agent-orchestration` 时启动；**默认 `up` 永不包含它** | `docker/docker-compose.yml` |
| 3 | Worker **不发布宿主端口**、**不被 API/前端 `depends_on` 拉起** | 同上 |
| 4 | 编排只允许 7 个只读 handler：`supplier_360` / `supplier_risk` / `graph_traverse` / `wiki_search` / `wiki_read` / `rule_evaluate` / `coverage_status` | `READ_ONLY_HANDLER_REFS` |
| 5 | 两个新菜单项**不进**默认 `seed_menu_config.ITEMS`（默认暴露 = 提前上线） | `backend/scripts/seed_menu_config.py` |
| 6 | **绝不重新生成 `SECRET_KEY`**：它是 Fernet 主密钥，换新 = 5 个数据源密码 + 6 把 LLM key 永久不可解 | 运维红线 |
| 7 | **绝不 `cat`/`print` `.env`**（含 `docker/.env`）；改配置用编辑器，验证用 `docker compose config` 的**服务名**而不是环境变量值。唯一例外：读回**单个非敏感开关**（如 `docker compose … exec backend printenv ENABLE_AGENT_ORCHESTRATION`，沿用本手册统一的 `-f` 参数）—— 只读这一个变量，**不要 dump 整个环境**，更不涉及任何密钥/口令 | 运维红线 |
| 8 | 生产操作全部由人手动执行 | 本手册 |
| 9 | **阶段 0 第一波不翻 `agent_tool_config.enabled`** —— 只补 `input_schema`。当 4 个 wiki 工具**实测为 `false`** 时，翻 `enabled` 会让 4 个既有 legacy Agent 从 409 不可运行变为可执行（§2.5、§9 坑 12） | 本手册 §2.5 |

---

## 2. 阶段 0：让工具「可绑定」（前置，最容易漏）

**进入条件**：能登录目标环境的元数据库、且能读写 `agent_tool_config`。
阶段 0 **不需要** 0135（它只改 `agent_tool_config` 的一列）。
但必须先知道一件事：**0135 是新镜像的硬前置** —— 0135 的迁移脚本只随**新镜像**
交付，旧镜像的 alembic head 是 `0134` ⇒ 在**旧镜像**里执行 `alembic upgrade head`
是 **no-op**（详见 §4 阶段 1 / 阶段 2）。

### 2.1 实测现状（2026-10-28，开发库 `qa_metadata` 只读查询；与 §2.5 同一次查询）

| name | handler_ref | enabled | input_schema | 可绑定？ |
|---|---|---|---|---|
| `supplier_360` | supplier_360 | true | `{}` | ❌ schema 不合法 |
| `supplier_risk` | supplier_risk | true | `{}` | ❌ schema 不合法 |
| `graph_traverse` | graph_traverse | true | `{}` | ❌ schema 不合法 |
| `wiki_search` | wiki_search | **false** | `{}` | ❌ schema 不合法（该库遗留：已禁用） |
| `wiki_read` | wiki_read | **false** | `{}` | ❌ schema 不合法（该库遗留：已禁用） |
| `rule_evaluate` | rule_evaluate | **false** | `{}` | ❌ schema 不合法（该库遗留：已禁用） |
| `coverage_status` | coverage_status | **false** | `{}` | ❌ schema 不合法（该库遗留：已禁用） |

> ⚠️ **`enabled` 这一列是历史行状态**，**不是**本仓 seed 的产物：
> 本仓 `backend/scripts/seed_agent_tool_configs.py:35/45/55/68/78/88/98` 把
> **全部 7 个工具**都写成 `"enabled": True`，
> `backend/app/services/agent_tool_config_service.py:339` 也是
> `enabled=fields.get("enabled", True)`；**没有任何迁移**把 wiki 工具置为 disabled。
> ⇒ 在任何由**本仓 seed** 造出来的环境里，这 4 行**本来就是 `true`**。
> 所以「四行是 `false`」**不是**本仓可复现的基线，只是那台库的遗留状态；
> 阶段 0 的判据因此是**分支式**的（见 §2.4）。

> 复核命令（只读）：
> ```sql
> SELECT name, handler_ref, enabled, input_schema
> FROM agent_tool_config ORDER BY name;
> ```

### 2.2 绑定一个工具通常要做**两件事**；**第一波只做第 1 步**

1. **写合法的 object schema**（下表三选一，必须与真实 handler 契约一致）；
2. **把该工具 `enabled = true`**（否则即使 schema 合法也不可绑定）。

> ⚠️ **第 2 步不是无副作用的**：对某些工具，它会改变**既有** legacy Agent 的行为。
> ⇒ **第一波（推荐）只做第 1 步** —— 只绑定三个**已经 `enabled = true`** 的工具
> （`supplier_360` / `supplier_risk` / `graph_traverse`），**一个 `enabled` 开关都不要翻**。
> 4 个 wiki 工具属于**后续波次**，需要**单独评估 + 单独审批**。详见 §2.5。

### 2.3 参考 schema（与真实 handler 参数契约一致）

`supplier_360` / `supplier_risk` / `graph_traverse` 的 handler 读 `args["key"]`（字符串）：

```json
{"type": "object", "properties": {"key": {"type": "string"}}, "additionalProperties": false}
```

`wiki_search` / `wiki_read` / `rule_evaluate` 的 handler 读 `args["query"]`（字符串）：

```json
{"type": "object", "properties": {"query": {"type": "string"}}, "additionalProperties": false}
```

`coverage_status` 是无参工具（handler 读 `args = {}`）：

```json
{"type": "object", "properties": {}}
```

> `additionalProperties: false` 是有意义的：编排侧 `validateToolArguments` 会**递归拒绝**
> schema 未声明的键。写成 `"additionalProperties": true` 会被 `validateObjectSchema` 判不合格。
>
> 这些 schema 与线上「工具定义」页面（`/admin/tools`）维护的是同一张表
> `agent_tool_config`（列：`name` / `handler_kind` / `handler_ref` / `enabled` / `input_schema`）。

#### 首选：走 UI（`/admin/tools`「工具定义」页）

与 seed 脚本的「UI 优先」纪律一致 —— 页面上改的值就是权威值。

> ⚠️ **但 `input_schema` 有例外**：backend 每次启动都跑
> `seed_agent_tool_configs.py`，其更新分支
> （`backend/app/services/agent_tool_config_service.py:349`
> 的 `existing.input_schema = fields.get("input_schema", {})`）
> 会把 `input_schema` 重置为 `{}` ⇒ **写完 schema 后重建 backend 容器有可能
> 把它抹掉**（触发条件：该行元数据与 seed 不一致）。因此**每次重建后必须重跑
> §2.4 的复核**。

1. 打开 `/admin/tools`，找到目标工具（按 `name`）点「编辑」；
2. 「输入 schema」填 §2.3 里对应的 JSON（`type` / `properties` / `additionalProperties`）；
3. **第一波：不要动「启用」开关**（三个目标工具本来就是已启用状态，保存 schema 即可）；
   翻「启用」属于 §2.5 的**后续波次**，需要单独评估 + 单独审批；
4. 保存后按 §2.4 复核（只读 SQL + `tool-options` 都要过）。

#### 兜底 / 进阶：直接写 SQL

页面改不了、或要批量改时再用。**用之前先备份该行**（阶段 1 已给出备份命令：
`COPY (SELECT * FROM agent_tool_config) TO '/tmp/agent_tool_config_backup.csv' CSV HEADER;`）。

```sql
-- 第一波：只改 schema，**不动 enabled**（示例：supplier_360）
UPDATE agent_tool_config
SET input_schema = '{"type":"object","properties":{"key":{"type":"string"}},"additionalProperties":false}'::jsonb
WHERE name = 'supplier_360';
```

> ⚠️ **不要在这一步顺手写 `enabled = true`** —— 那是 §2.5 的后续波次，
> 会改变 4 个既有 legacy Agent 的可运行性。

> ⚠️ SQL 路径绕过页面的校验与留痕，改完**必须**执行 §2.4 的复核，
> 并把改动记进灰度验收记录。

### 2.4 验证

```sql
SELECT name, enabled, input_schema FROM agent_tool_config
WHERE handler_ref IN ('supplier_360','supplier_risk','graph_traverse',
                      'wiki_search','wiki_read','rule_evaluate','coverage_status');
```

判据（**分支式**）：先实测 `enabled`，再按分支走 ——
「四行是 `false`」是那台库的**历史行状态**，不是本仓可复现的基线（见 §2.1 的 ⚠️）。

- **共同项（两个分支都要满足）**：`supplier_360` / `supplier_risk` / `graph_traverse`
  三行的 `enabled` **未被改动**，且 `input_schema` 形如 §2.3；
- **分支 A —— 实测 `wiki_search` / `wiki_read` / `rule_evaluate` / `coverage_status`
  四行的 `enabled` 为 `false`**：
  - 本波次（只补 schema）**成立**；
  - 这四行**必须保持 `false`**（没被顺手翻掉）—— 翻它们属于 §2.5 的**后续波次**，
    需要单独评估 + 单独审批；
  - 灰度验收记录里写明「这 4 个工具在本环境实测为 `false`，本波次未触碰」。
- **分支 B —— 实测那四行的 `enabled` 为 `true`**（**本仓 seed 造出来的环境就是这一支**）：
  - 这 4 个 legacy Agent（§2.5 表里的 id 6 / 7 / 8 / 9）**本来就在执行**，
    本波次**只需补 schema**，`enabled` 一个都不用动；
  - **必须把「它们本来就是启用的」写进灰度验收记录**（否则后续有人按 §2.5 的
    「409 不可运行」叙述去判断，会得出相反结论）。
- **再开 flag 后**调用 `GET /api/v1/agent-orchestrations/tool-options`，
  上面三个工具必须出现在返回列表里（该端点只列**当前有效**的工具）。

### 2.5 波次划分：第一波（只补 schema） vs 后续波次（翻 `enabled`）

**为什么必须分波次。** 白名单里的 4 个 wiki 工具（`wiki_search` / `wiki_read` /
`rule_evaluate` / `coverage_status`）**分别绑定在 4 个 `status = active` 的
既有 legacy Agent 上**（实测于 2026-10-28 开发库 `qa_metadata` 只读查询，
与 §2.1 是**同一次查询**）。

> ⚠️ **前提校正（务必先读）**：这 4 个工具的 `enabled = false` 是**那台库的
> 历史行状态**，**不是**本仓 seed 的产物 —— 本仓
> `backend/scripts/seed_agent_tool_configs.py:35/45/55/68/78/88/98` 把
> **全部 7 个工具**都写成 `"enabled": True`
> （`backend/app/services/agent_tool_config_service.py:339` 的
> `enabled=fields.get("enabled", True)`），**没有任何迁移**把 wiki 工具置为 disabled。
> ⇒ 下文的「409 不可运行」**只在实测为 `false` 的环境里成立**；
> 在由本仓 seed 造出来的环境里，这 4 个工具**本来就是启用的**、
> 那 4 个 legacy Agent **本来就是可运行的** ⇒ 下面的行为变更推演**不适用**，
> 本波次**只需补 schema**（并按 §2.4 的分支 B 把这件事写进灰度验收记录）。

| 4 个 wiki 工具 | 绑定的既有 legacy Agent | Agent 状态 |
|---|---|---|
| `wiki_search` | id 6 `WIKI_SEARCH_AGENT` | active |
| `wiki_read` | id 7 `WIKI_READ_AGENT` | active |
| `rule_evaluate` | id 8 `WIKI_RULE_AGENT` | active |
| `coverage_status` | id 9 `WIKI_COVERAGE_AGENT` | active |

legacy 运行时解析绑定工具走 `AgentToolConfigRegistry`，它**只装载 `enabled = true`
的行**（`backend/app/services/agent_tool_config_registry.py:33` 的 `warmUp`、
`:79` 的 `reload_one`）；工具缺失或被禁用时抛
`ConflictError(409 tool_unbound_or_disabled)`
（`backend/app/services/agent_runtime_service.py:165-178`，其 docstring 明写
「tool 不存在或被禁用 → ConflictError(409 tool_unbound_or_disabled)」）。

⇒ **实测为 `false` 的环境里**，这 4 个 Agent **今天就是 409 不可运行**；
把 `enabled` 翻成 `true` 会让它们**从「409 不可运行」变成「真的执行」** ——
这是对**既有功能**的行为变更，与本特性的底线「现有功能零行为变化」冲突。
⇒ **实测为 `true` 的环境里**，它们**本来就在执行**，翻 `enabled` 是个**空操作**
（后续波次对这类环境**没有意义**；它只对**新出现**的 disabled 工具才有价值）。

⇒ 而**只改 `input_schema` 对 legacy 路径是无副作用的**：
`AgentToolAssembly.assemble` 只是把 `input_schema` 拷进描述符
（`backend/app/services/agent_tools.py:281`），legacy 的入参来自 `arg_extractor`
（`backend/app/services/agent_runtime_service.py:140`），编排模块之外没有生产代码读
`input_schema`。**⇒ `enabled` 是唯一影响行为的开关。**

**波次约定：**

| 波次 | 动作 | 影响面 | 审批 |
|---|---|---|---|
| **第一波（推荐，本手册默认）** | 只给 `supplier_360` / `supplier_risk` / `graph_traverse` 补合法 schema；**一个 `enabled` 都不翻** | 既有 legacy 行为**零变化**（这三个工具本来就已启用，legacy 读的是 `arg_extractor`） | 随本特性的灰度审批 |
| **后续波次（另行评估、另行审批）** | 翻 4 个 wiki 工具的 `enabled = true`，使其可被编排绑定 | **仅当这 4 个工具实测为 `false`**：4 个既有 legacy Agent 从 409 不可运行变为可执行 —— 必须单独评估（ACL、prompt 面、输出质量）并单独审批。**实测为 `true` 时**：无行为变化，但也无收益 | **单独审批**；影响必须记进灰度验收记录 |

> 后续波次的 SQL 形如
> `UPDATE agent_tool_config SET enabled = true WHERE name = 'wiki_search';`
> —— **本手册的阶段 0 不执行它**。要执行时，先按上表登记 4 个 Agent 的影响面，
> 再走独立审批，并在灰度验收记录里逐个写明「从 409 → 可运行」
> （**仅在实测为 `false` 时**才有这一句；实测为 `true` 时记录「本来就是启用的，
> 本次翻开关为空操作」）。

---

## 3. 部署契约（Worker 长什么样）

| 项 | 值 |
|---|---|
| 服务名 | `agent-orchestration-worker`（容器名 `qa-agent-orchestration-worker`） |
| profile | `agent-orchestration`（**只有**它 ⇒ 默认不启动） |
| 镜像 | 与 `backend` 同源（同一 `docker/Dockerfile.backend` + 同一 build context），compose 自动派生名 `qa-system-agent-orchestration-worker`（compose 版本不同可能是下划线 `qa-system_agent-orchestration-worker`） |
| 端口 | **无**（不发布宿主端口） |
| 命令 | `python -m app.workers.agent_orchestration_worker`（覆盖镜像默认 CMD；Worker **不跑 alembic**，迁移由 backend 容器负责） |
| 环境 | `env_file: .env` + `ENABLE_AGENT_ORCHESTRATION=true`（**覆盖** .env 里的同名变量） |
| 依赖 | 与 backend 相同：`postgres` / `neo4j` / `milvus-standalone`（均 `service_healthy`） |
| 重启策略 | `unless-stopped`（与 backend 一致） |
| CentOS | **不在** `docker-compose.centos.yml` 里重复定义 —— overlay 合并时继承基础文件定义（含 `profiles`） |

启动 / 停止命令（生产在 `docker/` 目录下执行，overlay 已合并）：

```bash
# 启动（仅此一条命令会带起 Worker）
docker compose -f docker-compose.yml -f docker-compose.centos.yml \
  --profile agent-orchestration up -d --no-build agent-orchestration-worker

# 优雅停止（SIGTERM：当前 run 跑完再退）
docker compose -f docker-compose.yml -f docker-compose.centos.yml \
  --profile agent-orchestration stop agent-orchestration-worker
```

> ⚠️ **启动前 Worker 镜像必须已在位**：Worker 服务有 `build:` 且**没有** `image:` 键，
> compose 派生出的名字（`qa-system-agent-orchestration-worker`，见 §3 镜像行）与 backend 的
> 派生名**不同** ⇒ 必须先按**阶段 2**（部署镜像）把镜像放到目标机、名字与目标机
> `config --images` 的输出一致。`--no-build` 保证启动**绝不触发构建**：
> 镜像缺失时直接报 “image not found”，而不是偷偷拿当前 build context 构建一个。

> ⚠️ **不要用 `docker kill -9`**：会留下悬挂租约（`lease_until` 未过期时别的 Worker 接不了）。
> 停之前先做 §7 的租约排空检查。

---

## 4. 分阶段灰度序列

每个阶段都有**进入条件**和**验收判据**；不满足不要进下一阶段。

### 阶段 1：备份 + 记录当前版本（flag 仍关闭）

**进入条件**：

- 目标环境当前跑的是**旧镜像**（本特性尚未上线，`ENABLE_AGENT_ORCHESTRATION` 关闭）；
- 阶段 0 的三个目标工具 schema 已按 §2.4 复核通过；
- **0135 是新镜像的硬前置**：0135 的迁移脚本只随**新镜像**交付，旧镜像的 alembic
  head 是 `0134` ⇒ **在旧镜像下执行不了 0135**。
  ⇒ 本阶段**不执行迁移**，迁移**明确归到阶段 2 的镜像更新**（见下面第 2 步与阶段 2）。

1. 备份（元数据库）：
   ```bash
   pg_dump -h <pg-host> -U <user> -d qa_metadata -Fc -f /backup/qa_metadata_$(date +%Y%m%d-%H%M).dump
   ```
   另存一份现有 `agent_tool_config`：
   ```sql
   COPY (SELECT * FROM agent_tool_config) TO '/tmp/agent_tool_config_backup.csv' CSV HEADER;
   ```
2. **检查并记录当前版本（只读，不迁移）**：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     exec backend alembic current
   ```
   → **记录输出**（迁移前应为 `0134`）。
   > ⚠️ **不要在这里执行 `alembic upgrade head`** —— 此刻跑着的还是**旧镜像**，
   > 它的 alembic head 就是 `0134` ⇒ 这条命令是 **no-op**，`alembic current`
   > 会永远停在 `0134`，本阶段的验收判据就永远过不去。
   > **0135 由阶段 2 的新镜像执行**（新镜像的 backend 容器**第一次启动**时，
   > 镜像 CMD `docker/Dockerfile.backend:84`
   > = `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000`
   > 会自动跑 `alembic upgrade head`）。
3. 5 张表（**只增不改**既有表）：`agent_orchestration_profile` /
   `agent_orchestration_binding` / `agent_orchestration_run` /
   `agent_orchestration_step` / `agent_orchestration_usage`
   —— **本阶段它们还不在**（迁移未执行，符合预期）；建表发生在阶段 2。
4. 验收（**与「迁移在阶段 2」这条路径自洽**）：
   - 备份已生成（`qa_metadata_*.dump` + `agent_tool_config_backup.csv`）；
   - 已**记录**迁移前的 `alembic current`（旧镜像 ⇒ 应为 `0134`）；
   - 5 张 `agent_orchestration*` 表**尚不存在**；
   - **既有表行数没变**。
   ⇒ 「`alembic current = 0135`」与「五张表都在」这两条判据**属于阶段 2**
   （见阶段 2 第 4 步），因为**只有新镜像能执行 0135**。

> **回滚口径**：本阶段的回滚是「把 flag 保持关闭」，
> **不是** `alembic downgrade` —— `0135` 的 downgrade 会 **DROP 这 5 张表**。
> 表留着无害（没有路由就没有写入），留表回滚是本手册的既定口径。
>
> ⚠️ **0135 之后的回滚口径不是「回滚镜像」**，而是「**新镜像 + 关 flag**」——
> 详见 §5：一旦 0135 已执行，回到**旧镜像**会被启动期的 schema 漂移门
> （`backend/app/infrastructure/schema_drift.py:113-115`，`"alembic"` /
> `"missing_table"` 均为 `blocking`）挡住 ⇒ 旧镜像的 backend **起不来**。

### 阶段 2：部署镜像（**0135 在这一步随新镜像执行**）

1. 正常更新 backend 镜像 —— **0135 的迁移随新镜像执行**：
   新镜像的 backend 容器**第一次启动**时，镜像 CMD（`docker/Dockerfile.backend:84`）
   会自动跑 `alembic upgrade head` ⇒ **0135 就在这一步落库**。
2. Worker 镜像：与 backend **同一份 Dockerfile/context**，内容等价。
   离线机（CentOS，无外网、无构建机）做法二选一：
   - **免重建**（推荐）：把已传输的 backend 镜像打一个 Worker 名的 tag
     ```bash
     docker tag qa-system-backend:latest qa-system-agent-orchestration-worker:latest
     ```
     tag 名必须与目标机 compose 解析出的名字一致 —— 以目标机
     `docker compose -f docker-compose.yml -f docker-compose.centos.yml --profile agent-orchestration config --images`
     的输出为准（不同 compose 版本可能是下划线）。
   - **重新构建**（构建机上）：
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.centos.yml \
       --profile agent-orchestration build agent-orchestration-worker
     ```
     再 `docker save` 进传输包。
3. 注意：`docker compose -f docker-compose.yml -f docker-compose.centos.yml config --images`
   （**不带** `--profile`）**不含** Worker 镜像
   ⇒ 离线镜像包脚本（`backend/scripts/build_offline_images.py`）默认不会带它，
   需要按上面第 2 步单独补进传输包。
4. **迁移验收（0135 已随新镜像执行）** —— 在**新镜像的 backend 容器已经启动**
   之后执行（阶段 4 / 阶段 8 的 `up -d --force-recreate --no-build backend`
   就是那一次启动；若此刻 backend 容器还没用新镜像重启过，本步顺延到它启动之后）：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     exec backend alembic current   # 期望 0135
   ```
   - `alembic current` = `0135`；
   - `\dt agent_orchestration*` **五张表都在**：
     `agent_orchestration_profile` / `agent_orchestration_binding` /
     `agent_orchestration_run` / `agent_orchestration_step` /
     `agent_orchestration_usage`；
   - **既有表行数没变**（0135 只建表，不改既有表）。
   > ⚠️ 若 `alembic current` 仍是 `0134` ⇒ **新镜像没真正跑起来**（跑的还是旧镜像，
   > 旧镜像的 alembic head 就是 `0134`，`alembic upgrade head` 是 no-op）
   > ⇒ **停下排查镜像，不要继续往下走**。

### 阶段 3：验证「默认仍然是关的」

```bash
# ① 新路由不存在 ⇒ 404（不是 401！路由没注册时不存在鉴权层）
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:8000/api/v1/agent-orchestrations
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:8000/api/v1/agent-runs

# ② Worker 没起来
docker compose -f docker-compose.yml -f docker-compose.centos.yml \
  ps | grep agent-orchestration   # 期望：无输出
```

验收判据：
- 两条 curl 都是 **404**；
- `docker compose ... ps` 里没有 Worker；
- **旧契约不变**：`/api/v1/chat` 等既有端点行为与上线前一致（抽样比对关键返回）。
  ⚠️ **这条成立的前提是阶段 0 只补了 schema、没有翻任何 `enabled`**（§2.5 第一波）。
  如果有人提前翻了 4 个 wiki 工具的 `enabled`，4 个既有 legacy Agent 会从
  「409 不可运行」变成「真的执行」⇒ **旧契约已变**，此时本判据**不成立**，
  必须按 §2.5 走后续波次的单独评估与审批；
- 菜单里没有「编排配置 / 编排任务」两项。

> ⚠️ 判据必须是 404 而不是 401：路由未注册时请求不会进入鉴权依赖，
> 用「没带 token 所以 401」当证据是典型的假绿。

### 阶段 3.5：PRE-ENABLE CHECKLIST（把 `ENABLE_AGENT_ORCHESTRATION` 打开**之前**必须逐项关闭）

> 本特性**出厂即关闭**（`ENABLE_AGENT_ORCHESTRATION` 默认 `false`，不注册新路由、不起 Worker）。
> 下面 **14 项**由 `security-reviewer`、`code-reviewer` 与最终评审共同提出，
> **全部要求：在生产把开关打开之前关闭**。
> 任一项未关闭 ⇒ **不要**把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。
>
> **编号说明（可追溯性用）**：
>
> - **P1 / P2 / P3** —— 开发期评审的发现（安全 MEDIUM `M1`/`M2`/`M3`；
>   代码评审 `M10`/`M4`/`M11`）。原始评审报告是开发期产物、不随仓库存档，
>   **下面每一项都自带完整描述**（事实 + 影响 + 关闭方式）⇒
>   **不需要任何外部文档就能照着勾**。
> - **P4 – P11** —— 最终评审 §3 的条目编号（`A-10` / `A-20` / `A-30` / `A-39` /
>   `A-61` / `A-68` / `A-72` / `A-81`+`A-82` 合并为一条），**每条都是开放闸门**。
> - **P12 – P14** —— 同样来自最终评审 §3（`A-79` / `A-80` / `B-28`），但它们是
>   **灰度手册里命令与文字的机械修正**，**已由本次修复派单直接关闭**
>   ⇒ 留在清单里只作**留痕**（每条都带证据）。
>
> **P1 / P2 / P3 的措辞形态不同，请按各自形态理解**：
> P1 是「**已实证不可利用**（当前）+ **前提条件**（前提一旦被破坏就变成可利用路径）」，
> P2 / P3 是「**待修** + **缓解窗口**（未修时靠什么撑住）」。
> **登记进清单 ≠ 已处置**：每一条都必须有真实的关闭动作。

- [ ] **P1 — 只读白名单的 TOCTOU（读-改-写竞态）｜已实证不可利用 + 前提条件**
      **事实**：白名单断言发生在 `prepare()` 读到的**第一份**行上
      （`backend/app/services/agent_orchestration_tool_executor.py:90-91`：
      `_loadEnabledToolConfig` + `_assertReadOnlyBuiltin`），而**真正被执行**的 handler
      是从**第二次**读取装配出来的（`_resolveTool` → `registry.reload_one`，同文件 `:253`）。
      两次读取之间如果管理员改了 `handler_ref`，断言过的行与执行的行就不是同一行。
      **当前状态：已实证不可利用** —— 评审在真实 PostgreSQL 上验证过：同一 session 内
      `expire_on_commit=False` 的 identity map 让第二次读拿到的是**同一行版本**
      ⇒ 断言行与执行行一致（**同一 session 内自洽**）。
      **前提条件（一旦破坏就变成可利用路径）**：
      ① 两次读之间**没有别的会话 / 进程**改这一行；
      ② 白名单之外的 handler 保持惰性 —— 当前唯一不在白名单里的 handler
      `nl2sql_default` 是惰性占位符，**一旦把 NL2SQL 接进编排，前提 ② 就被破坏**。
      **影响**：前提被破坏时，被执行的对象可能不是被断言过的那一行 ⇒ 只读白名单被绕过。
      **关闭方式**：在**实际被执行的那一行**上重新断言只读白名单。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P2 — 租约只在「轮边界」续｜待修 + 缓解窗口**
      **事实**：`renewLease` 在整个仓库里**只被调用一次**，位于 `_safetyPoint`
      （`backend/app/services/agent_orchestration_runner.py:156-168`，续租调用在 `:162`）；
      编排路径的 LLM 客户端**没有显式 timeout**（`openai_client.py` 不传 `timeout`，
      沿用 OpenAI SDK 默认读超时 600s），而租约默认 **60s**
      （`AGENT_ORCHESTRATION_LEASE_SECONDS`，`backend/app/config.py:169`）。
      ⇒ 单轮规划慢于 60s 就会**丢租约** ⇒ `_recoverOneExpired` 重新排队 ⇒ 该轮**重做**
      （重复 LLM 花费；`getOrCreatePlanStep` 复用 RUNNING 的 PLAN 行且**不**累加
      `iteration_count`，所以 `max_iterations` 约束不到它）。
      **写入侧是安全的**（所有写都走 `_lockLeasedRun`）。
      **影响**（评审 ID `M4` / `C-04`）：慢工具被**第二个 Worker 并发重放**
      ⇒ 重复执行 + **重复 LLM 费用**，直接击穿本特性的成本预算语义。
      **关闭方式**：给 LLM 调用设显式 timeout（小于租约），或把续租下沉到轮内
      （工具调用内续租 / 租约时长绑定最长工具耗时）。
      **缓解窗口（未修时靠什么撑住）**：灰度期间按**单 Worker** 部署（杜绝第二个
      Worker 并发重放）、只跑**只读快工具**，并把 `AGENT_ORCHESTRATION_LEASE_SECONDS`
      调大到覆盖最长工具耗时；
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P3 — Planner 的工具目录无长度 / 数量上界｜待修 + 缓解窗口**
      **事实**：`_buildMessages` / `_reduceToolCatalog`
      （`backend/app/services/agent_orchestration_planner.py:371-410`）把工具目录原样拼进
      system prompt；`agent_tool_config.description` 是**无界 `Text`**，
      `tool_names` 也**没有元素个数上限**。
      ⇒ 目录膨胀会直接推高每轮 prompt 成本与超窗风险。
      **影响**（评审 ID `M11` / `C-11`）：绕开了本分支唯一的提示预算防线，
      且**每次请求都生效**（不是偶发）。
      **关闭方式**：给目录加元素数上限与描述长度截断（并在 `tool_names` 上加
      `max_length`）。
      **缓解窗口（未修时靠什么撑住）**：灰度期间由管理员把工具目录控制在
      **白名单规模（7 个）**以内，避免 `description` / `input_schema` 膨胀；
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P4 — A-10 · `validateObjectSchema` 接受 `$ref`/`anyOf`/`oneOf`/`allOf`，嵌套对象绕过严格遍历**
      **事实**：`backend/app/services/agent_orchestration_validation.py:71` 的
      `validateObjectSchema` 要求 `type == "object"`、`properties` 是 dict、
      `additionalProperties` 为 `false` 或缺省、拒绝 `patternProperties`；
      但**不拒绝** `$ref` / `anyOf` / `oneOf` / `allOf`，且 `properties` 的**值**
      （嵌套对象）**不做递归校验** ⇒ 组合子可以绕过严格遍历。
      **影响**：写进 `input_schema` 的组合子不会被展开校验 ⇒ 编排侧的工具入参校验
      比预期的松（嵌套对象里的未声明键可能不被拒）。
      **关闭方式**：启用前加一句「**拒绝组合子**」（`$ref`/`anyOf`/`oneOf`/`allOf`
      一律不接受），并对 `properties` 的值递归校验。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P5 — A-20 · `GET /{agentCode}` 仅要求登录，无 owner/admin 过滤（= I1，跨部门信息泄露）**
      **事实**：`backend/app/api/v1/agent_orchestrations.py:91-104` 的 `getProfile`（`:96` 起）
      只挂了 `Depends(getCurrentUser)`，**没有** owner / admin 过滤
      ⇒ 任何登录用户按 `agentCode` 就能读到别人部门的 profile。
      **影响**：跨部门信息泄露（profile 里含 instructions 与工具绑定）。
      **关闭方式**：加 owner/admin 过滤（**越权统一 404**，不要用 403 暴露存在性）。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P6 — A-30 · PATCH 接受未校验的 `arg_extractor_kind` ⇒ 一次配置编辑可致下次启动预热崩**
      **事实**：`backend/app/services/agent_tool_config_service.py:183-184` 直接把请求里的
      `arg_extractor_kind` 赋给行，**没有**按 `ARG_EXTRACTORS`
      （`backend/app/services/agent_tools.py:232`）校验。
      **影响**：写入一个不存在的 extractor ⇒ 下次启动预热时 legacy 运行时解析绑定工具
      会崩 ⇒ **一次配置编辑就能打挂 backend 启动**。
      **关闭方式**：按 `ARG_EXTRACTORS` 校验 `arg_extractor_kind`，非法值 422。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P7 — A-39 · `completeStream` 消费者放弃 generator 时不写用量行；终态用量后 provider 抛错会被丢弃（= I13）**
      **事实**：`backend/app/services/agent_orchestration_metering.py` 的 `completeStream`
      已处理了一部分：provider 在终态块**之后**抛错 ⇒ 走 `except` 分支记终态用量；
      消费者在**还没有终态块**时放弃 ⇒ `finally` 派后台任务补记
      （`_scheduleAbandonRecord`）。
      **仍存缺口**：消费者**见到终态块之后**再放弃时，`finally` 里仍走
      `await self._recordStreamSuccess(terminal)` —— 在 `GeneratorExit` 处理中
      `await` 会抛 `RuntimeError` ⇒ 那一行用量**照样丢**。
      **影响**：违反「所有 LLM 调用都要有用量记录」⇒ 台账漏账、成本不可核对。
      **关闭方式**：补用量行 —— 把终态落账也改成与放弃路径一致的后台补记，
      并补一条「消费者提前 break（含终态块之后）」的用例。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P8 — A-61 · 租约过期以调用方墙钟为准，多主机时钟偏移可能提前回收活租约**
      **事实**：租约的 `lease_until` 用**各 Worker 自己的墙钟**计算与判定
      ⇒ 多主机部署时若节点之间有时钟偏移，可能把**还活着的**租约判成过期并回收。
      **影响**：活着的 run 被第二个 Worker 抢走 ⇒ 重复执行 + 重复 LLM 费用
      （与 `C-04` / P2 叠加）。
      **关闭方式**：部署手册写明 **NTP** 要求（灰度期间所有节点与同一 NTP 源同步）。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P9 — A-68 · 步骤表 `(run_id, iteration_no, kind)` 无 DB 唯一约束**
      **事实**：`backend/alembic/versions/0135_agent_orchestration.py:137-177` 的
      `agent_orchestration_step` 只对 `("run_id", "step_no")` 建了唯一约束
      （`uq_agent_orchestration_step_run_step`），`iteration_no` / `kind` 是普通列
      ⇒ 同一 `(run_id, iteration_no, kind)` 可以写入多条步骤。
      **影响**：重放 / 并发下同一轮 PLAN / TOOL 步骤可能重复落库，关联计量也随重复步骤
      虚增 ⇒ 轨迹与成本统计不可信。
      **关闭方式**：启用前在步骤表补唯一约束 `(run_id, iteration_no, kind)`。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P10 — A-72 · `AUTH_MODE=stub` 下 12 条新路由的**运行期**匿名 401 未覆盖**
      **事实**：`backend/app/tests/integration/test_agent_orchestration_route_auth.py`
      的 docstring 明写「断言的是**路由依赖声明**（静态结构），不是运行时行为」
      ⇒ 12 条新路由的**运行期**匿名请求被拒**没有被覆盖**。
      **影响**：鉴权依赖可能在重构中静默丢失而测试仍绿 ⇒ 匿名可访问（数据泄露）。
      **关闭方式**：补运行期匿名请求被拒的断言（匿名请求 ⇒ 401），
      与现有的静态声明断言并存。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [ ] **P11 — A-81 + A-82 · `AgentRunRead` 双份导出（不同形状）；`StepStatus` 双份导出（不同取值集）**
      **事实**：`frontend/src/types/agentRuntime.ts:9` 与
      `frontend/src/types/agentOrchestration.ts:160` 各有一份 `AgentRunRead`（形状不同）；
      `frontend/src/types/chat.ts:142` 与 `frontend/src/types/agentOrchestration.ts:21`
      各有一份 `StepStatus`（取值集不同：小写 `"pending" | "running" | …` vs 大写
      `"RUNNING" | "SUCCEEDED" | …`，后端口径见
      `backend/app/services/agent_orchestration_types.py:39`）。
      评审把 **A-82 记为 A-81 的重复条目** ⇒ 这里**合并为一条**。
      **影响**：同名类型两套取值 ⇒ 前后端对齐时极易拿错那一份 ⇒ 类型系统不再兜底。
      **关闭方式**：**定名或按域重命名**（如 `AgentRunRead` → `OrchestrationAgentRunRead`、
      `StepStatus` → `OrchestrationStepStatus`），并统一到后端枚举的取值集。
      **未关闭 ⇒ 不要把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`。**

- [x] **P12 — A-79 · 阶段 8 / 回滚的 recreate 命令漏了 `-f` 覆盖标志｜已关闭（本派单已修）**
      **事实**：评审指出阶段 8 / 回滚的 `docker compose` 命令漏了
      `-f docker-compose.yml -f docker-compose.centos.yml`，在 CentOS overlay 环境下
      会解析出错误的镜像 / 卷。
      **关闭方式**：**所有**生产用 compose 命令都带两个 `-f`。
      **已关闭 + 证据**：本手册的生产命令（阶段 1 第 2 步、阶段 2 第 2/4 步、阶段 3、
      阶段 4 第 2/3/4 步、阶段 8 第 2 步、§5 回滚第 1/2 步与回滚后校验段）
      **已逐条核对**，每一条都带 `-f docker-compose.yml -f docker-compose.centos.yml`。

- [x] **P13 — A-80 · `docker compose up -d --force-recreate backend` 在离线生产机上可能触发镜像构建｜已关闭（本派单已修）**
      **事实**：backend 服务带 `build:` 且没有 `image:` 键 ⇒ 少了 `--no-build`，
      `up` 可能隐式触发一次构建；离线机上这次构建会直接失败并可能把 backend 带下去。
      **关闭方式**：所有 `up -d --force-recreate` 都带 `--no-build`。
      **已关闭 + 证据**：本手册的 `up -d --force-recreate`（阶段 4 第 2 步、
      阶段 8 第 2 步、§5 回滚第 2 步）**已逐条核对**，每一条都带 `--no-build`。

- [x] **P14 — B-28 · `--force-recreate` 带来的**未被记录的 backend 重启窗口**｜已关闭（本派单已修）**
      **事实**：`up -d --force-recreate` 会停掉旧容器再起新容器 ⇒ 有一段时间
      backend **不可用**，手册原先没有记录这个窗口。
      **关闭方式**：手册注明「**重建 backend 会有一段重启窗口，灰度窗口内择时执行**」。
      **已关闭 + 证据**：阶段 4 第 2 步与阶段 8 第 2 步旁均已加注该提示。

> 本清单**不要求**在本手册里改代码——它是**启用前的闸门**。
> 其中 **3 条（A-79 / A-80 / B-28，即 P12 – P14）已由本次修复派单直接关闭**
> （证据见各条），其余 **11 条（P1 – P11）仍是开放闸门**。
>
> ⚠️ **登记进清单 ≠ 已处置**：**每一条都必须有真实的关闭动作**
> （改代码 / 改配置 / 写进验收记录 / 演练一次），否则**保持
> `ENABLE_AGENT_ORCHESTRATION=false`**（特性出厂即关，业务不受影响），
> 把未关闭的项挂到变更记录的「已知限制」里，走独立变更再修。

### 阶段 4：测试环境打开 flag + 起 Worker

1. 在**测试环境**的 `docker/.env` 里把 `ENABLE_AGENT_ORCHESTRATION` 改成 `true`
   （用编辑器改，**不要 cat**）。
2. **重建** backend 容器（路由在进程启动时注册）：
   > ⚠️ 必须用 `up -d --force-recreate --no-build`，**不要用 `docker compose restart`**：
   > `restart` 只是把**既有容器**停掉再起，**不会重新读取 `env_file`** ——
   > 容器里仍是创建时烘进去的 `ENABLE_AGENT_ORCHESTRATION=false`，
   > 路由不会注册，下一步的 401 判据必然失败（且会把人引向错误的原因）。
   > `--no-build` 保证这一步**只重建容器、不重建镜像**（镜像部署是阶段 2 的事；
   > 少了它，离线机上可能触发一次构建尝试而失败，把 backend 带下去）。
   > ⚠️ **重建 backend 会有一段重启窗口，灰度窗口内择时执行** ——
   > `up -d --force-recreate` 会先停旧容器再起新容器，这段时间 backend **不可用**
   > （旧容器已停、新容器还没监听）。
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     up -d --force-recreate --no-build backend
   ```

   然后**把开关读回来确认它真的生效**（只读**单个非敏感开关**，不 dump 整个环境）：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     exec backend printenv ENABLE_AGENT_ORCHESTRATION   # 期望：true
   ```
   输出不是 `true` ⇒ **停下排查**（多半是 env 文件没被重新读取），不要继续往下走。

   **复核 schema（重建后必做）** —— 重建会触发 backend 启动时的 seed，
   它**可能**把 `input_schema` 重置为 `{}`（见 §2.3 的 ⚠️）：
   ```sql
   SELECT name, enabled, input_schema FROM agent_tool_config
   WHERE handler_ref IN ('supplier_360','supplier_risk','graph_traverse',
                         'wiki_search','wiki_read','rule_evaluate','coverage_status');
   ```
   **期望**：三个目标工具（`supplier_360` / `supplier_risk` / `graph_traverse`）
   的 `input_schema` **仍形如 §2.3**（不是 `{}`），且 `enabled` 未被改动。
   若 `input_schema` 是 `{}` ⇒ 按 §2.4 的兜底 SQL **重写一次**，再复核。
3. 启动 Worker（§3 的启动命令；Worker 镜像必须已按阶段 2 就位，
   `--no-build` 让镜像缺失时直接报错而不是隐式构建）：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     --profile agent-orchestration up -d --no-build agent-orchestration-worker
   ```
4. 验收：
   - `docker compose -f docker-compose.yml -f docker-compose.centos.yml logs --tail 50 agent-orchestration-worker`
     出现 `agent orchestration worker 启动（worker=... poll=2.0s lease=60s）`；
   - 同一时间 `GET /api/v1/agent-orchestrations` 返回 **401**（路由已注册，只是没登录）
     —— 与阶段 3 的 404 形成对照，说明「flag 真的把路由打开了」。

### 阶段 5：验收跑通

1. `GET /api/v1/agent-orchestrations/tool-options`
   → 期望能看到阶段 0 第一波准备好的**三个**工具
   （`supplier_360` / `supplier_risk` / `graph_traverse`，**至少 1 个**，
   否则后面必然 422）。
   ⚠️ **4 个 wiki 工具不应因为本波次而出现在这个列表里** —— 本波次**不翻**它们的
   `enabled`。若它们**本来就**出现在列表里（实测为 `true` 的分支 B，见 §2.4），
   那是该环境的历史状态，**不是**本波次造成的。
2. 建一个**只读** profile：
   ```bash
   curl -X POST http://<host>:8000/api/v1/agent-orchestrations \
     -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
     -d '{"agentCode":"<agent_code>","instructions":"只读汇总，禁止任何写操作",
          "toolNames":["supplier_360"]}'
   ```
   - profile 创建后是 **disabled**，需要显式启用：
     `POST /api/v1/agent-orchestrations/<agent_code>/toggle`，body `{"enabled":true,"revision":1}`
     （`revision` 是乐观锁，改过之后要用最新的）。
3. 建一次 run：
   ```bash
   curl -X POST http://<host>:8000/api/v1/agent-runs \
     -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' \
     -d '{"agentCode":"<agent_code>","input":"<问题>","modelConfigId":<id>}'
   ```
   → 期望 **202** + `{"runId":"...","status":"QUEUED"}`
   （`AgentRunCreated` 是 CamelModel，字段序列化成 **camelCase**：`runId` / `status`；
   找 `run_id` 是找不到的）。
4. 验收清单：
   - run 被 Worker 领走：`GET /api/v1/agent-runs/<runId>` 状态从 `QUEUED` → `RUNNING` → `SUCCEEDED`；
   - 轨迹：`GET /api/v1/agent-runs/<runId>/steps` 有步骤记录；
   - 计量：`promptTokens` / `completionTokens` / `totalTokens` > 0，
     `costSource` 不得是 `UNKNOWN` 之外的糊弄值（`ESTIMATED` 需在验收记录里注明）；
   - 只读性：本次 run 没有产生任何写操作（编排只允许白名单里的只读工具）。

### 阶段 6：显式播种菜单（**唯一**的菜单入口）

> **前置**：本仓**已包含**这两个前端路由 —— `frontend/src/App.tsx:115-116`
> **无条件注册**了 `<Route path="agent-orchestrations" …/>` 与
> `<Route path="agent-tasks" …/>` ⇒ **不需要**再等前端任务。
> 播种前要确认的是**目标环境的 frontend 镜像已包含本次前端改动**
> （镜像比本仓旧 ⇒ 点进去是 **404**，不是「功能没做」）。
> 菜单行先于页面存在的话，侧边栏点进去就是 404 —— 入口暴露 ≠ 功能可用，
> 顺序反了就是「看得见点不动」。

```bash
cd backend
python -m scripts.seed_agent_orchestration_menu
```

- 首次输出 `seed_agent_orchestration_menu: 2 rows inserted`；重复执行输出 `0 rows inserted`
  （幂等：`on_conflict_do_nothing(index_elements=["code"])`）。
- 播种内容（挂在 `section.aiAgent` 下，sort_order 140/150，不与既有 110/120/130 冲突）：

  | code | label_key | icon_code | sort_order | path |
  |---|---|---|---|---|
  | `item.agentOrchestration` | `menu.item.agentOrchestration` | `appstore` | 140 | `/agent-orchestrations` |
  | `item.agentTasks` | `menu.item.agentTasks` | `thunderbolt` | 150 | `/agent-tasks` |

- 验证：
  ```sql
  SELECT c.code, c.path, c.sort_order, p.code AS parent
  FROM menu_config c JOIN menu_config p ON p.id = c.parent_id
  WHERE c.code IN ('item.agentOrchestration','item.agentTasks');
  ```
  → 两行，parent 均为 `section.aiAgent`。
- **UI 优先**：已存在的行不会被覆盖（哪怕被菜单管理页改过 label/path/排序）。
- 父菜单 `section.aiAgent` 不存在时脚本**直接报错**（不静默跳过）——
  此时先跑 `python -m scripts.seed_menu_config`。
- ⚠️ 默认菜单种子里**没有**这两项，所以新装库默认看不到入口（这是刻意的）。

### 阶段 7：浏览器冒烟

1. 登录 → 侧边栏「智能体」分组下出现「编排配置」「编排任务」两项；
2. 点进去页面能打开（**无 500**），空态提示正常；
3. 旧菜单项位置/图标没有被改动（父菜单分组与其它子项顺序不变）。

### 阶段 8：生产启用（**需要审批**）

生产由人手动执行，顺序与测试环境完全一致：

0. **阶段 3.5 的 PRE-ENABLE CHECKLIST 14 项已逐项关闭**（缺一不可；其中 P12 – P14
   已由本次修复派单关闭，其余 **11 项 P1 – P11 必须各自有真实关闭动作** ——
   「登记进清单 ≠ 已处置」）；
1. 阶段 1（备份 + 记录当前版本）→ 阶段 2（部署镜像，**0135 随新镜像执行**）
   → 阶段 3（验证默认关闭）；
2. 生产 `docker/.env` 设 `ENABLE_AGENT_ORCHESTRATION=true` → **重建** backend 容器：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     up -d --force-recreate --no-build backend
   ```
   （**不要用 `docker compose restart`**，它不重读 `env_file`，开关不会生效；
   `--no-build` 保证只重建容器、不重建镜像）
   > ⚠️ **重建 backend 会有一段重启窗口，灰度窗口内择时执行** ——
   > `up -d --force-recreate` 会先停旧容器再起新容器，这段时间 backend **不可用**。
   → 再读回确认（只读**单个非敏感开关**）：
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.centos.yml \
     exec backend printenv ENABLE_AGENT_ORCHESTRATION
   ```
   输出 `true` 才继续；

   **复核 schema（重建后必做）** —— 重建会触发 backend 启动时的 seed，
   它**可能**把 `input_schema` 重置为 `{}`（见 §2.3 的 ⚠️）：
   ```sql
   SELECT name, enabled, input_schema FROM agent_tool_config
   WHERE handler_ref IN ('supplier_360','supplier_risk','graph_traverse',
                         'wiki_search','wiki_read','rule_evaluate','coverage_status');
   ```
   **期望**：三个目标工具（`supplier_360` / `supplier_risk` / `graph_traverse`）
   的 `input_schema` **仍形如 §2.3**（不是 `{}`），且 `enabled` 未被改动。
   若 `input_schema` 是 `{}` ⇒ 按 §2.4 的兜底 SQL **重写一次**，再复核；
3. 启动 Worker（§3 命令）；
4. 阶段 5 验收（**只跑一个只读 profile**）；
5. 阶段 6 播种菜单 → 阶段 7 浏览器冒烟；
6. 观察一个工作日：`agent_orchestration_run` 的状态分布、token 成本、失败原因。

---

## 5. 回滚手册（分钟级，不动数据）

> ⚠️ **回滚口径 = 「新镜像 + 关 flag」，**不是**「回滚镜像」。**
> 0135 一旦执行，DB 的 `alembic_version` 就是 `0135`；此时若把镜像换回**旧镜像**
> （alembic head = `0134`），旧 backend 启动时的 schema 漂移门会判「迁移版本不一致」
> ⇒ **blocking** ⇒ 容器**起不来**
> （`backend/app/infrastructure/schema_drift.py:113-115`：
> `"alembic"` / `"missing_table"` 均为 `blocking`）。
> ⇒ **回滚时镜像保持新版本**，只把 `ENABLE_AGENT_ORCHESTRATION` 改回 `false`
> （路由不注册、Worker 不起 ⇒ 功能关闭，backend 照常运行）。
> **不要** `alembic downgrade 0135`（那会 DROP 5 张表，见 §9 坑 7）。

| 顺序 | 动作 | 期望结果 |
|---|---|---|
| 1 | 排空租约（§7）→ `docker compose -f docker-compose.yml -f docker-compose.centos.yml --profile agent-orchestration stop agent-orchestration-worker` | Worker 退出，无悬挂租约 |
| 2 | 生产 `docker/.env` 把 `ENABLE_AGENT_ORCHESTRATION` 改回 `false` → **重建** backend（`docker compose -f docker-compose.yml -f docker-compose.centos.yml up -d --force-recreate --no-build backend`；**不能用 `restart`**，它不重读 `env_file`）→ 读回确认（只读**单个非敏感开关**）`docker compose -f docker-compose.yml -f docker-compose.centos.yml exec backend printenv ENABLE_AGENT_ORCHESTRATION` 输出 `false` | 新路由消失：`curl` 回到 **404** |
| 3 | 菜单行可保留（前端入口隐藏即可）；要彻底摘掉再执行 `DELETE FROM menu_config WHERE code IN ('item.agentOrchestration','item.agentTasks')` | — |
| 4 | **不要** `alembic downgrade 0135` | 5 张表保留，历史 run/配置不丢 |
| 5 | **不要**重新生成 `SECRET_KEY` | 见 §1 约束 6 |

回滚后校验：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<host>:8000/api/v1/agent-runs   # 404
docker compose -f docker-compose.yml -f docker-compose.centos.yml \
  ps | grep agent-orchestration                                               # 无
```

---

## 6. 相关接口清单（供冒烟/验收用）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/agent-orchestrations` | 建 profile（创建后默认 disabled） |
| GET | `/api/v1/agent-orchestrations` | 列 profile |
| GET | `/api/v1/agent-orchestrations/tool-options` | 列**当前有效**的编排工具（不含 handlerRef） |
| GET/PUT | `/api/v1/agent-orchestrations/{agentCode}` | 查/改 profile（PUT 需带 `revision`） |
| POST | `/api/v1/agent-orchestrations/{agentCode}/toggle` | 启停 profile（`enabled` + `revision`） |
| POST | `/api/v1/agent-runs` | 建 run（202，QUEUED） |
| GET | `/api/v1/agent-runs` / `/{runId}` / `/{runId}/steps` | 列/查/轨迹 |
| POST | `/api/v1/agent-runs/{runId}/resume` | 恢复 PAUSED 的 run（按 pause_code 决定是否需要 `input`） |
| POST | `/api/v1/agent-runs/{runId}/cancel` | 请求取消 |

- 全部需要登录（`Depends(getCurrentUser)`）：**flag 关闭 ⇒ 404**；
  **flag 开启但没带 token ⇒ 401**。两者含义完全不同，别混。
- 编排不在 API 进程里跑：FastAPI 的 `BackgroundTasks` 一律不用，
  执行体只在 Worker 进程里。

---

## 7. 停 Worker 前的租约排空检查

Worker 领走一条 run 时会写 `lease_owner` / `lease_until`（租约默认 60s）。
强杀进程会让这条 run 的租约悬挂，直到 `lease_until` 过期才有人能接手。

```sql
-- ① 正在跑的 run
SELECT run_id, status, lease_owner, lease_until, updated_time
FROM agent_orchestration_run
WHERE status = 'RUNNING'
ORDER BY updated_time;

-- ② 租约已过期但状态还是 RUNNING 的（悬挂租约）
SELECT run_id, lease_owner, lease_until
FROM agent_orchestration_run
WHERE status = 'RUNNING' AND lease_until < now();
```

排空判据：**① 的结果为空**（或只剩 ② 这类已被放弃的悬挂租约）。

- 仍有 `RUNNING` 时：先等它自然结束（Worker 会续租），
  或显式取消 `POST /api/v1/agent-runs/<runId>/cancel` 再停。
- `QUEUED` 的 run 不用管：Worker 下次启动会重新领（这是**期望行为**）。
- 停完再确认一次：
  ```sql
  SELECT count(*) FROM agent_orchestration_run WHERE status = 'RUNNING';
  ```

---

## 8. PAUSED / CANCELLED run 检查

```sql
-- ① 状态 × 暂停码分布
SELECT status, pause_code, count(*)
FROM agent_orchestration_run
GROUP BY status, pause_code
ORDER BY status, pause_code;

-- ② 需要人工关注的 run
SELECT run_id, agent_code, status, pause_code, pause_detail,
       cancel_requested, lease_owner, updated_time
FROM agent_orchestration_run
WHERE status IN ('PAUSED','CANCELLED')
ORDER BY updated_time DESC;
```

`pause_code` 语义与处置：

| pause_code | 含义 | 处置 |
|---|---|---|
| `NEEDS_INPUT` | 缺输入 | `POST /{runId}/resume`，body 带 `input` |
| `AMBIGUOUS` | 提问有歧义 | 同上，`input` 里补齐指代/范围 |
| `PERMISSION_DENIED` | 复查 ACL 时权限不足 | 调整用户/角色的工具授权后再 resume |
| `BUDGET_EXHAUSTED` | 预算耗尽 | resume 时用 `additionalIterations` / `additionalToolCalls` / `additionalCostUsd` / `additionalTimeoutSeconds` 放宽 |
| `TOOL_FAILED` | 工具连续失败 | 先查工具自身可用性/schema，再 resume |
| `PLANNER_OUTPUT_INVALID` | Planner 输出不合法 | 查 LLM 输出与提示词（可能需要换模型配置） |
| `AGENT_UNAVAILABLE` | 绑定的 agent 不可用 | 恢复 agent / 模型配置后 resume |
| `INFRASTRUCTURE_ERROR` | 基础设施异常（DB/LLM 连接） | 先恢复依赖，再 resume |

- `CANCELLED` 的 run 应当 `cancel_requested = true`；
  若出现「状态 CANCELLED 但仍占着租约」的 run，说明取消与执行有竞态，
  按 §7 处理悬挂租约。
- **恢复后要复查**：resume 会重新校验当前工具是否仍然合法
  （阶段 0 改过 schema/enabled 之后，老 run 的工具快照可能已不匹配）。

---

## 9. 已知坑（提前知道，少走弯路）

1. **`docker/.env` 不在仓库里**（gitignore）⇒ `docker compose config` 会直接报
   “env file not found”。做配置校验时用一份**假的** `.env` + 假的必填环境变量，
   不要打印真 `.env`。
2. **`docker-compose.centos.yml` 是 overlay，不能单独 `config`**：
   `mysql-demo` 只有在合并基础文件后才有 image/build ⇒ 必须
   `-f docker-compose.yml -f docker-compose.centos.yml`。
3. **overlay 里有 `!override` 标签** ⇒ `yaml.safe_load` 直接抛
   `ConstructorError`；解析 overlay 的脚本/测试要注册 `!override` 的
   multi-constructor（仓库里已有范例：`backend/app/tests/unit/test_compose_centos_overlay.py`）。
4. **compose 自动派生的镜像名跨版本不同**（`qa-system-backend` vs `qa-system_backend`）：
   离线部署一律以**目标机** `docker compose -f docker-compose.yml -f docker-compose.centos.yml config --images` 的输出为准。
5. **`config --images` 不带 `--profile` 时不含 Worker 镜像** ⇒ 离线包要单独补。
6. **profile 里的变量插值照样会发生**：即使 profile 没激活，
   `${XXX:?}` 形式的必填变量缺失也会让 `config` 失败。
7. **不要 `alembic downgrade 0135`**：会 DROP 5 张表（回滚口径 = 停 Worker + 关 flag）。
   **也不要回滚到旧镜像**：0135 执行后 DB 版本是 `0135`，旧镜像的 head 是 `0134`
   ⇒ 启动期的 schema 漂移门判 **blocking** ⇒ backend 起不来
   （`backend/app/infrastructure/schema_drift.py:113-115`）。
   ⇒ 回滚 = **新镜像 + 关 flag**（见 §5）。
8. **菜单入口 ≠ 功能上线**：菜单是显式播种的，路由受 flag 控制 ——
   两边都要做，缺一个就是「看得见点不动」或「能调但没入口」。
9. **`docker compose restart` 不重读 `env_file`**：它只把既有容器停掉再起，
   容器里的环境变量还是**创建时**烘进去的那份 ⇒ 改完 `.env` 的开关不会生效。
   改 env 文件后的正确动作是 **`up -d --force-recreate --no-build <service>`**
   （沿用本手册统一的 `-f docker-compose.yml -f docker-compose.centos.yml` 参数），
   然后**只读回那一个非敏感开关**确认：
   `docker compose … exec <service> printenv <单个非敏感开关变量>`
   —— 只读这一个变量，不要 dump 整个环境（见 §1 约束 7）。
10. **重建容器 ≠ 重建镜像**：`--force-recreate` 只重新创建容器，
    加上 `--no-build` 后**绝不触发镜像构建**（离线机上一次构建尝试会直接失败
    并可能把 backend 带下去）。镜像的部署是**更早的阶段 2** 的事；
    阶段 2 已经把镜像（含 Worker 镜像）放到目标机，重建阶段只复用它。
11. **Worker 启动同理必须带 `--no-build`**：Worker 服务有 `build:` 且没有 `image:` 键，
    compose 派生名与 backend 的**不同** ⇒ 启动前镜像必须已按**阶段 2** 就位
    （名字以目标机 `config --images` 为准）。带 `--no-build` 后，镜像缺失会**明确报错**
    而不是隐式构建一个来自当前 build context 的镜像。
12. **「把工具启用一下」不是无副作用的**：现象 —— 阶段 0 顺手把
    `wiki_search` / `wiki_read` / `rule_evaluate` / `coverage_status` 的 `enabled`
    翻成 `true` 以便编排绑定，结果**4 个既有 legacy Agent**（`WIKI_SEARCH_AGENT` id 6 /
    `WIKI_READ_AGENT` id 7 / `WIKI_RULE_AGENT` id 8 / `WIKI_COVERAGE_AGENT` id 9，
    全部 `status = active`）的行为变了。为什么 —— legacy 运行时经
    `AgentToolConfigRegistry` 解析绑定工具，它**只装载 `enabled = true` 的行**
    （`backend/app/services/agent_tool_config_registry.py:33` warmUp、`:79` reload_one），
    工具缺失/禁用时抛 `ConflictError(409 tool_unbound_or_disabled)`
    （`backend/app/services/agent_runtime_service.py:165-178`）⇒ 这 4 个 Agent
    **从「409 不可运行」变成「真的执行」**，是既有功能的行为变更。
    **只改 `input_schema` 是无害的**（`AgentToolAssembly.assemble` 只把它拷进描述符，
    legacy 入参来自 `arg_extractor`，编排模块之外没有生产代码读它）——
    **⇒ `enabled` 是唯一影响行为的开关**。
    怎么办 —— **第一波只补 schema**，只绑定本来就已启用的
    `supplier_360` / `supplier_risk` / `graph_traverse`；4 个 wiki 工具留到
    **后续波次**，单独评估 + 单独审批（§2.5）。
    ⚠️ **前提校正**：这 4 个工具的 `enabled = false` 是**那台库的历史行状态**，
    **不是**本仓 seed 的产物（本仓 seed 把 7 个工具全写成 `enabled = True`，
    见 §2.1 / §2.4）⇒ 在由本仓 seed 造出来的环境里，它们**本来就是启用的**、
    这 4 个 Agent **本来就是可运行的**，上面的行为变更**不会发生**。

---

## 10. 上线检查清单

- [ ] 阶段 0（**第一波：只补 schema，不翻 `enabled`**）：`supplier_360` / `supplier_risk` /
      `graph_traverse` 的 `input_schema` 合法；它们的 `enabled` **未被改动**；
      4 个 wiki 工具的 `enabled` **按 §2.4 的分支判定**（实测 `false` ⇒ 保持 `false`；
      实测 `true` ⇒ 保持 `true` 并在验收记录里写明「它们本来就是启用的」）；
      `tool-options` 能看到那三个工具（优先在 `/admin/tools` 页面改；SQL 只是兜底路径）
      —— 详见 §2.4 / §2.5
- [ ] 阶段 3.5：PRE-ENABLE CHECKLIST **14 项**（P1 – P14）**已逐项关闭**
      （P12 – P14 已由本次修复派单关闭；P1 – P11 必须各自有真实关闭动作 ——
      **登记 ≠ 已处置**；缺一 ⇒ 保持 flag = `false`）
- [ ] 阶段 1：已备份；已**记录**迁移前的 `alembic current`（旧镜像 ⇒ `0134`）；
      5 张表尚未建（迁移在阶段 2）
- [ ] 阶段 2：新镜像已就位；新 backend 容器启动后 `alembic current = 0135`；
      5 张表在；既有表行数未变
- [ ] 阶段 2：Worker 镜像已在目标机就位（名字与 `config --images` 一致）
- [ ] 阶段 3：新路由 **404**；`docker compose … ps` 无 Worker；旧契约不变
- [ ] 阶段 4：backend 是 **重建**（`--force-recreate --no-build`）的，
      `printenv` 读回开关 = `true`；
      flag 打开后路由变 **401**（= 已注册）；Worker 日志出现「worker 启动」；
      **重建后已复核 `input_schema`**（三个目标工具仍形如 §2.3，不是 `{}`）
- [ ] 阶段 5：profile 可创建/启用；run 走完 `QUEUED→RUNNING→SUCCEEDED`；token 有计量
- [ ] 阶段 6：目标环境的 frontend 镜像**已包含**本分支前端改动
      （本仓 `frontend/src/App.tsx:115-116` 已无条件注册这两条路由）；
      菜单脚本输出 `2 rows inserted`（首次）；SQL 查到两行挂在 `section.aiAgent`
- [ ] 阶段 7：浏览器两项菜单可见可打开，无 500
- [ ] 阶段 8：生产启用**已获审批**；观察期指标已安排
- [ ] 回滚路径已演练：停 Worker（先排空租约）→ **新镜像 + flag 关** → 新路由回到 404；
      表保留（**不回滚镜像**、**不 downgrade**）
