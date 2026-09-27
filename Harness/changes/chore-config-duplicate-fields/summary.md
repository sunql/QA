# 变更：chore-config-duplicate-fields

- **日期**：2026-09-27
- **作者**：Claude / 启琳
- **Phase**：「声明值 ≠ 生效值」批次（chat-service-assessment §2.5 P2 #10 领头项）
- **状态**：done
- **关联变更**：无（独立小批；守卫与 M6 `test_no_duplicate_methods.py` 同源同构）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.5「`config.py` Settings 字段重复定义」行 + §3 P2 第 10 项
- **commit**：（见文末「关联」）

---

## 1. 需求

「声明值 ≠ 生效值」这一类的已查实领头项：`app/config.py` 的 `Settings` 有 **9 组重复字段声明**
（Python 类命名空间是 dict，**后写的静默赢**，前一份变死代码），其中 5 组**默认值不同**：

| 字段 | 旧块（L78–104，文件里读到的） | 生效值 | 实测影响 |
|---|---|---|---|
| `jwtTtlSeconds` | 3600（1h） | **86400（24h）** | session 寿命是声明值的 24 倍 |
| `bcryptRounds` | 12 | **10** | 哈希成本是声明值的 1/4（docstring 还写着「生产建议 ≥12」） |
| `jwtSecret` | `""`（意图：漏配即暴露/报错） | **`"development-jwt-secret-change-me"`** | 漏配 env 时**静默用公开已知密钥启动**；旧块注释承诺的「启动期校验」从未实现 |
| `jwtAudience` | `qa-system-web` | `qa-system` | aud 口径与声明不符 |
| `jwtAlgorithm` / `jwtIssuer` / `authMinDelayMs` / `dbPoolSize` / `dbMaxOverflow` | 值相同 | 值相同 | 纯噪声但同样误导 |

全树 AST 扫描（本次新建）另发现 1 组：`app/domain/schemas.py` `AgentDefinitionRead.created_time`
先声明 `datetime | None = None`、后被 `created_time: datetime` 覆盖——pydantic 合并出的
「annotation=datetime + default=None」**怪胎**：None 被拒、省略给 None，与 DB 契约
（`TimestampMixin.created_time nullable=False`）不一致。

用户口径（binding，两个决断都已拍板）：
1. **bcryptRounds 维持 10，只删重复行**（现网零行为变化；抬升 ≥12 另起一条，需显式设 `BCRYPT_ROUNDS`）；
2. **jwtSecret 加启动自检 warning**（空值/占位符大声告警，**不 fail-fast**——dev/测试零影响；生产已显式设置不受影响）。

**验收标准**：① 两组重复声明删除，全树零重复（AST 字段守卫常驻）；② `Settings` 51 字段生效默认值**逐字不变**（快照 diff 为空）；③ `AgentDefinitionRead.created_time` 生效语义与 DB 契约对齐且生产构造路径零变化；④ 启动期对空/占位 JWT 密钥 warning，真实密钥静默；⑤ §2.5/P2 #10 对应行关闭。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决/选定理由 |
|---|---|---|
| A | 只删 `config.py`，`schemas.py` 的 `created_time` 不动 | 否决：同一类腐化、守卫会立刻照报；留着等于给下个读者埋同款地雷 |
| B | 删声明 + 把 `bcryptRounds` 生效值抬回 12 | 否决（本批）：用户拍板**维持 10**——抬升是 prod env + 重启的行为变更，与「零行为变化」冲突；docstring 改为如实描述（当前 10 / 生产建议 12） |
| C | jwtSecret 缺省/占位时 **fail-fast** | 否决（本批）：最严格，但 dev 与全部测试装配都要显式补 `JWT_SECRET`，改动面失控；用户拍板 warning |
| D | **删声明 + 字段版 AST 守卫常驻 + jwtSecret 启动自检 warning**（选定） | 守卫与方法版（M6）同源同构；warning 用纯函数 + main.py lifespan 接线，tokenize 骨架级装配断言防「守卫休眠」 |

### 守卫的误报面（与方法版的差异）

字段版有一个方法版没有的**合法形态**要放行：无注解的同名赋值（`X = 1` 后 `X = 2`，
普通类常量的合法迭代）。守卫**只报带注解的声明**（`AnnAssign`）被重复；`x: int = 1` 后
`x = 2` 同样放行（声明一次 + 迭代覆值）。合成源码自证 + 全树扫描 + match-case 下钻 +
方法体内局部注解排除，全部有用例。

### `AgentDefinitionRead.created_time` 删哪份

删前实测（真身构造探针）：`created_time=None` 被 `datetime_type` 拒 ⇒ 唯一构造点
`agent_registry_service.agentToRead` 走 `model_validate(ORM 行)`，而 ORM 列
`nullable=False` ⇒ **生产不存在省略路径**。删除较早的 Optional 行后形态为
`created_time: datetime`（required）——**与 DB 契约一致**。
如实标注：对「省略 created_time」的构造，行为从「静默给 None」变为「ValidationError」，
该路径生产不可达（唯一构造点恒从 DB 行取值），属收紧而非回归。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本。`Settings` 与 `AgentDefinitionRead` 均为 Pydantic 模型，
重复声明是**类体内死代码**，非 DB 结构。

## 4. 接口契约变更

| 面 | 变更 |
|---|---|
| `Settings`（`app/config.py`） | 删除旧块 9 行重复声明（jwt×5 / bcryptRounds / authMinDelayMs / dbPoolSize / dbMaxOverflow）及其两段失效注释；生效默认值**零变化**（51 字段快照 diff 为空）。`bcryptRounds` docstring 改为如实描述；`dbPoolSize/dbMaxOverflow` docstring 并入「system_config 运行时覆盖 + 2026-09-19 bump」运营知识（原注释随死代码一并删除） |
| `AgentDefinitionRead.created_time`（`app/domain/schemas.py:2684`） | 删除被覆盖的 `created_time: datetime \| None = None`；生效形态 `datetime`（required），与 `TimestampMixin`（`nullable=False`）对齐 |
| `jwtSecretInsecurityReason(secret)`（`app/config.py`，新增纯函数） | 空 → 原因文案；== `DEV_JWT_SECRET_PLACEHOLDER` → 原因文案；其余 → None。不发明「长度不足」等启发式（长度策略属 authMode=real fail-fast 范畴，另议） |
| `DEV_JWT_SECRET_PLACEHOLDER`（`app/config.py`，新增常量） | 占位符字面量单点，供自检与测试引用 |
| main.py lifespan | stub-auth 安全告警块之后接线：`_jwtReason` 非空则 `logger.warning`（不阻断） |

## 5. 实现要点

- **TDD**：先写 `test_no_duplicate_fields.py`（8 例：合成重复检出 / Optional→非 Optional 危险形态 /
  两种合法迭代放行 / match-case 下钻 / 方法体内排除 / 跨类同名排除 / 全树扫描），全树用例
  按预期 **RED**（列出全部 10 组）；删声明后 **GREEN**。
- **零行为变化的证据**：删声明前后 `Settings.model_fields` 51 字段默认值 repr 快照 **diff 为空**
  （`Settings()` 构造亦验证 `jwtSecret` 生效默认 = 占位符）。
- **jwtSecret 守卫 TDD**：`test_jwt_secret_startup_guard.py` 先 RED（import 不存在），
  实现纯函数 + lifespan 接线后 GREEN；含 tokenize 骨架级装配断言（防「函数存在但
  main.py 没接线」的休眠形态——testapp-wiring-blindspot 的教训）。
- lifespan 内 import 与 stub-auth 告警块同款（函数内局部 import），不触碰模块级 import 排序。

## 6. 测试

| 层 | 范围 | 结果 |
|---|---|---|
| 新增守卫 | `test_no_duplicate_fields.py` 8 例 | 8 passed（先 RED 照报 10 组） |
| 新增守卫 | `test_jwt_secret_startup_guard.py` 5 例 | 5 passed（先 RED import 失败） |
| 聚焦 | unit+services `-k "agent or config or jwt or settings"` | 104 passed；1 failed = `test_weight_from_system_config_db_value`（**预存失败**，ADS 加权死代码，见 memory `qa-system-ads-weight-dead-code`） |
| 集成切片 | `test_app_wiring.py`（main.py 改动必跑） | 2 passed |
| 集成切片 | `test_agent_registry_api.py` + `test_agent_registry_audit.py` + `test_auth_endpoints.py` | 36 passed |
| 全量 | unit+services | **2536 passed, 2 failed**（+14 = 本批新增；2 failed 与基线完全相同：`test_weight_from_system_config_db_value` + `test_dependencies.py::test_stub_disabled_raises_permission_denied`，均为 memory 记录的预存失败） |
| ruff | 本批 5 文件 | 全绿；main.py/schemas.py 的 6+28 项经 `git stash` 基线对照**全部预存**，delta = 0 |
| 探针 | 独立 AST 全树复扫 | duplicate annotated fields: **NONE** |
| 探针 | 生效默认值快照 diff | 51/51 字段 NONE |

## 7. 安全审查

- **本批降低暴露面**：jwtSecret 漏配从「静默用公开已知密钥」变为「启动期大声 warning」。
  不 fail-fast 是用户拍板（dev 体验优先），prod 已显式设置（容器 env 64-hex），现网无告警
  （容器日志核验）。
- 无新增攻击面：纯函数自检 + 日志，不引入网络/DB/反序列化路径。
- bcryptRounds 维持 10：不弱于现状；「抬 12」登记为后续项（见 §8）。

## 8. 部署验证（2026-09-27）

- `./scripts/deploy_backend.sh` → 启动成功；`app/config.py` / `app/main.py` / `app/domain/schemas.py`
  容器内 md5 与本地 **MATCH**（3/3）。
- 健康：直连 `localhost:8000/api/v1/health` **200**；前端 nginx `localhost:5173/api/v1/health` **200**。
  （注：health 真实路径是 `/api/v1/health`；`/health` 404 是探针路径错误，非缺陷——
  main.py:527 注释记载过 catch-all 遮蔽历史。）
- 容器内真机探针：`jwtSecretInsecurityReason(占位符)` → 告警文案；`("")` → 告警文案；
  `getSettings().jwtSecret`（prod 真实 64-hex）→ **None**（与启动日志无告警一致）。
- 无迁移、无前端改动，无镜像重建需求。

## 9. 关联

- commit：`chore: 删 Settings/AgentDefinitionRead 重复字段声明 + AST 类体字段守卫`、
  `feat: jwtSecret 启动自检 warning（空值/占位符大声告警）`、
  `docs: config 重复字段批 SSOT 与评估文档 §2.5/P2#10 关闭`
- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.5 行关闭、§3 P2 #10 勾选、§0 进度行更新
- 守卫同源：`app/tests/unit/test_no_duplicate_methods.py`（M6 方法版）
- 后续（本批**未做**，用户拍板/登记）：
  1. `BCRYPT_ROUNDS=12` 抬升（prod env + 重启；bcrypt 校验自带成本参数，旧哈希不受影响）；
  2. authMode=real 下 jwtSecret 长度/占位 fail-fast（本批只 warning）；
  3. 评估文档其余挂账（§2.4 LOW、`sql_guard.py` 引用与 `IntentType` docstring 复核、L3 前端标签等）。

## SSOT 校验清单

- [x] `Settings` 重复声明 = 9 组，全部删除；`AgentDefinitionRead.created_time` = 1 组，已删除
- [x] 生效默认值零变化：51 字段快照 diff 为空（`/tmp/settings_before.json` 对照法）
- [x] 守卫自身被守卫：合成源码用例证明能报 + 合法形态（无注解迭代）不误报
- [x] main.py 接线有 tokenize 骨架断言（防守卫休眠）
- [x] 两个用户决断落实在代码与 docstring：bcryptRounds=10 如实描述、jwtSecret 只 warning
- [x] 全量 2536 passed；2 failed 均为 memory 记录的预存失败（ADS 加权死代码 / Header.lower）
- [x] ruff delta = 0（本批文件全绿；main.py/schemas.py 预存项经 stash 基线对照）
- [x] 容器 md5 3/3 MATCH + health 200×2 + 容器内自检函数三态探针
