"""语义发布影子比对：快照 schema 文本 vs 直读 schema 文本（P2 / D13）。

比对的两侧
----------
- **直读侧**：治理表（``ontology_class`` / ``ontology_property`` / ``ontology_join``）
  → ORM 实体 → ``nl2sql_schema.buildSchemaText``。这是**今天的生产路径**。
- **快照侧**：已发布快照（``semantic_release.manifest_json``）→ 复原视图
  （``semantic_schema_adapter.restoreSnapshotSchema``）→ 同一个
  ``buildSchemaText``。

同一份输入、同一套渲染器 ⇒ 两侧的差异只可能来自「数据来源不同」，
不会混进「渲染逻辑不同」的噪音。这是归零差异（差异 = 0）的唯一可信口径。

⚠️ **两侧的类集合必须逐项相等**（不只是条数相等）——快照多一个类或少一个类
都会让 schema 文本变宽/变窄 ⇒ 行为漂移。脚本把这条作为**硬断言**先跑，
不通过就直接非零退出。

⚠️ **必须比子集，不只比全集**（安全评审 MEDIUM-2）：生产渲染的是**召回子集**
（≤ 30 类），子集外的父类 / 外键目标解析成 ``None`` 这条逻辑**只在子集里
才被触发**。只比全集 ⇒ 所有父类 / FK 目标都在「子集」内 ⇒ 这条逻辑一次都
没跑到，比对工具就永远发现不了子集语义下的偏差。
⇒ ``--subset-mode each`` 会把每个类单独切成一个子集逐一比对。

⚠️ **JOIN 段的覆盖范围随模式而变**：一条 JOIN 要渲染需要**两端同在子集内**。

- ``none``（全集）⇒ JOIN 段**完整**覆盖；
- ``groups``（每组 5 类）⇒ 只覆盖**同组内**的 JOIN（跨组的边在该组里查不到
  两端 ⇒ 不渲染）—— 不是「覆盖了 JOIN 段」，是「覆盖了一部分」；
- ``each``（每子集 1 类）⇒ ``### JOIN 关系`` 恒为空。

⇒ 想覆盖**全部** JOIN 段，必须跑 ``none``（全集）。

用法::

    # 只看差异（全集；**只有这个模式完整覆盖 JOIN 段**）
    ./.venv/bin/python scripts/semantic_schema_shadow_diff.py \\
        --database-url-env QA_METADATA_DSN

    # 逐类子集比对（生产语义；**不覆盖 JOIN 段**）
    ./.venv/bin/python scripts/semantic_schema_shadow_diff.py \\
        --database-url-env QA_METADATA_DSN --subset-mode each

    # 抓基线 fixture（**直读侧全集**原文；与子集模式无关）
    ./.venv/bin/python scripts/semantic_schema_shadow_diff.py \\
        --database-url-env QA_METADATA_DSN \\
        --baseline-out ../Harness/changes/feat-semantic-registry-nl2sql/baseline/schema_text_direct.txt

退出码
------
- ``0``：内容差异 0 **且** 顺序差异 0（完全一致）；
- ``1``：有**内容**差异（快照丢了事实）——必须修；
- ``2``：硬断言不通过（类集合不等 / 无 ACTIVE 快照 / 目标库不符 /
  **快照 schema 版本与代码不一致** / 快照内容畸形）；
- ``3``：内容差异 0，但**顺序**有差异（已知差异，需按 D14 判据单独计量）。
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from collections import Counter
from pathlib import Path

# 允许从 scripts/ 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.db_snapshot_guard import (  # noqa: E402
    assertSameDatabase,
    buildSessionFactory,
    connectionErrorExit,
    maskDatabaseUrl,
    resolveDatabaseUrl,
)
from scripts.kpi_metric_db_guard import assertSafeTarget  # noqa: E402

# 逐行 diff 的展示上限：全量 diff 可能上千行，报告里只留前 N 条 + 总数。
# 截断**不隐藏**差异条数（总数永远打印）——「报告非空」是 D13 的完成判据。
_MAX_DIFF_LINES = 200

# 子集模式：逐类切分时的分组大小（groups 模式）。
_SUBSET_GROUP_SIZE = 5

# 顺序差异的归因类别（D14 的顺序判据）。
_ORDER_KIND_CLASS = "类块"
_ORDER_KIND_JOIN = "JOIN 段"
_ORDER_KIND_BLOCK_ORDER = "块顺序"
_ORDER_KIND_PREAMBLE = "公共段落"
_ORDER_KIND_OTHER = "其它"

# **已知差异清单**（封闭）：只有这两类的顺序差异可以被接受。
# 清单外的顺序差异一律进「归因缺口」⇒ 退出码 1 ⇒ 必须显式决定，不许默认放行。
_ACCEPTED_ORDER_KINDS = frozenset({_ORDER_KIND_CLASS, _ORDER_KIND_JOIN})

# 渲染器里**非类块**的固定段落标题前缀（`nl2sql_schema.buildSchemaText`）。
# 「JOIN 关系」与「间接 JOIN 路径参考」两段都是 JOIN 条目列表 ⇒ 同归 JOIN 段。
_JOIN_SECTION_PREFIXES = ("### JOIN 关系", "### 间接 JOIN 路径参考")

# 类块标题的判别标记：渲染器对每个类无条件追加 `: table=<表名>`
# （`nl2sql_schema.py:317`）⇒ 用它**正面**认类块，而不是「不是 JOIN 就是类块」。
#
# 为什么必须正面认：排除法（「不是 JOIN 段 ⇒ 类块」）会让任何**新段落**
# （如「关键过滤口径摘要」）的顺序差异借「类块」的名义混进已知差异清单
# ⇒ 清单就不再封闭，第三类差异能绕过退出码 1。
# 正面认的失败方向是**安全**的：渲染器改了标题格式 ⇒ 类块不再被认出
# ⇒ 顺序差异进缺口 ⇒ 退出码 1（吵闹），而不是静默放行（安静）。
_CLASS_HEADER_MARKER = ": table="


async def _collect(session) -> tuple[list, list]:
    """直读侧：治理表里的全部活类 + 全部 join。"""
    from app.services.ontology_service import OntologyService

    ontology = OntologyService()
    classes = await ontology.listClasses(session)
    joins = await ontology.listJoins(session)
    return classes, joins


def _subsets(classNames: list[str], mode: str) -> list[tuple[str, list[str]]]:
    """按模式产出「要比的子集」。

    - ``none``：全集一个子集（**唯一覆盖 JOIN 段的模式**）；
    - ``each``：每个类单独成一个子集（子集外引用逻辑在这里被真正触发）；
    - ``groups``：按固定大小分组（比 each 便宜，覆盖组合场景）。
    """
    if mode == "none":
        return [("全集", list(classNames))]
    if mode == "each":
        return [(name, [name]) for name in classNames]
    if mode == "groups":
        groups: list[tuple[str, list[str]]] = []
        for start in range(0, len(classNames), _SUBSET_GROUP_SIZE):
            chunk = classNames[start:start + _SUBSET_GROUP_SIZE]
            groups.append((f"#{start // _SUBSET_GROUP_SIZE}", chunk))
        return groups
    raise SystemExit(f"未知的子集模式：{mode}")


def _manifestClassNames(manifest) -> list[str]:
    """快照里的类名清单。"""
    if not isinstance(manifest, dict):
        return []
    names: list[str] = []
    for raw in manifest.get("classes") or []:
        if isinstance(raw, dict) and isinstance(raw.get("className"), str):
            names.append(raw["className"])
    return names


def _classSetMismatch(directNames: list[str], manifestNames: list[str]) -> str | None:
    """两侧类集合逐项比对：不等 ⇒ 返回错误说明（相等 ⇒ `None`）。"""
    if sorted(directNames) == sorted(manifestNames):
        return None
    onlyDirect = sorted(set(directNames) - set(manifestNames))
    onlyManifest = sorted(set(manifestNames) - set(directNames))
    return (
        "类集合不相等（硬断言不通过）："
        f"仅治理表有（{len(onlyDirect)}）：{'、'.join(onlyDirect)}；"
        f"仅快照有（{len(onlyManifest)}）：{'、'.join(onlyManifest)}"
    )


def _versionGate(snapshot) -> str | None:
    """快照 schema 版本闸门：与代码不一致 ⇒ 返回错误说明（一致 ⇒ `None`）。

    为什么影子比对也要过这道闸：版本漂移时，复原层会把新字段读成空 ⇒
    比对会报「快照丢了事实」⇒ 把**版本问题**误判成**数据问题**，
    白白排查一轮。闸门与运行时（`loadClassesForSchema`）共用同一个实现。
    """
    from app.services.semantic_schema_adapter import schemaVersionMismatch

    mismatched = schemaVersionMismatch(snapshot)
    if not mismatched:
        return None
    return (
        f"快照的 schema 版本与当前代码不一致（{mismatched}）"
        "⇒ 复原层读不到新字段，先修版本再谈差异"
    )


def _compareSubset(
    *,
    manifest: dict,
    byName: dict,
    joins: list,
    label: str,
    names: list[str],
) -> dict:
    """一个子集的比对结果（直读侧 vs 快照侧）。"""
    from app.services.nl2sql_schema import buildSchemaText
    from app.services.semantic_schema_adapter import restoreSnapshotSchema

    subsetOrm = [byName[name] for name in names]
    restoredClasses, restoredJoins = restoreSnapshotSchema(manifest, names)
    directText = buildSchemaText(subsetOrm, joins=joins)
    snapshotText = buildSchemaText(restoredClasses, joins=restoredJoins)
    directLines = directText.splitlines()
    snapshotLines = snapshotText.splitlines()
    diffLines = list(
        difflib.unified_diff(
            directLines,
            snapshotLines,
            fromfile="direct(治理表)",
            tofile="snapshot(发布快照)",
            lineterm="",
        )
    )
    changeLines = [
        line
        for line in diffLines
        if line[:1] in "+-" and line[:3] not in ("---", "+++")
    ]
    return {
        "label": label,
        "names": names,
        "directText": directText,
        "snapshotText": snapshotText,
        "diffLines": diffLines,
        "changeCount": len(changeLines),
        "contentDiff": _contentDiff(directLines, snapshotLines),
        "orderOnly": _orderOnlyClasses(directLines, snapshotLines),
        "blockMismatches": _blockContentMismatches(directLines, snapshotLines),
        "orderAttribution": _orderAttribution(directLines, snapshotLines),
    }


def _compareSubsets(
    *,
    manifest: dict,
    classes: list,
    joins: list,
    subsetMode: str,
) -> list[dict]:
    """按模式逐个子集比对。"""
    byName = {cls.class_name: cls for cls in classes}
    directNames = [cls.class_name for cls in classes]
    results: list[dict] = []
    for label, names in _subsets(directNames, subsetMode):
        results.append(
            _compareSubset(
                manifest=manifest, byName=byName, joins=joins, label=label, names=names
            )
        )
    return results


def _baselineText(classes: list, joins: list) -> str:
    """直读侧**全集**的 schema 文本（基线 fixture 的内容）。

    为什么固定用全集：基线是「今天生产路径的完整输出」的参照点。
    若在 ``each`` 模式下拿「第一个子集」的文本当基线 ⇒ 文件里只有一类
    ⇒ 后续拿它比对会得到假绿/假红，而文件本身不会报错。
    """
    from app.services.nl2sql_schema import buildSchemaText

    return buildSchemaText(classes, joins=joins)


async def _run(args: argparse.Namespace, databaseUrl: str, targetDb: str) -> int:
    from sqlalchemy.exc import DBAPIError

    from app.domain.exceptions import DomainError
    from app.services.semantic_registry_service import (
        SemanticReleaseIntegrityError,
        semanticRegistry,
    )

    factory = buildSessionFactory(databaseUrl)
    try:
        async with factory() as session:
            # 守卫放行的库名必须等于会话**真正连的**库。
            await assertSameDatabase(session, targetDb, databaseUrl)

            classes, joins = await _collect(session)
            directNames = [cls.class_name for cls in classes]

            snapshot = await semanticRegistry.getActive(session)
            preconditionError = _preconditionError(snapshot, directNames)
            if preconditionError:
                print(f"❌ {preconditionError}")
                return 2

            subsetResults = _compareSubsets(
                manifest=snapshot.manifest,
                classes=classes,
                joins=joins,
                subsetMode=args.subset_mode,
            )
            print(f"比对子集数：{len(subsetResults)}（模式 {args.subset_mode}）")
            return _report(
                args,
                snapshot=snapshot,
                subsetResults=subsetResults,
                directNames=directNames,
                classes=classes,
                joins=joins,
            )
    except DomainError as exc:
        # 快照陈旧 / 内容畸形（`app.domain.exceptions.DomainError` 的子类）。
        print(f"❌ 快照不可用：{exc}")
        return 2
    except SemanticReleaseIntegrityError as exc:
        print(f"❌ 快照完整性校验失败：{exc}")
        return 2
    except (DBAPIError, OSError, TypeError, ValueError) as exc:
        # TypeError / ValueError：驱动参数非法时抛的是这两个，不是 DBAPIError
        # ⇒ 不接住就会吐裸 traceback（可能带 DSN），取代打码后的干净退出。
        raise connectionErrorExit(exc, databaseUrl) from exc
    finally:
        await factory.kw["bind"].dispose()


def _preconditionError(snapshot, directNames: list[str]) -> str | None:
    """比对前置三道硬断言（快照存在 / 版本一致 / 类集合一致）。

    拆出来是为了让 `_run` 保持在 50 行以内；**判据本身一个字没少**。
    """
    if snapshot is None:
        return "没有 ACTIVE 发布快照 ⇒ 无从比对（先跑 semantic_publish_snapshot.py）"
    versionError = _versionGate(snapshot)
    if versionError:
        return versionError
    return _classSetMismatch(directNames, _manifestClassNames(snapshot.manifest))


def _report(
    args: argparse.Namespace,
    *,
    snapshot,
    subsetResults: list[dict],
    directNames: list[str],
    classes: list,
    joins: list,
) -> int:
    """出报告 + 落基线 + 算退出码。"""
    totalContent = sum(item["contentDiff"] for item in subsetResults)
    totalOrder = sum(item["changeCount"] for item in subsetResults)
    totalBlockGaps = sum(len(item["orderAttribution"]["gaps"]) for item in subsetResults)
    totalBlockOrderGap = sum(
        item["orderAttribution"]["byKind"].get(_ORDER_KIND_BLOCK_ORDER, 0)
        for item in subsetResults
    )
    worst = max(subsetResults, key=lambda item: item["contentDiff"], default=None)

    report = _renderReport(
        snapshot=snapshot,
        subsetResults=subsetResults,
        subsetMode=args.subset_mode,
        totalContent=totalContent,
        totalOrder=totalOrder,
        totalBlockGaps=totalBlockGaps,
        totalBlockOrderGap=totalBlockOrderGap,
        directNames=directNames,
    )
    if args.report_out:
        _write(args.report_out, report)
        print(f"✅ 报告已写：{args.report_out}")
    else:
        print(report)

    # 基线在比对**之后**才写：比对失败时不该留下一个「看起来是基线」的文件。
    # 基线固定是**直读侧全集**（与子集模式无关）⇒ 不会把某个子集的文本
    # 当成全集基线写出去。
    if args.baseline_out and totalContent == 0:
        _write(args.baseline_out, _baselineText(classes, joins))
        print(f"✅ 基线 fixture 已写：{args.baseline_out}")

    print(
        f"内容差异行数：{totalContent} / 顺序敏感差异行数：{totalOrder}"
        f" / 子集数：{len(subsetResults)}"
    )
    exitCode = _exitCode(
        totalContent,
        totalOrder,
        blockMismatches=totalBlockGaps,
        blockOrderGap=totalBlockOrderGap,
    )
    if worst is not None and worst["contentDiff"] > 0:
        print(f"❌ 最差子集：{worst['label']}（内容差异 {worst['contentDiff']} 行）")
    elif exitCode == 3:
        print("⚠️ 内容等价但顺序有差异（已知差异，见 D14 判据）")
    return exitCode


def _exitCode(
    totalContent: int,
    totalOrder: int,
    *,
    blockMismatches: int = 0,
    blockOrderGap: int = 0,
) -> int:
    """差异指标 → 进程退出码。

    - ``0``：内容差异 0 **且** 顺序差异 0（完全一致）；
    - ``1``：有内容差异（快照丢了事实）——必须修；
    - ``3``：内容差异 0 但顺序有差异（已知差异，需按 D14 判据单独计量）。

    为什么顺序差异不能也返回 0：D14 的完成判据是**两条**——内容等价 且
    顺序等价。退出码 0 被流水线读作「完全一致」，顺序差异被吞掉 ⇒ 判据失效。

    `blockMismatches`（D14 新增）：**逐块**内容不等价的块数。全局行多重集
    相等但块归属不同（某行从类 A 搬到类 B）⇒ 事实挂到了别的表上 ⇒ 这是
    **内容差异**，不是顺序差异 ⇒ 退出码必须是 1，不能记成「已知差异 3」。

    `blockOrderGap`（D14 新增）：**块之间**的顺序差。块顺序变 = 「哪个类
    先出现」变了 ⇒ 影响面比列序大，且**不在** D14 接受的已知差异清单里
    （清单只有「类内列序」「JOIN 条目序」两类）⇒ 按必须修处理 ⇒ 退出码 1。
    """
    if totalContent > 0 or blockMismatches > 0 or blockOrderGap > 0:
        return 1
    if totalOrder > 0:
        return 3
    return 0


def _contentDiff(directLines: list[str], snapshotLines: list[str]) -> int:
    """**内容等价**差异行数（**按类块**比较行多重集，忽略顺序）。

    为什么必须**按块**比而不是整段文本比：整段文本的行多重集相等，只能证明
    「两段文本里的行一样多」，证明不了「每行属于同一个类」—— 一行从类 A 的
    块搬到类 B 的块时，整段多重集仍然相等，但事实归属变了（列挂到了别的表上）。
    ⇒ 逐块比 ⇒ 错位会被算成**内容差异**，不会被「内容差异 = 0」掩盖。

    两侧渲染的是同一批事实（类 / 列 / JOIN / 口径摘要）。A4 之前类内列序
    不同（治理表侧物理行序 vs 快照侧 propertyName）；A4 起渲染器两侧统一按
    共享排序键排序 ⇒ 正常应逐字节一致，出现顺序差异即回归。
    分类逻辑保留：它同时服务历史报告与回归定位。
    把「丢了内容」和「顺序不同」混成一个指标，就会把排序问题误报成
    数据缺失 —— 这正是 D13 要避免的误判。
    """
    total = 0
    directBlocks = _splitBlocks(directLines)
    snapshotBlocks = _splitBlocks(snapshotLines)
    for header in _blockHeaders(directBlocks, snapshotBlocks):
        total += _multisetGap(
            directBlocks.get(header, []), snapshotBlocks.get(header, [])
        )
    return total


def _multisetGap(left: list[str], right: list[str]) -> int:
    """两个行多重集之间的差集行数（多出 + 缺少）。"""
    onlyLeft = Counter(left) - Counter(right)
    onlyRight = Counter(right) - Counter(left)
    return sum(onlyLeft.values()) + sum(onlyRight.values())


def _blockHeaders(
    directBlocks: dict[str, list[str]], snapshotBlocks: dict[str, list[str]]
) -> list[str]:
    """两侧出现过的块标题（去重、**有序**：先直读侧、再快照侧新增的）。"""
    headers = list(directBlocks.keys())
    headers.extend(header for header in snapshotBlocks if header not in headers)
    return headers


def _orderOnlyClasses(directLines: list[str], snapshotLines: list[str]) -> list[str]:
    """仅「类内列序」不同的类清单（内容相同、顺序不同）。

    按 `### ` 标题切块，逐类比较块内行多重集：内容相同但顺序不同 ⇒ 归因到列序。
    """
    directBlocks = _splitBlocks(directLines)
    snapshotBlocks = _splitBlocks(snapshotLines)
    differing: list[str] = []
    for header, block in directBlocks.items():
        other = snapshotBlocks.get(header)
        if other is None:
            continue
        if block != other and Counter(block) == Counter(other):
            differing.append(header)
    return differing


def _blockContentMismatches(directLines: list[str], snapshotLines: list[str]) -> list[str]:
    """**逐块**比较内容（行多重集）：列出内容不等价的块标题（D14 归因缺口）。

    为什么全局「行多重集相等」不够：一行从类 A 的块搬到类 B 的块，全局多重集
    仍然相等，但事实归属变了（列挂到了别的表上）⇒ 那不是顺序差异，是内容差异。
    ⇒ D14「顺序差异 = 已知差异」的结论必须建立在**逐块内容都相等**之上。

    返回的标题为空串时表示「首个 `### ` 之前的公共段落」内容不等价。
    """
    directBlocks = _splitBlocks(directLines)
    snapshotBlocks = _splitBlocks(snapshotLines)
    mismatches: list[str] = []
    for header in _blockHeaders(directBlocks, snapshotBlocks):
        left = directBlocks.get(header, [])
        right = snapshotBlocks.get(header, [])
        if Counter(left) != Counter(right):
            mismatches.append(header)
    return mismatches


def _orderBlockKind(header: str) -> str:
    """顺序差异块的归类。

    - ``类块``：一个本体类的块（标题带 ``: table=``）—— 块内是「列清单」，
      顺序差异 = **类内列序**；
    - ``JOIN 段``：``### JOIN 关系`` / ``### 间接 JOIN 路径参考`` —— 块内是
      「边清单」，顺序差异 = **JOIN 条目序**；
    - ``公共段落`` / ``其它``：**不在**已知差异清单里（公共前言、摘要段、
      未来新增的段落）⇒ 它们的顺序差异进**归因缺口** ⇒ 退出码 1。

    归类只承认两类（已知差异清单封闭）：多出第三类 = 有没归因掉的差异
    ⇒ 那不是顺序问题 ⇒ 归因缺口。
    """
    if not header:
        return _ORDER_KIND_PREAMBLE
    if header.startswith(_JOIN_SECTION_PREFIXES):
        return _ORDER_KIND_JOIN
    if _CLASS_HEADER_MARKER in header:
        return _ORDER_KIND_CLASS
    return _ORDER_KIND_OTHER


def _orderAttribution(directLines: list[str], snapshotLines: list[str]) -> dict:
    """顺序差异的**归因**（D14 的顺序判据）。

    返回 ``{"byKind": {类别: 块数}, "gaps": [块标题], "orderOnly": [块标题]}``：

    - ``byKind``：**落在已知差异清单内**的纯顺序差异块，按类别计数
      （类块 / JOIN 段；另含「块顺序」这一项，见 ``_blockHeaderOrderGap``）；
    - ``gaps``：**归因缺口** —— ① 块级**内容**不等价的块（差异不是顺序造成的）；
      ② 纯顺序差异但**落在已知差异清单外**的块（摘要段 / 未来新增段落 /
      公共段落）—— 两者都 ⇒ 退出码 1 而不是 3。

    为什么必须归因：D14 要回答的是「切到快照路径后 prompt 变了什么」。
    只报「顺序敏感差异 N 行」回答不了这个问题 —— 那个数字不区分
    「类内列序变了」和「JOIN 条目序变了」，两者的影响面不同。
    """
    gaps = _blockContentMismatches(directLines, snapshotLines)
    orderOnly = _orderOnlyClasses(directLines, snapshotLines)
    byKind: dict[str, int] = {}
    for header in orderOnly:
        kind = _orderBlockKind(header)
        if kind not in _ACCEPTED_ORDER_KINDS:
            # 不在已知差异清单里的顺序差异 ⇒ 归因缺口（逼人显式决定）
            if header not in gaps:
                gaps.append(header)
            continue
        byKind[kind] = byKind.get(kind, 0) + 1
    blockOrderGap = _blockHeaderOrderGap(directLines, snapshotLines)
    if blockOrderGap:
        byKind[_ORDER_KIND_BLOCK_ORDER] = blockOrderGap
    return {"byKind": byKind, "gaps": gaps, "orderOnly": orderOnly}


def _blockHeaderOrderGap(directLines: list[str], snapshotLines: list[str]) -> int:
    """块**之间**的顺序差了多少个位置（D14 的第三类顺序差异）。

    为什么单独一类：块内列序变的是「同一个类的列谁先谁后」，块顺序变的是
    「哪个类先出现」—— 后者会改变模型看到的重点顺序，影响面更大，
    不许混进「类内列序」里被稀释掉。

    计数口径 = 两侧块标题序列**位置上不一致**的块数。
    """
    directHeaders = list(_splitBlocks(directLines).keys())
    snapshotHeaders = list(_splitBlocks(snapshotLines).keys())
    gap = 0
    for left, right in zip(directHeaders, snapshotHeaders, strict=False):
        if left != right:
            gap += 1
    gap += abs(len(directHeaders) - len(snapshotHeaders))
    return gap


def _splitBlocks(lines: list[str]) -> dict[str, list[str]]:
    """按 `### ` 标题把文本切成块；标题前的公共段落归到 "" 键。"""
    blocks: dict[str, list[str]] = {}
    current = ""
    blocks.setdefault(current, [])
    for line in lines:
        if line.startswith("### "):
            current = line
            blocks.setdefault(current, [])
            continue
        blocks[current].append(line)
    return blocks


def _renderReport(
    *,
    snapshot,
    subsetResults: list[dict],
    subsetMode: str,
    totalContent: int,
    totalOrder: int,
    totalBlockGaps: int,
    totalBlockOrderGap: int,
    directNames: list[str],
) -> str:
    lines: list[str] = []
    lines.append("# 语义发布影子比对报告")
    lines.append("")
    lines.append(f"- 发布版本：{snapshot.version}（id={snapshot.id}）")
    lines.append(f"- manifest_hash：{snapshot.manifest_hash}")
    lines.append(f"- 类集合：两侧一致，共 {len(directNames)} 类")
    lines.append(f"- 子集模式：{subsetMode}（比对子集 {len(subsetResults)} 个）")
    lines.append("")
    lines.append("## 差异指标")
    lines.append("")
    lines.append(
        f"- **内容差异行数：{totalContent}**（**按类块**比行多重集；>0 = 快照丢了事实"
        "或事实挂到了别的类下）"
    )
    lines.append(f"- 顺序敏感差异行数：{totalOrder}（逐行序列；含纯顺序差异）")
    orderOnlyTotal = sum(len(item["orderOnly"]) for item in subsetResults)
    lines.append(
        f"- 仅顺序不同的块：{orderOnlyTotal}"
        "（落在已知差异清单外的块会计入下面的「归因缺口」）"
    )
    byKind: dict[str, int] = {}
    for item in subsetResults:
        for kind, count in item["orderAttribution"]["byKind"].items():
            byKind[kind] = byKind.get(kind, 0) + count
    kindText = "、".join(f"{kind} {count}" for kind, count in sorted(byKind.items()))
    lines.append(
        f"- 顺序差异归因：{kindText or '（无）'}"
        "（类块 = 类内列序；JOIN 段 = JOIN 条目序；已知差异清单**封闭**，只有这两类）"
    )
    lines.append(
        f"- 顺序差异归因缺口：{totalBlockGaps}"
        "（块级内容不一致，或顺序差异落在已知差异清单外；**必须为 0**，"
        "否则差异不只是列序问题）"
    )
    lines.append(
        f"- 块顺序差：{totalBlockOrderGap}"
        "（块与块之间的先后差；**必须为 0** —— 块顺序变 = 哪个类先出现变了，"
        "不在 D14 接受的已知差异清单里）"
    )
    lines.append("")
    if totalContent == 0:
        lines.append(
            "内容差异 = 0 ✅（两侧**逐类块**渲染出的行集合一致；"
            "同一行的归属也一致）"
        )
    else:
        lines.append(f"内容差异 = {totalContent} ❌")
        for item in subsetResults:
            if item["contentDiff"] == 0:
                continue
            lines.append("")
            lines.append(f"### 子集 `{item['label']}`（内容差异 {item['contentDiff']} 行）")
            lines.append("")
            lines.append("类：" + "、".join(item["names"]))
            lines.append("")
            lines.append("```diff")
            lines.extend(item["diffLines"][:_MAX_DIFF_LINES])
            if len(item["diffLines"]) > _MAX_DIFF_LINES:
                lines.append(f"...（已截断，完整差异 {len(item['diffLines'])} 行）")
            lines.append("```")
    if orderOnlyTotal:
        lines.append("")
        lines.append(
            "顺序差异归因（A4 前为已知差异，A4 起**属回归**）：A4 之前治理表侧"
            "属性顺序 = 物理行序（`OntologyClass.properties` 关系**没有 ORDER BY**）、"
            "JOIN 顺序 = 自增 id；快照侧 = propertyName / joinKey 排序。"
            "A4 起渲染器两侧共用 `propertyNameSortKey` / `joinKeySortKey` ⇒ "
            "本段非空说明有一侧退回了原顺序或用错了键。"
            f"受影响块：{kindText or '（无）'}。"
        )
    return "\n".join(lines) + "\n"


def _write(path: str, content: str) -> None:
    """写报告/基线文件：0600 + **不跟随符号链接**。

    报告与基线里是**生产元数据**（类名 / 表名 / 列名）⇒ 权限收紧到 0600；
    ``write_text`` 会**跟随符号链接** ⇒ 预先放一个软链就能把内容写到别处
    ⇒ 改用 ``O_NOFOLLOW``（命中软链直接失败）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        raise SystemExit(f"拒绝写入（目标是符号链接或不可写）：{target}") from exc
    # 权限在**写入之前**收紧：既有文件若是 0644，先 chmod 再写，
    # 否则「写完之后再 chmod」的窗口里内容是可被其他用户读的（安全评审 L-1）。
    try:
        os.fchmod(fd, 0o600)
    except OSError as exc:
        os.close(fd)
        raise SystemExit(f"拒绝写入（无法收紧文件权限）：{target}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="语义发布影子比对：快照 vs 直读 schema 文本")
    parser.add_argument(
        "--database-url", default=None,
        help="目标库 DSN（显式；口令建议改用 --database-url-env）",
    )
    parser.add_argument(
        "--database-url-env", default=None,
        help="从该环境变量读目标库 DSN（口令不进 argv）",
    )
    parser.add_argument(
        "--subset-mode", default="none", choices=["none", "each", "groups"],
        help="要比的子集口径：none=全集（唯一**完整**覆盖 JOIN 段）/ each=逐类（不覆盖 JOIN 段）/ groups=定长分组（只覆盖同组内的 JOIN）",
    )
    parser.add_argument(
        "--baseline-out", default=None,
        help="把直读侧**全集**文本写成基线 fixture（与子集模式无关）",
    )
    parser.add_argument("--report-out", default=None, help="把比对报告写成文件")
    parser.add_argument(
        "--allow-prod-target", action="store_true",
        help="允许以正式库 qa_metadata（prod 同源）为比对目标（本脚本只读）",
    )
    args = parser.parse_args()

    databaseUrl = resolveDatabaseUrl(args.database_url, args.database_url_env)
    targetDb = assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_target)
    print(f"目标库：{targetDb} @ {maskDatabaseUrl(databaseUrl)}")
    return asyncio.run(_run(args, databaseUrl, targetDb))


if __name__ == "__main__":
    raise SystemExit(main())
