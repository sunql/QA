"""CentOS overlay 契约测试。

overlay 是纯 YAML，没有可单测的函数。这里的「测试」是把它当数据断言，
防止后续编辑破坏 Mac 开发环境或漏掉 CentOS 专属处理。

⚠️ 本文件必须能证明 `!override` 这个标签本身存在 —— 它是 overlay 里最关键的
一个 token：丢了它（或者把它拼错），`./init` bind mount 会静默回到生产宿主机上，
而 PyYAML 解析出来的树完全一样。所以标签不是「剥掉就完事」，必须被**记录**
下来并断言（见 `_TaggedList` / `test_mysql_demo_volumes_use_override_tag`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
OVERLAY = REPO_ROOT / "docker" / "docker-compose.centos.yml"
BASE_COMPOSE = REPO_ROOT / "docker" / "docker-compose.yml"

#: 主 compose 里 mysql-demo 上唯一的 bind mount（docker-compose.yml:142）。
#: overlay 的职责就是把它去掉，所以这个集合是「overlay 声称已消除的清单」——
#: 主 compose 改了这里，测试立刻红。
EXPECTED_BASE_BIND_MOUNTS = {"./init:/docker-entrypoint-initdb.d"}


class _TaggedList(list):
    """带 Compose 标签的列表。

    `yaml` 的 multi constructor 会把未知标签（如 `!override`）连同节点一起
    交给我们。把标签记在值上，而不是直接丢掉，标签就成了**可断言的**：
    拼错成 `!overide` 时 `.tag` 变成 `"overide"`，断言立刻失败。
    """

    def __init__(self, values, tag: str):
        super().__init__(values)
        self.tag = tag


class _TaggedMap(dict):
    """带 Compose 标签的映射（`!override` 也可能挂在 mapping 上）。"""

    def __init__(self, values, tag: str):
        super().__init__(values)
        self.tag = tag


class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader。

    overlay 是写给 `docker compose` 读的。`!override`（Compose ≥ 2.24）
    表示「整个替换掉这个字段」，PyYAML 不认识它，`yaml.safe_load` 会抛
    ConstructorError。对本测试而言，标签的**值**没有语义，我们要断言的是
    替换之后留下的内容 —— 但标签**本身**必须留痕，否则测试无法区分
    「带 `!override` 的列表」和「不带标签的普通列表」，恒真断言就此产生。
    """


def _unwrapUnknownTag(loader, suffix, node):
    """剥掉未知标签，但把标签名记进值对象。

    `suffix` 形如 `"!override"`（含前导 `!`），这里统一去掉 `!`。
    """
    tag = suffix.lstrip("!")
    if isinstance(node, yaml.SequenceNode):
        return _TaggedList(loader.construct_sequence(node, deep=True), tag)
    if isinstance(node, yaml.MappingNode):
        return _TaggedMap(loader.construct_mapping(node, deep=True), tag)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


def _loadComposeFile(path: Path) -> dict:
    """读 compose 文件；文件不存在时抛 FileNotFoundError（测试 ERROR），而不是 skip。

    skip 会让「文件被误删」和「测试还没写」长得一模一样 —— 这正是
    qa-system-chat-plan-empty-fixture-pattern 记录的假绿模式。
    """
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _loadComposeFile(OVERLAY)


@pytest.fixture(scope="module")
def base_compose() -> dict:
    """主 compose —— 用来做 overlay 与 base 之间的漂移检查。"""
    return _loadComposeFile(BASE_COMPOSE)


def _isNamedVolume(spec: str) -> bool:
    """区分命名卷与 bind mount。

    bind mount 的 source 以 `.` / `/` / `~` 开头（相对路径、绝对路径、home 展开）；
    命名卷的 source 是裸名字。
    """
    source = str(spec).split(":", 1)[0].strip()
    return not (source.startswith(".") or source.startswith("/") or source.startswith("~"))


def test_overlay_defines_ollama_service(overlay):
    assert "ollama" in overlay["services"]


def test_ollama_binds_all_interfaces(overlay):
    """默认只听 127.0.0.1，容器经 host-gateway 够不着 —— 这是最易漏的一条。"""
    assert overlay["services"]["ollama"]["environment"]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_ollama_persists_models_in_named_volume(overlay):
    """模型 blob 必须落卷，否则重启要重新灌几十 GB。

    断言**所有**挂载都是命名卷：只查 `volumes[0]` 的话，后面追加一个 bind mount
    会悄悄溜过去，把模型目录挂到宿主某处 —— 恰恰是 overlay 要避免的形态。
    """
    volumes = overlay["services"]["ollama"]["volumes"]
    assert "ollama_models:/root/.ollama" in volumes
    assert all(_isNamedVolume(v) for v in volumes), f"ollama 不应有 bind mount: {volumes}"


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

    并且要区分「bind mount 被删掉」和「volumes 整个没了」：后者同样满足
    「没有 ./init」，但意味着 MySQL 数据目录会落进容器可写层。
    """
    volumes = overlay["services"]["mysql-demo"]["volumes"]
    assert len(volumes) > 0, "mysql-demo 的 volumes 整体消失 —— MySQL 数据会落容器可写层"
    assert all("./init" not in str(v) for v in volumes)
    assert all(_isNamedVolume(v) for v in volumes), f"overlay 不应保留任何 bind mount: {volumes}"


def test_overlay_keeps_mysql_data_volume(overlay):
    """`!override` 是整字段替换，不是「删除其中一项」。

    漏掉这一条：MySQL 的数据目录会落进容器可写层，`docker compose down`
    之后就没了 —— 而这正是 overlay 里唯一有数据风险的写法。
    """
    assert "mysql_data:/var/lib/mysql" in overlay["services"]["mysql-demo"]["volumes"]


def test_mysql_demo_volumes_use_override_tag(overlay):
    """`!override` 必须真的写在 mysql-demo.volumes 上。

    这是 overlay 里最关键的一个 token：Compose 对 list 型字段按 target 合并，
    没有 `!override` 的话 `./init` bind mount 会**静默**回到生产宿主机
    （Compose v5.6.0 实测：`!overide` 这种拼错也不报错，exit 0、stderr 为空）。
    而纯 PyYAML 解析出来的树在「带 tag」与「不带 tag」时完全一样 —— 所以
    标签必须留痕才能断言，光看解析结果永远发现不了问题。
    """
    volumes = overlay["services"]["mysql-demo"]["volumes"]
    assert isinstance(volumes, _TaggedList), (
        "mysql-demo.volumes 必须带 !override 标签（当前没有标签，说明 !override 被删掉了）"
    )
    assert volumes.tag == "override", (
        f"mysql-demo.volumes 的标签应为 'override'，实际为 {volumes.tag!r}（拼错会让 bind mount 静默复活）"
    )


def test_overlay_volumes_match_base_minus_bind_mounts(overlay, base_compose):
    """overlay 与主 compose 之间的漂移守卫（`!override` 的维护债）。

    `!override` 是整字段替换：主 compose 里给 mysql-demo 新加的卷**不会**
    自动进到 overlay（overlay 会把它盖掉）。所以两边必须手工同步。

    本测试断言：
      1. 主 compose 上 mysql-demo 的 bind mount 集合恰好是已知的那个
         —— 新增 bind mount 时测试立刻红（否则它会绕过 overlay 复活）；
      2. overlay 的卷集合恰好等于主 compose 的命名卷集合
         —— 新增命名卷时测试立刻红。
    """
    base_volumes = base_compose["services"]["mysql-demo"]["volumes"]
    base_binds = {v for v in base_volumes if not _isNamedVolume(v)}
    base_named = [v for v in base_volumes if _isNamedVolume(v)]

    assert base_binds == EXPECTED_BASE_BIND_MOUNTS, (
        f"主 compose 的 mysql-demo bind mount 集合变了：{base_binds}；"
        f"overlay 只会消除 {EXPECTED_BASE_BIND_MOUNTS}"
    )
    assert list(overlay["services"]["mysql-demo"]["volumes"]) == base_named, (
        f"overlay 的 mysql-demo 卷 {list(overlay['services']['mysql-demo']['volumes'])} "
        f"与主 compose 的命名卷 {base_named} 不一致"
    )


def test_overlay_does_not_touch_other_services(overlay):
    """overlay 只允许改 ollama 与 mysql-demo，其余服务一律不得出现。"""
    assert set(overlay["services"]) == {"ollama", "mysql-demo"}
