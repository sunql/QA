"""复用键指纹比对。

三条不可让步的性质：
1. 任何输出（stdout / stderr / 异常消息）都不含密钥明文
2. CRLF 行尾被归一，不能因此报出「假不一致」（否则运维会去查一个不存在的问题）
3. 失败原因必须能区分开：读不到文件（3）≠ 用法错（2）≠ 键不一致（1）
   —— 运维只看得懂退出码，混成一个「非零」会让他去查一个不存在的不一致
"""

from __future__ import annotations

import pytest

from scripts.verify_env_keys import (
    REUSE_KEYS,
    compare,
    extractValues,
    findEmptyValues,
    fingerprint,
    main,
    parseEnvFile,
    render,
)

# sha256("")[:12] —— 空值指纹。缺失/空值一旦被当成正常指纹渲染出来，
# 报告里就会出现两行一模一样的 12 位十六进制，运维无从判断该不该动手。
EMPTY_FP = "e3b0c44298fc"


def _allKeysDict() -> dict[str, str]:
    """4 个复用键齐备、无空值无重复的键值表。"""
    return {
        "SECRET_KEY": "a",
        "NEO4J_PASSWORD": "b",
        "MINIO_ROOT_USER": "c",
        "MINIO_ROOT_PASSWORD": "d",
    }


def _envBody(secretKey: str = "a") -> str:
    """4 个复用键齐备的 .env 文本。"""
    return (
        f"SECRET_KEY={secretKey}\n"
        "NEO4J_PASSWORD=b\n"
        "MINIO_ROOT_USER=c\n"
        "MINIO_ROOT_PASSWORD=d\n"
    )


def test_reuse_keys_are_exactly_the_four_from_spec():
    assert REUSE_KEYS == (
        "SECRET_KEY",
        "NEO4J_PASSWORD",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
    )


def test_fingerprint_is_stable_and_opaque():
    a = fingerprint("abc")
    assert a == fingerprint("abc")
    assert len(a) == 12
    assert "abc" not in a


def test_extract_strips_crlf():
    assert extractValues("SECRET_KEY=abc\r\nOTHER=x\r\n")["SECRET_KEY"] == "abc"


def test_extract_ignores_comments_and_blanks():
    text = "# 注释\n\nSECRET_KEY=abc\n"
    assert extractValues(text) == {"SECRET_KEY": "abc"}


def test_extract_keeps_equals_signs_inside_value():
    """base64 / 口令里可能有 = 号，只能切第一个。"""
    assert extractValues("SECRET_KEY=ab=cd=ef\n")["SECRET_KEY"] == "ab=cd=ef"


def test_extract_strips_surrounding_quotes():
    assert extractValues('SECRET_KEY="abc"\n')["SECRET_KEY"] == "abc"


def test_extract_last_wins_on_duplicate_key():
    """真实文件里 docker/.env 的 NEO4J_PASSWORD 出现了两次（第 14、40 行）。

    重复键必须与 docker compose 取值一致 —— compose 也是「后出现者胜」。
    若这里改成 first-wins，本脚本会比对 compose 根本没在用的那个值，
    报出一个运维查不出来的「不一致」。这条测试把该行为钉死。
    """
    out = extractValues("NEO4J_PASSWORD=first\nNEO4J_PASSWORD=second\n")
    assert out["NEO4J_PASSWORD"] == "second"


def test_compare_reports_all_keys_not_just_the_first_diff():
    """必须报**全部**不一致项，且顺序跟随 REUSE_KEYS。

    旧版这条测试只让 1 个键不同，于是「只返回第一处不一致」的实现照样绿 ——
    名字宣称的行为根本没被测到。这里让 2 个键不同并逐项断言。
    """
    src = _allKeysDict()
    dst = dict(src, SECRET_KEY="WRONG1", MINIO_ROOT_PASSWORD="WRONG4")
    mismatches = compare(src, dst)
    assert mismatches == [
        ("SECRET_KEY", fingerprint("a"), fingerprint("WRONG1")),
        ("MINIO_ROOT_PASSWORD", fingerprint("d"), fingerprint("WRONG4")),
    ], "两处不一致都要报，且顺序必须跟随 REUSE_KEYS"


def test_compare_empty_on_full_match():
    assert compare(_allKeysDict(), _allKeysDict()) == []


def test_missing_key_is_reported_not_silently_skipped():
    mismatches = compare(_allKeysDict(), {})
    assert len(mismatches) == 4, "缺失必须报，不能当作一致"


def test_compare_marks_missing_dest_side_as_none_not_empty_fingerprint():
    """目标侧缺失必须是 None，不是 fingerprint("")。

    否则报告里会出现「源端=目标端=e3b0c44298fc」两行一样的值，
    运维看到会以为「这俩一样，是误报」。
    """
    mismatches = compare(_allKeysDict(), {})
    assert mismatches == [
        ("SECRET_KEY", fingerprint("a"), None),
        ("NEO4J_PASSWORD", fingerprint("b"), None),
        ("MINIO_ROOT_USER", fingerprint("c"), None),
        ("MINIO_ROOT_PASSWORD", fingerprint("d"), None),
    ]
    assert EMPTY_FP not in [fp for _, _, fp in mismatches if fp]


def test_compare_marks_missing_source_side_as_none():
    mismatches = compare({}, _allKeysDict())
    assert mismatches == [
        ("SECRET_KEY", None, fingerprint("a")),
        ("NEO4J_PASSWORD", None, fingerprint("b")),
        ("MINIO_ROOT_USER", None, fingerprint("c")),
        ("MINIO_ROOT_PASSWORD", None, fingerprint("d")),
    ]


def test_render_output_contains_only_fingerprints():
    """报告里只能出现指纹。若有人后来加了「顺手打印明文方便排查」，这里会红。"""
    report = render([("SECRET_KEY", fingerprint("real-secret"), fingerprint("wrong-secret"))])
    assert "SECRET_KEY" in report
    assert "real-secret" not in report
    assert "wrong-secret" not in report
    assert fingerprint("wrong-secret") in report


def test_render_missing_side_shows_marker_not_empty_fingerprint():
    """缺失侧渲染成「缺失」字样，绝不能让空值指纹冒充成一个真实指纹。"""
    report = render([("SECRET_KEY", None, fingerprint("real"))])
    assert "SECRET_KEY" in report
    assert "缺失" in report
    assert EMPTY_FP not in report, "缺失侧不得渲染成 fingerprint('')"


def test_render_missing_dest_side_shows_marker():
    report = render([("NEO4J_PASSWORD", fingerprint("real"), None)])
    assert "缺失" in report
    assert EMPTY_FP not in report
    assert fingerprint("real") in report


def test_render_on_match_prints_no_secret_names():
    out = render([])
    assert out.strip().endswith("✓")
    for key in REUSE_KEYS:
        assert key not in out


def test_find_empty_values_flags_both_sides_empty():
    """两端都空 —— 判一致可以，但空 SECRET_KEY 当 Fernet 主密钥是不能用的。"""
    assert findEmptyValues({"SECRET_KEY": "", "NEO4J_PASSWORD": "b"}, dict(
        {"SECRET_KEY": "", "NEO4J_PASSWORD": "b"})) == ["SECRET_KEY"]
    assert findEmptyValues({"SECRET_KEY": ""}, {"SECRET_KEY": "x"}) == []


def test_empty_value_on_both_sides_warns_but_still_exits_zero(tmp_path, capsys):
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    body = "SECRET_KEY=\nNEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\nMINIO_ROOT_PASSWORD=d\n"
    src.write_text(body, encoding="utf-8")
    dst.write_text(body, encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 0
    out = capsys.readouterr().out
    assert "WARN" in out, "两端都空必须留一条告警，否则空 SECRET_KEY 会被无声放行"
    assert "全部一致" in out


def test_parse_env_file_reads_from_disk(tmp_path):
    """parseEnvFile 是唯一碰文件系统的入口，不能只测不覆盖。"""
    p = tmp_path / ".env"
    p.write_text("# c\nSECRET_KEY=abc\n", encoding="utf-8")
    assert parseEnvFile(p) == {"SECRET_KEY": "abc"}


def test_parse_env_file_raises_oserror_on_missing_path(tmp_path):
    """parseEnvFile 不吞异常 —— 把「读不到」映射成退出码是 main 的职责。"""
    with pytest.raises(OSError):
        parseEnvFile(tmp_path / "nope.env")


def test_main_exit_code_zero_on_match(tmp_path, capsys):
    """退出码是这个脚本唯一的交付物 —— 门禁脚本靠它，不靠人眼看输出。"""
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    body = _envBody()
    src.write_text(body, encoding="utf-8")
    dst.write_text(body, encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 0
    assert "全部一致" in capsys.readouterr().out


def test_main_exit_code_one_on_mismatch(tmp_path, capsys):
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    src.write_text(_envBody(), encoding="utf-8")
    dst.write_text(_envBody("WRONG"), encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 1
    captured = capsys.readouterr()
    assert "DIFF" in captured.out
    assert "WRONG" not in captured.out, "不一致时 stdout 也不许回显明文"
    assert "WRONG" not in captured.err, "stderr 同样不许回显明文"
    assert captured.err.strip() == "", "比对报告只走 stdout；stderr 留给真正的错误"


def test_main_reports_missing_key_as_marker_not_empty_fingerprint(tmp_path, capsys):
    """端到端：目标侧缺 SECRET_KEY 时，运维看到的报告里必须是「缺失」。

    若渲染成 fingerprint("")，报告会印出「源端=目标端=e3b0c44298fc」两行一样的值，
    运维会认为「这俩本来就一样，是误报」，从而放行一个缺密钥的迁移。
    """
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    src.write_text(_envBody(), encoding="utf-8")
    dst.write_text("NEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\nMINIO_ROOT_PASSWORD=d\n",
                   encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 1
    out = capsys.readouterr().out
    assert "SECRET_KEY" in out
    assert "缺失" in out
    assert EMPTY_FP not in out, "缺失侧渲染成空值指纹，运维会误以为两端相同"


def test_main_exit_code_two_on_bad_usage(capsys):
    """参数个数不对必须与「比对不一致」区分开 —— 否则门禁会把用法错误读成迁移失败。"""
    assert main(["prog"]) == 2
    assert capsys.readouterr().err.strip() != ""


def test_main_exit_code_three_on_unreadable_env(tmp_path, capsys):
    """读不到 .env 必须有自己的退出码。

    真实故障：operator 的 scp 失败或路径写错。若这里沿用 1，门禁读成
    「有键不一致」，而 stdout 里一行 DIFF 都没有 —— 运维会去查一个不存在的问题。
    """
    src = tmp_path / "a.env"
    src.write_text(_envBody(), encoding="utf-8")
    assert main(["prog", str(src), str(tmp_path / "nope.env")]) == 3
    captured = capsys.readouterr()
    assert captured.err.strip() != "", "读不到文件要有可读的原因"
    assert captured.out.strip() == "", "读不到文件时不得输出比对报告，否则会被误读成结论"


def test_exit_codes_three_two_one_zero_are_all_distinguishable(tmp_path):
    """四条路径的退出码两两不同 —— 门禁只能靠退出码分类。"""
    good = tmp_path / "good.env"
    good.write_text(_envBody(), encoding="utf-8")
    bad = tmp_path / "bad.env"
    bad.write_text(_envBody("WRONG"), encoding="utf-8")
    missing = tmp_path / "missing.env"

    assert main(["prog"]) == 2
    assert main(["prog", str(good), str(missing)]) == 3
    assert main(["prog", str(good), str(bad)]) == 1
    assert main(["prog", str(good), str(good)]) == 0


def test_io_error_message_carries_no_secret_value(tmp_path, capsys):
    """IO 错误消息只能带 errno + 路径，绝不能带任何值。"""
    rawValue = "SUPER-SECRET-NEVER-PRINT"
    src = tmp_path / "a.env"
    src.write_text(_envBody(rawValue), encoding="utf-8")
    assert main(["prog", str(src), str(tmp_path / "nope.env")]) == 3
    captured = capsys.readouterr()
    assert rawValue not in captured.err, "IO 错误消息不得回显任何值"
    assert rawValue not in captured.out
