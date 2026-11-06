"""merge-gate 的「已知抖动白名单」：加载、校验、豁免计算。

背景（PR #107 实证，2026-11-06）
--------------------------------
闸门判据是「失败集合对差，净新增为空才合并」⇒ **任何一个用例在两侧的「失败与否」不同，
就一律拦下**。而 `test_backfill_is_idempotent` 因 Milvus 可见性竞态**随机红**
⇒ 同代码不同结论 ⇒ 闸门随机挡掉约一半的 PR ⇒ 每次都要人工做三件套证伪。

⇒ 白名单机制：让「结论不确定的用例」**不再充当判据**。

本文件钉死三条硬约束（安全栏）：

1. **精确 node ID**：只豁免清单里那**一个**用例，不豁免文件、不豁免前缀、不豁免通配；
2. **失败关闭**：白名单读不到 / JSON 非法 / 条目不合规 ⇒ 抛错 ⇒ 闸门退出码 3，
   **绝不**退化成「空白名单」（判据读不出来 ⇒ 不知道 ≠ 没有 ⇒ 保守拦下）；
3. **只许 main 上 CODEOWNER 维护**：白名单文件属于「CI 相关面」（见 `test_ci_surface.py`），
   且闸门从**默认分支的 checkout** 读它 ⇒ PR 不能给自己开豁免。

豁免的接线与报告在 `test_ci_failure_set.py` / `test_ci_report.py`。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.utils import ci_flakes
from app.utils.ci_flakes import (
    FlakeEntry,
    FlakeWhitelistError,
    applyFlakeExemptions,
    loadFlakeWhitelist,
)

_VALID_ENTRY = {
    "nodeId": "app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent",
    "reason": "Milvus 可见性竞态：backfill 删旧行后重插，第二次 backfill 可能查到旧行",
    "since": "2026-11-06",
}


def _writeWhitelist(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# T1：加载与校验
# ---------------------------------------------------------------------------


def test_loadFlakeWhitelist_reads_valid_entries(tmp_path: Path) -> None:
    """合法白名单能读出来，字段原样保留。"""
    path = _writeWhitelist(tmp_path / "whitelist.json", {"entries": [_VALID_ENTRY]})

    entries = loadFlakeWhitelist(path)

    assert len(entries) == 1
    assert entries[0].nodeId == _VALID_ENTRY["nodeId"]
    assert entries[0].reason == _VALID_ENTRY["reason"]
    assert entries[0].since == "2026-11-06"


def test_loadFlakeWhitelist_accepts_empty_entry_list(tmp_path: Path) -> None:
    """空清单合法（还没有任何已知抖动时）。"""
    path = _writeWhitelist(tmp_path / "whitelist.json", {"entries": []})

    assert loadFlakeWhitelist(path) == ()


def test_loadFlakeWhitelist_rejects_missing_file(tmp_path: Path) -> None:
    """**文件不存在** ⇒ 失败关闭（不是「空白名单」）。

    白名单是判据的一部分 ⇒ 读不出来 ⇒「不知道」不等于「没有」⇒ 保守拦下。
    """
    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(tmp_path / "not-there.json")

    assert "不存在" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_invalid_json(tmp_path: Path) -> None:
    """JSON 非法 ⇒ 失败关闭。"""
    path = tmp_path / "whitelist.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "JSON" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_non_object_payload(tmp_path: Path) -> None:
    """顶层不是对象 ⇒ 失败关闭。"""
    path = _writeWhitelist(tmp_path / "whitelist.json", ["a", "b"])

    with pytest.raises(FlakeWhitelistError):
        loadFlakeWhitelist(path)


def test_loadFlakeWhitelist_rejects_entry_without_node_id(tmp_path: Path) -> None:
    """缺 `nodeId` ⇒ 失败关闭。"""
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{"reason": "某个原因"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "nodeId" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_blank_reason(tmp_path: Path) -> None:
    """`reason` 为空 ⇒ 失败关闭。

    理由是**审计依据**（CODEOWNER 复核时要看「为什么信它」）⇒ 空理由等于无授权。
    """
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{"nodeId": "app/tests/x.py::test_y", "reason": "   "}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "reason" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_file_level_node_id(tmp_path: Path) -> None:
    """**收集期错误**的 ID（文件级、无 `::`）不接受。

    文件级 ID 会把**整个文件**的失败都豁免掉 ⇒ 爆炸半径从 1 个用例放大到 1 个文件
    ⇒ 违反「精确 node ID」约束 ⇒ 宁可漏豁免，不可错豁免。
    """
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{"nodeId": "app/tests/integration/test_x.py", "reason": "原因"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "node ID" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_wildcard_node_id(tmp_path: Path) -> None:
    """通配（`*`）不接受。"""
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{"nodeId": "app/tests/integration/test_x.py::*", "reason": "原因"}]},
    )

    with pytest.raises(FlakeWhitelistError):
        loadFlakeWhitelist(path)


def test_loadFlakeWhitelist_rejects_duplicate_node_id(tmp_path: Path) -> None:
    """重复 `nodeId` ⇒ 失败关闭（歧义 ⇒ 人工确认）。"""
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [_VALID_ENTRY, _VALID_ENTRY]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "重复" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_bad_since_format(tmp_path: Path) -> None:
    """`since` 给了就必须是 `YYYY-MM-DD`。"""
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{**_VALID_ENTRY, "since": "去年"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "since" in str(excinfo.value)


def test_defaultWhitelistPath_points_at_github_dir() -> None:
    """默认路径落在**仓库根**的 `.github/` 下（与 CODEOWNERS 同处 ⇒ 授权面一眼可见）。

    ⚠️ 2026-11-06 代码评审 M-7：旧守卫只断言**最后两段**（`name` + `parent.name`）
    ⇒ 任何 `<任意目录>/.github/ci-known-flaky-tests.json` 都能蒙混过关
    ⇒ 「默认路径 = 仓库根的 `.github/`」这条属性其实**没被钉住**
    ⇒ 而「PR 不能给自己开豁免」整条链就架在这个默认路径上
    ⇒ 必须断言**整个父目录 == 仓库根/.github**（不是「末两段像」）。
    """
    path = ci_flakes.defaultWhitelistPath()
    repoRoot = Path(__file__).resolve().parents[4]

    assert path.name == "ci-known-flaky-tests.json"
    assert path.parent == repoRoot / ".github", (
        f"默认白名单路径不在仓库根的 .github 下：{path}"
        " ⇒ 闸门读到的可能不是仓库里那份白名单"
    )


def test_repo_whitelist_file_is_valid_and_non_empty() -> None:
    """**仓库里真实的白名单文件**必须能通过加载器的全部校验。

    这条守卫把「数据文件本身」也纳入 TDD：改了格式而忘了改加载器 ⇒ 这里直接转红。

    ⚠️ **不要求非空**（2026-11-06 安全审查 L-3）：白名单条目会随抖动被修复
    而逐条清理 ⇒ 若守卫要求非空，清理到最后一条时守卫自己转红
    ⇒ 「清理」这件事反而需要先改守卫 ⇒ 大家就不敢清理了。
    ⇒ 只校验「格式合法」；条目数量是**人的判断**，不是守卫的判断。
    """
    entries = loadFlakeWhitelist(ci_flakes.defaultWhitelistPath())

    for entry in entries:
        assert "::" in entry.nodeId
        assert entry.reason.strip()


# ---------------------------------------------------------------------------
# T2：豁免计算
# ---------------------------------------------------------------------------

_FLAKY = "app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent"
_STABLE = "app/tests/unit/test_chat_service.py::test_reply"


def _entry(nodeId: str = _FLAKY, reason: str = "抖动原因") -> FlakeEntry:
    return FlakeEntry(nodeId=nodeId, reason=reason, since="2026-11-06")


def test_applyFlakeExemptions_removes_flaky_from_base_only() -> None:
    """基线红 / PR 绿 ⇒ 白名单用例被剔除 ⇒ **两侧集合相等**（不再有净丢失）。"""
    outcome = applyFlakeExemptions(frozenset({_FLAKY, _STABLE}), frozenset({_STABLE}), [_entry()])

    assert outcome.baseFailures == frozenset({_STABLE})
    assert outcome.prFailures == frozenset({_STABLE})
    assert len(outcome.exemptions) == 1
    assert outcome.exemptions[0].nodeId == _FLAKY
    assert outcome.exemptions[0].inBase is True
    assert outcome.exemptions[0].inPr is False


def test_applyFlakeExemptions_removes_flaky_from_pr_only() -> None:
    """基线绿 / PR 红 ⇒ 同样豁免（**对称**语义：结论是噪声 ⇒ 不作证据）。

    这是「对称 vs 单向」的设计取舍点：白名单断言的是「**这个用例的结论不是证据**」，
    而不是「这个用例的失败只在某个方向上可忽略」。
    """
    outcome = applyFlakeExemptions(frozenset({_STABLE}), frozenset({_FLAKY, _STABLE}), [_entry()])

    assert outcome.baseFailures == frozenset({_STABLE})
    assert outcome.prFailures == frozenset({_STABLE})
    assert outcome.exemptions[0].inBase is False
    assert outcome.exemptions[0].inPr is True


def test_applyFlakeExemptions_removes_flaky_from_both_sides() -> None:
    """两侧都红 ⇒ 也剔除（否则它会变成「两边都有 ⇒ 无差异」，白跑一趟）。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}), frozenset({_FLAKY, _STABLE}), [_entry()]
    )

    assert outcome.baseFailures == frozenset({_STABLE})
    assert outcome.prFailures == frozenset({_STABLE})
    assert outcome.exemptions[0].inBase is True
    assert outcome.exemptions[0].inPr is True


def test_applyFlakeExemptions_keeps_non_whitelisted_failures() -> None:
    """不在白名单里的失败**原样保留**（豁免不是「大范围忽略」）。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}), frozenset({_FLAKY, _STABLE}), [_entry()]
    )

    assert _STABLE in outcome.baseFailures
    assert _STABLE in outcome.prFailures


def test_applyFlakeExemptions_records_no_exemption_when_not_hit() -> None:
    """白名单里有但这次没命中 ⇒ **不产生**豁免记录（报告里就不该出现）。"""
    outcome = applyFlakeExemptions(frozenset({_STABLE}), frozenset({_STABLE}), [_entry()])

    assert outcome.exemptions == ()
    assert outcome.baseFailures == frozenset({_STABLE})


def test_applyFlakeExemptions_does_not_mutate_inputs() -> None:
    """输入集合**不可变** ⇒ 调用后原集合不变（不可变数据纪律）。"""
    base = frozenset({_FLAKY})
    pr = frozenset({_FLAKY})

    applyFlakeExemptions(base, pr, [_entry()])

    assert base == frozenset({_FLAKY})
    assert pr == frozenset({_FLAKY})


def test_applyFlakeExemptions_carries_reason_into_exemption() -> None:
    """豁免记录必须带上**理由**（报告里要给人看 ⇒ 审计依据不能丢）。"""
    outcome = applyFlakeExemptions(frozenset({_FLAKY}), frozenset(), [_entry(reason="可见性竞态")])

    assert outcome.exemptions[0].reason == "可见性竞态"


def test_loadFlakeWhitelist_rejects_entry_that_is_not_an_object(tmp_path: Path) -> None:
    """条目**不是对象**（例如写成字符串）⇒ 失败关闭。

    条目形状不合法 ⇒ 读出来的「白名单」就不是白名单 ⇒ 绝不能当成合法条目跳过。
    """
    path = _writeWhitelist(tmp_path / "whitelist.json", {"entries": ["app/tests/x.py::test_y"]})

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "不是对象" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_entries_that_are_not_a_list(tmp_path: Path) -> None:
    """`entries` **不是数组**（例如写成对象）⇒ 失败关闭。"""
    path = _writeWhitelist(tmp_path / "whitelist.json", {"entries": {"nodeId": "x"}})

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "entries" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_unreadable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件**存在但读不到**（权限 / IO 错误）⇒ 失败关闭（不是「当成空白名单」）。

    `Path.is_file()` 说它存在，`read_text` 却失败 ⇒ 这种情况必须与「不存在」同样处理。
    """
    path = _writeWhitelist(tmp_path / "whitelist.json", {"entries": []})

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        raise OSError("模拟 IO 错误")

    monkeypatch.setattr(Path, "read_text", boom)

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "读不到" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_non_utf8_file(tmp_path: Path) -> None:
    """文件**不是合法 UTF-8** ⇒ 失败关闭（退出码 3，不是裸 traceback）。

    `UnicodeDecodeError` 是 `ValueError` 的子类、**不是** `OSError`
    ⇒ 只捕 `OSError` 时它会逃出加载器 ⇒ 逃出调用方的 `except FlakeWhitelistError`
    ⇒ CLI 以**退出码 1** 结束 ⇒ 而 1 的语义是「净新增失败」⇒ 操作者会查错方向。
    """
    path = tmp_path / "whitelist.json"
    path.write_bytes(b'{"entries": [{"nodeId": "a.py::t\xff", "reason": "x"}]}')

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "读不到" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_node_id_with_trailing_newline(tmp_path: Path) -> None:
    """node ID 末尾带换行 ⇒ 拒绝。

    Python 的 `$` 匹配「**末尾换行之前**」⇒ 用 `^...$` 校验时 `"a.py::b\\n"` 能通过
    ⇒ 但它永远匹配不到任何真实失败 ID ⇒ 白名单条目**静默失效**
    ⇒ 闸门继续随机挡人，而报告里看不出「有条目一直没生效」。
    """
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{**_VALID_ENTRY, "nodeId": _VALID_ENTRY["nodeId"] + "\n"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "node ID" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_entry_without_since(tmp_path: Path) -> None:
    """`since` **必填**：它是这条授权的签发日期，也是老化复核的锚点。

    缺失 ⇒ 不知道这条豁免「授权了多久」⇒ 无授权 ⇒ 失败关闭。
    """
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{"nodeId": "app/tests/x.py::test_y", "reason": "原因"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "since" in str(excinfo.value)


def test_loadFlakeWhitelist_rejects_since_that_is_not_a_real_calendar_date(
    tmp_path: Path,
) -> None:
    """`since` **形状对但日历上不存在** ⇒ 失败关闭。

    ⚠️ 2026-11-06 安全审查 M-1 实测：`2026-02-30` 能通过
    `\\d{4}-\\d{2}-\\d{2}` 的**形状**校验 ⇒ 加载通过 ⇒ 直到**渲染报告**时
    `date.fromisoformat("2026-02-30")` 才抛 `ValueError`
    ⇒ 闸门对**全仓每个 PR** 都以「渲染报告失败」结束（退出码 3）
    ⇒ 判据整体瘫痪，且报错指向错误的方向（"报告渲染" 而不是 "白名单非法"）。

    ⇒ 日历合法性必须在**加载期**校验。
    """
    path = _writeWhitelist(
        tmp_path / "whitelist.json",
        {"entries": [{**_VALID_ENTRY, "since": "2026-02-30"}]},
    )

    with pytest.raises(FlakeWhitelistError) as excinfo:
        loadFlakeWhitelist(path)

    assert "since" in str(excinfo.value)


# ---------------------------------------------------------------------------
# T3：老化机制（条目不会永久有效）
# ---------------------------------------------------------------------------


def test_isEntryStale_is_false_for_a_recent_entry() -> None:
    """刚签发的条目不算老化。"""
    assert ci_flakes.isEntryStale("2026-11-06", date(2026, 11, 6)) is False


def test_isEntryStale_is_true_after_the_age_limit() -> None:
    """超过 `MAX_ENTRY_AGE_DAYS` ⇒ 老化 ⇒ 报告里提示复核。

    为什么需要老化：白名单一旦写下就永久有效 ⇒ 抖动被修好后条目不会自动消失
    ⇒ 此后该用例若发生**确定性**回归，会被永久豁免（两侧都被剔 ⇒ 净差恒为 0）
    ⇒ 这个用例从此**再也不回到判据**。
    """
    assert ci_flakes.isEntryStale("2026-01-01", date(2026, 11, 6)) is True


def test_isEntryStale_does_not_raise_on_an_unparsable_date() -> None:
    """非法日期**不许抛异常** —— 老化判定在**报告渲染路径**上。

    `ci_report._exemptionLines` 会对每条豁免调 `isEntryStale` ⇒ 它抛异常
    ⇒ 整份报告渲染失败 ⇒ 闸门退化成退出码 3（「渲染报告失败」）
    ⇒ 判据整体瘫痪，且报错指向错误的方向。

    ⇒ 非法日期按「**已老化**」处理（提示复核），不抛。
    """
    assert ci_flakes.isEntryStale("不是日期", date(2026, 11, 6)) is True


# ---------------------------------------------------------------------------
# T4：精确匹配的**语义**（不只是加载期的形状校验）
# ---------------------------------------------------------------------------


def test_applyFlakeExemptions_only_exempts_the_exact_node_id() -> None:
    """**精确匹配**：只有清单里那**一个**用例被豁免。

    同文件的其他用例、名字以它为前缀的用例、文件级 ID **都不豁免**。

    ⚠️ 这条钉的是「匹配**语义**」，不只是「加载期校验」：
    加载器只管住条目的**形状**（必须含 `::`），管不住匹配期会不会退化成前缀匹配。
    实测（2026-11-06 代码审查 H1）：把精确成员判定改成 `startswith` 前缀匹配
    ⇒ **原有 125 条用例全绿、0 条转红** ⇒ 爆炸半径从 1 个用例放大到 1 个文件
    ⇒ 而这正是「精确 node ID」硬约束存在的唯一理由。
    """
    flaky = "app/tests/integration/test_reconcile.py::test_backfill_is_idempotent"
    sibling = "app/tests/integration/test_reconcile.py::test_backfill_is_idempotent_v2"
    other = "app/tests/integration/test_reconcile.py::test_something_else"
    fileLevel = "app/tests/integration/test_reconcile.py"

    outcome = applyFlakeExemptions(
        frozenset({flaky, sibling, other, fileLevel}),
        frozenset({flaky, sibling, other, fileLevel}),
        [_entry(nodeId=flaky)],
    )

    assert outcome.baseFailures == frozenset({sibling, other, fileLevel})
    assert outcome.prFailures == frozenset({sibling, other, fileLevel})
    assert [item.nodeId for item in outcome.exemptions] == [flaky]


def test_applyFlakeExemptions_reports_raw_failure_counts() -> None:
    """**原始**失败条数（豁免之前）由豁免层给出 ⇒ 渲染层不做「加法还原」。

    还原是隐含契约：将来谁把原始集合传进渲染层，头部条数就会虚高。
    """
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}), frozenset({_STABLE}), [_entry()]
    )

    assert outcome.rawBaseCount == 2
    assert outcome.rawPrCount == 1
    assert len(outcome.baseFailures) == 1
    assert len(outcome.prFailures) == 1


def test_applyFlakeExemptions_carries_since_into_exemption() -> None:
    """豁免记录带上**签发日期** ⇒ 报告里能看到这条豁免「授权了多久」。"""
    outcome = applyFlakeExemptions(frozenset({_FLAKY}), frozenset(), [_entry()])

    assert outcome.exemptions[0].since == "2026-11-06"


# ---------------------------------------------------------------------------
# T5：豁免必须**条件化**在「该用例所在文件本次未被改动」
# ---------------------------------------------------------------------------
#
# 为什么（2026-11-06 安全审查 H-2）：
# 对称豁免的语义是「这个用例的**结论**不是证据」⇒ PR 侧**确定性**弄红它也不拦
# ⇒ 于是 PR 只要**改了那个用例所在的文件**，就可以让它确定性红而闸门无感。
#
# ⇒ 豁免条件化在「该用例所在文件在本次 PR 中**未被改动**」：
#   - 抖动用例本身没被碰 ⇒ 它的红绿仍是噪声 ⇒ 照常豁免（不引入随机阻塞）；
#   - 抖动用例所在文件被改了 ⇒ 它的红**可能不是抖动** ⇒ **不豁免** ⇒ 照常计入判据。
#
# ⚠️ 这条**不引入随机阻塞**：判据只依赖「改了哪些文件」（服务端事实），
# 不依赖用例这一次红没红 ⇒ 结论稳定可复现。

_FLAKY_FILE = "app/tests/integration/test_reconcile_milvus_id_mapping.py"
# compare API 给的是**仓库相对**路径；node ID 里的是 **rootdir 相对**（`backend/` 之下）。
_FLAKY_FILE_IN_REPO = f"backend/{_FLAKY_FILE}"


def test_applyFlakeExemptions_skips_the_entry_whose_file_the_pr_changed() -> None:
    """PR 改了白名单用例**所在文件** ⇒ 该豁免**不生效**（该用例照常计入判据）。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}),
        frozenset({_STABLE}),
        [_entry()],
        changedFiles=(_FLAKY_FILE_IN_REPO,),
    )

    assert _FLAKY in outcome.baseFailures
    assert outcome.exemptions == ()
    assert [item.nodeId for item in outcome.skipped] == [_FLAKY]


def test_applyFlakeExemptions_still_exempts_when_that_file_was_not_changed() -> None:
    """反向守卫：**别的**文件被改了 ⇒ 豁免照常生效（否则抖动用例会永远挡人）。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}),
        frozenset({_STABLE}),
        [_entry()],
        changedFiles=("backend/app/services/chat_service.py",),
    )

    assert _FLAKY not in outcome.baseFailures
    assert [item.nodeId for item in outcome.exemptions] == [_FLAKY]


def test_applyFlakeExemptions_only_cares_about_the_file_of_that_entry() -> None:
    """**同目录下的别的文件**被改了 ⇒ 不影响本条豁免（按文件精确匹配，不按目录）。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}),
        frozenset({_STABLE}),
        [_entry()],
        changedFiles=("backend/app/tests/integration/test_something_else.py",),
    )

    assert [item.nodeId for item in outcome.exemptions] == [_FLAKY]


def test_applyFlakeExemptions_exempts_when_the_changed_file_list_is_unknown() -> None:
    """改动清单**未知**（`None`）⇒ 照常豁免。

    「判定不出来」由「CI 相关面」守卫以**退出码 6** 兜住
    ⇒ 豁免层不必重复拦（否则退出码会变成 1 =「净新增失败」⇒ 语义反而错）。
    """
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY, _STABLE}),
        frozenset({_STABLE}),
        [_entry()],
        changedFiles=None,
    )

    assert [item.nodeId for item in outcome.exemptions] == [_FLAKY]


def test_applyFlakeExemptions_records_the_skip_reason_on_both_sides() -> None:
    """被跳过的豁免要记录它**命中了哪一侧** ⇒ 报告里能说清「它本来会算什么」。"""
    outcome = applyFlakeExemptions(
        frozenset({_FLAKY}),
        frozenset({_FLAKY}),
        [_entry()],
        changedFiles=(_FLAKY_FILE_IN_REPO,),
    )

    assert outcome.skipped[0].inBase is True
    assert outcome.skipped[0].inPr is True
    assert outcome.skipped[0].reason == "抖动原因"


def test_loadFlakeWhitelist_rejects_a_since_date_in_the_future(tmp_path: Path) -> None:
    """`since` 不得是**未来**日期。

    ⚠️ 2026-11-06 代码评审 L-4：`since` 是「签发日期」⇒ 未来的日期说不通
    （还没签发就授权了）⇒ 且会让这条豁免在 `MAX_ENTRY_AGE_DAYS` 内**永不老化**
    ⇒ 老化复核机制对它失效 ⇒ 必须拒绝。
    """
    path = tmp_path / "whitelist.json"
    path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "nodeId": "app/tests/unit/test_a.py::test_x",
                        "reason": "抖动",
                        "since": "2099-01-01",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FlakeWhitelistError) as error:
        loadFlakeWhitelist(path)

    assert "since" in str(error.value)
