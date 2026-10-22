"""CentOS overlay 契约测试。

overlay 是纯 YAML，没有可单测的函数。这里的「测试」是把它当数据断言，
防止后续编辑破坏 Mac 开发环境或漏掉 CentOS 专属处理。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

OVERLAY = Path(__file__).resolve().parents[4] / "docker" / "docker-compose.centos.yml"


class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader。

    overlay 是写给 `docker compose` 读的。`!override`（Compose ≥ 2.24）
    表示「整个替换掉这个字段」，PyYAML 不认识它，`yaml.safe_load` 会抛
    ConstructorError。对本测试而言，标签本身没有语义 —— 我们要断言的是
    替换之后留下的内容，所以直接把标签剥掉、保留值。
    """


def _unwrapUnknownTag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


@pytest.fixture(scope="module")
def overlay() -> dict:
    """文件不存在时抛 FileNotFoundError（测试 ERROR），而不是 skip。

    skip 会让「文件被误删」和「测试还没写」长得一模一样 —— 这正是
    qa-system-chat-plan-empty-fixture-pattern 记录的假绿模式。
    """
    return yaml.load(OVERLAY.read_text(encoding="utf-8"), Loader=_ComposeLoader)


def test_overlay_defines_ollama_service(overlay):
    assert "ollama" in overlay["services"]


def test_ollama_binds_all_interfaces(overlay):
    """默认只听 127.0.0.1，容器经 host-gateway 够不着 —— 这是最易漏的一条。"""
    assert overlay["services"]["ollama"]["environment"]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_ollama_persists_models_in_named_volume(overlay):
    """模型 blob 必须落卷，否则重启要重新灌几十 GB。"""
    assert "ollama_models" in overlay["services"]["ollama"]["volumes"][0]


def test_ollama_memory_settings_present(overlay):
    """32G 上预算合计 ≈32G（spec §7.4）。缺这两项会直接开始 swap。"""
    env = overlay["services"]["ollama"]["environment"]
    assert env["OLLAMA_KEEP_ALIVE"] == "5m"
    assert env["OLLAMA_NUM_PARALLEL"] == 1


def test_ollama_does_not_publish_ports(overlay):
    """只有 backend 需要访问 Ollama；暴露宿主端口等于把推理面开到 LAN。"""
    assert "ports" not in overlay["services"]["ollama"]


def test_overlay_drops_init_bind_mount(overlay):
    """唯一那个 bind mount 挂在 mysql-demo 上（docker-compose.yml:142）。

    这个断言必须查真正被改动的服务。查错服务会让「什么都没改」和
    「改对了」长得一模一样 —— 恒真断言是本仓最常见的假绿来源。
    """
    volumes = overlay["services"]["mysql-demo"]["volumes"]
    assert volumes is not None
    assert all("./init" not in str(v) for v in volumes)


def test_overlay_keeps_mysql_data_volume(overlay):
    """`!override` 是整字段替换，不是「删除其中一项」。

    漏掉这一条：MySQL 的数据目录会落进容器可写层，`docker compose down`
    之后就没了 —— 而这正是 overlay 里唯一有数据风险的写法。
    """
    assert "mysql_data:/var/lib/mysql" in overlay["services"]["mysql-demo"]["volumes"]


def test_overlay_does_not_touch_other_services(overlay):
    """overlay 只允许改 ollama 与 mysql-demo，其余服务一律不得出现。"""
    assert set(overlay["services"]) == {"ollama", "mysql-demo"}
