"""preflight_centos 宿主勘察单测。

只测判定逻辑，不测真实宿主 —— probe 函数全部注入替身。
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import types

import pytest

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


# glibc 2.17 的 ldd --version 真实输出（CentOS 7.9）：四行，末行以 PURPOSE. 收尾
LDD_VERSION_OUTPUT = """ldd (GNU libc) 2.17
Copyright (C) 1992-2016 Free Software Foundation, Inc.
This is free software; see the source for copying conditions.  There is NO
warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
"""


def _stubSubprocess(monkeypatch, stdout: str):
    """把 preflight_centos 里的 subprocess 换成只回固定 stdout 的替身。"""
    class _Done:
        def __init__(self, text: str) -> None:
            self.stdout = text

    def fakeRun(cmd, **kwargs):
        fakeRun.calls.append(list(cmd))
        return _Done(stdout)

    fakeRun.calls = []
    monkeypatch.setattr(
        preflight_centos,
        "subprocess",
        types.SimpleNamespace(PIPE=-1, run=fakeRun),
    )
    return fakeRun


def test_probeGlibc_reads_only_the_first_line(monkeypatch):
    """ldd --version 是四行；对整段 split() 取末字段会拿到 'PURPOSE.'。

    'PURPOSE.' → _kernelTuple → (0,) → < (2,17,0) → 好机器被报成 glibc 0.x 阻断。
    """
    stub = _stubSubprocess(monkeypatch, LDD_VERSION_OUTPUT)
    assert preflight_centos._probeGlibc() == "2.17"
    assert stub.calls == [["ldd", "--version"]]
    # 端到端：好机器必须判为通过，message 为空
    assert evaluate("glibc", preflight_centos._probeGlibc()) == ("hard", "")


def _stubSubprocessByCommand(monkeypatch, outputs):
    """按命令首元素返回不同 stdout 的替身；未登记的命令返回空串。

    上一版替身对**每个**命令都回同一段非空 stdout，于是「findmnt 什么也没查到」
    这一种真实宿主上最要紧的情况根本表达不出来 —— 断言只能锁 argv，锁不住行为。
    """
    class _Done:
        def __init__(self, text: str) -> None:
            self.stdout = text

    def fakeRun(cmd, **kwargs):
        fakeRun.calls.append(list(cmd))
        return _Done(outputs.get(cmd[0], ""))

    fakeRun.calls = []
    monkeypatch.setattr(
        preflight_centos,
        "subprocess",
        types.SimpleNamespace(PIPE=-1, run=fakeRun),
    )
    return fakeRun


XFS_INFO_FTYPE0 = "xfs_info = {\n\tftype=0\n}\n"
XFS_INFO_FTYPE1 = "xfs_info = {\n\tftype=1\n}\n"


def test_probeXfsFtype_reports_ftype_zero_on_xfs(monkeypatch):
    """XFS 且 ftype=0 → 必须报 0（设计稿 §6 P0 头号 hard failure）。"""
    _stubSubprocessByCommand(monkeypatch, {"findmnt": "xfs\n", "xfs_info": XFS_INFO_FTYPE0})
    assert preflight_centos._probeXfsFtype() == "0"
    assert evaluate("xfs_ftype", "0")[0] == "hard"


def test_probeXfsFtype_passes_on_xfs_with_ftype_one(monkeypatch):
    """XFS 且 ftype=1 → 报 1，判定不拦。"""
    _stubSubprocessByCommand(monkeypatch, {"findmnt": "xfs\n", "xfs_info": XFS_INFO_FTYPE1})
    assert preflight_centos._probeXfsFtype() == "1"
    assert evaluate("xfs_ftype", "1")[1] == ""


def test_probeXfsFtype_passes_on_non_xfs(monkeypatch):
    """非 XFS 宿主（ext4 等）不适用 ftype 判定 → 放行。"""
    _stubSubprocessByCommand(monkeypatch, {"findmnt": "ext4\n"})
    assert preflight_centos._probeXfsFtype() == "1"


def test_probeXfsFtype_raises_when_filesystem_type_undeterminable(monkeypatch):
    """判定不出文件系统类型必须抛（硬失败），不能回 '1' 冒充「已通过」。

    勘察脚本的职责是抓拦路石；「没量到」不等于「量到合格」。
    """
    _stubSubprocessByCommand(monkeypatch, {"findmnt": ""})
    with pytest.raises(RuntimeError):
        preflight_centos._probeXfsFtype()


def test_probeXfsFtype_uses_target_form_of_findmnt(monkeypatch):
    """findmnt 的位置参数是**精确挂载点匹配**：/var/lib 不是挂载点时返回空（rc=1）。

    util-linux 2.23.2（CentOS 7）实测：
        findmnt -no FSTYPE /var/lib   → 空, rc=1
        findmnt -no FSTYPE -T /var/lib → 所在文件系统类型, rc=0
    故必须用 -T 解析「包含该路径的文件系统」。
    """
    stub = _stubSubprocessByCommand(monkeypatch, {"findmnt": "xfs\n", "xfs_info": XFS_INFO_FTYPE1})
    preflight_centos._probeXfsFtype()
    findmntCall = stub.calls[0]
    assert findmntCall[:3] == ["findmnt", "-no", "FSTYPE"]
    assert "-T" in findmntCall
    assert findmntCall[-1] == preflight_centos.DOCKER_PARENT


def test_evaluate_tolerates_non_numeric_values():
    """evaluate 是发布出去的接口，测试直接调 —— 不能因 'unknown' 抛 ValueError。"""
    for name in ("cpu_cores", "mem_total_gb", "docker_disk_free_gb"):
        severity, message = evaluate(name, "unknown")
        assert severity == "hard"
        assert "无法解析" in message


def test_main_exits_1_when_verdict_fails(monkeypatch, capsys):
    """判定不通过 → 退出码 1，且补救建议必须印出来（只印 value 运维没法动手）。"""
    monkeypatch.setattr(preflight_centos, "PROBES", (("xfs_ftype", lambda: "0"),))
    assert preflight_centos.main() == 1
    captured = capsys.readouterr()
    assert "[FAIL] xfs_ftype: 0" in captured.out
    assert "mkfs.xfs -m ftype=1" in captured.out
    assert "阻断" in captured.err


def test_main_exits_0_with_warning_on_soft_failure(monkeypatch, capsys):
    """soft 未满足（缺 AVX2）→ 退出码 0，但 stderr 要有告警。"""
    monkeypatch.setattr(preflight_centos, "PROBES", (("cpu_avx2", lambda: "absent"),))
    assert preflight_centos.main() == 0
    captured = capsys.readouterr()
    assert "缺 AVX2" in captured.out
    assert "警告" in captured.err


def test_main_json_output_is_parseable(monkeypatch, capsys):
    monkeypatch.setattr(preflight_centos, "PROBES", (("xfs_ftype", lambda: "0"),))
    monkeypatch.setattr(sys, "argv", ["preflight_centos", "--json"])
    preflight_centos.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "name": "xfs_ftype",
            "ok": False,
            "value": "0",
            "severity": "hard",
            "message": preflight_centos.evaluate("xfs_ftype", "0")[1],
        }
    ]


def test_evaluate_rejects_non_finite_numbers():
    """nan/inf 不是「合格的测量值」。

    float('nan') < 32.0 是 False ⇒ 若照单全收，evaluate('mem_total_gb', 'nan')
    会返回 message='' —— 一台内存未知的机器被判为通过 32 GB 门槛。
    """
    for name in ("cpu_cores", "mem_total_gb", "docker_disk_free_gb"):
        for bad in ("nan", "inf", "-inf"):
            severity, message = evaluate(name, bad)
            assert severity == "hard", (name, bad)
            assert "无法解析" in message, (name, bad)


def test_asFloat_returns_none_for_non_numeric_input():
    """_asFloat 必须对 None 也成立 —— float(None) 抛的是 TypeError 不是 ValueError。"""
    assert preflight_centos._asFloat(None) is None
    assert preflight_centos._asFloat("unknown") is None
    assert preflight_centos._asFloat("nan") is None
    assert preflight_centos._asFloat("12.5") == 12.5


def test_module_annotations_never_evaluate_callable_subscription():
    """3.6 上 `collections.abc.Callable[...]` 不可下标（PEP 585 要 3.9）。

    def 的参数注解在**定义时**求值，所以裸写 `fn: Callable[[], str]`
    会让整个模块在 3.6 上 import 就炸（TypeError: 'ABCMeta' object is not
    subscriptable）。凡提到 Callable 的注解必须加引号，永不求值。
    """
    tree = ast.parse(_moduleSource())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        annotations = [arg.annotation for arg in node.args.args]
        annotations.append(node.returns)
        for ann in annotations:
            if ann is None:
                continue
            if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                continue  # 加了引号 → 永不求值，安全
            if "Callable" in ast.dump(ann):
                offenders.append(f"{node.name}: {ast.dump(ann)}")
    assert offenders == []


def test_module_has_no_repo_internal_imports():
    """这个文件会被拷到裸 CentOS 宿主当独立脚本跑。

    backend/scripts/__init__.py 里就有 `from __future__ import annotations`
    （3.7+ 语法），所以只要本模块 import 了 scripts 包，3.6 上就是 SyntaxError。
    故只允许标准库。
    """
    tree = ast.parse(_moduleSource())
    repoRoots = {"scripts", "app", "backend"}
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    offenders = [name for name in imported if name.split(".")[0] in repoRoots]
    assert offenders == []


def _moduleSource() -> str:
    """preflight_centos.py 的源码文本（用于静态守护）。"""
    return inspect.getsource(preflight_centos)
