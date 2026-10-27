"""编排 Worker 部署形状 + 编排菜单种子契约单测（Task 11）。

两块契约：

1. **部署形状（Compose）** —— Worker 必须 profile 门控、默认不启动、不发布
   宿主端口、不被 API/前端服务拉起、镜像与 backend 同源。
2. **菜单种子** —— 两个新菜单项**不得**出现在默认 ``seed_menu_config.ITEMS``
   里（默认暴露 = 提前上线），必须由显式脚本播种。

「有效形状」优先于「原始 YAML」：是否默认启动是 Compose 的 profile 语义决定的，
只看 YAML 文本看不出门禁/合并结果。所以除了纯 YAML 断言，还用
``docker compose config`` 渲染**生效后**的配置来断言（默认 vs ``--profile``）。

⚠️ 假绿防线：有效形状断言一律针对「渲染后的服务集合」，禁止只断言某个字段
存在就算过（服务缺失时字段同样「不存在」，会静默通过）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BASE_COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"
CENTOS_COMPOSE = REPO_ROOT / "docker" / "docker-compose.centos.yml"

WORKER_SERVICE = "agent-orchestration-worker"
WORKER_PROFILE = "agent-orchestration"
WORKER_COMMAND = ["python", "-m", "app.workers.agent_orchestration_worker"]
WORKER_MODULE = "app.workers.agent_orchestration_worker"

#: 编排菜单的 code / path（与 seed 脚本的 ITEMS 一一对应）。
ORCHESTRATION_MENU_CODES = {"item.agentOrchestration", "item.agentTasks"}
ORCHESTRATION_MENU_PATHS = {"/agent-orchestrations", "/agent-tasks"}

#: 渲染 compose 时用的假环境变量。`docker/.env` 在仓库里被 gitignore，
#: 缺失会让 `docker compose config` 直接报错，所以这里自造一份。
DUMMY_ENV: dict[str, str] = {
    "POSTGRES_PASSWORD": "dummy",
    "NEO4J_PASSWORD": "dummy",
    "MINIO_ROOT_USER": "dummy",
    "MINIO_ROOT_PASSWORD": "dummy",
    "MYSQL_ROOT_PASSWORD": "dummy",
    "MYSQL_USER": "dummy",
    "MYSQL_PASSWORD": "dummy",
    # 敌意值：worker 必须用自己的 `environment` 把它盖成 true，
    # 否则「Worker 起来了但 flag 是 false ⇒ 静默不轮询」。
    "ENABLE_AGENT_ORCHESTRATION": "false",
}


# ----------------------------------------------------------------------
# Compose 文件解析（!override 容错）
# ----------------------------------------------------------------------
class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader（overlay 里有 `!override`）。"""


def _unwrapUnknownTag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


def _loadComposeFile(path: Path) -> dict:
    """读 compose 文件；文件不存在时抛 FileNotFoundError（测试 ERROR），不 skip。"""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


@pytest.fixture(scope="module")
def baseCompose() -> dict:
    return _loadComposeFile(BASE_COMPOSE)


@pytest.fixture(scope="module")
def centosOverlay() -> dict:
    return _loadComposeFile(CENTOS_COMPOSE)


def _profiledServices(compose: dict) -> dict:
    """返回带 profiles 键的服务（profile 门控的服务）。"""
    return {
        name: svc
        for name, svc in (compose.get("services") or {}).items()
        if isinstance(svc, dict) and "profiles" in svc
    }


# ----------------------------------------------------------------------
# 1. 纯 YAML 层面的部署契约
# ----------------------------------------------------------------------
def test_base_compose_defines_exactly_one_profiled_service(baseCompose):
    """整个 compose 里只允许有一个 profile 门控的服务（就是编排 Worker）。

    「恰好一个」比「至少一个」强：多出来的门控服务会让运维的启动命令含义漂移。
    """
    profiled = _profiledServices(baseCompose)
    assert set(profiled) == {WORKER_SERVICE}, (
        f"带 profiles 的服务集合应为 {{{WORKER_SERVICE}}}，实际 {sorted(profiled)}"
    )
    assert WORKER_PROFILE in profiled[WORKER_SERVICE]["profiles"]
    assert profiled[WORKER_SERVICE]["profiles"] == [WORKER_PROFILE]


def test_worker_command_runs_worker_module(baseCompose):
    worker = baseCompose["services"][WORKER_SERVICE]
    assert worker.get("command") == WORKER_COMMAND


def test_worker_enables_feature_flag(baseCompose):
    """Worker 必须显式打开 flag —— 否则容器起来了却不轮询（静默空转）。"""
    worker = baseCompose["services"][WORKER_SERVICE]
    env = worker.get("environment") or {}
    assert str(env.get("ENABLE_AGENT_ORCHESTRATION")).lower() == "true"


def test_worker_publishes_no_ports(baseCompose):
    """Worker 不对外提供服务，绝不能发布宿主端口。"""
    worker = baseCompose["services"][WORKER_SERVICE]
    assert "ports" not in worker, f"Worker 不得发布端口：{worker.get('ports')}"
    assert not worker.get("network_mode", "").startswith("host")


def test_worker_reuses_backend_build(baseCompose):
    """Worker 必须与 backend 同源构建（同一 Dockerfile + 同一 context）。"""
    services = baseCompose["services"]
    assert services[WORKER_SERVICE]["build"] == services["backend"]["build"]


def test_worker_matches_backend_service_conventions(baseCompose):
    """env_file / restart / extra_hosts / depends_on 与 backend 保持一致。

    依赖集合必须完全相同：少一个（如 neo4j）会随机启动失败，多一个会拖慢启动。
    """
    services = baseCompose["services"]
    worker = services[WORKER_SERVICE]
    backend = services["backend"]
    assert worker["env_file"] == backend["env_file"]
    assert worker["restart"] == backend["restart"]
    assert worker["extra_hosts"] == backend["extra_hosts"]
    assert set(worker["depends_on"]) == set(backend["depends_on"])


def test_backend_service_does_not_run_worker(baseCompose):
    """默认 backend 服务的命令里不得含 Worker 入口。

    禁止「API 顺手把 Worker 拉起来」：那会让 feature flag 关闭时也起进程。
    """
    backend = baseCompose["services"]["backend"]
    serialized = json.dumps(backend, ensure_ascii=False)
    assert WORKER_MODULE not in serialized
    assert "agent_orchestration_worker" not in serialized
    assert WORKER_SERVICE not in serialized


def test_no_service_auto_starts_worker(baseCompose):
    """没有任何服务（API / 前端 / 其他）通过 depends_on 拉起 Worker。"""
    services = baseCompose["services"]
    for name, svc in services.items():
        if name == WORKER_SERVICE:
            continue
        depends = svc.get("depends_on") or {}
        assert WORKER_SERVICE not in depends, f"{name} 依赖了 Worker"
        assert WORKER_MODULE not in json.dumps(svc, ensure_ascii=False)


def test_centos_overlay_does_not_redefine_worker(centosOverlay):
    """CentOS overlay 不重复定义 Worker —— 基础文件里的定义会被合并继承。

    重复定义会引入漂移风险（overlay 少写一个 profiles ⇒ 生产默认就起 Worker）。
    """
    assert WORKER_SERVICE not in (centosOverlay.get("services") or {})


# ----------------------------------------------------------------------
# 2. 有效形状：docker compose config 渲染后的结果
# ----------------------------------------------------------------------
def _dockerComposeAvailable() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return proc.returncode == 0


@pytest.fixture(scope="module")
def composeWorkspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把两个 compose 文件复制到临时目录，并造一份假的 .env。

    `docker/.env` 在仓库里被 gitignore，缺失时 `docker compose config` 直接失败；
    复制到临时目录可以避免测试读真实凭据（也避免打印 .env）。
    """
    workspace = tmp_path_factory.mktemp("compose")
    shutil.copyfile(BASE_COMPOSE, workspace / "docker-compose.yml")
    shutil.copyfile(CENTOS_COMPOSE, workspace / "docker-compose.centos.yml")
    (workspace / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in DUMMY_ENV.items()) + "\n",
        encoding="utf-8",
    )
    return workspace


def _renderComposeConfig(workspace: Path, *, profile: str | None = None,
                          withCentos: bool = False) -> dict:
    args = ["docker", "compose", "-f", str(workspace / "docker-compose.yml")]
    if withCentos:
        args += ["-f", str(workspace / "docker-compose.centos.yml")]
    if profile:
        args += ["--profile", profile]
    args += ["config", "--format", "json"]
    env = dict(os.environ)
    env.update(DUMMY_ENV)
    proc = subprocess.run(args, capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0, (
        f"docker compose config 失败：{' '.join(args)}\n{proc.stderr or ''}"
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def defaultConfig(composeWorkspace: Path) -> dict:
    if not _dockerComposeAvailable():
        pytest.skip("docker compose 不可用，有效形状断言无法执行")
    return _renderComposeConfig(composeWorkspace)


@pytest.fixture(scope="module")
def profileConfig(composeWorkspace: Path) -> dict:
    if not _dockerComposeAvailable():
        pytest.skip("docker compose 不可用，有效形状断言无法执行")
    return _renderComposeConfig(composeWorkspace, profile=WORKER_PROFILE)


def test_default_config_excludes_worker_service(defaultConfig):
    """默认部署形状：Worker 不在生效服务集合里（profile 门控生效）。"""
    services = defaultConfig["services"]
    assert WORKER_SERVICE not in services, (
        f"默认 config 里出现了 Worker：{sorted(services)}"
    )


def test_profile_config_includes_worker_with_expected_shape(profileConfig):
    services = profileConfig["services"]
    assert WORKER_SERVICE in services, (
        f"--profile {WORKER_PROFILE} 后仍缺 Worker：{sorted(services)}"
    )
    worker = services[WORKER_SERVICE]
    assert worker.get("command") == WORKER_COMMAND
    assert str(worker["environment"]["ENABLE_AGENT_ORCHESTRATION"]).lower() == "true"
    assert worker.get("profiles") == [WORKER_PROFILE]
    assert worker["build"] == services["backend"]["build"]
    assert "ports" not in worker


def test_profile_config_keeps_backend_unchanged(profileConfig):
    """打开 profile 不得改变 backend 服务的形状（命令仍是镜像默认 CMD）。"""
    backend = profileConfig["services"]["backend"]
    assert not backend.get("command")
    assert WORKER_MODULE not in json.dumps(backend, ensure_ascii=False)


def test_profile_config_has_no_service_depending_on_worker(profileConfig):
    for name, svc in profileConfig["services"].items():
        if name == WORKER_SERVICE:
            continue
        assert WORKER_SERVICE not in (svc.get("depends_on") or {})
        assert WORKER_MODULE not in json.dumps(svc, ensure_ascii=False)


def test_centos_merged_config_keeps_worker_opt_in(composeWorkspace: Path):
    """CentOS 部署（base + overlay 合并）同样必须默认不起 Worker。"""
    if not _dockerComposeAvailable():
        pytest.skip("docker compose 不可用，有效形状断言无法执行")
    mergedDefault = _renderComposeConfig(composeWorkspace, withCentos=True)
    assert WORKER_SERVICE not in mergedDefault["services"]

    mergedProfile = _renderComposeConfig(
        composeWorkspace, profile=WORKER_PROFILE, withCentos=True
    )
    assert WORKER_SERVICE in mergedProfile["services"]
    worker = mergedProfile["services"][WORKER_SERVICE]
    assert worker.get("profiles") == [WORKER_PROFILE]
    assert "ports" not in worker
    assert worker["build"] == mergedProfile["services"]["backend"]["build"]


def test_default_compose_config_is_valid(profileConfig):
    """默认 config 必须能被 compose 正常渲染（不靠 skip 掩盖解析失败）。"""
    if not _dockerComposeAvailable():
        pytest.skip("docker compose 不可用，有效形状断言无法执行")
    assert "backend" in profileConfig["services"]
    assert "frontend" in profileConfig["services"]


# ----------------------------------------------------------------------
# 3. 菜单种子脚本契约
# ----------------------------------------------------------------------
def _loadSeedModule() -> Any:
    """按路径加载 seed 脚本模块（脚本不在 app 包里）。"""
    path = REPO_ROOT / "backend" / "scripts" / "seed_agent_orchestration_menu.py"
    spec = importlib.util.spec_from_file_location("seed_agent_orchestration_menu", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_orchestration_seed_declares_exactly_two_items():
    """种子项的字段值必须与部署契约一致（path / sort_order / label_key）。"""
    module = _loadSeedModule()
    items = {item["code"]: item for item in module.ITEMS}
    assert set(items) == ORCHESTRATION_MENU_CODES
    assert items["item.agentOrchestration"] == {
        "code": "item.agentOrchestration",
        "label_key": "menu.item.agentOrchestration",
        "icon_code": "appstore",
        "sort_order": 140,
        "path": "/agent-orchestrations",
    }
    assert items["item.agentTasks"] == {
        "code": "item.agentTasks",
        "label_key": "menu.item.agentTasks",
        "icon_code": "thunderbolt",
        "sort_order": 150,
        "path": "/agent-tasks",
    }


def test_orchestration_seed_targets_ai_agent_section():
    module = _loadSeedModule()
    assert module.SECTION_CODE == "section.aiAgent"


def test_default_menu_seed_does_not_expose_orchestration_items():
    """默认 seed 不得包含编排菜单项 —— 否则新装库就提前暴露入口。

    这是「不提前上线」的第一道闸门：菜单行进了默认 ITEMS，就等于默认上线。
    """
    module = _loadSeedModule()
    assert module.SECTION_CODE == "section.aiAgent"
    from scripts import seed_menu_config as defaultSeed

    defaultCodes = {item["code"] for item in defaultSeed.ITEMS}
    defaultPaths = {item.get("path") for item in defaultSeed.ITEMS}
    assert defaultCodes.isdisjoint(ORCHESTRATION_MENU_CODES)
    assert defaultPaths.isdisjoint(ORCHESTRATION_MENU_PATHS)
