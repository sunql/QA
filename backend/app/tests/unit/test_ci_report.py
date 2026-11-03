"""合并判据报告的渲染守卫。

报告是**人读的唯一结论** ⇒ 它的三条纪律必须被钉死：

1. **每一个**插入的外部字符串都过净化（控制字符 / ANSI / OSC 注入）；
2. **行数有上界** —— 失败用例 ID 的条数由 PR 作者控制，
   无上界就能用几百行 `+` 把结论埋掉（结论在第 1 行，但只看尾巴的人会被骗）；
3. **被净化/脱敏过的 ID 要带指纹** —— 否则两个不同的原始 ID 会渲染成同一个可读串
   ⇒ 报告无法区分「同一个用例」与「两个长得一样的用例」。
"""

from __future__ import annotations

from app.utils.ci_failure_set import (
    CiJob,
    JobAssessment,
    Verdict,
    diffFailureSets,
)
from app.utils.ci_report import renderReport


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
