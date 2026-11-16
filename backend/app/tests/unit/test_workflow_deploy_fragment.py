"""P5 生产部署片段守卫（Compose **v1 1.29.2** 兼容 + 与代码同源）。

为什么需要这道守卫
------------------
生产 compose 是 **v1 1.29.2**（生产未装 v2 插件）⇒ 以下写法在生产**直接炸**：

- ``!override`` / ``!reset``：v2 ≥ 2.24 才有 ⇒ v1 解析即报错；
- ``profiles:``：v2 特性 ⇒ v1 静默忽略 ⇒ 以为"默认不启动"，其实灰度开关失效；
- ``docker compose`` 子命令：生产只有 ``docker-compose``（连字符）。

片段写错一次 = 生产容器起不来 = 现场改文件救火。
⇒ 把「片段必须 v1 安全」「片段里的模块路径 / 开关名必须与代码一致」
钉成**可执行**的断言，而不是 runbook 里的一句提醒。

断言口径
--------
断**片段的解析结果**（不是原始文本）—— 注释里写「禁 profiles」不算违规，
真正违规是**结构里出现该键**。另用「未知 YAML tag 必抛错」的 loader 反向证明
解析器确实在检查 tag（否则 ``!override`` 会被静默放过）。

⚠️ 本守卫只覆盖**结构**，不覆盖「生产 compose 的 version 字段是什么」——
那一项只能在生产侧用 ``docker-compose config --quiet`` 实测（runbook §2）。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

# 片段在仓内的位置（相对仓根）。
_FRAGMENT_REL = pathlib.Path(
    "Harness/changes/2026-11-16-p5-workflow-command/compose.workflow-worker.centos7.fragment.yml"
)

# 生产服务名 / 容器名（钉死：runbook 与验收探针都按这两个名字查）。
_SERVICE_NAME = "workflow-worker"
_CONTAINER_NAME = "qa-workflow-worker"

# 生产 backend 现役镜像（离线加载 + 显式 tag；**不是** compose 派生名）。
_BACKEND_IMAGE = "qa-backend:offline"

# Worker 的启动命令（必须与仓内模块路径逐字一致）。
_COMMAND = ["python", "-m", "app.workers.workflow_worker"]

# 片段里**不允许出现**的键：
#   - v2 专属（profiles / develop / deploy）：生产是 v1 ⇒ 炸或静默失效；
#   - 本片段不需要的（build / ports / volumes / depends_on）：生产是离线镜像单容器。
_FORBIDDEN_KEYS = ("profiles", "develop", "deploy", "build", "ports", "volumes", "depends_on")


def _repoRoot() -> pathlib.Path:
    # backend/app/tests/unit/<file>.py ⇒ parents[4] = 仓根
    return pathlib.Path(__file__).resolve().parents[4]


def _fragmentPath() -> pathlib.Path:
    return _repoRoot() / _FRAGMENT_REL


def _loadFragment() -> dict:
    """解析片段（当作 ``services:`` 下的映射）。

    ⚠️ 用 SafeLoader：遇到 ``!override`` / ``!reset`` 这类未知 tag 会抛
    ``ConstructorError`` ⇒ v2 写法会被本守卫直接抓住。
    """

    text = _fragmentPath().read_text(encoding="utf-8")
    parsed = yaml.safe_load("services:\n" + text)
    assert isinstance(parsed, dict), "片段解析结果不是映射"
    return parsed


@pytest.fixture()
def service() -> dict:
    path = _fragmentPath()
    assert path.exists(), f"生产部署片段缺失：{_FRAGMENT_REL}"
    services = _loadFragment()["services"]
    assert _SERVICE_NAME in services, f"片段里没有 {_SERVICE_NAME} 服务：{sorted(services)}"
    return services[_SERVICE_NAME]


# =============================================================================
# v1 兼容性
# =============================================================================


def test_片段不含v2专属键(service: dict):
    """v1 1.29.2 不认这些键（``profiles`` 还是**静默忽略** ⇒ 更危险）。"""

    offenders = [key for key in _FORBIDDEN_KEYS if key in service]

    assert offenders == [], f"片段含生产用不了的键（生产是 v1 单文件）：{offenders}"


def test_片段用镜像而非构建(service: dict):
    """生产是离线镜像 ⇒ 必须显式写 ``image:``（不写 ⇒ compose 去拉不存在的镜像名）。

    同时钉住「复用 backend 现役镜像」⇒ 不新增镜像（计划 D-9 / D-11）。
    """

    assert service.get("image") == _BACKEND_IMAGE, (
        f"worker 必须复用 backend 现役镜像 {_BACKEND_IMAGE}（不新增镜像）：{service.get('image')!r}"
    )
    assert "build" not in service, "生产带 build: 会触发现场构建"


def test_片段不发布端口且不依赖其它服务(service: dict):
    """Worker 只出站访问 PG ⇒ 不发布端口；不写 ``depends_on``（v1 各 version 形状不一）。"""

    assert "ports" not in service
    assert "depends_on" not in service


def test_片段沿用现有环境注入方式(service: dict):
    """容器环境仍走 ``env_file``（与生产 backend 同源）⇒ 秘钥不进 compose 文件。"""

    assert service.get("env_file") == [".env"], service.get("env_file")
    assert "environment" in service, "开关必须显式写在 environment（覆盖 env_file 的同名变量）"


# =============================================================================
# 与代码同源（防止改了一边忘了另一边）
# =============================================================================


def test_启动命令指向仓内真实模块(service: dict):
    """``command`` 里的模块路径必须真实存在（拼错 = 容器起来即退）。"""

    assert service.get("command") == _COMMAND, service.get("command")

    modulePath = pathlib.Path(__file__).resolve().parents[2] / "workers" / "workflow_worker.py"
    assert modulePath.exists(), f"仓内没有 worker 模块：{modulePath}"
    assert 'if __name__ == "__main__":' in modulePath.read_text(encoding="utf-8"), (
        "worker 模块必须支持 `python -m` 入口（否则片段里的 command 起不来）"
    )


def test_开关名与配置别名一致(service: dict):
    """片段里的开关名必须等于 ``app/config.py`` 的 alias（改名漂移 ⇒ 开关静默失效）。"""

    assert service.get("environment", {}).get("ENABLE_WORKFLOW") == "true"

    configText = (pathlib.Path(__file__).resolve().parents[2] / "config.py").read_text(encoding="utf-8")
    assert 'alias="ENABLE_WORKFLOW"' in configText, (
        "config.py 里没有 ENABLE_WORKFLOW 别名 ⇒ 片段写的开关名对不上（开关会静默失效）"
    )


def test_容器名钉死(service: dict):
    """容器名被 runbook 与验收探针引用 ⇒ 改名会让排障脚本失效。"""

    assert service.get("container_name") == _CONTAINER_NAME


def test_片段不会因未知tag被静默放过():
    """反向自检：``!override`` 这类未知 tag 必须让解析**失败**。

    否则守卫会以为「片段能被解析」= 合规，而生产 v1 会当场炸。
    """

    badFragment = "  x:\n    volumes: !override []\n"

    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("services:\n" + badFragment)


def test_片段是services下的服务块():
    """片段的缩进必须是 2 空格（= 挂在 ``services:`` 下的服务块）。

    ⚠️ **实测坑**（2026-11-17 本地 `config --quiet` 复现）：直接
    ``cat fragment >> compose.yml``（追加到**文件末尾**）时，片段会落进**最后一个
    顶层键**里（生产 compose 末尾是 ``volumes:``）⇒ compose 报
    ``volumes.workflow-worker additional properties ... not allowed``。
    ⇒ 必须用 runbook §9.2 的 awk 插入法（插到 ``services:`` 行之后）。

    本断言把「片段自己是一个纯服务块」钉死（顶层解析只得出服务名），
    让人一眼看出它该挂在哪里。
    """

    text = _fragmentPath().read_text(encoding="utf-8")

    serviceLines = [
        line for line in text.splitlines() if line.strip().startswith(f"{_SERVICE_NAME}:")
    ]
    assert len(serviceLines) == 1, f"服务键应恰好出现一次：{serviceLines}"
    indent = len(serviceLines[0]) - len(serviceLines[0].lstrip())
    assert indent == 2, f"服务键缩进必须是 2 空格（挂在 services: 下），实际 {indent}"

    topLevel = yaml.safe_load(text)
    assert list(topLevel) == [_SERVICE_NAME], (
        f"片段顶层只应有服务名，实际 {list(topLevel)}（混进顶层键 ⇒ 挂错位置）"
    )
