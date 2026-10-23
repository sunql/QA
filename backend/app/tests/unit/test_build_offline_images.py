"""离线镜像清单必须与 compose 实际用到的镜像一致。

这是防「漏打一个镜像，到了 CentOS 才 pull 失败」的唯一低成本检查。
compose 里新增/删除服务时，这个测试会先红。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[4]
COMPOSE_FILES = [REPO / "docker" / "docker-compose.yml",
                 REPO / "docker" / "docker-compose.centos.yml"]
SCRIPT = REPO / "backend" / "scripts" / "build_offline_images.sh"


class _ComposeLoader(yaml.SafeLoader):
    """能读懂 Compose 专属 YAML 标签的 SafeLoader。

    overlay 里有 `!override`（Compose ≥ 2.24 的整字段替换标签）。
    `yaml.safe_load` 不认识它，会抛 ConstructorError —— 于是这个测试在
    overlay 一落地就直接 ERROR，而不是给出有意义的断言。
    标签本身对「收集镜像名」没有语义，剥掉、保留值即可。
    """


def _unwrapUnknownTag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _unwrapUnknownTag)


def _composeImages() -> set[str]:
    images: set[str] = set()
    for path in COMPOSE_FILES:
        if not path.exists():
            continue
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)
        for svc in (data.get("services") or {}).values():
            if "image" in svc:
                images.add(svc["image"])
    return images


def _scriptImages() -> set[str]:
    """从 IMAGES=(...) 数组里取镜像名。

    数组元素带引号（  "postgres:16-alpine" ），正则必须容许首尾引号，
    否则 listed 恒为空、missing 恒为全集，这个测试就永久失败。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"^IMAGES=\((.*?)^\)", text, re.S | re.M)
    assert block, "找不到 IMAGES=(...) 数组"
    return set(re.findall(r"""['"]?([a-z0-9._/-]+:[a-zA-Z0-9._-]+)['"]?""",
                          block.group(1)))


def _scriptCode() -> str:
    """脚本里去掉注释之后的正文。

    裸子串断言最常见的假绿来源就是注释：注释里写一句 "docker save" 就能让
    `assert "docker save" in text` 成立，命令真被删了测试照样绿。
    凡是「某个东西必须真的在跑」的断言，都得在这份正文上做。

    整行注释和行尾注释都要剥 —— 只剥整行的话，把命令挪到行尾注释里
    （`build frontend  # --build-arg ...`）照样能骗过断言。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    return "\n".join(lines)


def test_script_exists():
    assert SCRIPT.exists(), "build_offline_images.sh 必须存在"


def test_script_lists_every_compose_image():
    missing = _composeImages() - _scriptImages()
    assert not missing, f"脚本漏了这些镜像：{sorted(missing)}"
    # compose 文件的**路径**也得对：Step 5 从**仓库根**调用本脚本，脚本自己不 cd，
    # 裸文件名（-f docker-compose.yml）会 no such file or directory。
    code = _scriptCode()
    assert "-f docker/docker-compose.yml" in code, \
        "compose 路径必须是 docker/docker-compose.yml（Step 5 从仓库根调用）"
    assert "-f docker/docker-compose.centos.yml" in code, \
        "compose 路径必须是 docker/docker-compose.centos.yml（Step 5 从仓库根调用）"
    # ⚠️ 只断「存在」不够：脚本里有两处 compose 调用，把其中一处退回裸文件名
    # 时「存在」仍然成立。所以反过来断裸写法一次都不许出现。
    assert "-f docker-compose.yml" not in code, "compose 路径不能退回裸文件名"
    assert "-f docker-compose.centos.yml" not in code, "compose 路径不能退回裸文件名"


def test_script_parser_is_not_silently_empty():
    """守卫上面那个引号 bug：解析出 0 个镜像时必须红，而不是让 missing 变成全集。

    下限取 9 —— 合并后 compose 的 8 个外部镜像 + alpine:3.20（通用工具容器，
    Task 4 Step 6 的 run 门禁也要用它）。写成 8 的话，正则只多吐出一个也能过。
    """
    assert len(_scriptImages()) >= 9, f"只解析出 {len(_scriptImages())} 个镜像，正则多半坏了"


def test_compose_image_loader_tolerates_the_centos_overlay():
    """overlay 用 `!override`，普通 safe_load 读不了 —— 守卫这一点别退回去。"""
    images = _composeImages()
    assert "ollama/ollama:latest" in images, "overlay 里的 ollama 必须被收进来"
    assert "postgres:16-alpine" in images


def test_script_forces_amd64_platform():
    """脚本把平台抽成了变量（`docker pull --platform "$PLATFORM"`），
    所以不能断言字面量 "--platform linux/amd64" 在文件里 —— 那样恒失败。
    拆成三条：pull 用了 flag、PLATFORM 的值确实是 amd64、compose build 侧也钉住了。
    三条缺一不可：只断 flag 的话，写成 PLATFORM=linux/arm64 照样过；
    只断 pull 的话，build 侧退化成 arm64 镜像照样过。

    compose build 侧不是靠 `--platform`：`docker compose build` 根本没有这个 flag
    （compose v2.27.1 与 v5.6.0 的 build 子命令都没有），写了会报 unknown flag。
    真正生效的是 DOCKER_DEFAULT_PLATFORM —— compose 的 applyPlatforms() 读它
    给没写 platform 的服务补 build.platforms。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    code = _scriptCode()
    assert "--platform" in text, "拉镜像和 build 都没钉平台"
    assert re.search(r'^PLATFORM="linux/amd64"$', text, re.M), \
        "Mac 是 arm64，不钉 amd64 会打出跑不了的镜像"
    # ⚠️ 下面两条必须断**正文**而不是全文：注释里就写着 "--platform" 和
    # "DOCKER_DEFAULT_PLATFORM"，断全文的话命令被删了测试照样绿。
    assert "--platform" in code, "docker pull 必须显式钉平台"
    assert re.search(r'DOCKER_DEFAULT_PLATFORM="\$PLATFORM"', code), \
        "compose build 必须用 DOCKER_DEFAULT_PLATFORM 钉住 amd64"


def test_script_passes_npm_registry_to_the_frontend_build():
    """NPM_REGISTRY 必须以 `--build-arg` 传进 build 阶段。

    只写 `NPM_REGISTRY=xxx docker compose build` 是不够的：宿主环境变量只参与
    compose 自身的 ${} 插值，buildkit 收不到（实测 build args 里只有
    VITE_API_BASE_URL）。少了 --build-arg，Dockerfile.frontend 的
    ARG NPM_REGISTRY 会退回默认的 registry.npmjs.org —— 正是这条要防的卡死。
    """
    code = _scriptCode()
    assert '--build-arg NPM_REGISTRY="$NPM_REGISTRY"' in code, \
        "NPM_REGISTRY 必须用 --build-arg 传，否则前端构建会走官方 npm 源"


def test_script_saves_and_checksums():
    text = SCRIPT.read_text(encoding="utf-8")
    code = _scriptCode()
    assert "docker save" in text
    assert "sha256sum" in text
    # ⚠️ 上面两条是子串断言，注释里出现 "docker save" 也能满足它们 ——
    # 只断子串的话，把两处 docker save 全删掉测试照样绿。两个 tar
    # （外部镜像包 + 本地构建包）必须各有一条真正的 docker save 命令。
    assert code.count("docker save -o") >= 2, \
        "外部镜像包与本地构建包两个 tar 都要有 docker save 命令"
    assert "sha256sum" in code, "sha256sum 必须真的在跑，不能只写在注释里"
    # 镜像 ID 清单是 Step 5 门禁 2（DIGEST-OK 比对）的输入，脚本不产出它
    # Step 5 就无从比对。而且必须带硬断言：文件为空时下游比对会「空空如也 ⇒ 通过」。
    assert re.search(r'>\s*images\.digests\.txt', code), \
        "必须产出镜像 ID 清单 images.digests.txt"
    assert re.search(r'\[ "\$n" -ge (\d+) \]', code), \
        "images.digests.txt 必须有行数硬断言，否则空文件会让下游比对恒过"
    assert int(re.search(r'\[ "\$n" -ge (\d+) \]', code).group(1)) >= 10, \
        "行数下限太低，空清单照样能过"
