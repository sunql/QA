"""preflight_image_secrets.py 单元测试（纯逻辑，不碰 docker）。

验证部署门禁脚本能把「.env 被烤进镜像」这件事判红：

1. :func:`classifySecretFiles` 只认真 ``.env``，放行 ``.env.example`` 模板
2. 反向守卫：**探针自身要能被证伪** —— 镜像里连 ``.env.example`` 都没有时，
   「无敏感文件」这一项必须 FAIL，否则一个坏掉的 ``find`` 会让检查静默假绿
3. :func:`runChecks` 的 docker 调用形状正确（注入假 runner，不真调 docker）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "preflight_image_secrets.py"


def _loadModule():
    spec = importlib.util.spec_from_file_location("preflight_image_secrets", str(_SCRIPT))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass 需要能通过 cls.__module__ 在 sys.modules 里找到模块
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fakeRunner(output: str):
    """返回一个可注入的 subprocess.run 替身，只回 stdout。"""

    class _Completed:
        stdout = output
        returncode = 0

    def _run(cmd, **kwargs):  # noqa: ANN001, ARG001
        return _Completed()

    return _run


def test_classify_flags_real_env_but_spares_example() -> None:
    preflight = _loadModule()
    classify = preflight.classifySecretFiles

    paths = [
        "/app/.env",  # 真密钥 → 必须拦
        "/app/.env.local",
        "/app/.env.production",
        "/app/.env.example",  # 模板 → 必须放行（新人 onboarding 依赖它）
        "/app/app/config.py",
        "/app/alembic/env.py",
        "/app/.venv/lib/python3.12/site-packages/.env",  # 依赖树里同名不算泄露
    ]
    assert classify(paths) == [
        "/app/.env",
        "/app/.env.local",
        "/app/.env.production",
    ]


def test_classify_empty_input_is_clean() -> None:
    preflight = _loadModule()
    assert preflight.classifySecretFiles([]) == []


def test_run_checks_passes_when_only_example_present() -> None:
    preflight = _loadModule()
    results = preflight.runChecks("img", runner=_fakeRunner("/app/.env.example\n"))

    assert [r.name for r in results] == ["no_secret_env_files", "env_example_present"]
    assert all(r.passed for r in results), [r.detail for r in results if not r.passed]


def test_reverse_guard_broken_probe_cannot_pass_silently() -> None:
    """反向守卫：探针坏掉（连模板都找不到）时**整体不得全绿**。

    第一项在没有可疑文件时本就该 PASS —— 真正兜底的是第二项：``.env.example``
    必须存在。少了这条，一个写错的探针命令会让两项一起 PASS，那就是本脚本
    要防的那类假绿（对照 memory ``qa-system-same-status-code-false-green``）。

    这里断言的是**聚合结论**而非某一项的取值：脚本的契约是「全 PASS 才 exit 0」，
    逐项强绑反而会把检查名绑歪。
    """
    preflight = _loadModule()
    results = preflight.runChecks("img", runner=_fakeRunner(""))  # 探针啥也没返回

    assert all(r.passed for r in results) is False, "探针失效时不得全绿"
    byName = {r.name: r for r in results}
    assert byName["env_example_present"].passed is False
    assert "探针可能失效" in byName["env_example_present"].detail


def test_real_env_file_fails_the_gate() -> None:
    preflight = _loadModule()
    results = preflight.runChecks(
        "img", runner=_fakeRunner("/app/.env\n/app/.env.example\n")
    )

    byName = {r.name: r for r in results}
    assert byName["no_secret_env_files"].passed is False
    assert "/app/.env" in byName["no_secret_env_files"].detail
    assert byName["env_example_present"].passed is True