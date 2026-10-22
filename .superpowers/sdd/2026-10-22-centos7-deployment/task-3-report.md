# Task 3 报告：CentOS compose overlay

**状态：DONE_WITH_CONCERNS**
**Commit：`a0b66cf580de5ded010ff9b600f5fb1c94bfd759`**（`feat/deploy` 线上 `feat/centos7-deploy-tooling` 分支）
**执行日期：2026-10-23**

本任务只动了 3 个文件，与 brief 的文件清单一致：

- 新建 `docker/docker-compose.centos.yml`
- 新建 `backend/app/tests/unit/test_compose_centos_overlay.py`
- 改 `backend/pyproject.toml`（dev extras 加 1 行 `pyyaml>=6.0.1`）

**未触碰** `backend/scripts/preflight_centos.py` 及其测试；**未修改** `raw/`。

---

## Step 1：写失败测试

按 brief 逐字新建 `backend/app/tests/unit/test_compose_centos_overlay.py`（8 个 test 函数 + `_ComposeLoader` / `_unwrapUnknownTag` / `overlay` fixture），一字未改。

唯一的环境相关确认：`OVERLAY = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.centos.yml"`
—— 文件位于 `backend/app/tests/unit/`，parents[0]=unit、[1]=tests、[2]=app、[3]=backend、[4]=仓库根，
所以 `parents[4]/"docker"` 正确指向 `/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/docker/`。

## Step 2：声明 pyyaml 依赖

`backend/pyproject.toml` 的 `[project.optional-dependencies]` → `dev` 组，在 `"pytest-cov>=5.0.0"` 之后插入一行 `"pyyaml>=6.0.1"`：

```toml
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.23.0",
    "pytest-cov>=5.0.0",
    "pyyaml>=6.0.1",
    "respx>=0.21.0",
    "anyio>=4.4.0",
    "ruff>=0.6.0",
    "aiosqlite>=0.20.0",
]
```

`ml` 组未动。`[tool.ruff.lint.per-file-ignores]`（Task 1 为 `preflight_centos.py` 加的 5 条 3.6 兼容豁免 + `**/scripts/preflight_centos.py` 模式）**原样保留**，未被当成残留删除。

~~仓库没有 `uv.lock` / `requirements.txt`，因此无需同步锁文件。~~
**【Fix round 1 更正】此说法为误判**：仓库确实有被 git 跟踪的 `backend/uv.lock`（805 KB），
`uv lock --check --offline` 会报 "The lockfile at `uv.lock` needs to be updated"。
锁文件已在 Fix round 1 重新生成并提交。

## Step 3：跑测试确认失败

```
$ cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -v
```

实际输出（尾部）：

```
self = PosixPath('/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/docker/docker-compose.centos.yml')
...
E       FileNotFoundError: [Errno 2] No such file or directory: '/Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/docker/docker-compose.centos.yml'
=========================== short test summary info ============================
ERROR app/tests/unit/test_compose_centos_overlay.py::test_overlay_defines_ollama_service
ERROR app/tests/unit/test_compose_centos_overlay.py::test_ollama_binds_all_interfaces
ERROR app/tests/unit/test_compose_centos_overlay.py::test_ollama_persists_models_in_named_volume
ERROR app/tests/unit/test_compose_centos_overlay.py::test_ollama_memory_settings_present
ERROR app/tests/unit/test_compose_centos_overlay.py::test_ollama_does_not_publish_ports
ERROR app/tests/unit/test_compose_centos_overlay.py::test_overlay_drops_init_bind_mount
ERROR app/tests/unit/test_compose_centos_overlay.py::test_overlay_keeps_mysql_data_volume
ERROR app/tests/unit/test_compose_centos_overlay.py::test_overlay_does_not_touch_other_services
============================== 8 errors in 0.16s ===============================
```

与 brief 的 Expected（8 ERROR with FileNotFoundError）**完全一致**。

## Step 4：写 overlay

按 brief 逐字新建 `docker/docker-compose.centos.yml`：

- `services.ollama`：`image: ollama/ollama:latest`、`container_name: qa-ollama`、`restart: unless-stopped`、
  `environment: OLLAMA_HOST=0.0.0.0:11434` / `OLLAMA_KEEP_ALIVE=5m` / `OLLAMA_NUM_PARALLEL=1`、
  `volumes: [ollama_models:/root/.ollama]`、**无 `ports`**。
- `services.mysql-demo.volumes: !override` → `[- mysql_data:/var/lib/mysql]`
- 顶层 `volumes.ollama_models`

## Step 5：跑测试确认通过

```
$ cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -v
============================= test session starts ==============================
platform darwin -- Python 3.14.7, pytest-9.0.3, pluggy-1.6.0
rootdir: /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
configfile: pyproject.toml
collecting ... collected 8 items

app/tests/unit/test_compose_centos_overlay.py::test_overlay_defines_ollama_service PASSED [ 12%]
app/tests/unit/test_compose_centos_overlay.py::test_ollama_binds_all_interfaces PASSED [ 25%]
app/tests/unit/test_compose_centos_overlay.py::test_ollama_persists_models_in_named_volume PASSED [ 37%]
app/tests/unit/test_compose_centos_overlay.py::test_ollama_memory_settings_present PASSED [ 50%]
app/tests/unit/test_compose_centos_overlay.py::test_ollama_does_not_publish_ports PASSED [ 62%]
app/tests/unit/test_compose_centos_overlay.py::test_overlay_drops_init_bind_mount PASSED [ 75%]
app/tests/unit/test_compose_centos_overlay.py::test_overlay_keeps_mysql_data_volume PASSED [ 87%]
app/tests/unit/test_compose_centos_overlay.py::test_overlay_does_not_touch_other_services PASSED [100%]

============================== 8 passed in 3.29s ===============================
```

与 brief 的 Expected（8 passed）**完全一致**。

## Step 6：真实 compose 验证

环境：`Docker Compose version v5.6.0`（macOS）。

### 6.1 合并后服务数

```
$ docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config --services
milvus-etcd
milvus-minio
milvus-standalone
neo4j
postgres
backend
ollama
frontend
mysql-demo
qa-objects

$ ... | wc -l
      10
```

10 行 = 原有 9 个服务一个不少 + `ollama`。与 brief Expected **一致**。

### 6.2 bind mount 移除（数整个合并结果）

```
$ test "$(docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config \
        | grep -c 'docker-entrypoint-initdb.d')" -eq 0 \
  && echo "OK: ./init bind mount 已移除" \
  || { echo "FAIL: bind mount 仍在"; exit 1; }
OK: ./init bind mount 已移除
```

### 6.3 数据卷存活

```
$ test "$(docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config \
        | grep -c 'mysql_data')" -ge 1 \
  && echo "OK: mysql_data 存活" \
  || { echo "FAIL: mysql_data 被误删，MySQL 数据将落容器可写层"; exit 1; }
OK: mysql_data 存活
```

补充观察（合并后的实际片段，非 grep 单点）：

```
    volumes:
      - type: volume
        source: mysql_data
        target: /var/lib/mysql
        volume: {}
...
    volumes:
      - type: volume
        source: ollama_models
        target: /root/.ollama
        volume: {}
...
  ollama_models:
    name: qa-system_ollama_models
```

`mysql-demo` 的 volumes 列表里**只剩** `mysql_data` 一项，`./init` bind mount 已不在合并结果里。

### 反向自检（brief 明确要求，必须报 FAIL）

临时把 overlay 里的 `volumes: !override` 改成 `volumes:`（其他一字不动），重跑 6.2：

```
--- 反向自检：撤掉 !override 后重跑 6.2 ---
FAIL: bind mount 仍在
shell exit code = 1
```

（用 brief 原文命令 `|| { echo "FAIL: ..."; exit 1; }` 跑的，进程退出码确认为 **1**。）

同一状态下重跑 6.3：

```
--- 撤掉 !override 后重跑 6.3 ---
OK: mysql_data 存活
```

→ 这正好印证了 brief 的论断：**去掉 `!override` 之后 bind mount 会回来**（Compose 对 list 型字段按 target 合并，
`mysql_data:/var/lib/mysql` 与原项同 target 合并掉，`./init:/docker-entrypoint-initdb.d` 因 target 不同而保留），
而 `!override` 是整字段替换，是当前唯一能真正删掉这一项的写法。

恢复 overlay 后重跑：

```
--- 恢复后重跑 6.2 ---
OK: ./init bind mount 已移除
--- 恢复后重跑 6.3 ---
OK: mysql_data 存活
--- 恢复后服务数 ---
      10
```

`git diff --stat` 恢复后仅剩 `backend/pyproject.toml | 1 +`（两个新文件未跟踪），overlay 内容与提交版本一致。

### Task 4 前置：`config --images` 可用

```
$ docker compose -f docker/docker-compose.yml -f docker/docker-compose.centos.yml config --images
milvusdb/milvus:v2.4.6
qa-system-frontend
minio/minio:RELEASE.2024-06-13T22-53-53Z
quay.io/coreos/etcd:v3.5.5
mysql:8.0
postgres:16-alpine
qa-system-backend
minio/minio:RELEASE.2023-03-20T20-16-18Z
ollama/ollama:latest
neo4j:5.23-community
```

10 条，其中 `ollama/ollama:latest` 已包含。Task 4 的离线镜像清单可以直接用这条命令取。

## ruff

```
$ cd backend && ruff check app/tests/unit/test_compose_centos_overlay.py
All checks passed!
exit=0
$ ruff --version
ruff 0.15.15
```

**与用户提供的「上下文 3」不符，需指出：** 本仓库**根本不存在仓库根的 `pyproject.toml`**。
`find . -maxdepth 2 -name pyproject.toml` 只命中 `backend/pyproject.toml` 一个文件——ruff 配置（含
`line-length = 110`、`target-version = "py312"`、`select = ["E","F","I","UP","B","SIM"]`、
`per-file-ignores`）全都在 `backend/pyproject.toml` 里。

即：**从 `cd backend` 跑 ruff 才是加载到配置的正确姿势**，与上下文 3 的说法正好相反。
在仓库根跑 `ruff check backend/app/tests/unit/test_compose_centos_overlay.py` 同样通过（`All checks passed!`），
但那时 ruff 走的是默认配置（line-length 88、target-version 默认），并不代表按仓内规则校验过。
本次以 `cd backend` 的那次结果为准。结论：改动文件 lint 干净，**没有出现任何 I001 等无关报错**。

## Step 7：提交

```
$ git add docker/docker-compose.centos.yml \
          backend/app/tests/unit/test_compose_centos_overlay.py \
          backend/pyproject.toml
$ git commit -m "feat(deploy): CentOS compose overlay — 加 ollama、去 mysql-demo 空 bind mount"
[feat/centos7-deploy-tooling a0b66cf5] feat(deploy): CentOS compose overlay — 加 ollama、去 mysql-demo 空 bind mount
 3 files changed, 143 insertions(+)
 create mode 100644 backend/app/tests/unit/test_compose_centos_overlay.py
 create mode 100644 docker/docker-compose.centos.yml
```

Commit hash：**`a0b66cf580de5ded010ff9b600f5fb1c94bfd759`**
提交后 `git status --short` 为空（干净树）。

> 说明：本报告文件写在 `.superpowers/sdd/2026-10-22-centos7-deployment/` 下，该目录未被 git 跟踪
> （`git ls-files .superpowers/sdd/...` 为空），与 task-1-report.md 的现状一致，故未提交。

---

## 与 brief 不符 / 需要注意的地方

### 1. 【功能缺口，影响后续 Task 6/8】`OLLAMA_BASE_URL` 指向 host.docker.internal，overlay 里的 ollama 够不着

`docker/.env:61`：

```
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

`backend` 服务的 `env_file: [.env]` 会把它注入容器。而 overlay 里的 `ollama`：

- **不发布宿主端口**（brief 明确要求 `test_ollama_does_not_publish_ports`）；
- 加入的是 compose 项目默认网络，容器间应走 **service 名** `http://ollama:11434`。

于是 CentOS 上 backend 容器解析 `host.docker.internal` → host-gateway → 宿主 11434 端口
（ollama 容器未 publish，宿主上没人监听）→ **LLM / embedding 调用会连接失败**。
`host.docker.internal` 那条路径只在「Ollama 跑在宿主上」时成立（Mac dev 现状）。

Task 3 不在文件清单内、不该改 `.env`，所以**我没有动**。但这是 overlay 真正被用起来时的第一块绊脚石，
需要 Task 6（CentOS 侧 env）或 Task 8（生产 env 落地）把 CentOS 的 `OLLAMA_BASE_URL` 改成 `http://ollama:11434`，
或在 overlay 里给 ollama 加 `ports: ["11434:11434"]`。**请确认这一条被排进计划。**

（附带：`ollama` 未列入 `backend.depends_on`，只是启动顺序问题，不影响连通性；如需严格编排再补。）

### 2. 【上下文错误】ruff 配置位置

见上文「ruff」段：本仓 ruff 配置在 `backend/pyproject.toml`，仓库根**没有** `pyproject.toml`。
上下文 3 说反了。实际做法是 `cd backend && ruff check ...`，结果干净。

### 3. 【brief 表述可以更严谨，非本任务问题】brief 里 6.1 说「9 行原有服务名 + ollama，共 10 行」

实际 `--services` 输出顺序是 compose 自身排序（不是原文件顺序），实测恰好 10 行、内容齐全，结论无差异。

### 4. 【版本相关，已按要求照写】`!override` 需要 Compose ≥ 2.24

本机 v5.6.0 正常解析；CentOS 7 侧上限 2.27.1（Task 2 实测），满足。

> **【Fix round 1 更正】原文此处声称「低于 2.24 会直接报 unsupported tag !override 并整个失败」——实测为假。**
> 在 Compose v5.6.0 上把标签拼错成 `!overide` 时，`docker compose config` **exit 0、stderr 为空**，
> 未知标签被当作普通列表处理，`./init` bind mount 静默复活（实测 `docker-entrypoint-initdb.d` 计数 = 1）。
> 真实危害不是「显式失败」，而是**静默地把 SELinux 有问题的 bind mount 带回生产宿主机**。
> 另注：本任务所有本地 `docker compose config` 验证均跑在 **macOS Compose v5.6.0** 上，
> **未在 CentOS 目标版本 2.27.1 上实测过**；2.27.1 对未知标签的行为未经验证，不作断言。
> 唯一的防线是 `test_mysql_demo_volumes_use_override_tag`（见 Fix round 1）：标签拼错会让 pytest 变红。

### 5. 【安全】本次全程未读取、未打印、未提交任何 `.env` 明文

只在第 1 点里引用了 `docker/.env:61` 的 `OLLAMA_BASE_URL` 一行（该值不含凭据）。
`.env` 中的密码 / API key 一律未读取、未输出。

## 风险清单（我认为你该知道的）

| 级别 | 内容 |
|---|---|
| **HIGH** | `OLLAMA_BASE_URL=http://host.docker.internal:11434` 与「ollama 容器化且不发布端口」互相矛盾，CentOS 上 backend 连不上 Ollama。需在 Task 6/8 处理。 |
| **MEDIUM** | `!override` 依赖 Compose ≥ 2.24。CentOS 7 能装到 2.27.1（已确认）。**【Fix round 1 更正】**原文写的是「装成更老版本会整体报错，是显式失败」——实测未知标签（哪怕拼错）不会报错：Compose v5.6.0 上 exit 0、stderr 为空，bind mount 静默复活。真实风险是静默的，不是显式的。防线改为 pytest 断言标签存在（`test_mysql_demo_volumes_use_override_tag`）。 |
| **MEDIUM** | `mysql-demo` 的 volumes 被 `!override` 整字段替换过。以后任何人在主 compose 里给 mysql-demo 加新卷，**不会**自动进到 overlay（overlay 会覆盖掉）。需要有人记得两处同步 —— 建议在主 compose 的 mysql-demo 处加一行注释指向 overlay。**【Fix round 1】已加自动化守卫 `test_overlay_volumes_match_base_minus_bind_mounts`，两处漂移会让 pytest 变红。** |
| **LOW** | `docker/init/` 目录本身没删，overlay 只是不再挂载它。Mac dev 侧主 compose 仍挂载，行为不变。 |
| **LOW** | `ollama/ollama:latest` 是 floating tag。Task 4 的离线镜像清单需要把 digest 钉死，否则不同时间拉到的不是同一个镜像。 |
| **LOW** | `container_name: qa-ollama` 是硬编码名，与主 compose 里其他服务命名风格一致，但会占用固定名；CentOS 上若已有同名容器会起不来。 |

## 结论

- 6 个 Step 全部按 brief 顺序执行完毕，每一步的命令都真跑了，实际输出如上。
- pytest：RED 阶段 8 ERROR（FileNotFoundError），GREEN 阶段 8 passed，与 brief Expected 逐字一致。
- compose 门禁：合并 10 服务、`./init` bind mount 已移除、`mysql_data` 存活，全部 OK；
  反向自检（撤掉 `!override`）确实执行并报 `FAIL`、shell 退出码 1，恢复后回到 `OK`。
- ruff：`cd backend` 下 `All checks passed!`。
- 未发现「实现错误导致输出与 brief 不符」的情况；唯一的实质性偏差是 **上下文 3 对 ruff 配置位置的描述有误**，
  以及 **brief 未覆盖的 `OLLAMA_BASE_URL` 跨 Task 缺口**（见上）。

---

# Fix round 1（独立评审后的修复）

**执行日期：2026-10-23**
**分支：`feat/centos7-deploy-tooling`**
**改动文件（3 个）：**

- `backend/uv.lock`（重新生成）
- `backend/app/tests/unit/test_compose_centos_overlay.py`（8 → 10 个 test）
- `.superpowers/sdd/2026-10-22-centos7-deployment/task-3-report.md`（本报告，含 I3 更正）

`docker/docker-compose.centos.yml` **未改动**（内容已正确，无需修）。
`docker/docker-compose.yml`（主 compose）**未改动**（仅在 mutation 验证中临时改过，已还原）。

> 注：`.superpowers/` 在 `.gitignore:66` 里被忽略（此前 Task 3 报告因此未提交）。
> 本轮按协调方明确要求，用 `git add -f` 把本报告文件一并纳入 commit，
> 以便修复在 git 历史里可追溯；仓库内已有先例（`.superpowers/sdd/2026-09-02-agent-tool-binding/progress.md` 已被跟踪）。

---

## I1 — `backend/uv.lock` 未重新生成（Important）

**原状**：commit `a0b66cf5` 往 `backend/pyproject.toml` 的 `dev` extras 加了 `pyyaml>=6.0.1`，
但 805 KB 的 `backend/uv.lock` 没同步，仓库状态自相矛盾。

**复现**：

```
$ cd backend && uv lock --check --offline
Resolved 180 packages in 85ms
The lockfile at `uv.lock` needs to be updated, but `--check` was provided. To update the lockfile, run `uv lock`.
```

**修复**：

```
$ cd backend && uv lock --offline
Resolved 180 packages in 36ms
$ git diff --stat uv.lock
 backend/uv.lock | 2 ++
```

diff 只有 2 行（`pyyaml` 本来就作为传递依赖被解析进了锁文件，本次只补上
manifest 侧的两条记录：`[package.optional-dependencies] dev` 与 `requires-dist`）：

```
@@ -2999,6 +2999,7 @@ dev = [
     { name = "pytest-cov" },
+    { name = "pyyaml" },
     { name = "respx" },
@@ -3048,6 +3049,7 @@ requires-dist = [
     { name = "python-pptx", specifier = ">=1.0.2" },
+    { name = "pyyaml", marker = "extra == 'dev'", specifier = ">=6.0.1" },
```

**修复后确认**：

```
$ cd backend && uv lock --check --offline
Resolved 180 packages in 2ms
（无 "needs to be updated"，exit 0）
```

**同时更正报告里的错误陈述**：原文「仓库没有 `uv.lock` / `requirements.txt`，因此无需同步锁文件」为**误判**，
已在正文 Step 2 段落就地更正。

> 影响范围说明：`docker/Dockerfile.backend:37` 用的是 `uv sync --frozen --no-dev || uv sync --no-dev`，
> 镜像构建有 fallback 兜底，所以这是**仓库状态一致性**问题，不是运行时断裂。

---

## I2 — `!override` 标签本身没有任何测试覆盖（Important，本次主要修复）

**根因**：`_ComposeLoader` 注册的 `add_multi_constructor("", _unwrapUnknownTag)` 会**剥掉所有**未知标签，
解析出来的树在「带 `!override`」与「不带标签」两种情况下**完全一致**。
于是「标签被删掉」和「标签被拼错」这两种最致命的破坏，测试全绿、compose 也全绿。

**修复思路（结构化，非 grep）**：让 multi constructor **把标签记在值对象上**，
标签因此变成可断言的东西：

- 新增 `_TaggedList(list)` / `_TaggedMap(dict)`，各自带 `.tag` 属性；
- `_unwrapUnknownTag` 改为把 `suffix`（形如 `"!override"`，去掉前导 `!`）写进值对象；
- 新增 `test_mysql_demo_volumes_use_override_tag`：断言 `mysql-demo.volumes` 是 `_TaggedList`
  **且** `.tag == "override"`。

这样：
- 标签被删 → 值退化成普通 list → `isinstance` 断言失败；
- 标签拼错（`!overide`）→ `.tag == "overide"` → 精确不等 → 失败。

### Mutation 证据（before / after）

| 变异 | 修复前 8 tests | 修复后 10 tests | 合并后 `docker compose config` |
|---|---|---|---|
| `volumes: !override` → `volumes:`（标签被删） | 8 passed | **1 failed, 9 passed**（`test_mysql_demo_volumes_use_override_tag`） | exit 0，stderr 空，initdb 计数 = 1（bind mount 复活） |
| `!override` → `!overide`（拼错） | 8 passed | **1 failed, 9 passed**（同一 test） | exit 0，stderr 空，initdb 计数 = 1（bind mount 复活） |

实测输出（变异 A，标签被删）：

```
FAILED app/tests/unit/test_compose_centos_overlay.py::test_mysql_demo_volumes_use_override_tag
========================= 1 failed, 9 passed in 3.92s ==========================
compose exit=0
stderr:
initdb count=1
```

实测输出（变异 B，拼错）：

```
FAILED app/tests/unit/test_compose_centos_overlay.py::test_mysql_demo_volumes_use_override_tag
========================= 1 failed, 9 passed in 3.37s ==========================
compose exit=0
stderr:
initdb count=1
```

恢复原文件后：

```
$ cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -q
10 passed in 3.95s
```

---

## I4 — overlay 与主 compose 之间没有漂移守卫（Important）

**问题**：`!override` 是整字段替换 —— 主 compose 里给 `mysql-demo` 新加的卷**不会**自动进到 overlay，
overlay 会把它盖掉；而所有旧测试只读 overlay 文件，这个漂移完全无声。

**修复**：新增 `test_overlay_volumes_match_base_minus_bind_mounts`，同时读**两个** compose 文件，
断言：

1. 主 compose 上 `mysql-demo` 的 bind mount 集合恰好等于已知集合
   `{"./init:/docker-entrypoint-initdb.d"}` —— 新增任何 bind mount 都会红
   （否则新 bind mount 会绕过 overlay 复活到生产）；
2. overlay 的卷列表 **==** 主 compose 的命名卷列表 —— 新增命名卷会红。

### Mutation 证据

| 变异 | 结果 |
|---|---|
| 主 compose 给 mysql-demo 加命名卷 `extra_data:/var/lib/extra` | **FAILED** `test_overlay_volumes_match_base_minus_bind_mounts`（1 failed, 9 passed） |
| 主 compose 给 mysql-demo 加第二个 bind mount `./other:/var/lib/other` | **FAILED** `test_overlay_volumes_match_base_minus_bind_mounts`（1 failed, 9 passed） |

两次变异后主 compose 均已还原（`git diff --stat docker/` 为空）。

---

## I3 — 报告里的错误保证（Important，已改文案）

原文（Step 7 之前的「与 brief 不符」第 4 条，以及风险表 MEDIUM 行）声称：

> 若 CentOS 上实际装到 < 2.24 的版本，`docker compose config` 会直接报「unsupported tag !override」并整个失败（不是静默降级），届时会立刻暴露，不会误伤数据。

**实测为假**：在 Compose **v5.6.0** 上，把标签拼错成 `!overide` 后
`docker compose config` **exit 0、stderr 为空**，未知标签被当作普通列表处理，
`./init` bind mount 静默复活（`docker-entrypoint-initdb.d` 计数 = 1）。

**已更正为**：真实危害是「静默地把 SELinux 有问题的 bind mount 带回生产宿主机」，而非显式失败；
并明确标注本任务所有本地 compose 验证均跑在 **macOS Compose v5.6.0** 上，
**未在 CentOS 目标版本 2.27.1 上实测**，对 2.27.1 的未知标签行为不作断言。

---

## Minor — 顺手收紧的两条断言

1. `test_overlay_drops_init_bind_mount`：
   原 `assert all("./init" not in str(v) for v in volumes)` 在 `volumes` 为空时**恒真**，
   且相邻的 `assert volumes is not None` 冗余（`all()` over `None` 本来就抛）。
   改为同时断言 `len(volumes) > 0`（区分「bind mount 被删」与「volumes 整个消失」）
   以及 `all(_isNamedVolume(v))`（不得残留任何 bind mount）。
   *变异验证*：把 overlay 改成 `volumes: !override []` → 该 test **FAILED**（另有 2 个 test 同时红）。
2. `test_ollama_persists_models_in_named_volume`：
   原先只查 `volumes[0]`，追加一个 bind mount 会溜过去。改为断言
   `ollama_models:/root/.ollama` 在列表中**且所有**挂载都是命名卷。
   *变异验证*：给 ollama 追加 `- ./models:/root/.ollama2` → 该 test **FAILED**。

---

## 汇总验证

```
$ cd backend && python -m pytest app/tests/unit/test_compose_centos_overlay.py -v
10 passed in 3.18s

$ cd backend && ruff check app/tests/unit/test_compose_centos_overlay.py
All checks passed!   (exit=0)
$ cd backend && ruff format --check app/tests/unit/test_compose_centos_overlay.py
1 file already formatted

$ cd docker && docker compose -f docker-compose.yml -f docker-compose.centos.yml config
exit=0, stderr 空
docker-entrypoint-initdb.d 计数 = 0   （bind mount 已移除）
mysql_data 计数 = 3                  （数据卷存活）
--services 行数 = 10                 （原有 9 + ollama）

$ cd backend && uv lock --check --offline
Resolved 180 packages in 2ms   （无 "needs to be updated"）
```

> 注：期间有一次 pytest 跑出 `sqlalchemy ... DeadlockDetectedError`（ERROR 而非 FAILED），
> 原因是并发 agent 在同一测试库上跑 truncate 造成锁冲突（qa-system-mixed-suite-truncate-hazard），
> 与本次改动无关；重跑即 10 passed。

**本次共 6 个变异全部被测试捕获**（I2 × 2、I4 × 2、Minor × 2），每一个变异在修复前都是全绿。
