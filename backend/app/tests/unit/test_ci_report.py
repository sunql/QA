"""合并判据报告的渲染守卫。

报告是**人读的唯一结论** ⇒ 它的三条纪律必须被钉死：

1. **每一个**插入的外部字符串都过净化（控制字符 / ANSI / OSC 注入）；
2. **行数有上界** —— 失败用例 ID 的条数由 PR 作者控制，
   无上界就能用几百行 `+` 把结论埋掉（结论在第 1 行，但只看尾巴的人会被骗）；
3. **被净化/脱敏过的 ID 要带指纹** —— 否则两个不同的原始 ID 会渲染成同一个可读串
   ⇒ 报告无法区分「同一个用例」与「两个长得一样的用例」。
"""

from __future__ import annotations

from datetime import date

from app.utils.ci_failure_set import (
    CiJob,
    JobAssessment,
    Verdict,
    diffFailureSets,
)
from app.utils.ci_flakes import ExemptionOutcome, FlakeEntry, applyFlakeExemptions
from app.utils.ci_report import MAX_REPORT_BYTES, renderReport


def _assessment(name: str = "Backend / unit shard 0") -> JobAssessment:
    return JobAssessment(
        job=CiJob(jobId=1, name=name, conclusion="failure"),
        marker=None,
        failureIds=(),
        isUsable=True,
        unusableReason=None,
        hasFailureWithoutDetail=False,
        markerCount=0,
    )


def _render(
    added: tuple[str, ...] = (),
    removed: tuple[str, ...] = (),
    notes: tuple[str, ...] = (),
) -> str:
    baseFailures = frozenset({"app/tests/unit/test_a.py::test_x"})
    prFailures = frozenset(baseFailures | set(added))
    diff = diffFailureSets(baseFailures, prFailures)
    return renderReport(
        "111",
        "222",
        baseFailures,
        prFailures,
        diff,
        [_assessment()],
        [_assessment()],
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
        notes,
    )


def test_report_adds_fingerprint_when_failure_id_was_sanitized() -> None:
    """被净化过的 ID 必须带 sha256 指纹 ⇒ 两个原始 ID 不会渲染成同一个串。"""
    controlCharId = "app/tests/unit/test_a.py::test_x\x1b[31m"
    otherControlCharId = "app/tests/unit/test_a.py::test_y\x1b[31m"

    report = _render(added=(controlCharId, otherControlCharId))

    assert "sha256:" in report
    fingerprints = [line for line in report.splitlines() if "sha256:" in line]
    assert len(fingerprints) == 2
    assert fingerprints[0] != fingerprints[1]


def test_report_keeps_plain_failure_id_untouched() -> None:
    """没被净化过的 ID 不加指纹（指纹只在**需要**区分时才出现）。"""
    report = _render(added=("app/tests/unit/test_new.py::test_y",))

    assert "sha256:" not in report
    assert "app/tests/unit/test_new.py::test_y" in report


def test_report_caps_total_line_count() -> None:
    """报告总行数有上界 ⇒ 几百个 job 也埋不掉结论。

    （失败 ID 清单另有一层 50 条上限 ⇒ 这里用「很多 job」把总行数顶上去，
    验的是**第二层**上限：`_MAX_REPORT_LINES`。）
    """
    manyJobs = [
        JobAssessment(
            job=CiJob(jobId=index, name=f"Backend / unit shard {index}", conclusion="failure"),
            marker=None,
            failureIds=(),
            isUsable=True,
            unusableReason=None,
            hasFailureWithoutDetail=False,
            markerCount=0,
        )
        for index in range(300)
    ]
    baseFailures = frozenset({"app/tests/unit/test_a.py::test_x"})
    diff = diffFailureSets(baseFailures, baseFailures)
    report = renderReport(
        "111",
        "222",
        baseFailures,
        baseFailures,
        diff,
        manyJobs,
        manyJobs,
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
    )

    lines = report.splitlines()
    assert len(lines) <= 210
    assert "报告已截断" in report
    # 结论必须在**头部**（截断不能把结论切掉）。
    assert lines[0].startswith("结论：")


def test_report_truncates_failure_id_list() -> None:
    """清单过长时只列前若干条 + 明示剩余条数（不是静默丢弃）。"""
    added = tuple(f"app/tests/unit/test_{index}.py::test_x" for index in range(200))

    report = _render(added=added)

    assert "另有" in report
    assert "未列出" in report
    listed = [line for line in report.splitlines() if "::test_x" in line]
    assert len(listed) <= 51


def test_report_shows_notes() -> None:
    """`notes`（fork PR 提示等）必须在报告里显式出现。"""
    report = _render(notes=("PR 来自 fork 仓库，无法自动核对 run 是否最新，请人工确认",))

    assert "提示：" in report
    assert "请人工确认" in report


def test_report_never_prints_control_sequences() -> None:
    """失败用例 ID 里的 ANSI/OSC 序列必须被剔除（否则可擦除已打印的结论行）。"""
    report = _render(added=("app/tests/unit/test_a.py::test_x\x1b[2J\x07",))

    assert "\x1b" not in report
    assert "\x07" not in report


# ---------------------------------------------------------------------------
# 已知抖动豁免（B7）：豁免必须**出现在报告里**（不是静默放行）
# ---------------------------------------------------------------------------

_FLAKE_NODE_ID = "app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent"


def _renderWithExemptions(
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    entries: list[FlakeEntry],
) -> str:
    """按「先豁免、再对差」的顺序渲染报告（与闸门真实顺序一致）。

    ⚠️ `rawBaseCount` / `rawPrCount` **必须显式传**（豁免**之前**的条数）——
    渲染层不做「加法还原」（还原是隐含契约：将来谁把原始集合传进来，
    头部条数就会虚高）。
    """
    outcome = applyFlakeExemptions(baseFailures, prFailures, entries)
    return renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        [_assessment()],
        [_assessment()],
        Verdict(canMerge=True, blockingReason=None, reasonCode="MERGEABLE"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )


def test_report_shows_flake_exemption_section() -> None:
    """豁免段必须出现，且逐条给出 node ID / 出现侧 / 理由。"""
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"}),
        frozenset({"app/tests/unit/test_a.py::test_x"}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "已知抖动豁免" in report
    assert _FLAKE_NODE_ID in report
    assert "基线侧" in report
    assert "Milvus 可见性竞态" in report


def test_report_warns_when_pr_side_hits_flake() -> None:
    """PR 侧命中已知抖动用例 ⇒ 必须 ⚠ 明示（对称豁免的代价是「PR 侧红也不拦」）。

    这条是**对称豁免**这个设计取舍的兜底：结论不再拦人，但**人必须看得见**。
    """
    report = _renderWithExemptions(
        frozenset({"app/tests/unit/test_a.py::test_x"}),
        frozenset({"app/tests/unit/test_a.py::test_x", _FLAKE_NODE_ID}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "⚠️" in report
    assert "PR 侧" in report


def test_report_shows_raw_and_counted_failure_counts() -> None:
    """头部要同时给「原始失败条数」与「计入对差的条数」——差额由豁免明细解释。"""
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"}),
        frozenset({"app/tests/unit/test_a.py::test_x"}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "计入对差" in report
    assert "失败 2 条" in report
    assert "失败 1 条" in report


def test_report_marks_exemption_hitting_both_sides() -> None:
    """两侧都命中 ⇒ 标注「两侧」（不是只标一侧 ⇒ 让人误判方向）。"""
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID}),
        frozenset({_FLAKE_NODE_ID}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="两侧都抖", since="2026-11-06")],
    )

    assert "两侧" in report


# ---------------------------------------------------------------------------
# 豁免的**可见性**：since / 老化 / 封顶 / 顺序
# ---------------------------------------------------------------------------


def test_report_shows_exemption_since_date() -> None:
    """豁免条目必须显示**签发日期**（`since`）⇒ 复核者看得到「授权了多久」。"""
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID}),
        frozenset(),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "since 2026-11-06" in report


def test_report_warns_when_exemption_is_stale() -> None:
    """签发日期超过老化上限 ⇒ ⚠ 提示复核。

    为什么需要：白名单一旦写下就永久有效 ⇒ 抖动被修好后条目不会自动消失
    ⇒ 此后该用例若发生**确定性**回归，会被永久豁免（两侧都被剔 ⇒ 净差恒为 0）
    ⇒ 这个用例从此**再也不回到判据**。⇒ 必须有一条机制逼人回头看它。
    """
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID}),
        frozenset(),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="很久以前的抖动", since="2020-01-01")],
    )

    assert "⚠️" in report
    assert "老化" in report


def test_report_does_not_warn_for_a_recent_exemption() -> None:
    """反向守卫：没超期就**不**提示老化（否则报告里全是噪音 ⇒ 真警告被淹没）。

    ⚠️ `since` 取**当天**（而不是写死日期）⇒ 与机器时钟无关，恒定可判。
    """
    today = date.today().isoformat()
    report = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID}),
        frozenset(),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="刚签发", since=today)],
    )

    assert "老化" not in report


def test_report_caps_the_exemption_list() -> None:
    """豁免清单过长 ⇒ 只列前若干条 + 明示剩余条数（不静默丢弃）。

    ⚠️ 豁免段每条最多 3 行（条目 + PR 侧 ⚠ + 老化 ⚠）⇒ 白名单越大，越先把
    后面的「净新增失败用例」清单挤出 200 行窗口 ⇒ **失败的证据被豁免的说明挤掉**
    （方向反了）⇒ 必须给豁免段单独封顶。
    """
    entries = [
        FlakeEntry(nodeId=f"app/tests/unit/test_flake_{index}.py::test_x", reason="抖动", since="2026-11-06")
        for index in range(30)
    ]

    report = _renderWithExemptions(
        frozenset({entry.nodeId for entry in entries}),
        frozenset(),
        entries,
    )

    assert "已知抖动豁免 30 条" in report
    assert "另有" in report
    assert "条豁免未列出" in report
    listed = [line for line in report.splitlines() if "test_flake_" in line]
    assert len(listed) <= 21


def _manyJobAssessments(count: int = 300) -> list[JobAssessment]:
    return [
        JobAssessment(
            job=CiJob(jobId=index, name=f"Backend / unit shard {index}", conclusion="failure"),
            marker=None,
            failureIds=(),
            isUsable=True,
            unusableReason=None,
            hasFailureWithoutDetail=False,
            markerCount=0,
        )
        for index in range(count)
    ]


def test_report_lists_new_failures_before_job_details() -> None:
    """「净新增失败用例」清单必须排在 job 明细**之前**。

    ⚠️ 截断是**从尾部砍**的 ⇒ 清单排最后就会被先砍掉 ⇒ 失败的证据被 job 明细
    挤掉 ⇒ 结论看似完整、实际看不到新增了什么。
    """
    outcome = applyFlakeExemptions(
        frozenset({"app/tests/unit/test_a.py::test_x"}),
        frozenset({"app/tests/unit/test_a.py::test_x", "app/tests/unit/test_new.py::test_y"}),
        [],
    )
    report = renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        _manyJobAssessments(),
        _manyJobAssessments(),
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )

    lines = report.splitlines()
    newFailureIndex = next(index for index, line in enumerate(lines) if line.startswith("净新增失败用例"))
    jobDetailIndex = next(index for index, line in enumerate(lines) if "[基线]" in line)

    assert newFailureIndex < jobDetailIndex


def test_report_keeps_evidence_when_truncated() -> None:
    """报告被截断时，**豁免段与净新增失败清单都不得被砍掉**。

    这两条是判据的「证据」：豁免段让人知道「有条目的结论被忽略了」，
    失败清单让人知道「新增了什么」⇒ 砍掉任一条 = 结论不可复核。
    """
    entries = [
        FlakeEntry(nodeId=f"app/tests/unit/test_flake_{index}.py::test_x", reason="抖动", since="2026-11-06")
        for index in range(5)
    ]
    baseFailures = frozenset({"app/tests/unit/test_a.py::test_x"})
    prFailures = baseFailures | {entry.nodeId for entry in entries} | {"app/tests/unit/test_new.py::test_y"}
    outcome = applyFlakeExemptions(baseFailures, prFailures, entries)

    report = renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        _manyJobAssessments(),
        _manyJobAssessments(),
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )

    assert "报告已截断" in report
    assert "已知抖动豁免" in report
    assert "净新增失败用例" in report
    assert "app/tests/unit/test_new.py::test_y" in report


# ---------------------------------------------------------------------------
# 2026-11-06 代码评审 M-1 / L-1 / L-2：报告的**字节窗口**与**证据可见性**
# ---------------------------------------------------------------------------


def test_report_keeps_the_whole_new_failure_list_next_to_a_long_exemption_section() -> None:
    """白名单变长 + job 变多时，**净新增失败清单必须一条不少地留在报告里**。

    ⚠️ 2026-11-06 代码评审 M-1（实测）：闸门把报告按 **8000 字节**塞进 check 摘要，
    而豁免段在**净新增清单之前** ⇒ 白名单长到 12 条时「净新增失败用例：」落在
    7284 字节处 ⇒ 清单被切断；到 20 条时整段落在窗口之外 ⇒ 摘要里**只字未提**新增了什么
    ⇒ 操作者以为「就这么几条」。

    ⇒ 三条硬要求：① 净新增清单排在豁免段**之前**（证据优先于说明）；
    ② 报告整体有**字节上限**且上限 ≤ 摘要窗口 ⇒ 截断由工具**明示**、不静默；
    ③ 截断提示必须在**头部**（尾部会被一起砍掉 ⇒ 等于没提示）。
    """
    entries = [
        FlakeEntry(
            nodeId=f"app/tests/unit/test_flake_{index}.py::test_x",
            reason="抖动" * 60,
            since="2026-11-06",
        )
        for index in range(20)
    ]
    added = tuple(f"app/tests/unit/test_new_{index}.py::test_y" for index in range(50))
    baseFailures = frozenset({entry.nodeId for entry in entries})
    prFailures = baseFailures | set(added)
    outcome = applyFlakeExemptions(baseFailures, prFailures, entries)

    report = renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        _manyJobAssessments(),
        _manyJobAssessments(),
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )

    lines = report.splitlines()
    assert "净新增失败用例" in report
    for nodeId in added:
        assert nodeId in report, f"净新增清单被挤掉了：{nodeId}"
    # 证据必须排在豁免说明**之前** ⇒ 尾部截断时最后被砍的是说明，不是证据。
    assert next(i for i, line in enumerate(lines) if line.startswith("净新增失败用例")) < next(
        i for i, line in enumerate(lines) if "已知抖动豁免" in line
    )


def _byteHeavyScenario() -> tuple[tuple[FlakeEntry, ...], tuple[str, ...], ExemptionOutcome]:
    """造一个「**字节**超限、但**行数**不超限」的场景。

    ⚠️ 这是**字节上限**那条守卫能否生效的关键：若行数也超限，
    则「行数上限」会先触发 ⇒ 报告照样被截断、照样有头部提示
    ⇒ 「字节上限被去掉」这条变异**照样绿** ⇒ 守卫假绿
    （2026-11-06 变异取证实证：旧 fixture 是 19810 字节 / 198 行
    ⇒ 字节上限与行数上限**几乎同时**触发 ⇒ 断言分辨不出是哪一个）。

    ⇒ 这里的 **node ID 与理由都很长**（每个字段被 `sanitizeForReport`
    截到 200 字符 ⇒ 一行约 800 字节）⇒ 20 条豁免就超过 20000 字节；
    而**行数**刻意压在 200 行以下（⇒ 行数上限不触发）
    ⇒ 只有字节上限能解释「为什么被截断」。
    """
    entries = [
        FlakeEntry(
            nodeId=f"app/tests/unit/test_flake_{index}_" + ("x" * 170) + ".py::test_y",
            reason="抖动理由" * 400,
            since="2026-11-06",
        )
        for index in range(20)
    ]
    added = tuple(
        f"app/tests/unit/test_new_{index}_" + ("y" * 170) + ".py::test_z" for index in range(50)
    )
    baseFailures = frozenset({entry.nodeId for entry in entries})
    prFailures = baseFailures | set(added)
    return entries, added, applyFlakeExemptions(baseFailures, prFailures, entries)


def test_report_is_capped_in_bytes_and_marks_the_truncation_in_the_header() -> None:
    """报告有**字节上限**，且截断提示在**头部**（不是尾部 ⇒ 不会跟内容一起被砍掉）。"""
    _, _, outcome = _byteHeavyScenario()

    report = renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        _manyJobAssessments(2),
        _manyJobAssessments(2),
        Verdict(canMerge=False, blockingReason="净新增失败", reasonCode="NEW_FAILURES"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )

    assert len(report.encode("utf-8")) <= MAX_REPORT_BYTES
    lines = report.splitlines()
    assert lines[0].startswith("结论：")
    assert "报告已截断" in lines[1], f"截断提示必须在头部（第 2 行），实际第 2 行是：{lines[1]!r}"
    # ⚠️ 必须指明是**字节**上限触发的 ⇒ 否则「行数上限先触发」也能满足本用例
    # ⇒ 「去掉字节上限」这条变异会照样绿 ⇒ 守卫假绿（2026-11-06 变异取证实证）。
    assert "字节上限" in lines[1], f"截断提示必须指明字节上限，实际第 2 行是：{lines[1]!r}"
    # 前提自检：行数**没有**超限 ⇒ 唯一可能的截断原因是字节上限。
    assert len(lines) <= 200, f"这个场景本该只触发字节上限，实际行数 {len(lines)}"



def test_report_reconciles_the_per_job_failure_count_with_the_exemptions() -> None:
    """逐 job 的「失败 N 条」必须与豁免**对得上账**。

    ⚠️ 2026-11-06 代码评审 L-2：逐 job 的失败数是**原始**条数（含被豁免的），
    而头部是「失败 N 条（计入对差 M 条）」⇒ 两处数字对不上 ⇒ 读者无法核账
    ⇒ 只能选择「不信其中之一」。
    ⇒ 逐 job 行必须标出「其中 N 条是已知抖动豁免」。
    """
    assessment = JobAssessment(
        job=CiJob(jobId=1, name="Backend / unit shard 0", conclusion="failure"),
        marker=None,
        failureIds=(_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"),
        isUsable=True,
        unusableReason=None,
        hasFailureWithoutDetail=False,
        markerCount=0,
    )
    baseFailures = frozenset({_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"})
    prFailures = baseFailures
    entries = [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")]
    outcome = applyFlakeExemptions(baseFailures, prFailures, entries)

    report = renderReport(
        "111",
        "222",
        outcome.baseFailures,
        outcome.prFailures,
        diffFailureSets(outcome.baseFailures, outcome.prFailures),
        [assessment],
        [assessment],
        Verdict(canMerge=True, blockingReason=None, reasonCode="MERGEABLE"),
        "a" * 40,
        "b" * 40,
        (),
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
    )

    jobLine = next(line for line in report.splitlines() if "[基线]" in line)
    assert "含已知抖动豁免 1 条" in jobLine, jobLine


def test_report_warns_only_when_a_flake_is_newly_red_on_the_pr_side() -> None:
    """「⚠️ PR 侧命中」只对 **PR 侧新增红** 触发（两侧都红是抖动的常见形态）。

    ⚠️ 2026-11-06 代码评审 L-1：两侧都命中（基线红、PR 也红）是最典型的抖动形态
    ⇒ 却也被算成「PR 侧命中」⇒ 对**每次**抖动都报「请人工确认不是确定性回归」
    ⇒ 告警变成噪音 ⇒ 真告警被淹没。

    ⇒ 真正危险的只有「基线不红、PR 红了」——那才可能是被掩盖的确定性回归。
    """
    bothSides = _renderWithExemptions(
        frozenset({_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"}),
        frozenset({_FLAKE_NODE_ID, "app/tests/unit/test_a.py::test_x"}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "请人工确认不是确定性回归" not in bothSides

    prOnly = _renderWithExemptions(
        frozenset({"app/tests/unit/test_a.py::test_x"}),
        frozenset({"app/tests/unit/test_a.py::test_x", _FLAKE_NODE_ID}),
        [FlakeEntry(nodeId=_FLAKE_NODE_ID, reason="Milvus 可见性竞态", since="2026-11-06")],
    )

    assert "请人工确认不是确定性回归" in prOnly
