"""离线镜像脚本（Python 版）的行为单测。

只喂假数据：不真调 docker、不真跑 compose、不碰网络。

断言的是**行为**（喂假数据看输出），不是脚本文本的形状 ——
旧 bash 版只能断 `"awk '([^']*)'" in text` 这种字符串，
转换逻辑本身测不到，tag 归一与分隔符两个缺陷正是这么漏出去的
（`config --images` 给裸名、`docker images` 给带 tag 的，两边永远对不上）。

依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md §6.1。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts import build_offline_images as mod
from scripts.build_offline_images import (
    MIN_IMAGE_COUNT,
    NPM_REGISTRY_DEFAULT,
    PLATFORM,
    collectDigests,
    filterPackaged,
    normalizeTag,
)

REPO = Path(__file__).resolve().parents[4]
COMPOSE_FILES = [REPO / "docker" / "docker-compose.yml",
                 REPO / "docker" / "docker-compose.centos.yml"]

# compose 里没有 image: 字段的两个服务 —— 它们是本地 build 出来的
BUILT_IMAGE_NAMES = ["qa-system-backend:latest", "qa-system-frontend:latest"]


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


class FakeDocker:
    """假 docker：按命令形状回放固定输出，并记录所有调用。

    协议与脚本里的 runCommand 一致：run(args, env=None) -> stdout。
    """

    def __init__(self, composeImages=None, localImages=None, ids=None):
        self.composeImages = composeImages if composeImages is not None else []
        self.localImages = localImages if localImages is not None else []
        self.ids = ids or {}
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, args, env=None):
        args = list(args)
        self.calls.append((args, env))
        if args[:2] == ["docker", "pull"]:
            return ""
        if args[:2] == ["docker", "compose"]:
            if "config" in args:
                return "".join(name + "\n" for name in self.composeImages)
            return ""
        if args[:2] == ["docker", "images"]:
            return "".join(name + "\n" for name in self.localImages)
        if args[:3] == ["docker", "image", "inspect"]:
            return self.ids.get(args[-1], "sha256:" + "0" * 64)
        if args[:2] == ["docker", "save"]:
            return ""
        raise AssertionError(f"未预期的命令：{args!r}")

    def commands(self) -> list[list[str]]:
        return [args for args, _ in self.calls]


def _fakeId(tag: str) -> str:
    """给一个 tag 编一个稳定的假短 ID（12 位，与 docker images 的 {{.ID}} 同形）。"""
    seed = sum(ord(ch) for ch in tag)
    return "sha256:" + format(seed, "012x")


def _defaultComposeImages() -> list[str]:
    """compose 视角：8 个外部镜像（带 image: 字段）+ 2 个本地 build 的裸名。"""
    return sorted(_composeImages()) + ["qa-system-backend", "qa-system-frontend"]


def _localImages() -> list[str]:
    """本机视角：脚本 pull 的 9 个 + 本地 build 的 2 个。"""
    return sorted(mod.IMAGES) + BUILT_IMAGE_NAMES


# ---------------------------------------------------------------- normalizeTag


def test_normalizeTag_appends_latest_to_bare_name():
    assert normalizeTag("qa-system-backend") == "qa-system-backend:latest"


def test_normalizeTag_keeps_existing_tag():
    assert normalizeTag("postgres:16-alpine") == "postgres:16-alpine"


def test_normalizeTag_handles_registry_path_without_tag():
    """带 registry 路径的裸名也要补 tag（老 sed 的 /^([^/:]+)$/ 漏了这种）。"""
    assert normalizeTag("myorg/foo") == "myorg/foo:latest"


def test_normalizeTag_keeps_registry_port_repo():
    """registry 带端口（host:5000/foo）时，冒号不是 tag 分隔符。"""
    assert normalizeTag("host:5000/foo") == "host:5000/foo:latest"


def test_normalizeTag_keeps_registry_path_with_tag():
    assert normalizeTag("quay.io/coreos/etcd:v3.5.5") == "quay.io/coreos/etcd:v3.5.5"


# --------------------------------------------------------------- filterPackaged


def test_filterPackaged_drops_unrelated_images():
    packaged = {"postgres:16-alpine", "qa-system-backend:latest"}
    lines = ["postgres:16-alpine", "hello-world:latest", "argwarn-b:latest"]
    assert filterPackaged(packaged, lines) == ["postgres:16-alpine"]


def test_filterPackaged_drops_stale_tags_of_same_repo():
    """构建机留着 qa-system-backend:pre-deploy-… 时，只按仓库名匹配会把它也收进去
    ⇒ Step 5 那边「缺一个」⇒ 假红。必须按 repo:tag 精确匹配。"""
    packaged = {"qa-system-backend:latest"}
    lines = ["qa-system-backend:pre-deploy-20261022", "qa-system-backend:latest"]
    assert filterPackaged(packaged, lines) == ["qa-system-backend:latest"]


def test_filterPackaged_dedupes_repeated_names():
    packaged = {"postgres:16-alpine"}
    lines = ["postgres:16-alpine", "postgres:16-alpine"]
    assert filterPackaged(packaged, lines) == ["postgres:16-alpine"]


# --------------------------------------------------------------- collectDigests


def test_collectDigests_emits_one_line_per_tag():
    """重复 tag 必须只出一行（Step 5 门禁 3 是 n -eq 11，多一行就挂）。"""
    def fakeInspect(tag: str) -> str:
        return "sha256:d9e853e87e55aaaa"

    out = collectDigests(["alpine:3.20"], fakeInspect)
    assert out == ["alpine:3.20 d9e853e87e55"]


def test_collectDigests_strips_sha256_prefix_and_truncates_to_12():
    """inspect 的 {{.Id}} 是 64 位带 sha256: 前缀的，
    而 Step 5 消费侧的 docker images 给的是 12 位无前缀 ——
    两步（去前缀 + 截断）少做任何一步，11 行全部对不上 ⇒ DIGEST-MISMATCH 假红。"""
    def fakeInspect(tag: str) -> str:
        return "sha256:70303b0ef676bbbb"

    assert collectDigests(["qa-system-backend:latest"], fakeInspect) == \
        ["qa-system-backend:latest 70303b0ef676"]


def test_collectDigests_sorts_by_repo_tag():
    def fakeInspect(tag: str) -> str:
        return "sha256:" + "1" * 64

    out = collectDigests(["postgres:16-alpine", "alpine:3.20"], fakeInspect)
    assert out == ["alpine:3.20 111111111111", "postgres:16-alpine 111111111111"]


def test_collectDigests_line_matches_step5_consumer_format():
    """Step 5 消费侧是 `docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}"`，
    产出的行必须与它逐字节同形（单空格 + 12 位小写十六进制，无前缀）。"""
    def fakeInspect(tag: str) -> str:
        return "sha256:d9e853e87e55526f"

    line = collectDigests(["postgres:16-alpine"], fakeInspect)[0]
    assert re.fullmatch(r"[^ ]+ [0-9a-f]{12}", line), line


def test_collectDigests_dedupes_same_tag():
    def fakeInspect(tag: str) -> str:
        return "sha256:" + "2" * 64

    assert collectDigests(["alpine:3.20", "alpine:3.20"], fakeInspect) == \
        ["alpine:3.20 222222222222"]


# ------------------------------------------------------------------ 契约常量


def test_images_matches_compose_external_images():
    """compose 里新增/删除外部镜像时这个测试先红（防漏打镜像）。"""
    missing = _composeImages() - set(mod.IMAGES)
    assert not missing, f"脚本漏了这些镜像：{sorted(missing)}"


def test_images_constant_is_the_nine_external_images():
    assert mod.IMAGES == (
        "postgres:16-alpine",
        "neo4j:5.23-community",
        "quay.io/coreos/etcd:v3.5.5",
        "minio/minio:RELEASE.2023-03-20T20-16-18Z",
        "milvusdb/milvus:v2.4.6",
        "minio/minio:RELEASE.2024-06-13T22-53-53Z",
        "mysql:8.0",
        "alpine:3.20",
        "ollama/ollama:latest",
    )


def test_min_image_count_is_eleven():
    assert MIN_IMAGE_COUNT == 11


def test_platform_is_amd64():
    """Mac 是 arm64、CentOS 是 x86_64 —— 平台不钉死会打出跑不了的镜像。"""
    assert PLATFORM == "linux/amd64"


def test_npm_registry_default_is_the_mirror():
    assert NPM_REGISTRY_DEFAULT == "https://registry.npmmirror.com"


# ------------------------------------------------------------------ 命令形状


def test_pull_command_pins_platform():
    """拉镜像必须显式钉平台。"""
    args = mod.pullCommand("postgres:16-alpine")
    assert args[:2] == ["docker", "pull"]
    assert "--platform" in args
    assert args[args.index("--platform") + 1] == PLATFORM


def test_compose_build_command_has_no_platform_flag():
    """`docker compose build` 没有 --platform 这个 flag（compose v2.27.1 / v5.6.0
    的 build 子命令选项里都没有），写成它会 unknown flag 直接中断脚本。"""
    args = mod.buildCommand(NPM_REGISTRY_DEFAULT)
    assert "--platform" not in args


def test_compose_build_pins_platform_through_environment():
    """不设 DOCKER_DEFAULT_PLATFORM 时 compose 的 build --print 里根本没有
    platforms 字段 ⇒ 静默按宿主架构构建（比 build 报错危险得多）。"""
    env = mod.buildEnv()
    assert env["DOCKER_DEFAULT_PLATFORM"] == PLATFORM


def test_compose_build_passes_npm_registry_as_build_arg():
    """环境变量前缀传不进 buildkit（宿主变量只参与 compose 自身的 ${} 插值），
    必须走 --build-arg。"""
    args = mod.buildCommand("https://example.test/npm")
    assert "--build-arg" in args
    assert "NPM_REGISTRY=https://example.test/npm" in args


def test_compose_build_targets_backend_and_frontend():
    args = mod.buildCommand(NPM_REGISTRY_DEFAULT)
    assert args[-2:] == ["backend", "frontend"]


def test_compose_args_use_repo_relative_docker_paths():
    """compose 文件在 docker/ 下，build context 是 ..（= 仓库根）。
    本脚本从**仓库根**被调用、自己不 cd ⇒ 必须带 docker/ 前缀。"""
    args = mod.composeArgs()
    assert "docker/docker-compose.yml" in args
    assert "docker/docker-compose.centos.yml" in args
    assert "docker-compose.yml" not in args
    assert "docker-compose.centos.yml" not in args


def test_inspect_command_uses_image_id_not_repo_digest():
    """必须用镜像 ID（.Id）而不是 RepoDigest —— 从 tar load 进来的镜像
    没有 RepoDigests，比对它必然两边都空 ⇒ 恒过。"""
    args = mod.inspectCommand("alpine:3.20")
    assert args[:3] == ["docker", "image", "inspect"]
    assert "{{.Id}}" in args
    assert "RepoDigest" not in args


# ------------------------------------------------------------------ 全流程


def test_pipeline_pulls_every_image_and_saves_two_tars():
    docker = FakeDocker(composeImages=_defaultComposeImages(),
                        localImages=_localImages())
    mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")

    pulled = [args[-1] for args in docker.commands() if args[:2] == ["docker", "pull"]]
    assert pulled == list(mod.IMAGES)

    saves = [args for args in docker.commands() if args[:2] == ["docker", "save"]]
    assert len(saves) == 2
    # 命令形如 docker save -o <路径> <镜像...>
    assert saves[0][4:] == list(mod.IMAGES)
    assert saves[1][4:] == BUILT_IMAGE_NAMES
    assert saves[0][3].endswith("all-20260101-0000.tar")
    assert saves[1][3].endswith("built-20260101-0000.tar")


def test_pipeline_never_uses_compose_images_q():
    """`docker compose images -q` 列出**所有**服务的镜像（含刚 pull 的 9 个），
    离线包凭空多几个 GB。只能用 config --images 减去外部镜像。"""
    docker = FakeDocker(composeImages=_defaultComposeImages(),
                        localImages=_localImages())
    mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")

    for args in docker.commands():
        assert "images" not in args or "-q" not in args, f"不该用 compose images -q：{args!r}"


def test_pipeline_collects_digests_only_for_packaged_images():
    """digests 只记打包的那 11 个 —— 联网构建机跑着开发栈，
    `docker images` 全量导出必然让 Step 5 门禁 2 假红。"""
    docker = FakeDocker(
        composeImages=_defaultComposeImages(),
        localImages=_localImages()
        + ["hello-world:latest", "argwarn-b:latest", "qa-system-backend:pre-deploy-20261022"],
    )
    bundle = mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")

    assert len(bundle.digests) == MIN_IMAGE_COUNT
    recorded = {line.split(" ")[0] for line in bundle.digests}
    assert recorded == set(mod.IMAGES) | set(BUILT_IMAGE_NAMES)
    assert "hello-world:latest" not in recorded
    assert "qa-system-backend:pre-deploy-20261022" not in recorded


def test_pipeline_reports_missing_built_images_with_readable_error():
    """拿不到本地 build 镜像时要给一句人话报错，不能让天书冒出来。"""
    docker = FakeDocker(composeImages=_defaultComposeImages(),
                        localImages=sorted(mod.IMAGES))
    with pytest.raises(RuntimeError) as exc:
        mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")
    message = str(exc.value)
    assert "qa-system-backend:latest" in message
    assert "qa-system-frontend:latest" in message


def test_pipeline_rejects_short_digest_list():
    """digests 行数不足时必须硬失败 —— 空/短清单会让下游比对「空空如也 ⇒ 通过」。"""
    # 只 build 出来 1 个镜像 ⇒ 打包 10 个 < 11 ⇒ 行数硬断言必须拦住
    docker = FakeDocker(composeImages=sorted(_composeImages()) + ["qa-system-backend"],
                        localImages=sorted(mod.IMAGES) + ["qa-system-backend:latest"])
    with pytest.raises(RuntimeError) as exc:
        mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")
    assert str(MIN_IMAGE_COUNT) in str(exc.value)


def test_pipeline_build_command_carries_platform_env():
    docker = FakeDocker(composeImages=_defaultComposeImages(),
                        localImages=_localImages())
    mod.collectOfflineImages(docker, "/tmp/qa-out", tag="20260101-0000")

    buildEnvs = [env for args, env in docker.calls
                 if args[:2] == ["docker", "compose"] and "build" in args]
    assert buildEnvs and buildEnvs[0]["DOCKER_DEFAULT_PLATFORM"] == PLATFORM


# ------------------------------------------------------------------ 产物文件


def test_write_digests_writes_one_line_per_image(tmp_path):
    path = mod.writeDigests(str(tmp_path), ["alpine:3.20 d9e853e87e55"])
    assert Path(path).read_text(encoding="utf-8") == "alpine:3.20 d9e853e87e55\n"


def test_write_manifest_covers_only_the_given_tars(tmp_path):
    """校验和只算本次产出的两个 tar —— 目录里可能留着上一次的 tar，
    清单列进不属于本次转移包的文件，Step 5 的 sha256sum -c 就对不上。"""
    current = tmp_path / "all-20260101-0000.tar"
    stale = tmp_path / "all-20251231-0000.tar"
    current.write_bytes(b"current")
    stale.write_bytes(b"stale")

    path = mod.writeManifest(str(tmp_path), [str(current)])
    content = Path(path).read_text(encoding="utf-8")
    assert "all-20260101-0000.tar" in content
    assert "all-20251231-0000.tar" not in content
