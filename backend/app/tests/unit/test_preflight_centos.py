"""preflight_centos 宿主勘察单测。

只测判定逻辑，不测真实宿主 —— probe 函数全部注入替身。
"""

from __future__ import annotations

from scripts import preflight_centos
from scripts.preflight_centos import ProbeResult, evaluate, runProbe


def test_runProbe_records_ok_when_probe_returns_value():
    result = runProbe("glibc", lambda: "2.17")
    assert result == ProbeResult("glibc", True, "2.17", "hard")


def test_runProbe_records_failure_when_probe_raises():
    def boom() -> str:
        raise RuntimeError("command not found")

    result = runProbe("xfs_ftype", boom)
    assert result.ok is False
    assert result.severity == "hard"
    assert "command not found" in result.value


def test_evaluate_flags_xfs_ftype_zero_as_hard_failure():
    """XFS ftype=0 会让 overlay2 的 d_type 永远为 0，Postgres 索引全坏。"""
    severity, message = evaluate("xfs_ftype", "0")
    assert severity == "hard"
    assert message.startswith("ftype=0 必须")


def test_evaluate_flags_avx2_absent_as_soft_only():
    """缺 AVX2 只影响 mysql:8.0，可降级 8.0.32，不阻断。"""
    verdict = evaluate("cpu_avx2", "absent")
    assert verdict[0] == "soft"


def test_evaluate_accepts_ftype_one():
    assert evaluate("xfs_ftype", "1")[0] == "hard"  # severity 原样，ok 由调用方看


def test_evaluate_flags_kernel_below_3_10_514():
    """3.10.0-514 是 overlay2 的 RH 回移点，低于它 overlay2 起不来。"""
    _, message = evaluate("kernel", "2.6.32-754.el7")
    assert "3.10.0-514" in message


def test_evaluate_accepts_kernel_at_and_above_3_10_514():
    """build 号也参与比较 —— 3.10.0-514 与 3.10.0-1160 只差 build。"""
    assert evaluate("kernel", "3.10.0-514.el7")[1] == ""
    assert evaluate("kernel", "3.10.0-1160.el7.x86_64")[1] == ""


def test_kernelTuple_keeps_the_build_number():
    """只取到 (3,10,0) 会把 3.10.0-514 误判为低于自己。"""
    from scripts.preflight_centos import _kernelTuple
    assert _kernelTuple("3.10.0-1160.el7.x86_64") == (3, 10, 0, 1160)
    assert _kernelTuple("2.6.32-754.el7") == (2, 6, 32, 754)
    assert _kernelTuple("2.17") == (2, 17, 0)


def test_evaluate_blocks_undersized_host():
    """16C/32G 是 spec §1.1 的目标环境，不满足就该在装机前拦住。"""
    assert evaluate("cpu_cores", "8")[0] == "hard"
    assert evaluate("mem_total_gb", "16.0")[0] == "hard"
    assert evaluate("docker_disk_free_gb", "42.0")[0] == "hard"
    assert evaluate("cpu_cores", "16")[1] == ""
    assert evaluate("mem_total_gb", "63.0")[1] == ""


def test_evaluate_only_flags_enforcing_selinux():
    """Disabled/Permissive 都是常态，不该报成问题。"""
    assert evaluate("selinux", "Disabled")[1] == ""
    assert evaluate("selinux", "Permissive")[1] == ""
    assert "setenforce" in evaluate("selinux", "Enforcing")[1]


def test_main_reports_probe_crash_instead_of_raising(monkeypatch, capsys):
    """探测失败时 value 是异常文本，不能再进 evaluate 的 float()/int() 分支。

    否则 /proc/meminfo 读不到这类宿主上，勘察脚本自己先崩掉 —— 而「读不到」
    恰恰是它该回答的结论。
    """
    def boom() -> str:
        raise FileNotFoundError("/proc/meminfo")

    monkeypatch.setattr(preflight_centos, "PROBES", (("mem_total_gb", boom),))
    assert preflight_centos.main() == 1
    captured = capsys.readouterr()
    assert "[FAIL] mem_total_gb" in captured.out
    assert "FileNotFoundError" in captured.out