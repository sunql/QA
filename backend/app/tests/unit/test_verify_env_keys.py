"""复用键指纹比对。

两条不可让步的性质：
1. 任何输出都不含密钥明文
2. CRLF 造成的假不一致必须被识别出来（否则运维会去查一个不存在的问题）
"""

from __future__ import annotations

from scripts.verify_env_keys import (
    REUSE_KEYS,
    compare,
    extractValues,
    fingerprint,
    main,
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
    src = {"SECRET_KEY": "a", "NEO4J_PASSWORD": "b", "MINIO_ROOT_USER": "c",
           "MINIO_ROOT_PASSWORD": "d"}
    dst = dict(src, SECRET_KEY="WRONG")
    mismatches = compare(src, dst)
    assert mismatches == [("SECRET_KEY", fingerprint("a"), fingerprint("WRONG"))]


def test_compare_empty_on_full_match():
    src = {"SECRET_KEY": "a", "NEO4J_PASSWORD": "b", "MINIO_ROOT_USER": "c",
           "MINIO_ROOT_PASSWORD": "d"}
    assert compare(src, src) == []


def test_missing_key_is_reported_not_silently_skipped():
    src = {"SECRET_KEY": "a", "NEO4J_PASSWORD": "b", "MINIO_ROOT_USER": "c",
           "MINIO_ROOT_PASSWORD": "d"}
    mismatches = compare(src, {})
    assert len(mismatches) == 4, "缺失必须报，不能当作一致"


def test_render_output_contains_only_fingerprints():
    """报告里只能出现指纹。若有人后来加了「顺手打印明文方便排查」，这里会红。"""
    from scripts.verify_env_keys import render
    report = render([("SECRET_KEY", fingerprint("real-secret"), fingerprint("wrong-secret"))])
    assert "SECRET_KEY" in report
    assert "real-secret" not in report
    assert "wrong-secret" not in report
    assert fingerprint("wrong-secret") in report


def test_render_on_match_prints_no_secret_names():
    from scripts.verify_env_keys import render
    out = render([])
    assert out.strip().endswith("✓")
    for key in REUSE_KEYS:
        assert key not in out


def test_parse_env_file_reads_from_disk(tmp_path):
    """parseEnvFile 是唯一碰文件系统的入口，不能只测不覆盖。"""
    from scripts.verify_env_keys import parseEnvFile
    p = tmp_path / ".env"
    p.write_text("# c\nSECRET_KEY=abc\n", encoding="utf-8")
    assert parseEnvFile(p) == {"SECRET_KEY": "abc"}


def test_main_exit_code_zero_on_match(tmp_path, capsys):
    """退出码是这个脚本唯一的交付物 —— 门禁脚本靠它，不靠人眼看输出。"""
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    body = "SECRET_KEY=a\nNEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\nMINIO_ROOT_PASSWORD=d\n"
    src.write_text(body, encoding="utf-8")
    dst.write_text(body, encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 0
    assert "全部一致" in capsys.readouterr().out


def test_main_exit_code_one_on_mismatch(tmp_path, capsys):
    src, dst = tmp_path / "a.env", tmp_path / "b.env"
    src.write_text("SECRET_KEY=a\nNEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\n"
                   "MINIO_ROOT_PASSWORD=d\n", encoding="utf-8")
    dst.write_text("SECRET_KEY=WRONG\nNEO4J_PASSWORD=b\nMINIO_ROOT_USER=c\n"
                   "MINIO_ROOT_PASSWORD=d\n", encoding="utf-8")
    assert main(["prog", str(src), str(dst)]) == 1
    out = capsys.readouterr().out
    assert "DIFF" in out
    assert "WRONG" not in out, "不一致时也不许回显明文"


def test_main_exit_code_two_on_bad_usage(capsys):
    """参数个数不对必须与「比对不一致」区分开 —— 否则门禁会把用法错误读成迁移失败。"""
    assert main(["prog"]) == 2
    assert capsys.readouterr().err.strip() != ""