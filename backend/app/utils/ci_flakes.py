"""merge-gate 的「已知抖动白名单」：加载、校验、豁免计算。

为什么需要
----------
合并闸门的判据是「**失败集合对差，净新增为空才合并**」⇒ **任何一个用例在两侧的
「失败与否」不同，就一律拦下**。

而有些用例的结论**本身就是不确定的**（PR #107 实证，2026-11-06）：
`app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent`
因 Milvus 可见性竞态**随机红** ⇒ 同代码不同结论 ⇒ 闸门随机挡掉约一半的 PR
⇒ 每次都要人工做三件套证伪（「断言未触发 + 同代码不同结论 + diff 碰不到」）。

⇒ 本模块提供一条**可复用**的通道：让「结论不确定的用例」**不再充当判据**。

前三条硬约束（安全栏；第四条见下一节）
--------------------------------------
1. **精确 node ID**：只豁免清单里那**一个**用例 —— 不豁免文件、不豁免前缀、不豁免通配。
   （收集期错误的 ID 形如 `.../x.py`（无 `::`）⇒ **不豁免**：宁可漏豁免，不可错豁免。）
2. **失败关闭**：白名单读不到 / JSON 非法 / 条目不合规 ⇒ 抛 `FlakeWhitelistError`
   ⇒ 闸门退出码 3 ⇒ **绝不**退化成「空白名单」。
   （判据读不出来 ⇒「不知道」不等于「没有」⇒ 保守拦下；与 `ci_surface` 同款纪律。）
   ⚠️ `since` 必须是**真实存在的日历日期**（不只是形状对）：`2026-02-30` 能过正则
   ⇒ 若不校验，它会活到**渲染报告**时才抛 ⇒ 闸门对全仓每个 PR 都以
   「渲染报告失败」结束（退出码 3）⇒ 判据整体瘫痪，且报错指向错误的方向。
3. **只许 main 上 CODEOWNER 维护**：白名单文件属于「CI 相关面」（改它 = 改判据），
   且闸门从**默认分支的 checkout** 读它（`workflow_run` 语义）⇒ **PR 不能给自己开豁免**。

第四条：豁免**条件化在「该用例所在文件本次未被改动」**
----------------------------------------------------
对称豁免的代价是「PR 侧**确定性**弄红一个白名单用例，闸门也不会拦它」。
⇒ 若 PR **直接改了那个用例所在的文件**，它的红就可能不再是抖动
⇒ 此时**不豁免** ⇒ 该用例照常计入判据 ⇒ 闸门拦下（净新增/净丢失 ⇒ 人工确认），
   并在报告里明示「豁免未生效」⇒ 不静默。

⚠️ 这条**不引入随机阻塞**：判据只依赖「改了哪些文件」（服务端事实），
不依赖用例这一次红没红 ⇒ 结论稳定可复现。

⚠️ 豁免**只影响「对差」**，不影响：不可用 job 判定、覆盖等价判定、CI 相关面判定、
run 新鲜度判定 ⇒ 白名单**不能**用来掩盖「job 没跑完」「少跑了分片」「改了 conftest」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# 白名单数据文件放在仓库根的 `.github/` 下 ⇒ 与 CODEOWNERS 同处 ⇒ 授权面一眼可见。
DEFAULT_WHITELIST_RELATIVE_PATH = ".github/ci-known-flaky-tests.json"

# **精确** node ID 的形状：`<路径>.py::<名字>`（名字可含参数化后缀 `[param]` 与 `::`）。
# 刻意收紧：只接受「恰好一个用例」的 ID ⇒ 爆炸半径 = 1 个用例。
# 通配（`*`）与文件级 ID（无 `::`）一律拒绝。
#
# ⚠️ 用 `fullmatch` 而不是 `^...$`：Python 的 `$` **匹配末尾换行之前**
# ⇒ `"a.py::b\n"` 能通过 `^...$` 校验，却永远匹配不到任何真实失败 ID
# ⇒ 白名单条目**静默失效**（闸门继续随机挡人，报告里也看不出条目没生效）。
_EXACT_NODE_ID_PATTERN = re.compile(r"[\w./-]+\.py::[\w:.\[\]-]+")

# `since` 的形状：`YYYY-MM-DD`（同样用 `fullmatch`，理由见上）。
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

# 白名单条目的**最长保留期**（天）。超过 ⇒ 报告里 ⚠ 提示复核。
#
# 为什么需要：白名单一旦写下就永久有效 ⇒ 抖动被修好后条目不会自动消失
# ⇒ 此后该用例若发生**确定性**回归，会被永久豁免（两侧都被剔 ⇒ 净差恒为 0）
# ⇒ 这个用例从此**再也不回到判据**。⇒ 必须给条目一个老化机制。
MAX_ENTRY_AGE_DAYS = 180


class FlakeWhitelistError(RuntimeError):
    """白名单读不到 / 非法 / 条目不合规。

    抛出即「**失败关闭**」⇒ 调用方必须让闸门退出码 3（运行错误），
    **不得**把它当成「空白名单」继续判。
    """


@dataclass(frozen=True)
class FlakeEntry:
    """白名单里的一条：某个用例的结论「不是证据」。

    `since` **必填**：它是这条授权的签发日期，也是老化审查的锚点
    ⇒ 缺了它就不知道这条豁免「授权了多久」⇒ 加载器直接拒绝。
    """

    nodeId: str
    reason: str
    since: str


def isEntryStale(since: str, referenceDate: date | None = None) -> bool:
    """白名单条目是否已超过 `MAX_ENTRY_AGE_DAYS`（⇒ 应当复核或删除）。

    `referenceDate` 缺省取今天（测试可显式传入以免依赖当天日期）。

    ⚠️ **非法日期不抛异常**，按「已老化」处理。
    老化判定在**报告渲染路径**上（`ci_report._exemptionLines` 逐条调用）
    ⇒ 它抛异常 ⇒ 整份报告渲染失败 ⇒ 闸门退化成退出码 3（「渲染报告失败」）
    ⇒ 判据整体瘫痪，且报错指向错误的方向。
    ⇒ 非法日期本就意味着「这条授权的签发日期说不清」⇒ 提示复核才是正确处置。
    """
    reference = referenceDate if referenceDate is not None else date.today()
    try:
        signedOn = date.fromisoformat(since)
    except (TypeError, ValueError):
        return True
    return (reference - signedOn).days > MAX_ENTRY_AGE_DAYS


def defaultWhitelistPath() -> Path:
    """仓库根的默认白名单路径。

    由 `__file__` 上溯三级得到（`backend/app/utils/ci_flakes.py` ⇒ 仓库根），
    **不依赖当前工作目录** ⇒ 在 CI（`cd backend` 后运行）与本地行为一致。
    """
    return Path(__file__).resolve().parents[3] / DEFAULT_WHITELIST_RELATIVE_PATH


def _parseEntry(rawEntry: object, index: int, seenNodeIds: set[str]) -> FlakeEntry:
    """解析并校验一条白名单条目；不合规 ⇒ 抛 `FlakeWhitelistError`。"""
    if not isinstance(rawEntry, dict):
        raise FlakeWhitelistError(f"白名单第 {index + 1} 条不是对象")
    nodeId = rawEntry.get("nodeId")
    if not isinstance(nodeId, str) or not nodeId.strip():
        raise FlakeWhitelistError(f"白名单第 {index + 1} 条缺少 nodeId")
    if _EXACT_NODE_ID_PATTERN.fullmatch(nodeId) is None:
        raise FlakeWhitelistError(
            f"白名单第 {index + 1} 条的 nodeId 不是精确 node ID：{nodeId}"
            "（必须形如 `path/to/test_x.py::test_y`；不接受文件级 ID、不接受通配）"
        )
    if nodeId in seenNodeIds:
        raise FlakeWhitelistError(f"白名单里有重复的 nodeId：{nodeId}")
    reason = rawEntry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise FlakeWhitelistError(
            f"白名单第 {index + 1} 条缺少 reason"
            "（理由是 CODEOWNER 复核时的审计依据 ⇒ 空理由等于无授权）"
        )
    since = rawEntry.get("since")
    if not isinstance(since, str) or _DATE_PATTERN.fullmatch(since) is None:
        raise FlakeWhitelistError(
            f"白名单第 {index + 1} 条的 since 必须是 YYYY-MM-DD（必填）：{since!r}"
            "（它是这条授权的签发日期 ⇒ 老化复核的锚点 ⇒ 缺失即无授权）"
        )
    # ⚠️ **形状对 ≠ 日历上存在**：`2026-02-30` 能过上面的正则
    # ⇒ 若不在加载期校验，它会一路活到**渲染报告**时 `date.fromisoformat` 才抛
    # ⇒ 闸门对**全仓每个 PR** 都以「渲染报告失败」结束（退出码 3）
    # ⇒ 判据整体瘫痪，且报错指向错误的方向。
    try:
        signedOn = date.fromisoformat(since)
    except ValueError as error:
        raise FlakeWhitelistError(
            f"白名单第 {index + 1} 条的 since 不是合法日期：{since!r}"
            "（必须是真实存在的日历日期 ⇒ 它是老化复核的锚点）"
        ) from error
    # ⚠️ 未来日期说不通（还没签发就授权了）⇒ 且会让这条豁免在老化上限内**永不老化**
    # ⇒ 老化复核机制对它彻底失效 ⇒ 必须拒绝（2026-11-06 代码评审 L-4）。
    if signedOn > date.today():
        raise FlakeWhitelistError(
            f"白名单第 {index + 1} 条的 since 是**未来**日期：{since!r}"
            "（签发日期不能在未来 ⇒ 这条授权要么写错了，要么想永久免掉老化复核）"
        )
    seenNodeIds.add(nodeId)
    return FlakeEntry(nodeId=nodeId, reason=reason.strip(), since=since)


def loadFlakeWhitelist(path: Path) -> tuple[FlakeEntry, ...]:
    """读 + 校验白名单文件 ⇒ 返回条目元组。

    **任何异常形态都抛 `FlakeWhitelistError`**（失败关闭）：
    文件不存在 / 不可读 / JSON 非法 / 顶层不是对象 / `entries` 不是数组 /
    条目字段缺失或不合规 / `nodeId` 重复。
    """
    if not path.is_file():
        raise FlakeWhitelistError(
            f"白名单文件不存在：{path}"
            "（白名单是判据的一部分 ⇒ 失败关闭，不得当成空白名单）"
        )
    try:
        rawText = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        # ⚠️ `UnicodeDecodeError` 是 `ValueError` 的子类，**不是** `OSError`
        # ⇒ 只捕 `OSError` 时它会逃出本函数 ⇒ 逃出调用方的 `except FlakeWhitelistError`
        # ⇒ CLI 以裸 traceback + 退出码 1 结束（1 在语义上 = 「净新增失败」⇒ 查错方向）。
        raise FlakeWhitelistError(f"白名单文件读不到：{path}：{error}") from error
    try:
        payload = json.loads(rawText)
    except json.JSONDecodeError as error:
        raise FlakeWhitelistError(f"白名单文件不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, dict):
        raise FlakeWhitelistError(f"白名单顶层必须是对象：{path}")
    rawEntries = payload.get("entries")
    if not isinstance(rawEntries, list):
        raise FlakeWhitelistError(f"白名单的 entries 必须是数组：{path}")

    seenNodeIds: set[str] = set()
    return tuple(
        _parseEntry(rawEntry, index, seenNodeIds) for index, rawEntry in enumerate(rawEntries)
    )



@dataclass(frozen=True)
class FlakeExemption:
    """一次**实际发生**的豁免（白名单里有，但这次真的命中了）。"""

    nodeId: str
    reason: str
    inBase: bool
    inPr: bool
    since: str = ""


@dataclass(frozen=True)
class ExemptionOutcome:
    """豁免后的两侧失败集合 + 豁免明细。

    `baseFailures` / `prFailures` 是**豁免之后**的集合（= 真正参与对差的集合）。
    `rawBaseCount` / `rawPrCount` 是**豁免之前**的失败条数 ——
    报告层直接用这两个数，**不**在渲染时靠加法把原始条数「还原」出来
    （还原是隐含契约：将来谁把原始集合传进来，头部条数就会虚高）。

    `skipped` 是「**本来会命中、但因条件不满足而没豁免**」的条目
    ⇒ 报告层据此明示「豁免未生效」⇒ 不静默。
    """

    baseFailures: frozenset[str]
    prFailures: frozenset[str]
    exemptions: tuple[FlakeExemption, ...]
    rawBaseCount: int = 0
    rawPrCount: int = 0
    skipped: tuple[FlakeExemption, ...] = ()


def flakeFilePath(nodeId: str) -> str:
    """node ID ⇒ 它所在**测试文件**的路径（`::` 之前的部分）。

    node ID 的形状是 `<路径>.py::<用例名>`（用例名里还可以含 `::`，例如参数化的
    类方法）⇒ 按**第一个** `::` 切分。
    """
    return nodeId.split("::", 1)[0]


def _isTestFileChanged(nodeId: str, changedFiles: frozenset[str]) -> bool:
    """该用例**所在文件**是否在本次改动的文件清单里。

    ⚠️ **两套路径基准**（必须对上，否则这条守卫恒不生效）：
    - node ID 里的路径是 **rootdir 相对**（pytest 从 `backend/` 起收集 ⇒ `app/tests/...`）；
    - compare API 给的是**仓库相对**（`backend/app/tests/...`）。

    ⇒ 判定规则：清单里有条目**等于**该路径，或**以 `/` + 该路径结尾**。
    （结尾匹配天然覆盖「仓库相对」这一侧；「等于」覆盖「已经是仓库相对」的写法。）

    ⚠️ 匹配方向刻意偏保守：宁可**多判成「改过了」**（⇒ 不豁免 ⇒ 拦下来），
    不可漏判（⇒ 确定性回归被当成噪声放过去）。
    """
    testPath = flakeFilePath(nodeId)
    return any(
        changed == testPath or changed.endswith("/" + testPath)
        for changed in changedFiles
    )


def applyFlakeExemptions(
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    entries: Iterable[FlakeEntry],
    changedFiles: Iterable[str] | None = None,
) -> ExemptionOutcome:
    """把白名单命中的用例从**两侧**失败集合里剔除 ⇒ 产出参与对差的集合与豁免明细。

    **对称豁免**：白名单断言的是「**这个用例的结论不是证据**」，而不是
    「这个用例的失败只在某个方向上可忽略」⇒ 两侧都剔除。
    ⇒ 副作用（已知并接受）：PR 侧若**确定性**弄红了一个白名单用例，闸门也不会拦它
    ⇒ 兜底是**每次豁免都出现在报告里**（含「PR 侧命中」的显式提示）。

    只做**精确匹配**（集合成员判定，不是前缀 / 子串 / 正则）：
    不在白名单里的失败 ID 一律原样保留
    ⇒ 爆炸半径 = 1 个用例（同文件的其他用例、名字前缀相同的用例**都不豁免**）。

    ⚠️ **豁免条件化在「该用例所在文件本次未被改动」**（`changedFiles`）：
    对称豁免的代价是「PR 侧确定性弄红白名单用例也不拦」
    ⇒ 若 PR 直接改了那个用例所在的文件，它的红就**可能不是抖动**了
    ⇒ 此时**不豁免** ⇒ 该用例照常计入判据 ⇒ 闸门拦下（净新增/净丢失 ⇒ 人工确认）。

    ⚠️ 这条**不引入随机阻塞**：判据只依赖「改了哪些文件」（服务端事实），
    不依赖用例这一次红没红 ⇒ 结论稳定可复现。

    `changedFiles=None` ⇒ 改动清单**未知** ⇒ 照常豁免：
    「判定不出来」由「CI 相关面」守卫以**退出码 6** 兜住
    ⇒ 豁免层不必重复拦（否则退出码会变成 1 =「净新增失败」⇒ 语义反而错）。
    """
    entryByNodeId = {entry.nodeId: entry for entry in entries}
    filteredBase = set(baseFailures)
    filteredPr = set(prFailures)
    exemptions: list[FlakeExemption] = []
    skipped: list[FlakeExemption] = []
    changedSet = frozenset(changedFiles) if changedFiles is not None else None

    for nodeId in sorted(entryByNodeId):
        entry = entryByNodeId[nodeId]
        inBase = nodeId in filteredBase
        inPr = nodeId in filteredPr
        if not inBase and not inPr:
            continue
        record = FlakeExemption(
            nodeId=nodeId,
            reason=entry.reason,
            inBase=inBase,
            inPr=inPr,
            since=entry.since,
        )
        if changedSet is not None and _isTestFileChanged(nodeId, changedSet):
            # 该用例所在文件本次被改动 ⇒ 它的红绿可能不再是噪声 ⇒ **不豁免**。
            skipped.append(record)
            continue
        filteredBase.discard(nodeId)
        filteredPr.discard(nodeId)
        exemptions.append(record)

    return ExemptionOutcome(
        baseFailures=frozenset(filteredBase),
        prFailures=frozenset(filteredPr),
        exemptions=tuple(exemptions),
        rawBaseCount=len(baseFailures),
        rawPrCount=len(prFailures),
        skipped=tuple(skipped),
    )
