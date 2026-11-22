# 本体发现与语义发布：功能使用说明（G1–G4-0）

> 适用分支 `feat/ontology-evolution`｜2026-11-22｜面向：数据治理管理员
> 自测记录见 §六（本机 dev 环境真实执行，含输出）

## 一、前置条件

| 项 | 说明 |
|---|---|
| 数据源 | 已登记且 `is_active=true` 的业务数据源（自测用 `THBI Oracle`，id=1） |
| 前端 | G1 的发布管理页需要**用本分支代码构建**前端镜像后才能看到菜单；后端 API 不受影响 |
| 开关 | 语义发布路由需要 `ENABLE_SEMANTIC_RELEASE=true`；本体发现路由**无开关**（常驻） |
| 权限 | 本文所有写操作与全部新读面都是 **admin**；普通登录用户只能看 A1 的源/快照/差异 |
| 本地身份 | 开发环境 `AUTH_MODE=stub` 时可用 `X-User-Id` + `X-User-Roles: admin` 头模拟身份（下文 curl 均这样写） |

## 二、功能总览

| 功能 | 入口 | 作用 |
|---|---|---|
| **G1 发布管理页** | 前端「业务配置 → 语义发布」 | 浏览器完成 发布→校验→激活→归档→副本式回滚 |
| **G2 六个校验码** | 发布详情 → 校验结论（或 `GET /semantic/releases/{id}/checks`） | CLS-001/002、REL-001、AGT-001、GRF-001、MET-002 |
| **G3 发现流水线** | `POST /ontology-discovery/runs` → 轮询 → 候选/问题 → 提案 → 审核 | 剖析 → LLM 候选 → 提案 → 四眼审核 |
| **G4 发布联动** | `POST /ontology-discovery/proposals/{id}/publish` | 已批准提案 → 走既有发布链 → ACTIVE |

## 三、G1 + G2：语义发布（浏览器即可完成）

1. 浏览器打开前端 → 菜单「语义发布」。
2. **建草稿**：右上「新建草稿」→ 填版本号（如 `2026.11.22-1`）、可选域（如 `PROCUREMENT`）→ 确定。
   草稿允许带阻断项落库（评审要看被拦下的内容）。
3. **看校验**：点「详情」→ 抽屉里看「校验结论」表（severity × result 两列）。
   想复核时点「校验」（DRAFT 才可点）。
4. **激活**：DRAFT 上点「激活」→ 确认。域内只允许一个 ACTIVE。
5. **归档/回滚**：ACTIVE 可「归档」；任意版本可「回滚」，但**必须填一个新版本号**（副本式）。

校验码含义（失败时 severity）：

| code | 判据 | 级别 |
|---|---|---|
| CLS-001 | 类名唯一 / objectType 合法 / **继承无环** | ERROR |
| CLS-002 | 业务对象头类存在 / 主域存在且**启用** | ERROR |
| REL-001 | JOIN 两端类存在 + **活库**语义关系类型/端点合法 | ERROR |
| AGT-001 | Agent 域/层在启用词表 + 绑定工具已注册且启用 | WARNING |
| GRF-001 | 图 label 在 Neo4j 白名单 + 入图对象头类有主键 | WARNING |
| MET-002 | 已发布 KPI 绑**启用中**数据源 + 关联指标已发布 | WARNING |

> 提示：`AGT-001`/`GRF-001`/`MET-002` 是治理债（不阻断）；CLS/REL 是真结构坏，会阻断发布。

## 四、G3：发现流水线（API）

### 4.1 登记发现源（A1，若还没有）

```bash
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/sources \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  -d '{"name":"THBI 采购源","datasourceId":1,"owner":"data-owner","accessPolicy":{"mode":"READ_ONLY","schema_owner":"THBI"}}'
```

采集快照（内容寻址、幂等；`probe:THBI Oracle` 已有快照 id=1 可直接用）：

```bash
curl -X POST -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  'http://127.0.0.1:8010/api/v1/ontology-discovery/snapshots?sourceId=1'
```

### 4.2 触发一次运行（异步）

```bash
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/runs \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  -d '{"sourceId":1,"tables":["DIM_SUPPLIER"],"withCandidates":true}'
# → 202 {"id":4,"status":"RUNNING",...}
```

- `tables` 不传 = 快照**全部**表（很贵，建议显式指定）；`withCandidates=false` 只剖析不调 LLM。
- 限流 **2/minute**；同一源已有 RUNNING 任务时返回 409。

轮询：

```bash
curl -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  http://127.0.0.1:8010/api/v1/ontology-discovery/runs/4
```

状态：`RUNNING` → `SUCCEEDED` / `FAILED`（失败时 `errorMessage` 有原因，且**不写**半份剖析行）。

### 4.3 看候选与问题

```bash
curl -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  'http://127.0.0.1:8010/api/v1/ontology-discovery/candidates?runId=4&limit=10'
curl -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  'http://127.0.0.1:8010/api/v1/ontology-discovery/findings?runId=4'
```

每条候选都带 `evidenceRefs`（`channel=PROFILE` / `kind` / `ref` / `summary`，summary 里是实测数字）。
**证据指向事实里不存在的表/列时，该条不会落库。**

### 4.4 建提案（manifest 必须是完整快照）

```bash
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/proposals \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: alice' -H 'X-User-Roles: admin' \
  -d '{"sourceId":1,"title":"供应商主数据修订",
       "baseReleaseId":21,
       "manifest":{"schemaVersion":"v2","domains":[],"classes":[],"joins":[],
                   "metrics":[],"kpis":[],"businessObjects":[],"sourceBindings":[]}}'
```

- 给了 `baseReleaseId` 会**自动算影响面**（`impactJson`：变更数/破坏性/加性/风险级/分区分布）。
- manifest 不过契约 ⇒ 422，**不落库**。
- `diffJson` 存的是 `{"kind":"MANIFEST_REPLACEMENT","baseReleaseId":…,"manifest":{…}}`（完整快照，不是补丁）。

### 4.5 提交 + 四眼审核

```bash
# 提交（提案人）
curl -X POST -H 'X-User-Id: alice' -H 'X-User-Roles: admin' \
  http://127.0.0.1:8010/api/v1/ontology-discovery/proposals/1/submit

# 同人自审 → 409（四眼原则）
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/proposals/1/review \
  -H 'Content-Type: application/json' -H 'X-User-Id: alice' -H 'X-User-Roles: admin' \
  -d '{"decision":"APPROVE"}'

# 换人审核
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/proposals/1/review \
  -H 'Content-Type: application/json' -H 'X-User-Id: bob' -H 'X-User-Roles: admin' \
  -d '{"decision":"APPROVE","comment":"同意"}'
```

结论：`APPROVE` → APPROVED；`REJECT` → REJECTED；`REQUEST_CHANGES` → 退回 DRAFT（可改后重提）。

### 4.6 增量监测（G4-1）

```bash
curl -X POST -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  http://127.0.0.1:8010/api/v1/ontology-discovery/sources/1/monitor
# → {"sourceId":1,"snapshotId":8,"changed":true,"runId":5}
```

- 内容没变（快照同哈希）⇒ `changed=false`、**不起运行**（不花剖析/LLM 成本）。
- 变了 ⇒ 起一个 `trigger=DIFF` 的运行，剖析 + 候选自动跑；提案仍要人工组装 + 四眼审核
  （候选不是结论，机器不替人拍板本体）。
- 限流 5/minute；周期驱动由外部调度器定时调本端点。

### 4.7 评测（G4-2）

```bash
curl -X POST -H 'Content-Type: application/json' \
  -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  -d '{"datasetVersion":"pilot-2026-11"}' \
  http://127.0.0.1:8010/api/v1/ontology-discovery/runs/4/evaluate

curl -H 'X-User-Id: admin' -H 'X-User-Roles: admin' \
  'http://127.0.0.1:8010/api/v1/ontology-discovery/evaluations?limit=10'
```

结论三态：`PASS`（有候选且证据全可解析）/ `FAIL`（有候选但证据不实）/ `INCONCLUSIVE`（没候选）。
指标只含**可复算**项（候选数/问题数/分类分布/证据完整率）；准确率/召回率要业务标注集（G5）。

## 四点五、前台工作台（发现源 → 快照 → 运行 → 评审）

菜单：**业务配置 → 发现评审**（`/discovery-review`）。页面分三个页签，全程无需命令行：

### ① 发现源

- 列出现有发现源（名称 / 业务数据源 / 责任人 / 状态 / 快照数）；
- **登记发现源**：选择业务数据源（仅活跃）、填名称/责任人/可选库模式属主（Oracle 填 `THBI`）；
- **采集快照**：幂等（结构没变会提示「复用」）；采集后快照数 +1；
- **监测变化**：先采快照；结构没变不会起运行（不花 LLM 钱），变了会自动起一次运行；
- **编辑策略**：改 `schema_owner` / **仅采集这些表**（逗号分隔的表名白名单）；
- **停用/启用**：撤销或恢复授权。

> ⚠️ 大 schema 必须先限定表：例如 ZJTH 有 **1695 张表 / 75,195 列**，整 owner 采集约 **12 MB**，
> 会撞 4 MB 快照上限（`元数据快照过大：12139982 字节 > 4194304`）。
> 在该源「编辑策略 → 仅采集这些表」填需要的表（如 `SUPPLIER,ITEM`），或按业务域拆成多个发现源。

### ② 运行

- 列出运行（状态 / 源 / 快照 / 模型 / tokens / cost / 开始时间 / 失败原因）；有 RUNNING 时每 5 秒自动刷新；
- **触发运行**：选发现源 → 选表（来自该源最新快照，不选=全部，**建议显式指定**）→ 是否生成候选（LLM）→ 确定；
  - 返回「运行 #id 已受理」，等状态到 `SUCCEEDED` 后到「评审」页签判定；
  - 剖析需要连业务库，限流 2/分钟；同一源已有 RUNNING 时会提示冲突。

### ③ 评审

- 选一个 `SUCCEEDED` 的运行（卡片显示模型/tokens/cost）；
- 「候选」页签：逐条 **采纳 / 驳回 / 待证** + 备注；
- 「主数据问题」页签：**确认 / 已解决 / 忽略 / 重新打开** + 备注；
- 进度条「已判定 M / N」到 100% 即签收完成；判定可改判，历史在 `audit_log`。


### 评审后如何「修正 / 落地」到本体

评审页每行的 **「关联本体」**：

1. 类型选 **本体类 / 本体属性 / 本体指标**；
   - 属性需先选类，再选该类的属性；
2. 若候选的 `targetCode` 与已有类同名，弹窗顶部会提示 **「本体里已有类 X」** → 点
   **「打开本体管理」** 直接去改类/属性；
3. 若没有对应类，提示 **「暂无对应类」** → 先在本体管理页新建，再回来关联；
4. 保存后候选/问题会记录 `linkedEntityType + linkedEntityCode`（列表「关联本体」列可见）。

规则：**主数据问题置「已解决」前必须先关联本体对象**（否则后端拒绝）。

> 采纳只是业务意见；真正变更本体仍走：候选 → 提案 → 四眼审核 → 发布。
> 「采纳即自动写本体」暂不做，已登记为待优化事项（`Harness/rules/本体发现与落地边界.md`）。
> 采纳只是业务意见；真正变更本体仍走：候选 → 提案 → 四眼审核 → 发布。

## 五、G4：发布已批准提案

```bash
curl -X POST http://127.0.0.1:8010/api/v1/ontology-discovery/proposals/1/publish \
  -H 'Content-Type: application/json' \
  -H 'X-User-Id: bob' -H 'X-User-Roles: admin' \
  -d '{"version":"2026.11.22-publish1","domainCode":"PROCUREMENT"}'
```

流程：提案 manifest → DRAFT → VALIDATED → ACTIVE（**走既有发布链与校验闸**）；提案被置
`PUBLISHED` 并回填 `release_id`。

⚠️ **副作用警告**：这是**真的发布**——同作用域原有的 ACTIVE 会被归档。
在共享/生产环境务必：
1. 先确认 `version` 未被占用（作用域内唯一）；
2. 确认 `domainCode` 选对了（不传 = 全局发布，会归档全局 ACTIVE）；
3. DDL/内容不可逆时先备份。

## 六、自测记录（2026-11-22，本机 dev）

环境：后端源码临时起在 `:8010`（dev 库 5433，`AUTH_MODE=stub`），业务源 `THBI Oracle`（id=1）。

| 步骤 | 命令/入口 | 实测结果 |
|---|---|---|
| 列发现源 | `GET /sources` | 1 条：`probe:THBI Oracle`，快照数 1 |
| 只剖析运行 | `POST /runs {tables:["DIM_SUPPLIER"],withCandidates:false}` | 202 → 2.6s → **SUCCEEDED**，token=0 |
| 全量运行（含 LLM） | 同上 `withCandidates:true` | **SUCCEEDED**；`modelVersion=deepseek-flash`，prompt=17,064 / completion=4,589，cost≈0.0367 USD |
| 候选 | `GET /candidates?runId=4` | **8 条**（OBJECT/PROPERTY/RELATION/METRIC），每条 1–2 条证据（含实测 summary） |
| 问题 | `GET /findings?runId=4` | **5 条**（如 `UPDDAT_0` 最小 1599-12-31、`BPSNUM_0` 唯一键建议、冗余复制列） |
| 建提案 | `POST /proposals` | 201，DRAFT，`diffJson.kind=MANIFEST_REPLACEMENT` |
| 提交 | `POST /proposals/1/submit` | 200，IN_REVIEW |
| 同人自审 | `POST /proposals/1/review`（同 user） | **409**「不能审核自己提交的提案（四眼原则）」 |
| 换人审核 | 同上（另一 user） | 201，APPROVE；提案 **APPROVED** |
| G2 校验码 | `POST /semantic/releases {version:self-test-2026-11-22-a, domainCode:PROCUREMENT}` → `GET .../checks` | 64 条结论；**CLS-001/CLS-002/REL-001 PASS**（17 类 / 10 对象 / 40 JOIN）；**AGT-001 FAIL**（Agent 未绑工具）、**GRF-001 FAIL**（DWD_* label 不在白名单）、**MET-002 FAIL**（关联指标仍 DRAFT） |
| 发布 | 未在 dev 执行 | 会归档现有 PROCUREMENT ACTIVE；已在测试库用自动化用例验证（6 例） |

> 自测在 dev 留下：运行 3/4、8 候选 + 5 问题、提案 1（APPROVED）、草稿发布 22。都是可安全删除/忽略的治理在途件。

## 七、已知限制

- `POST /runs` 的 `tables` 子集目前闭包在进程内后台任务里（单进程；进程重启会丢任务）。
- 候选生成依赖可用的模型配置；无配置时运行仍 `SUCCEEDED`（剖析结果保留），只是没有候选。
- 发布只允许「提案 → 既有发布链」；发现域不写治理表。

## 附录：批量上传文档（生产）

### A. 页面上批量（推荐日常使用）

文档中心 → **上传文档**：

1. 「选择文件」支持**多选 / 多文件拖拽**；
2. 文档类型 / 安全级别 / 版本 / Owner 为**共用元数据**；
3. 点「开始上传」→ **串行**逐个上传（每份自动解析、分块、向量化）；
4. 弹窗内逐条显示 成功/失败；**失败的可以「只重试失败项」**；全部成功自动关闭并刷新列表。

> 单文件 ≤ 50 MB；重复内容会失败并提示「内容重复」（不会重复入库）。

### B. 脚本批量（作业化 / 一次性导入整目录）

```bash
cd /opt/qa-system
DRY_RUN=1 ./scripts/bulk_upload_documents.sh /data/docs/采购制度        # 先看会传哪些
USERNAME=admin PASSWORD='***' \
  ./scripts/bulk_upload_documents.sh /data/docs/采购制度
# 可选：DOCUMENT_TYPE=POLICY SECURITY_LEVEL=L1 VERSION=v1.0 OWNER=采购部；或 TOKEN='eyJ...'
```

- 只收 **pdf / docx / txt / md**；重复内容计 `DUP`；失败不中断并汇总。
- 适用于「整目录制度文件」这类一次几十上百份、不需要逐份调元数据的场景。
