"""导入后行数核对。对照 spec 附录 A 的**实测 count(*)**，不是 pg_stat 估算。

pg_stat_user_tables.n_live_tup 在这个库上偏差可达 3 个数量级
（audit_log 统计 6、真实 17100），拿它当基线等于没有基线。
"""

from __future__ import annotations

import asyncio
import json
import sys

from scripts.verify_migration import EXPECTED, checkAll, renderReport


def _asyncReturning(value):
    """造一个可 await 的替身，替掉真连库的 _queryAll。"""
    async def _fake(dsn: str) -> dict:
        return value

    return _fake


def _asyncFailing(exc: Exception):
    """造一个一被 await 就抛异常的替身，用来模拟连不上库。"""
    async def _fake(dsn: str) -> dict:
        raise exc

    return _fake


def _fakeAsyncpg(countsFor: dict[str, object]):
    """造一个 asyncpg 替身。

    countsFor 里的表名 → 抛出的异常对象；其余表 → 返回 100。
    exceptions 借用真实 asyncpg，保证实现里的 isinstance 判断与测试抛的是同一个类。
    """
    import types

    import asyncpg

    class FakeConn:
        async def fetchval(self, query: str) -> int:
            table = query.rsplit(" ", 1)[-1]
            if table in countsFor:
                raise countsFor[table]
            return 100

        async def close(self) -> None:
            return None

    async def connect(dsn: str) -> FakeConn:
        return FakeConn()

    return types.SimpleNamespace(
        exceptions=asyncpg.exceptions,
        connect=connect,
    )


def test_expected_matches_spec_appendix_a():
    assert EXPECTED["audit_log"] == 17100
    assert EXPECTED["ontology_class"] == 54
    assert EXPECTED["system_config"] == 28
    assert EXPECTED["session_token_usage"] == 2391
    assert EXPECTED["evidence"] == 1729
    assert EXPECTED["user_sessions"] == 544
    assert EXPECTED["session_message"] == 1420
    assert EXPECTED["multi_step_step"] == 27


def test_growing_set_covers_every_tolerated_table():
    """GROWING 常量此前没有任何测试引用 —— 写空了测试照样全绿。"""
    from scripts.verify_migration import GROWING
    # 断言左侧写成 set(...) 而不是 GROWING：ruff SIM300 会把全大写的名字
    # 当成「常量」，判成 Yoda 条件。语义与 brief 原写法完全一致。
    expectedGrowing = {"user_sessions", "session_message", "session_token_usage"}
    assert set(GROWING) == expectedGrowing
    assert set(EXPECTED) >= set(GROWING)


def test_checkAll_defaults_to_growing_tolerances():
    """不显式传 tolerate 时必须就是 GROWING，否则生产调用（走默认值）不受任何保护。"""
    assert checkAll(dict(EXPECTED, user_sessions=1)) != []
    assert checkAll(dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 3)) == []


def test_checkAll_tolerates_upward_growth_on_growing_tables():
    """导出与核对之间还会写入会话数据。多出来的行不是丢数据，必须放过。

    反向陷阱：把容忍条件写成 `got < want`（只放过小幅下降），
    每次正常增长都会变成一条「对不上」，门禁直接卡死。
    """
    grew = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 500,
                session_message=EXPECTED["session_message"] + 3000)
    assert checkAll(grew) == []


def test_checkAll_passes_static_tables_that_grew_after_the_baseline():
    """静态表「比基线多」是正常的，必须放过。

    基线是 dump **之前**量的，dump 的快照更晚 ⇒ 恢复出来的行数 >= 基线，
    多出来的部分是导出期间新写入的。判成「不一致」就是在好环境上报迁移失败。
    反向陷阱：写成 `got != want`，一次正确的恢复永远红。
    """
    assert checkAll(dict(EXPECTED, audit_log=17101, evidence=1800)) == []


def test_checkAll_fails_static_table_below_the_baseline():
    """静态表「比基线少」才是真丢数据 —— 这个方向不需要容差，精确检测。"""
    failures = checkAll(dict(EXPECTED, audit_log=EXPECTED["audit_log"] - 1))
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"


def test_growing_slack_is_unchanged_when_a_baseline_is_used():
    """GROWING 的语义一个字不动：向上放过、向下在 slack 内放过、超出才报。

    这里**不传 floor**，只验 slack 这一层。传了 floor 就测不出 slack 了：
    附录 A 下限是绝对的，会先于 slack 生效（见
    test_appendix_floor_also_covers_growing_tables）。两者混在一起时，
    真正生效的是更严的那条，slack 永远没被验到。
    """
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 10)        # 554
    withinSlack = dict(baseline, user_sessions=baseline["user_sessions"] - 20)     # 534
    assert withinSlack["user_sessions"] >= baseline["user_sessions"] * (1 - 0.20)  # slack 内
    assert checkAll(withinSlack, expect=baseline) == []
    beyondSlack = dict(baseline, user_sessions=1)
    assert len(checkAll(beyondSlack, expect=baseline)) == 1


def test_checkAll_passes_on_exact_match():
    assert checkAll(EXPECTED) == []


def test_checkAll_flags_wrong_count():
    bad = dict(EXPECTED, audit_log=6)   # 正是 pg_stat 会给出的值
    failures = checkAll(bad)
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"
    assert failures[0]["expected"] == 17100
    assert failures[0]["actual"] == 6


def test_checkAll_tolerates_probes_only_drift_downwards():
    """会话类表在导出后必然继续增长，少一点不算丢数据。"""
    drifted = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 3)
    assert checkAll(drifted, tolerate={"user_sessions"}) == []


def test_checkAll_still_flags_large_drop_even_if_tolerated():
    lost = dict(EXPECTED, user_sessions=1)
    assert len(checkAll(lost, tolerate={"user_sessions"})) == 1


def test_render_names_the_failing_table():
    out = renderReport(checkAll(dict(EXPECTED, system_config=0)))
    assert "system_config" in out and "28" in out


def test_parseDsn_strips_the_sqlalchemy_driver_suffix():
    """仓内 DATABASE_URL 是 postgresql+asyncpg://，asyncpg 不认这个 scheme。"""
    from scripts.verify_migration import _parseDsn
    assert _parseDsn("postgresql+asyncpg://u:p@postgres:5432/qa_metadata") \
        == "postgresql://u:p@postgres:5432/qa_metadata"


def test_parseDsn_leaves_plain_dsn_untouched():
    from scripts.verify_migration import _parseDsn
    plain = "postgresql://u:p@localhost:5433/qa_metadata"
    assert _parseDsn(plain) == plain


def test_main_exit_code_two_on_bad_usage(capsys):
    """2 必须与 1 区分，否则门禁会把「命令写错了」读成「迁移失败」。"""
    from scripts.verify_migration import main
    assert main(["prog"]) == 2
    assert main(["prog", "--dsn"]) == 2
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_zero_when_all_match(monkeypatch, capsys):
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", "postgresql://u:p@h:5432/db"]) == 0
    # 「全部对上」是**相等**语义。非对称规则下静态表只判「不低于」，
    # 比基线多 1 行也是绿的 —— 成功行若还写「对上」，运维会以为
    # 行数被逐格核对过。措辞必须跟着语义走，所以正向、反向都断言。
    out = capsys.readouterr().out
    assert "全部通过 ✓" in out
    assert "全部对上" not in out


def test_main_exit_code_one_when_counts_drift(monkeypatch, capsys):
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(EXPECTED, audit_log=6)))
    assert main(["prog", "--dsn", "postgresql://u:p@h:5432/db"]) == 1
    assert "audit_log" in capsys.readouterr().out


# ── I1：退出码 3 / _redact / 缺表 这三条路径此前零覆盖 ──────────────────────
# 变异证明：`return 3` 改 `return 1`、`_redact` 整个函数体清空、
# 整段 try/except 删除 —— 15 条测试全绿。

FAKE_DSN = "postgresql://qa_fake:s3cr3t-never-real@localhost:5433/qa_metadata"


def test_main_exit_code_three_when_database_unreachable(monkeypatch, capsys):
    """连不上库必须落到 3，而不是混进 1 里让运维去查一个不存在的数据问题。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncFailing(OSError("connection refused")))
    assert main(["prog", "--dsn", FAKE_DSN]) == 3
    err = capsys.readouterr().err
    assert "OSError" in err
    assert "connection refused" in err


def test_main_exit_code_three_output_never_echoes_credentials(monkeypatch, capsys):
    """错误路径同样不许把口令写进 stderr —— 门禁日志会被人贴进工单。"""
    from scripts.verify_migration import main
    leaky = OSError(f"cannot connect to {FAKE_DSN}")
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncFailing(leaky))
    assert main(["prog", "--dsn", FAKE_DSN]) == 3
    captured = capsys.readouterr()
    assert "s3cr3t-never-real" not in captured.err
    assert "s3cr3t-never-real" not in captured.out
    assert "<DSN>" in captured.err


def test_redact_masks_password_and_dsn():
    from scripts.verify_migration import _redact
    message = f"failed to reach {FAKE_DSN} (password s3cr3t-never-real)"
    out = _redact(FAKE_DSN, message)
    assert "s3cr3t-never-real" not in out
    assert "<DSN>" in out
    assert "***" in out


def test_redact_leaves_message_alone_without_credentials():
    """没有口令的 DSN 不该被瞎改写 —— 无凭据可抹时必须原样返回。"""
    from scripts.verify_migration import _redact
    assert _redact("postgresql://localhost:5433/qa_metadata", "boom") == "boom"
    assert _redact("", "boom") == "boom"


def test_redact_handles_dsn_without_password_part():
    """postgresql://user@host 形式（无冒号）不能被当成有口令而崩掉。"""
    from scripts.verify_migration import _redact
    assert _redact("postgresql://onlyuser@localhost:5433/db", "boom") == "boom"


def test_checkAll_reports_missing_table_with_its_own_reason():
    """缺表与「行数不对」是两种诊断，报告里必须能分开。"""
    from scripts.verify_migration import REASON_MISSING_TABLE
    partial = {table: n for table, n in EXPECTED.items() if table != "evidence"}
    failures = checkAll(partial)
    assert len(failures) == 1
    assert failures[0]["table"] == "evidence"
    assert failures[0]["reason"] == REASON_MISSING_TABLE


def test_checkAll_treats_explicit_none_as_missing_table():
    """_queryAll 对不存在的表回 None，与「键压根没有」必须同义。"""
    from scripts.verify_migration import REASON_MISSING_TABLE
    failures = checkAll(dict(EXPECTED, evidence=None))
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_MISSING_TABLE


def test_renderReport_counts_the_tables_actually_checked():
    """用基线核对时表数可能不等于 len(EXPECTED)，成功行不能报错数。"""
    assert "3 张表" in renderReport([], tableCount=3)


# ── I2：缺表被误报成「连接问题」 ──────────────────────────────────────────
# 半恢复的库（表还在、内容空）正是 spec §6.4 铁律场景，
# 报成连接问题会把运维送去查网络。


def test_queryAll_records_none_for_missing_table_and_keeps_querying(monkeypatch):
    """一张表不存在不能中断整轮统计：后面的表还得数出来，才能逐项定位。"""
    import asyncpg

    import scripts.verify_migration as vm
    monkeypatch.setitem(sys.modules, "asyncpg", _fakeAsyncpg({
        "evidence": asyncpg.exceptions.UndefinedTableError('relation "evidence" does not exist'),
    }))
    out = asyncio.run(vm._queryAll(FAKE_DSN))
    assert out["evidence"] is None
    # 循环必须继续：evidence 之后的表也要有结果
    assert out["user_sessions"] == 100
    assert out["multi_step_step"] == 100


def test_main_exit_code_one_when_table_is_missing(monkeypatch, capsys):
    """缺表 ⇒ 退出码 1 + 点名表名，不是 3。仍然失败关闭，只是诊断变了。"""
    from scripts.verify_migration import main
    partial = {table: n for table, n in EXPECTED.items() if table != "evidence"}
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(partial))
    assert main(["prog", "--dsn", FAKE_DSN]) == 1
    captured = capsys.readouterr()
    assert "evidence" in captured.out
    assert "连接" not in captured.out


# ── I3：空 --dsn 静默回落到 asyncpg 默认值 ────────────────────────────────
# python -m scripts.verify_migration --dsn "" 会真的去连 ::1:5432 / 127.0.0.1:5432，
# 也就是「悄悄查了别的库」→ 好的环境报「迁移失败」。brief 写明是退出码 2。

def test_main_exit_code_two_on_empty_dsn(monkeypatch, capsys):
    """空 DSN 是用法错误，且必须在碰数据库之前就退掉。"""
    from scripts.verify_migration import main
    touched = []

    async def _neverCalled(dsn: str) -> dict:
        touched.append(dsn)
        return {}

    monkeypatch.setattr("scripts.verify_migration._queryAll", _neverCalled)
    assert main(["prog", "--dsn", ""]) == 2
    assert touched == []
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_two_on_whitespace_only_dsn(monkeypatch):
    """全是空白同样是用法错误 —— strip() 后为空才算数。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", "   \t "]) == 2


# ── I5：基线改成「导出那一刻实测的行数」 ──────────────────────────────────
# 附录 A 是 2026-10-22 的一次快照：audit_log 已漂到 17101、evidence 1738、
# multi_step_step 30。基线文件由 Task 8 导出时产出。
# ⚠️ 别再写「静态表 ⇒ 门禁必红」：那是**相等**语义下的结论，已经作废。
# 非对称规则下静态表只判「不低于」，17101 ≥ 17100 是绿的 —— 向上漂移不再误报，
# 向下才精确到基线本身。几乎恒过的是**回落**路径（拿附录 A 当目标），
# 不是静态表判据本身。


def _writeBaseline(tmp_path, tables: dict, measuredAt: str = "2026-10-22T09:00:00+08:00",
                   name: str = "rowcounts.baseline.json"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "measured_at": measuredAt,
        "database": "qa_metadata",
        "tables": tables,
    }, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_loadBaseline_reads_export_time_counts(tmp_path):
    from scripts.verify_migration import loadBaseline
    path = _writeBaseline(tmp_path, dict(EXPECTED, audit_log=17101, evidence=1738))
    assert loadBaseline(path) == dict(EXPECTED, audit_log=17101, evidence=1738)


def test_loadBaseline_rejects_missing_file():
    from scripts.verify_migration import BaselineError, loadBaseline
    try:
        loadBaseline("/nonexistent/rowcounts.baseline.json")
    except BaselineError:
        return
    raise AssertionError("读不到基线文件必须是 BaselineError，而不是别的异常")


def test_loadBaseline_rejects_malformed_json(tmp_path):
    from scripts.verify_migration import BaselineError, loadBaseline
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    try:
        loadBaseline(str(path))
    except BaselineError:
        return
    raise AssertionError("坏 JSON 必须是 BaselineError")


def test_loadBaseline_rejects_table_counts_that_are_not_integers(tmp_path):
    from scripts.verify_migration import BaselineError, loadBaseline
    path = _writeBaseline(tmp_path, dict(EXPECTED, audit_log="一万七"))
    try:
        loadBaseline(path)
    except BaselineError:
        return
    raise AssertionError("非整数行数必须是 BaselineError")


def test_loadBaseline_rejects_baseline_missing_a_table(tmp_path):
    """基线里没提到的表不能被静默跳过 —— 那等于没核对。"""
    from scripts.verify_migration import BaselineError, loadBaseline
    partial = {table: n for table, n in EXPECTED.items() if table != "audit_log"}
    try:
        loadBaseline(_writeBaseline(tmp_path, partial))
    except BaselineError:
        return
    raise AssertionError("基线漏表必须是 BaselineError")


def test_checkAll_targets_the_measured_baseline_not_appendix_a(tmp_path):
    """实测值 17101 就是对的 —— 拿附录 A 的 17100 当目标会误报。"""
    baseline = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    assert checkAll(dict(baseline), expect=baseline, floor=EXPECTED) == []


def test_checkAll_flags_drift_against_the_baseline_target():
    """静态表比导出时少 ⇒ 传输丢了数据。"""
    baseline = dict(EXPECTED, audit_log=17101)
    failures = checkAll(dict(baseline, audit_log=17090), expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["table"] == "audit_log"
    assert failures[0]["expected"] == 17101
    assert failures[0]["actual"] == 17090


def test_checkAll_below_appendix_floor_gets_its_own_reason():
    """基线本身不可信（导错库）时，下限兜底。理由必须能与传输漂移区分开。"""
    from scripts.verify_migration import REASON_BELOW_FLOOR, REASON_DRIFT
    baseline = dict(EXPECTED, audit_log=500)
    failures = checkAll(dict(baseline), expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_BELOW_FLOOR
    assert failures[0]["reason"] != REASON_DRIFT
    assert REASON_BELOW_FLOOR in renderReport(failures)


def test_appendix_floor_also_covers_growing_tables():
    """下限是绝对的：GROWING 表在 slack 之内但已跌破附录 A，同样要报。

    也就是说**下限先于 slack 生效**。这不是 bug，是「附录 A 是绝对下限」
    这个裁定的直接后果 —— 别为了给 slack 腾空间把它改成可协商的。
    """
    from scripts.verify_migration import REASON_BELOW_FLOOR
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"])
    justBelowFloor = EXPECTED["user_sessions"] - 4      # slack 之内（−4 > −20%）
    failures = checkAll(dict(baseline, user_sessions=justBelowFloor),
                        expect=baseline, floor=EXPECTED)
    assert len(failures) == 1
    assert failures[0]["reason"] == REASON_BELOW_FLOOR


def test_checkAll_without_floor_keeps_appendix_a_as_plain_target():
    """不给 floor（未传基线的旧路径）时行为不变：GROWING 仍按 slack 容忍。"""
    assert checkAll(dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] - 4)) == []


def test_main_uses_baseline_target_end_to_end(monkeypatch, tmp_path, capsys):
    """端到端：基线 17101、库也是 17101 ⇒ 通过；且不再打「附录 A 已知漂移」的警。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 0
    captured = capsys.readouterr()
    assert "全部通过 ✓" in captured.out
    assert "已知漂移" not in captured.err


def test_main_reds_when_baseline_disagrees_with_the_restore(monkeypatch, tmp_path, capsys):
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17090)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 1
    assert "audit_log" in capsys.readouterr().out


def test_main_warns_loudly_when_baseline_omitted(monkeypatch, capsys):
    """静默回落 = 让运维重新制造这次要消除的困惑。回落必须吵。"""
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(EXPECTED)))
    assert main(["prog", "--dsn", FAKE_DSN]) == 0
    err = capsys.readouterr().err
    assert "2026-10-22" in err
    assert "已知漂移" in err
    assert "附录 A" in err
    assert "!" in err


def test_main_accepts_baseline_before_dsn(monkeypatch, tmp_path):
    from scripts.verify_migration import main
    baseline = dict(EXPECTED)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--baseline", _writeBaseline(tmp_path, baseline), "--dsn", FAKE_DSN]) == 0


def test_main_exit_code_three_when_baseline_unreadable(monkeypatch, capsys):
    """基线读不了 ⇒ 环境不可用（3），不是行数偏差（1），也不是不该去连库。"""
    from scripts.verify_migration import main
    touched = []

    async def _neverCalled(dsn: str) -> dict:
        touched.append(dsn)
        return {}

    monkeypatch.setattr("scripts.verify_migration._queryAll", _neverCalled)
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", "/nonexistent/baseline.json"]) == 3
    assert touched == []
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_two_on_empty_baseline_path():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", ""]) == 2


def test_main_exit_code_two_on_unknown_flag():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--nope", "x"]) == 2


def test_main_exit_code_two_on_baseline_without_value():
    from scripts.verify_migration import main
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline"]) == 2


# ── I-1：附录 A 的下限「接没接上」此前没有任何测试 ────────────────────────
# checkAll 内部的 floor 逻辑本身有测试，但 `main` 到底传没传 floor=EXPECTED
# 一条测试都没有。把 verify_migration.py:286 改成 checkAll(actual, expect=baseline)
# （或更危险的 floor=baseline）后，45 条测试全绿 —— 见本轮变异记录。
# 所以下面这几条必须在 **main 层面**钉住接线，checkAll 层的单测在两种接线
# 下都同样通过，钉不住。

def test_main_reports_below_appendix_floor_as_floor_not_drift(monkeypatch, tmp_path, capsys):
    """附录 A 的下限必须在 main 上真的接上：低于下限要报「低于附录 A 下限」。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=500)          # 基线本身已低于附录 A 的 17100
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(dict(baseline)))
    assert main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)]) == 1
    out = capsys.readouterr().out
    assert "audit_log" in out
    assert "低于附录 A 下限" in out


def test_main_keeps_growing_slack_when_baseline_is_used(monkeypatch, tmp_path, capsys):
    """下限是附录 A（不是基线），且 GROWING slack 在传基线时仍然生效。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, user_sessions=EXPECTED["user_sessions"] + 1)   # 545
    actual = dict(baseline, user_sessions=EXPECTED["user_sessions"])          # 544
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(actual))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 0, capsys.readouterr().out


def test_main_compares_against_the_baseline_not_appendix_a(monkeypatch, tmp_path, capsys):
    """目标是基线，不是附录 A —— 这条是两者唯一的区分点。

    为什么需要它：把 `main` 里的 `checkAll(actual, expect=baseline, floor=EXPECTED)`
    换成 `checkAll(actual)`（收了 --baseline 却拿附录 A 当目标）时，其余测试
    全部照样绿 —— 因为它们的基线值与 EXPECTED 相同或更高，两种目标结果一致。
    这里让基线 17500（源库在附录 A 之后又长了）、恢复出来 17200：
      目标=基线   → 17200 < 17500 → 丢数据，红
      目标=附录A  → 17200 >= 17100 → 放过，绿
    这个变异是被 mutation harness 抓出来的，不是想出来的。
    """
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17500)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17200)))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 1
    assert "audit_log" in capsys.readouterr().out


# ── I-3：静态表的非对称语义，main 层面钉住 ────────────────────────────────
# 下面两条合起来锁死方向：多了要过，少了要红。
# 方向写反（`got > want` 才过 / `got != want` 才红）时至少一条会红。

def test_main_passes_when_static_tables_come_back_above_baseline(monkeypatch, tmp_path, capsys):
    """dump 的快照比计数晚 ⇒ 恢复出来的只多不少。这是正常路径，必须绿。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    restored = dict(baseline, audit_log=17105, evidence=1740, multi_step_step=31)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(restored))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 0, capsys.readouterr().out


def test_main_fails_when_static_table_comes_back_below_baseline(monkeypatch, tmp_path, capsys):
    """少了才是丢数据。基线早于 dump ⇒ 这个方向是精确检测，不需要容差。"""
    from scripts.verify_migration import main
    baseline = dict(EXPECTED, audit_log=17101)
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(baseline, audit_log=17090)))
    rc = main(["prog", "--dsn", FAKE_DSN, "--baseline", _writeBaseline(tmp_path, baseline)])
    assert rc == 1
    assert "audit_log" in capsys.readouterr().out


def test_fallback_without_baseline_lets_drifted_static_tables_pass(monkeypatch, capsys):
    """回落路径（目标是 2026-10-22 的附录 A）现在对静态表基本恒过。

    这不是好事，是「不再误报」的代价：任何现库都比那份快照新，所以
    「不低于」几乎必然成立。这条测试把这个事实钉住，免得有人把回落
    路径当成「还在严格核对」来依赖。告警里必须说清楚这一点。
    """
    from scripts.verify_migration import main
    drifted = dict(EXPECTED, audit_log=17101, evidence=1738, multi_step_step=30)
    monkeypatch.setattr("scripts.verify_migration._queryAll", _asyncReturning(drifted))
    rc = main(["prog", "--dsn", FAKE_DSN])
    captured = capsys.readouterr()
    assert rc == 0, captured.out
    # 告警必须承认「这条路径几乎恒过」，否则回落会被当成真门禁
    assert "恒过" in captured.err or "低于" in captured.err
