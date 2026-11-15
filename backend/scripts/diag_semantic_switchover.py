"""D17 切换演练：`ENABLE_SEMANTIC_REGISTRY_NL2SQL` 开 / 关两态下**回放同一批问题**。

为什么需要这个演练（而不是只看 D13/D14 的影子比对）
----------------------------------------------------
影子比对证的是「两份 schema 文本**内容等价**」（静态）。而 G3 要求的是
「**切换前后回放结果一致**」（动态）：同一批问题，开关关时生成的 SQL 与
开关开时生成的 SQL，是否回答的是同一件事。

⚠️ 已知残余风险 R-1（`daily/D14.md`）：两侧渲染文本在**类内列序 / JOIN 条目序**上不同
（内容等价、顺序不同）⇒ prompt 不是逐字节相同。D14 当时**推测**漂移只发生在
「SELECT 列表顺序 / 别名」这类无关紧要处 —— 本演练的实测**否证了这条推测**
（见下）。⇒ 判据分两层：
1. **强判据**：归一化后的 SQL 文本逐字相同；
2. **弱判据**（R-1 允许时）：**只**容忍顶层投影列的**顺序与别名**，
   `FROM / JOIN / WHERE` 仍逐字比较 —— 满足才算「回放结果一致」。
两层都不满足 ⇒ 真差异，非零退出。

**为什么必须做同态对照（关→开→关→开）**：单样本的跨态差异可能只是 LLM 抖动。
只有「同一态重复两次逐字相同」（= 抖动为 0）时，跨态差异才能归因到开关。
实测（本地 Qwen，temperature=0）：同态两次**完全一致**、跨态**显著不同**
（OFF 侧多出 `ENAFLG_0=2` / `TCLCOD_0 IN (...)` 等过滤口径、ON 侧丢了
`DIM_IMATERIAL` 的 JOIN）⇒ 漂移是**确定性的**，不是抖动，且**不限于无关紧要处**。

四条机检判据
------------
1. **两态 schema 文本内容等价**：对同一份类子集，OFF（直读）与 ON（快照）渲染出的
   文本按**块内行多重集**比较 ⇒ 差异 = 0；同时报「整段文本顺序差异」行数（已知，非失败）。
2. **ON 态确实走了快照**：`renderSchemaWithRelease` 回报 `semanticReleaseId` 非 None；
   OFF 态必须为 None（否则「关」没关住 ⇒ 开关无效）。
3. **回放结果一致**：每个问题按 关→开→关→开 跑四遍 计划 + SQL，先做**同态对照**
   （同态两次之间的差异 = LLM 抖动），再按上面两层判据比较跨态差异。
4. **SQL Guard 链两态原样**：两态生成的 SQL 都必须通过 `_assert_read_only` 与
   `findShareDenominatorIssues`；**且**一条已知违规 SQL 在两态下都必须被拒。
   ⚠️ 这一条是**守卫函数自检**（函数本身有效、与开关无关），它**证明不了**
   `generateSql` 里到底有没有调用守卫 —— 那条接线由单测
   `test_开关打开走快照时generateSql仍过SQL_Guard链` 钉住（删掉 `_assert_read_only`
   调用 ⇒ 该用例转红）。

外加一项计时：**开关回落耗时** —— 改环境变量 + 清 Settings 缓存 ⇒ 下一次渲染即生效。
生产上等价于「改 env + 重启后端进程」，**不需要重新构建镜像、不需要回滚 DDL**
（新列可空，旧代码忽略它）。

只读性
------
本脚本**不执行**生成的 SQL（只生成 + 静态判守卫），对业务库零写入；
`session` 只用于读治理表 / 语义发布，全程 SELECT。

用法::

    # LLM key 走环境变量（不要进 argv）；DSN 同样走环境变量
    export QA_LOCAL_LLM_KEY=...        # 本地 OpenAI 兼容服务的 key
    ENABLE_SEMANTIC_REGISTRY_NL2SQL=true \\
      ./.venv/bin/python scripts/diag_semantic_switchover.py \\
        --database-url-env QA_METADATA_DSN

退出码
------
- ``0``：四条判据全过（回放一致）；
- ``2``：两态 schema 文本**内容不等价**（快照丢了 / 多了事实）；
- ``3``：跨态回放**确定性不一致**（两层判据都不满足，**且**同态对照证明不是抖动）；
- ``4``：前置条件不满足 / 执行异常（无 ACTIVE 发布 / 开关读不到 / LLM 或数据库不可达 /
  类子集缺类 / 回放中途发现某态没走快照）；
- ``5``：SQL Guard 链失效（合法 SQL 被拒，或违规 SQL 未被拒）；
- ``6``：**无法判定**（同态重复本身就不一致 ⇒ 抖动粒度大于判据 / SQL 不可解析 / 某态产不出 SQL）。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import sqlglot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.db_snapshot_guard import (  # noqa: E402
    buildSessionFactory,
    maskDatabaseUrl,
    resolveDatabaseUrl,
    scrubSecrets,
)

EXIT_OK = 0
EXIT_NOT_EQUIVALENT = 2
EXIT_REPLAY_MISMATCH = 3
EXIT_PRECONDITION = 4
EXIT_GUARD_BROKEN = 5
EXIT_INCONCLUSIVE = 6

# 演练用的固定类子集：生产渲染的是**召回子集**，不是全集（plan §3.2 / D13 评审 MEDIUM-2）。
# 选「采购订单明细 / 收货明细 / 到货订单明细 / 供应商 / 物料」五张表：它们之间**确有 JOIN 边**
# （实测 13 条命中子集内两端）⇒ JOIN 段非空 ⇒ 探针能覆盖「JOIN 条目序」这条已知差异。
_SUBSET_CLASSES = (
    "DIM_SUPPLIER",
    "DIM_IMATERIAL",
    "DWD_PURCHASE_ORDER_DTL",
    "DWD_GOODS_RECEIPT_DTL",
    "DWD_ARRIVAL_ORDER_DTL",
)

# 固定问题集：都是这套子集能答的业务问题（不依赖 Oracle 可达——本脚本不执行 SQL）。
_QUESTIONS = (
    "各供应商的收货明细条数",
    "各供应商的到货明细数量合计",
)

_SWITCH_ENV = "ENABLE_SEMANTIC_REGISTRY_NL2SQL"

# 一条**必定违规**的 SQL：证明守卫链在开关「开」时也没有被绕过。
_VIOLATING_SQL = "DELETE FROM DWD_ARRIVAL_NOTICE"


def _setSwitch(enabled: bool) -> float:
    """切开关并清 Settings 缓存；返回生效耗时（秒，= 生产上「改 env + 重启进程」的等价度量）。"""
    from app.config import getSettings

    startedAt = time.perf_counter()
    os.environ[_SWITCH_ENV] = "true" if enabled else "false"
    getSettings.cache_clear()
    # 真正生效点：下一次读设置。这里立刻读一次，把「读设置」的耗时也算进回落时间。
    # 用显式 raise 而不是 assert：`python -O` 会剥掉 assert，那时开关若没切成功，
    # 脚本会拿「关 vs 关」两份结果比较，还可能给出「两态一致」的**假绿**结论。
    actual = getSettings().enableSemanticRegistryNl2sql
    if actual is not enabled:
        raise RuntimeError(f"切开关失败：期望 {enabled}，实际 {actual}")
    return time.perf_counter() - startedAt


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _maskEndpoint(endpoint: str) -> str:
    """打印前给 endpoint 打码：userinfo（`user:pass@`）与整段 query 一律遮掉。

    DSN 侧有 `maskDatabaseUrl` 兜底，但 `--llm-endpoint` 走 argv —— 若有人把
    `https://user:sk-xxx@proxy/v1` 传进来，不打码就等于把凭据写进 shell 历史与 stdout。
    """
    parsed = urlparse(endpoint)
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = f"***@{netloc.rsplit('@', 1)[1]}"
    query = "?***" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{parsed.path}{query}"


def _warnIfExternalEndpoint(endpoint: str) -> None:
    """非本机 endpoint ⇒ 显式提示「会把本体 schema 文本发过去」。

    外发内容是**元数据**（表名 / 列名 / description / 业务别名 / JOIN 关系），
    不含业务行数据（本脚本不传 `valueSamples`）—— 但仍是内部资产，指向外部主机即构成外泄面。
    """
    host = (urlparse(endpoint).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        return
    print(
        f"⚠️ LLM endpoint 不是本机（{host}）⇒ 会把**本体 schema 文本**"
        "（表名 / 列名 / 描述 / 别名 / JOIN 关系；不含业务行数据）发送到该地址。"
        "确认这是可信服务再继续。",
        file=sys.stderr,
    )


def _failureMessage(exc: BaseException, databaseUrl: str, llmKey: str) -> str:
    """异常文本统一脱敏：沿用 DSN / DB 口令打码规则，**并额外抹掉 LLM key**。

    不复用 `connectionErrorExit`：它会 `raise SystemExit(字符串)` ⇒ 退出码变成 1，
    而本脚本的退出码是契约（0/2/3/4/5/6）；另外它只清 DB 口令，LLM 异常文本若
    回显了请求头值就没人清。
    """
    root = getattr(exc, "orig", None) or exc
    detail = scrubSecrets(str(root), databaseUrl)
    if len(llmKey) >= 4 and llmKey in detail:
        detail = detail.replace(llmKey, "***")
    return f"❌ 演练中止（{maskDatabaseUrl(databaseUrl)}）：{detail}"


def _splitBlocks(text: str) -> dict[str, list[str]]:
    """按 `### ` 切块（与闸门守卫同口径：块内行序不参与比较）。"""
    blocks: dict[str, list[str]] = {"": []}
    key = ""
    for line in text.splitlines():
        if line.startswith("### "):
            key = line
            blocks.setdefault(key, [])
            continue
        blocks[key].append(line)
    return blocks


def _contentDiff(left: str, right: str) -> list[str]:
    """块内行多重集差异（与 `semantic_schema_gate._factDiff` 同口径，这里独立实现以免依赖私有名）。"""
    from collections import Counter

    leftBlocks = _splitBlocks(left)
    rightBlocks = _splitBlocks(right)
    gaps: list[str] = []
    for key in sorted(set(leftBlocks) | set(rightBlocks)):
        lhs = Counter(leftBlocks.get(key, []))
        rhs = Counter(rightBlocks.get(key, []))
        if lhs != rhs:
            gaps.append(
                f"块 {key or '（前言）'}：左多 {sum((lhs - rhs).values())} 行 / "
                f"右多 {sum((rhs - lhs).values())} 行"
            )
    return gaps


def _orderDiffLines(left: str, right: str) -> int:
    """整段文本的行序差异条数（D14 的已知差异类别，**不计失败**）。"""
    import difflib

    differ = difflib.ndiff(left.splitlines(), right.splitlines())
    return sum(1 for line in differ if line[:1] in "+-")


_DIALECT = "oracle"


class _UnparsableSql(RuntimeError):
    """生成的 SQL 解析不了 ⇒ 判据不适用（**不许**退回文本比较来蒙一个结论）。"""


def _normalizeSql(sql: str) -> str:
    """**仅用于打印**（折叠空白）。不参与判据 —— 它会把字符串字面量的大小写一起抹平。"""
    return " ".join(sql.split())


def _canonicalSql(sql: str, *, ignoreProjectionDetail: bool) -> str:
    """判据用的规范化 SQL；解析失败 ⇒ 抛 `_UnparsableSql`，由调用方记「无法判定」。

    - **强判据**（`ignoreProjectionDetail=False`）：关键字大小写 / 空白 / 注释归一后逐字比较；
    - **弱判据**（`ignoreProjectionDetail=True`）：只**额外**容忍顶层投影列的**顺序与别名**，
      **FROM / JOIN / WHERE / GROUP BY / HAVING / ORDER BY 仍逐字比较**。

    弱判据为什么不能退化成「只比投影列集合」：实测的跨态差异正是
    「ON 丢了 `DIM_IMATERIAL` 的 JOIN」「OFF 多出 `ENAFLG_0 = 2`」这类**非投影**差异，
    投影列集合相同 ⇒ 会被判成「一致」⇒ 假绿。R-1 容忍的范围只有投影列序/别名。
    """
    try:
        tree = sqlglot.parse_one(sql, read=_DIALECT)
    except Exception as exc:  # noqa: BLE001 - 解析器异常一律视为「判不了」
        raise _UnparsableSql(f"SQL 无法按 {_DIALECT} 方言解析：{exc}") from exc
    # 只在**根就是单条 SELECT**（含 CTE：sqlglot 把 `WITH ... SELECT` 解析成 Select）时归一投影。
    # UNION 等多分支形态**不做**归一 ⇒ 退回强判据：`find(Select)` 只会拿到第一支，
    # 而 UNION 按位置绑定列，「只换第一支」是真差异、会被误判成一致（假绿）。
    if ignoreProjectionDetail and isinstance(tree, sqlglot.exp.Select) and tree.expressions:
        stripped = [
            expression.this if isinstance(expression, sqlglot.exp.Alias) else expression
            for expression in tree.expressions
        ]
        tree.set("expressions", sorted(stripped, key=lambda e: e.sql(dialect=_DIALECT)))
    return tree.sql(dialect=_DIALECT, comments=False)


def _guardVerdict(sql: str) -> tuple[bool, str]:
    """跑 SQL Guard 链（只读校验 + L1 占比分母守卫），返回 (是否放行, 说明)。

    ⚠️ 这里直接调用守卫函数 ⇒ 证明的是「函数有效」，**不是**「`generateSql` 调了它」。
    后者的接线由单测钉住（删调用 ⇒ 单测红）。
    """
    from app.services.nl2sql_semantic_guard import findShareDenominatorIssues
    from app.services.nl2sql_service import _assert_read_only

    try:
        _assert_read_only(sql)
    except Exception as exc:  # noqa: BLE001 - 拒因即判据
        return False, f"只读校验拒绝: {exc}"
    issues = findShareDenominatorIssues(sql)
    if issues:
        return False, f"占比分母守卫拦截: {issues[0]}"
    return True, "通过"


async def _loadSubset(session, ontology) -> tuple[list, list]:
    allClasses = await ontology.listClasses(session)
    byName = {cls.class_name: cls for cls in allClasses}
    missing = [name for name in _SUBSET_CLASSES if name not in byName]
    if missing:
        raise RuntimeError(f"类子集缺类：{missing}")
    classes = [byName[name] for name in _SUBSET_CLASSES]
    joins = await ontology.listJoins(session)
    return classes, joins


# 注：**不要**在读过类 / join 之后 `session.rollback()` —— rollback 必定 expire 全部 ORM 实例
# （`expire_on_commit=False` 只管 commit），随后渲染快照时属性访问会在错误上下文里触发 IO，
# 实测报 `greenlet_spawn has not been called`（评审 L-4 曾建议「定期 rollback」，实测行不通）。
# 本次接受 L-4：脚本对 metadata 库只读、单次运行数分钟，测试/dev 库未设 idle_in_transaction 超时。


async def _render(session, classes, joins) -> tuple[str, int | None]:
    """按当前开关状态渲染 schema 文本，并回报发布 id。"""
    from app.services.semantic_schema_gate import renderSchemaWithRelease

    result = await renderSchemaWithRelease(classes, joins=joins, session=session)
    return result.text, result.semanticReleaseId


async def _generate(llmClient, modelConfig, question: str, classes, joins, session) -> tuple[str, int | None]:
    """跑一遍「计划 → SQL」，返回 (SQL, 本轮使用的发布 id)。"""
    from app.services.nl2sql_service import Nl2SqlService

    nl2sql = Nl2SqlService()
    planResult = await nl2sql.generateQueryPlan(
        question, classes, llmClient, modelConfig, joins=joins, session=session
    )
    sqlResult = await nl2sql.generateSql(
        question,
        classes,
        llmClient,
        modelConfig,
        joins=joins,
        plan=getattr(planResult, "plan", None),
        session=session,
    )
    return sqlResult.sql, sqlResult.semanticReleaseId


async def _run(databaseUrl: str, llmKey: str, llmEndpoint: str, llmModel: str) -> int:
    from app.domain.enums import ProviderType
    from app.infrastructure.llm.openai_client import OpenAiClient
    from app.services.ontology_service import OntologyService
    from app.services.semantic_registry_service import semanticRegistry

    modelConfig = SimpleNamespace(
        model_name=llmModel, temperature=0.0, disable_thinking=False
    )
    llmClient = OpenAiClient(
        SimpleNamespace(
            model_name=llmModel,
            api_endpoint=llmEndpoint,
            temperature=0.0,
            disable_thinking=False,
        ),
        apiKey=llmKey,
        provider=ProviderType.OPENAI,
    )

    factory = buildSessionFactory(databaseUrl)
    async with factory() as session:
        active = await semanticRegistry.getActive(session)
        if active is None:
            print("前置条件不满足：库里没有 ACTIVE 语义发布", file=sys.stderr)
            return EXIT_PRECONDITION
        print(f"当前 ACTIVE 发布 id = {active.id}（version={active.version}）")

        ontology = OntologyService()
        classes, joins = await _loadSubset(session, ontology)
        print(f"演练类子集 = {[c.class_name for c in classes]}（{len(classes)} 类），join 数 = {len(joins)}")

        # ---- 判据 1 + 2：两态 schema 文本 ----
        fallbackSeconds = _setSwitch(False)
        print(f"\n开关切到**关**（生效耗时 {fallbackSeconds * 1000:.1f} ms）")
        offText, offReleaseId = await _render(session, classes, joins)

        _setSwitch(True)
        print("开关切到**开**")
        onText, onReleaseId = await _render(session, classes, joins)

        print(f"OFF 回报发布 id = {offReleaseId}（必须为 None）")
        print(f"ON  回报发布 id = {onReleaseId}（必须为 ACTIVE id）")
        if offReleaseId is not None:
            print("\n结论：开关**关**时仍回报了发布 id ⇒ 开关没关住", file=sys.stderr)
            return EXIT_PRECONDITION
        if onReleaseId != active.id:
            print(
                f"\n结论：开关**开**时未走 ACTIVE 快照（回报 {onReleaseId}，期望 {active.id}）"
                " ⇒ 本次不构成切换演练的有效证据",
                file=sys.stderr,
            )
            return EXIT_PRECONDITION

        gaps = _contentDiff(offText, onText)
        orderDiff = _orderDiffLines(offText, onText)
        print(f"两态 schema 文本行数：OFF={len(offText.splitlines())} / ON={len(onText.splitlines())}")
        print(f"内容差异块数 = {len(gaps)}（必须 0）")
        print(f"整段文本行序差异 = {orderDiff} 行（D14 已知差异，不计失败）")
        if gaps:
            for gap in gaps[:10]:
                print(f"  - {gap}")
            print("\n结论：两态 schema 文本**内容不等价**（快照丢了 / 多了事实）", file=sys.stderr)
            return EXIT_NOT_EQUIVALENT

        # ---- 判据 3：回放 ----
        # 每态跑 **两次**：同态两次之间的差异 = LLM 自身的抖动（对照组）；
        # 跨态差异若**不超过**同态抖动，就不能说「开关改变了行为」。
        print("\n== 回放（每个问题：关 → 开 → 关 → 开，共四次 计划+SQL）==")
        mismatches: list[str] = []
        noisy: list[str] = []
        for question in _QUESTIONS:
            _setSwitch(False)
            off1, off1Id = await _generate(llmClient, modelConfig, question, classes, joins, session)
            _setSwitch(True)
            on1, on1Id = await _generate(llmClient, modelConfig, question, classes, joins, session)
            _setSwitch(False)
            off2, off2Id = await _generate(llmClient, modelConfig, question, classes, joins, session)
            _setSwitch(True)
            on2, on2Id = await _generate(llmClient, modelConfig, question, classes, joins, session)

            # 判据 2 必须在**每一轮**都成立：某一轮 ON 降级为直读 ⇒ 跨态比较其实是
            # 「直读 vs 直读」⇒ 会给出假绿（这是判据 1 那一次独立渲染覆盖不到的）。
            if off1Id is not None or off2Id is not None:
                print(f"\n问题「{question}」：OFF 采样回报了发布 id ⇒ 开关没关住", file=sys.stderr)
                return EXIT_PRECONDITION
            if on1Id != active.id or on2Id != active.id:
                print(
                    f"\n问题「{question}」：ON 采样未走 ACTIVE 快照"
                    f"（回报 {on1Id} / {on2Id}，期望 {active.id}）⇒ 本次回放不构成有效证据",
                    file=sys.stderr,
                )
                return EXIT_PRECONDITION

            def _same(a: str, b: str) -> tuple[bool, bool]:
                sameText = _canonicalSql(a, ignoreProjectionDetail=False) == _canonicalSql(
                    b, ignoreProjectionDetail=False
                )
                sameWeak = _canonicalSql(a, ignoreProjectionDetail=True) == _canonicalSql(
                    b, ignoreProjectionDetail=True
                )
                return sameText, sameWeak

            try:
                offSameText, offSameWeak = _same(off1, off2)
                onSameText, onSameWeak = _same(on1, on2)
                crossSameText, crossSameWeak = _same(off1, on1)
            except _UnparsableSql as exc:
                print(f"\n问题「{question}」：{exc} ⇒ 该样本无法判定", file=sys.stderr)
                noisy.append(question)
                continue

            # 归因的前提：**同态两次逐字相同**（抖动 = 0）。只有弱判据成立不算数
            # —— 那时同态之间仍有文本差异，把跨态差异归给开关就是猜测。
            deterministic = offSameText and onSameText

            print(f"\n问题：{question}")
            print(f"  ON 态本轮发布 id = {on1Id} / {on2Id}（必须都 = {active.id}）")
            print(f"  对照组（关 vs 关）：逐字相同={offSameText} 仅投影列序不同={offSameWeak}")
            print(f"  对照组（开 vs 开）：逐字相同={onSameText} 仅投影列序不同={onSameWeak}")
            print(f"  实验组（关 vs 开）：逐字相同={crossSameText} 仅投影列序不同={crossSameWeak}")

            if crossSameText:
                print("  ⇒ 跨态判为一致：**逐字相同**（强判据）")
            elif crossSameWeak:
                print("  ⇒ 跨态判为一致：强判据不成立，但差异只在顶层投影列序 / 别名（R-1 容忍范围）")
            elif not deterministic:
                # 同态两次逐字都不同 ⇒ 抖动 ≈ 判据粒度 ⇒ 单样本不可判
                noisy.append(question)
                print("  ⇒ 同态重复本身就不逐字相同 ⇒ 无法把跨态差异归因到开关（记录为抖动）")
            else:
                mismatches.append(question)
                print("  OFF SQL：" + _normalizeSql(off1)[:400])
                print("  ON  SQL：" + _normalizeSql(on1)[:400])

            # ---- 判据 4：两态生成的 SQL 都必须过守卫 ----
            for label, sql in (("OFF1", off1), ("ON1", on1), ("OFF2", off2), ("ON2", on2)):
                passed, reason = _guardVerdict(sql)
                print(f"  {label} SQL Guard = {reason}")
                if not passed:
                    print(f"\n结论：{label} 态生成的合法 SQL 被守卫拒绝 ⇒ 守卫链异常", file=sys.stderr)
                    return EXIT_GUARD_BROKEN

        # 对照：违规 SQL 在两态下都必须被拒（守卫没被开关绕过）
        for enabled in (False, True):
            _setSwitch(enabled)
            passed, reason = _guardVerdict(_VIOLATING_SQL)
            print(f"\n开关={'开' if enabled else '关'} 时违规 SQL 判定 = {reason}")
            if passed:
                print(
                    "\n结论：违规 SQL 被放行 ⇒ SQL Guard 链被开关绕过",
                    file=sys.stderr,
                )
                return EXIT_GUARD_BROKEN

        if noisy:
            print(
                "\n⚠️ 以下问题的**同态重复**本身就不一致（LLM 抖动 ≥ 判据粒度），"
                f"单样本不可判：{noisy}"
            )
        if mismatches:
            print(
                f"\n结论：跨态回放**不一致**（强、弱判据都不满足）：{mismatches}",
                file=sys.stderr,
            )
            print(
                "  且**同态两次逐字相同** ⇒ 差异不是 LLM 抖动，而是开关**系统性地**"
                "改变了 prompt（两态只差「类内列序 / JOIN 条目序」，如上面两态文本行数相同、"
                "内容差异块 0 所示）⇒ 模型对行序敏感 ⇒ 计划与 SQL 都变。",
                file=sys.stderr,
            )
            print(
                "  含义：开关**不是**透明开关 —— 打开它会改变回答；生产开闸前必须做"
                "答案级影子评估，不能只凭「schema 内容等价」判定无影响。",
                file=sys.stderr,
            )
            return EXIT_REPLAY_MISMATCH
        if noisy:
            print(
                "\n结论：**无法判定** —— 同态抖动已到判据粒度，跨态差异无法归因；"
                "需加大样本、降低 temperature 或改用确定性替身复核",
                file=sys.stderr,
            )
            return EXIT_INCONCLUSIVE

        # 收尾：把开关切回**默认关**（演练不改变环境状态）
        _setSwitch(False)

    print(
        "\n结论：两态 schema 内容等价；ON 确实走快照；回放一致（SQL 逐字相同，或差异仅在投影列序/别名）；"
        "SQL Guard 链两态原样；开关回落毫秒级"
    )
    return EXIT_OK


def main() -> int:
    from app.domain.exceptions import Nl2SqlError

    parser = argparse.ArgumentParser(description="D17 语义快照切换演练（只读 + 双态回放）")
    parser.add_argument(
        "--database-url-env",
        default="QA_METADATA_DSN",
        help="存放 DSN 的环境变量名（避免口令进 argv）",
    )
    parser.add_argument(
        "--llm-key-env",
        default="QA_LOCAL_LLM_KEY",
        help="存放 LLM API key 的环境变量名（避免口令进 argv）",
    )
    parser.add_argument("--llm-endpoint", default="http://127.0.0.1:8888/v1")
    parser.add_argument("--llm-model", default="Qwen3.8-27B-4bit")
    args = parser.parse_args()

    rawUrl = os.getenv(args.database_url_env)
    if not rawUrl:
        print(f"环境变量 {args.database_url_env} 未设置", file=sys.stderr)
        return EXIT_PRECONDITION
    llmKey = os.getenv(args.llm_key_env)
    if not llmKey:
        print(f"环境变量 {args.llm_key_env} 未设置", file=sys.stderr)
        return EXIT_PRECONDITION

    parsedEndpoint = urlparse(args.llm_endpoint)
    if parsedEndpoint.scheme not in {"http", "https"} or not parsedEndpoint.hostname:
        print(
            f"--llm-endpoint 不是合法的 http(s) 地址：{_maskEndpoint(args.llm_endpoint)}",
            file=sys.stderr,
        )
        return EXIT_PRECONDITION

    try:
        databaseUrl = resolveDatabaseUrl(None, args.database_url_env, allowInlinePassword=False)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_PRECONDITION

    print(f"目标库 DSN = {maskDatabaseUrl(databaseUrl)}")
    print(f"LLM = {args.llm_model} @ {_maskEndpoint(args.llm_endpoint)}")
    _warnIfExternalEndpoint(args.llm_endpoint)

    # 开关必须显式打开（脚本内部会自己切两态；这里只做 fail-fast，避免拿「没装好」的环境跑）
    if os.getenv(_SWITCH_ENV, "").lower() not in {"1", "true", "yes", "on"}:
        print(f"前置条件不满足：{_SWITCH_ENV} 未打开", file=sys.stderr)
        return EXIT_PRECONDITION

    try:
        return asyncio.run(_run(databaseUrl, llmKey, args.llm_endpoint, args.llm_model))
    except SystemExit:
        raise
    except Nl2SqlError as exc:
        # 某一态连 SQL 都产不出 ⇒ 没有可比对象，判「无法判定」而非「不一致」
        print(_failureMessage(exc, databaseUrl, llmKey), file=sys.stderr)
        return EXIT_INCONCLUSIVE
    except Exception as exc:  # noqa: BLE001 - 其余归前置条件 / 执行异常（exit 4），文本先脱敏
        print(_failureMessage(exc, databaseUrl, llmKey), file=sys.stderr)
        return EXIT_PRECONDITION


if __name__ == "__main__":
    raise SystemExit(main())
