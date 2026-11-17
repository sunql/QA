# 生产止血 runbook：抬 Milvus 集合级 flush 配额 + 补回丢失的向量

> **适用**：2026-11-17 生产报错
> `Milvus embedding 同步失败 ... rate limit exceeded[rate=0.1]`。
> **根因与证据**见同目录 `summary.md`。
> **执行人**：生产宿主机（ai-server）上的**用户**；本文件所有命令均在生产宿主机执行。
> **零 DDL、零库表改动**：只改 Milvus 容器配置 + 调一次向量对账端点。

---

## 0. 一句话结论

Milvus 出厂把**每个集合的 flush 频率限死在 0.1 qps（每 10 秒 1 次）**，
而本系统的本体向量写路径是「删一次 + 插一次 = 同一集合 2 次 flush」。
批量导入时 N 个属性并发同步 ⇒ 2N 次 flush 抢一个「10 秒 1 次」的桶 ⇒
pymilvus 重试 75 次（约 210 秒）耗尽 ⇒ **该属性的向量同步硬失败**（CRUD 仍返回 200，
用户无感；旧向量还在不在取决于 delete 的落地与可见性，单看这条日志判不出来 ⇒
§5 的对账补齐同时就是**真实影响面的测量**）。

**立即止血两步**：
1. **抬配额**（本文件 §2–§4）：把 `flushRate.collection.max` 从 `0.1` 改成 `5` ⇒
   拒绝消失，flush 回到毫秒级（仓库里「flush 要 10-25s」的延迟税同时消失）。
2. **补回向量**（§5）：跑一次「补同步缺失向量」⇒ 把被删没写回的属性向量补回来。

---

## 1. Phase 0 — 取证（只读，先跑这三条再动手）

> 生产 compose 入口**以 `docker inspect` 的 label 为准**（不要凭目录名猜）：
> 生产是 **v1 `docker-compose 1.29.2`**（**没装 v2 插件** ⇒ 只能用 `docker-compose` 连字符命令），
> 单文件 `docker-compose.centos7.yml`；**不要加 `--remove-orphans`**。

```bash
# 1.1 权威确定 compose 文件与项目
docker inspect qa-milvus --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
docker inspect qa-milvus --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'

# 1.2 现有挂载（确认没挂过配置覆盖）
docker inspect qa-milvus --format '{{json .Mounts}}'

# 1.3 当前配额（应当看到 collection.max: 0.1）
docker exec qa-milvus sh -c 'grep -A6 "flushRate" /milvus/configs/milvus.yaml'

# 1.4 是否已存在 user.yaml（**存在就先把内容贴出来，别覆盖**）
docker exec qa-milvus sh -c 'ls -l /milvus/configs/user.yaml 2>&1; cat /milvus/configs/user.yaml 2>&1'
```

**期望**：
- 1.1 ⇒ `/opt/qa-system/docker/docker-compose.centos7.yml`（若不是这个文件，**停下**，把输出贴回来）；
- 1.3 ⇒ `collection: max: 0.1`；
- 1.4 ⇒ `No such file or directory`（即还没有 user.yaml）。

### 1.5 量化影响面（只读）

```bash
docker logs qa-backend --since 24h 2>&1 | grep -c "rate limit exceeded"
docker logs qa-backend --since 24h 2>&1 | grep "属性向量自动同步失败" | grep -o "id=[0-9]*" | sort -u | wc -l
docker logs qa-backend --since 24h 2>&1 | grep "属性向量自动同步失败" | grep -o "id=[0-9]*" | sort -u | head -30
```

- 第 1 条 = 被限流拒绝的总次数；
- 第 2 条 = **受影响（向量可能已丢）的属性个数** ⇒ §5 的补向量工作量；
- 第 3 条 = 受影响的 id 清单（贴回来留档）。

> 同时也看一眼 Milvus 侧有没有对应的拒绝记录：
> ```bash
> docker logs qa-milvus --since 24h 2>&1 | grep -i -E "rate limit|RateLimiter" | tail -20
> ```

---

## 2. Phase 1 — 写 `user.yaml`（只新增文件，不动 milvus.yaml）

Milvus 2.3+ 支持 `configs/user.yaml` **叠加**在 `milvus.yaml` 之上（优先级更高），
且官方推荐的 Docker 方式就是挂载这个文件 ⇒ **不要去改镜像里的 `milvus.yaml`**。

```bash
cd /opt/qa-system/docker      # 以 1.1 输出的目录为准
cat > milvus-user.yaml <<'EOF'
# 2026-11-17：抬集合级 flush 配额。
# 出厂值 0.1 qps ⇒ 每 10 秒只放行 1 次 flush；本体向量写路径「删+插」= 同集合 2 次 flush
# ⇒ 批量写必然被 grpc RateLimiter 拒绝（rate limit exceeded[rate=0.1]）。
# 5 qps 对本系统的写入量（百量级实体/天）足够，且远低于 compaction 压力阈值。
quotaAndLimits:
  flushRate:
    enabled: true
    collection:
      max: 5
EOF
cat milvus-user.yaml
```

---

## 3. Phase 2 — compose 挂载 + 重建 Milvus 容器

### 3.1 备份 compose 文件

```bash
cd /opt/qa-system/docker
cp -a docker-compose.centos7.yml docker-compose.centos7.yml.bak.20261117
ls -l docker-compose.centos7.yml*
```

### 3.2 在 milvus 服务的 `volumes` 里加一行

先看现状：

```bash
grep -n -A30 "^  milvus" docker-compose.centos7.yml
```

在 **`milvus` 服务**的 `volumes:` 下**追加**（不要替换、不要动其它服务的 volumes）：

```yaml
      - /opt/qa-system/docker/milvus-user.yaml:/milvus/configs/user.yaml:ro
```

> ⚠️ **服务名与 `container_name` 以 3.2 第一条命令的输出为准**。**只改 Milvus 这一个服务的 volumes**。
> 2026-11-17 生产实测：服务名 **`milvus-standalone`**、`container_name` **`qa-milvus`**
> ⇒ 命令末尾必须写 `milvus-standalone`（写 `milvus` 会 `ERROR: No such service`，不产生任何变更）。
> 同一文件里还有 `milvus-etcd` / `milvus-minio` 两个服务块，`grep "^  milvus"` 会全部命中，
> 别改错块。
> ⚠️ 不要用 `sed -i` 盲改（可能改到别的服务的 volumes）；先看清缩进再手改，改完 diff 一遍。

### 3.3 校验 + 重建（**只重建 Milvus 一个容器**）

```bash
cd /opt/qa-system/docker
docker-compose -p qa-system --env-file /opt/qa-system/docker/.env \
  -f /opt/qa-system/docker/docker-compose.centos7.yml config --quiet \
  && echo "CONFIG_OK"

docker-compose -p qa-system --env-file /opt/qa-system/docker/.env \
  -f /opt/qa-system/docker/docker-compose.centos7.yml up -d --no-deps milvus
```

> - `config --quiet` 先验 YAML（v1 会当场炸，不会等到 up）。
> - `--no-deps` ⇒ **不碰 etcd / minio / backend**。
> - **不要加 `--remove-orphans`**（v1 下会把 centos7 文件之外的服务当孤儿清掉）。
> - 若服务名不是 `milvus`（见 3.2），把命令末尾的 `milvus` 换成实际服务名。

### 3.4 验证

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'qa-milvus|qa-etcd|qa-minio|qa-backend'
docker exec qa-milvus sh -c 'cat /milvus/configs/user.yaml'
docker exec qa-milvus sh -c 'grep -A6 "flushRate" /milvus/configs/milvus.yaml'   # 仍是出厂 0.1（user.yaml 叠加在上面，正常）
docker exec qa-milvus curl -s http://localhost:9091/healthz
docker logs qa-milvus --since 3m 2>&1 | tail -30
```

**期望**：
- `qa-milvus` Up（healthy）；
- `/milvus/configs/user.yaml` 内容 = §2 写的；
- `healthz` 返回 `OK`；
- Milvus 启动日志无 `config` / `panic` 类报错。

> **后端不用重启**：pymilvus 会自动重连；Milvus 重建期间（10–30s）后端会刷连接错误日志，
> 恢复后自愈。**若后端持续报连接错**，再单独重启后端：
> ```bash
> docker-compose -p qa-system --env-file /opt/qa-system/docker/.env \
>   -f /opt/qa-system/docker/docker-compose.centos7.yml restart backend
> ```

---

## 4. Phase 3 — 验证限流消失（**不改数据**）

在本体管理页随便**编辑一个属性**（保存即可），然后：

```bash
docker logs qa-backend --since 5m 2>&1 | grep -E "rate limit exceeded|向量自动同步失败"
```

**期望**：**无输出**（改前这里必然出现 `rate limit exceeded[rate=0.1]`）。
⇒ 配额生效。

> 观察窗口内其它属性也可能被同步（同一批改的）⇒ 只要没有 `rate limit exceeded` 就算通过。

> ⚠️ **这一步是唯一判据**：挂上 `user.yaml` **不等于**配额生效 —— Milvus 是否在 2.4.6
> 这个版本上真的读取并叠加 `/milvus/configs/user.yaml`，**必须靠这里的「无 rate limit
> exceeded」来证明**，不能靠配置文件存在来推断。若这里仍出现 `rate limit exceeded`，
> 走 **§4.1 备选方案**。

### 4.1 备选方案：直接覆盖 `milvus.yaml`（`user.yaml` 叠加无效时）

若 §4 仍然报 `rate limit exceeded[rate=0.1]` ⇒ 说明该版本的 Milvus **没有**读取
`user.yaml` ⇒ 改为**导出容器里的完整 `milvus.yaml`，只改那一行**，再挂载覆盖：

```bash
cd /opt/qa-system/docker

# 4.1.1 导出容器里的出厂配置（先留档原文件）
docker exec qa-milvus sh -c 'cat /milvus/configs/milvus.yaml' > milvus.yaml.prod.bak.20261117
wc -l milvus.yaml.prod.bak.20261117

# 4.1.2 确认要改的那一行的上下文（应当看到 collection: max: 0.1）
grep -n -B4 -A2 "flushRate" milvus.yaml.prod.bak.20261117

# 4.1.3 只改 collection.max 这一处（0.1 → 5），不改其它任何行
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("milvus.yaml.prod.bak.20261117")
text = p.read_text(encoding="utf-8")
new, n = re.subn(
    r"(quotaAndLimits:\s*\n(?:.*\n)*?\s*flushRate:\s*\n(?:.*\n)*?\s*collection:\s*\n\s*max:\s*)0\.1(\s*)",
    r"\g<1>5\g<2>",
    text,
    count=1,
)
print("replacements:", n)
assert n == 1, "没有定位到唯一目标行 —— 停下人工确认后再改"
pathlib.Path("milvus.yaml.override.20261117").write_text(new, encoding="utf-8")
PY

# 4.1.4 确认只改了那一处
diff -u milvus.yaml.prod.bak.20261117 milvus.yaml.override.20261117
```

**期望**：`diff -u` **只输出一处**改动（`max: 0.1` → `max: 5`）。
若输出多于一处 ⇒ 不要挂载，回到 §4.1.3 人工核对。

然后把 §3.2 挂的那行换成：

```yaml
      - /opt/qa-system/docker/milvus.yaml.override.20261117:/milvus/configs/milvus.yaml:ro
```

（`user.yaml` 那行可以保留——无害；但若担心叠加语义不明，可以删掉。）

再执行 §3.3 的 `config --quiet` + `up -d --no-deps milvus`，然后**重新跑 §4**
（这次判据同样是「无 `rate limit exceeded`」）。

> ⚠️ **不要 `sed -i` 直接改容器里的 `milvus.yaml`** —— 容器重建就没了，且不利于回滚。
> 覆盖式挂载可回滚（把 volumes 那行删掉再 `up -d`）。

---

## 5. Phase 4 — 按 PG 为真源对账补齐（同时测出真实影响面）

失败的实体**最新向量没有按预期固化**；旧向量是否还在**不能凭日志断言**（Milvus 的 delete
可见性要 release+load 才生效）⇒ 用**对账**来定论：按 PG 为真源补齐**缺失**向量。
仓库已有现成入口（**admin**）：

### 方式 A：管理页按钮（推荐）

本体管理 → **类** 标签页 → 右上角 **「补同步缺失向量」** 按钮（仅 admin 可见）。
执行后会提示：`已同步 N 个类、M 个属性`（或「无缺失」）。

### 方式 B：接口

```bash
# 先确认后端在宿主上映射到哪个端口（生产不一定就是 8000）
docker port qa-backend
BASE=$(docker port qa-backend | head -1 | sed 's/.*://')
echo "BASE=http://127.0.0.1:$BASE"

# 换 token（生产 AUTH_MODE=real ⇒ 必须真登录）
TOKEN=$(curl -s -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin用户名>","password":"<admin密码>"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["accessToken"])')

curl -s -X POST "$BASE/api/v1/ontology/embeddings/sync-missing" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' | head -c 2000
```

> ⚠️ `Content-Type: application/json` **必须显式带**（裸 curl 不带会被 FastAPI 当 text/plain ⇒ 恒 422，
> 这是本仓库 2026-11-16 刚修过的坑，见 [[qa-system-raw-fetch-content-type-422]]）。

**注意**：
- 该端点**整批 insert ⇒ 单次 flush** ⇒ 不会撞配额（前提是 §3 已生效）。
- 它会给**所有缺失**的类/属性/指标补向量（含本次事故之外的存量缺口）⇒
  会消耗 embedding 调用（**走本地/配置的 embedding 服务，可能计费**）。
- 响应里的 `missingPropertyCount` / `failedPropertyCount` 是验收口径：
  **补完后应再跑一次，`missingPropertyCount` 应为 0（或与「本来就缺失的」一致）。**

---

## 6. 回滚

```bash
cd /opt/qa-system/docker
cp -a docker-compose.centos7.yml.bak.20261117 docker-compose.centos7.yml
docker-compose -p qa-system --env-file /opt/qa-system/docker/.env \
  -f /opt/qa-system/docker/docker-compose.centos7.yml up -d --no-deps milvus
docker exec qa-milvus sh -c 'ls -l /milvus/configs/user.yaml 2>&1'   # 应为 No such file
```

⚠️ **回滚 = 配额回到 0.1 qps** ⇒ 批量导入 / 批量编辑会**再次**触发同样的失败。
⇒ 回滚后**先不要做批量写操作**，等代码加固发版（`fix/milvus-flush-quota` 分支）后再放开。

---

## 7. 留档

把下面四段输出贴回来（写进 `Harness/changes/2026-11-17-fix-milvus-flush-quota/summary.md` 的验收段）：

1. §1.5 的三条影响面统计；
2. §3.4 的 `user.yaml` 内容 + `healthz` + 容器状态；
3. §4 的「无 rate limit」验证输出；
4. §5 的 `sync-missing` 响应摘要（`missingPropertyCount` / `syncedPropertyCount` / `failedPropertyCount`）。

---

## 附：为什么不在这一步直接改「每条属性不 flush」

Milvus 官方口径：**数据可见性由查询一致性保证，flush 只负责固化（持久化）**；
但本仓库 v1 删除路径的「删除可见性」历史上依赖段固化 / release+load（见
`milvus_client.refreshCollection` 的注释与「重复向量」历史问题）⇒
去掉在线路径的 flush 会改变删除可见性时序，**需要真实 Milvus 行为验证**，
而本地 Milvus 是生产数据面（禁连测试）⇒ 本次不动语义。
代码加固（**集合级写入串行化** + 导入路径批量收口 + 同步入口异步化）放在
`fix/milvus-flush-quota` 分支，作为**下一次发版**。

## 附二：为什么不把「删 + 插」的两次 flush 合并成一次

Milvus **不保证同一次 flush 里 delete 与 insert 的先后** ⇒ 合并后，delete 表达式
（`ontology_id == X`）有可能把**同一次 flush 里刚插入的那一行**一起删掉 ⇒ 向量
**静默消失**（比现在更难查：没有任何报错）。这条顺序语义**无法在本仓库实测**
（本地 Milvus 是生产数据面、禁写测试）⇒ 不做无法验证的语义改动。
替代做法就是本文件的 **§2 抬配额**：两次 flush 拉开到约 200ms，不再落在同一个
令牌窗口里。
