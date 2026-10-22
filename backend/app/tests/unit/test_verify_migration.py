"""导入后行数核对。对照 spec 附录 A 的**实测 count(*)**，不是 pg_stat 估算。

pg_stat_user_tables.n_live_tup 在这个库上偏差可达 3 个数量级
（audit_log 统计 6、真实 17100），拿它当基线等于没有基线。
"""

from __future__ import annotations

from scripts.verify_migration import EXPECTED, checkAll, renderReport


def _asyncReturning(value):
    """造一个可 await 的替身，替掉真连库的 _queryAll。"""
    async def _fake(dsn: str) -> dict:
        return value

    return _fake


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


def test_checkAll_still_flags_upward_growth_on_static_tables():
    """非 GROWING 表多出数据是真异常（多半是 seed 跑过了），不能一起放过。"""
    assert len(checkAll(dict(EXPECTED, audit_log=17101))) == 1


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
    assert "全部对上" in capsys.readouterr().out


def test_main_exit_code_one_when_counts_drift(monkeypatch, capsys):
    from scripts.verify_migration import main
    monkeypatch.setattr("scripts.verify_migration._queryAll",
                        _asyncReturning(dict(EXPECTED, audit_log=6)))
    assert main(["prog", "--dsn", "postgresql://u:p@h:5432/db"]) == 1
    assert "audit_log" in capsys.readouterr().out
