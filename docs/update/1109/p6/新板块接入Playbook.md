# 新板块接入 Playbook（P6 dry-run 产出，G7）

> 适用对象：要把**一个新业务板块**（域）接入智能问答平台的实施工程师 / 第三方。
> 本 Playbook 由 2026-11-15 的第二板块（ASSET 资产域）真实演练整理而来——每一步都在
> 测试环境实际执行过（见 [`../p6/演练记录_D58-D62.md`](演练记录_D58-D62.md)）。
> **全程零代码改动**：所有操作都走现有 API / 现有脚本 / 数据库数据准备。

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 环境 | 测试环境（平台已部署，`/api/v1/health` 通） |
| 账号 | 具备 admin 角色的账号（登录拿 JWT：`POST /api/v1/auth/login`） |
| 业务库 | 新板块的**业务数据库**可从平台后端网络访问（MySQL/PostgreSQL/Oracle） |
| 代码 | **不改**。P6 的验收就是「零代码接入」 |

```bash
TOKEN=$(curl -s -X POST http://<backend>/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<密码>"}' | python3 -c "import json,sys;print(json.load(sys.stdin)['accessToken'])")
```

---

## 1. 第一步：注册域（data_domain）

```bash
curl -X POST http://<backend>/api/v1/data-domains \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"<域代码>","name":"<中文名>","description":"<说明>"}'
```

- `code`：全大写英文（如 `ASSET`）。此后所有引用都用它。
- **注意**：域行只是目录；域与本体类的关联通过 **业务对象（BO）** 的
  `primaryDomainId` 建立（第 3 步）。

## 2. 第二步：建本体类 + 属性（ontology）

```bash
# 2.1 主表类
curl -X POST http://<backend>/api/v1/ontology/classes \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"className":"<类中文名>","classAlias":"<别名>","description":"<描述>",
       "sourceTable":"<物理表名>","objectType":"Master|Transaction|Reference|Event"}'
# ⇒ 记下返回的 id（后面 join / BO 都要用）

# 2.2 维表类（同上，objectType=Reference）

# 2.3 属性（每个类若干列；PK/FK 要标）
curl -X POST http://<backend>/api/v1/ontology/properties \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"classId":<类id>,"propertyName":"<列英文名>","propertyAlias":"<中文名>",
       "dataType":"STRING|INT|DECIMAL|DATETIME|BOOLEAN",
       "isPrimaryKey":true|false,"isForeignKey":true|false,
       "refClassId":<FK目标类id>,"sourceColumn":"<物理列名>"}'
```

**易错点（演练实测）**
- `propertyAlias` 与 `propertyName`/`sourceColumn` 同值时会被当作无别名（输出空）——起名要真正不同。
- FK 属性的 `refClassId` 指向**目标类**（不是表名）。

## 3. 第三步：建 join 与业务对象（BO）

```bash
# 3.1 join（类与类的连接目录；sourceColumns/targetColumns 是**物理列名数组**）
curl -X POST http://<backend>/api/v1/ontology/joins \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sourceClassId":<主表类id>,"sourceColumns":["<外键列>"],
       "targetClassId":<维表类id>,"targetColumns":["<被引用列>"],
       "joinType":"INNER|LEFT","relationType":"business","description":"<用途>"}'

# 3.2 BO（域 ↔ 本体类的桥：primaryDomainId 挂域，headerClassId 挂主类）
curl -X POST http://<backend>/api/v1/business-objects \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"code":"<BO代码>","name":"<中文名>","headerClassId":<主表类id>,
       "primaryDomainId":"<域代码>","graphLabel":"<主类中文名>"}'
```

**易错点（演练实测）**
- join 的 `targetColumns` 填**被引用列**（通常是维表的主键列）。填错列 LLM 会
  忠实地按错误的 join 生成 SQL ⇒ 关联不上——排查方法：看 chat 返回的 `sql`。
- BO 的 `graphLabel` **必须与 header 类的 `class_name` 完全一致**（治理校验），
  否则 400。
- join 列建错后**不能改**（join 列不可更新）⇒ 删除重建。

## 4. 第四步：业务库建表 + 灌数（数据准备，非平台代码）

在本板块的业务库里建与 `sourceTable` 一致的物理表并灌样例数据：

```bash
# MySQL 示例（表名/列名必须与 ontology 的 sourceTable/sourceColumn 完全一致）
mysql -h <业务库> -u <user> -p --default-character-set=utf8mb4 <<'SQL'
CREATE DATABASE IF NOT EXISTS <库名> CHARACTER SET utf8mb4;
USE <库名>;
CREATE TABLE <主表> (...); CREATE TABLE <维表> (...);
INSERT ...;
SQL
```

**易错点（演练实测）**：中文数据必须 `--default-character-set=utf8mb4`（否则乱码，
LLM 会在答案里如实报告乱码）。

```bash
# 把业务库注册为数据源
curl -X POST http://<backend>/api/v1/datasources \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"<数据源名>","type":"mysql|postgresql|oracle","host":"<host>",
       "port":<port>,"databaseName":"<库名>","username":"<user>","password":"<密码>",
       "isActive":true}'
```

## 5. 第五步：编译发布（语义发布）

经发布 API（测试环境需 `ENABLE_SEMANTIC_RELEASE=true`；本地栈若未开，
等价的**服务层调用**走容器内 `semanticReleaseService`，见演练记录 D59）：

```bash
# 编译成 DRAFT（把该域治理表编译成自包含快照）
curl -X POST http://<backend>/api/v1/semantic/releases \
  -d '{"version":"v-<板块>-1","domainCode":"<域代码>","note":"<说明>"}'
# DRAFT → VALIDATED → ACTIVE（状态机守卫；同域旧 ACTIVE 自动归档）
curl -X POST http://<backend>/api/v1/semantic/releases/<id>/validate
curl -X POST http://<backend>/api/v1/semantic/releases/<id>/activate
```

状态机：**DRAFT → VALIDATED → ACTIVE**（跳步会被 409 拒绝——演练实测踩到）。

## 6. 第六步（可选，语义镜像）：物化 + 对账

```bash
cd backend
# 预览（不写图）
NEO4J_URI=bolt://<测试图库>:7688 python -m scripts.semantic_graph_sync --release-id <id>
# 物化（写目标闸门默认只准测试实例；生产须显式 --allow-prod-write）
NEO4J_URI=bolt://<测试图库>:7688 python -m scripts.semantic_graph_sync --release-id <id> --apply
# 对账（必须 0 才算物化可信）
NEO4J_URI=bolt://<测试图库>:7688 python -m scripts.verify_graph_mirror --release-id <id>
```

## 7. 第七步：验证端到端

```bash
curl -X POST http://<backend>/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"sessionId":"<新会话id>","question":"<用类名/别名的业务问题>","datasourceId":<数据源id>}'
```

**验收**：`answer` 直接给出基于新板块数据的答案（演练实测 `[L4:answered]` +
数据表格）。**如果召回没命中**：换用类名/表名的措辞重试（召回按类名/别名/列名
命中，泛泛问法可能不命中——演练实测两次未命中、第三次按类名命中）。
**排查工具**：返回里的 `sql` 字段 = LLM 实际生成的 SQL（join 建错在这里看）。

## 8. 发布维度（可选进阶）

- **图谱按发布读**：`SEMANTIC_GRAPH_RELEASE_ID=<发布id>`（一键回退 = 拿掉）；
- **会话钉住**：会话内第一轮问答后自动钉住当时用的发布（重放一致）；
- **编排 run 钉住**：run 创建时自动冻结当前 ACTIVE（审计可查）。

---

## 9. 接入清单（checklist，照做即可）

- [ ] 1. `POST /data-domains` 注册域
- [ ] 2. `POST /ontology/classes` ×N + `POST /ontology/properties` ×M（PK/FK/refClassId 标对）
- [ ] 3. `POST /ontology/joins`（targetColumns = 被引用列）+ `POST /business-objects`（graphLabel = 类名）
- [ ] 4. 业务库建表灌数（utf8mb4）+ `POST /datasources`
- [ ] 5. 编译发布：DRAFT → VALIDATED → ACTIVE
- [ ] 6.（可选）镜像物化 + 对账 exit 0
- [ ] 7. chat 端到端问答通过（未命中 ⇒ 换类名措辞）
- [ ] 8.（可选）读侧开关（图谱/会话钉住）
