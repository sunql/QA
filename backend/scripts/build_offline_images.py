"""离线镜像构建与交付（联网 x86_64 构建机）。

把 9 个外部镜像 + 2 个本地 build 的镜像拉齐、`docker save` 成 tar，
并产出两个随包带走的文件：
    images.manifest.sha256  —— 本次产出的 tar 的校验和
    images.digests.txt      —— 打包的那 11 个镜像的短 ID 清单

依据 docs/superpowers/specs/2026-10-22-centos7-deployment-design.md §6.1。

形态说明：这一版是 Python 写的（旧的 bash 版已删）。计划里的全局约束写着
「有逻辑的脚本一律写 Python + pytest，bash 只做纯编排」—— 旧脚本里全是逻辑
（集合减、tag 归一、成员过滤、行数门禁），单测只能断脚本文本的形状，
转换逻辑本身测不到，tag 归一与分隔符两个缺陷正是这么漏出去的。

用法（必须从**仓库根**跑）：
    python3 backend/scripts/build_offline_images.py

⚠️ Python 版本：本脚本要跑在**联网 x86_64 构建机**上，而那台机器由谁提供、
什么系统都还没定（spec 附录「待定」清单第 4 条）。CentOS 7 自带的是
python3.6，所以本文件刻意保持 3.6 兼容：
- 不用 `from __future__ import annotations`（3.7+ 语法）
- 内建泛型注解一律加引号（"list[str]"），PEP 585 要 3.9
- 不用 subprocess.run(capture_output=..., text=...)，改用 PIPE + universal_newlines（3.7+）
（与同目录 preflight_centos.py 同一套约束与理由。）
"""

import hashlib
import os
import subprocess
import sys
from collections.abc import Iterable
from datetime import datetime
from typing import NamedTuple

# 与 docker-compose.yml + docker-compose.centos.yml 里带 image: 字段的服务一致
# （backend/frontend 是本地 build 出来的，不在此列）
IMAGES = (
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
# Mac 是 arm64、CentOS 是 x86_64 —— 平台不钉死会打出跑不了的镜像
PLATFORM = "linux/amd64"
# docker/Dockerfile.frontend 默认走 registry.npmjs.org，内网构建会卡死
# （qa-system-frontend-build-npm-registry）
NPM_REGISTRY_DEFAULT = "https://registry.npmmirror.com"
# 9 个外部镜像 + 2 个本地 build 的镜像
MIN_IMAGE_COUNT = 11
OUT_DIR_DEFAULT = "/opt/qa-transfer/images"
# compose 文件在 docker/ 下，build context 是 ..（= 仓库根）。
# 本脚本从**仓库根**被调用、自己不 cd ⇒ 必须带 docker/ 前缀，裸文件名会
# no such file or directory。CentOS 侧那几处裸名是在 cd /opt/qa/docker 之后的，
# 本来就对，别照着它们抄。
COMPOSE_BASE = "docker/docker-compose.yml"
COMPOSE_CENTOS = "docker/docker-compose.centos.yml"
BUILT_SERVICES = ("backend", "frontend")
# docker images 的 {{.ID}} 给的是 12 位短 ID —— digests 文件必须同形
SHORT_ID_LENGTH = 12
# docker image inspect --format '{{.Id}}' 给的是带 sha256: 前缀的 64 位全串
SHA256_PREFIX = "sha256:"


class OfflineImageBundle(NamedTuple):
    """一次离线镜像打包的结果。"""

    tag: str
    allTar: str
    builtTar: str
    packaged: list
    built: list
    digests: list


# ----------------------------------------------------------------- 纯函数


def normalizeTag(name: str) -> str:
    """裸名补 :latest；已带 tag 的原样保留。

    tag 分隔符只看**最后一个 '/' 之后**有没有冒号：`host:5000/foo` 的冒号是
    registry 端口，不是 tag。带 registry 路径的裸名（`myorg/foo`）也要补 ——
    老实现的 `/^([^/:]+)$/` 漏了这种，会把 `myorg/foo` 当成已带 tag 而原样留下。
    """
    _, _, tail = name.rpartition("/")
    if ":" in tail:
        return name
    return name + ":latest"


def filterPackaged(packaged: "Iterable[str]", imageNames: "Iterable[str]") -> "list[str]":
    """只保留在 packaged 里的镜像名（集合减 / 成员过滤）。

    匹配按 **repo:tag 精确**：`qa-system-backend:pre-deploy-…` 与
    `qa-system-backend:latest` 是两个不同的镜像，只按仓库名匹配会把构建机上
    留着的旧 tag 也收进去 ⇒ Step 5 那边「缺一个」⇒ 假红。

    返回值是**归一后**的名字（裸名补 :latest），且去重 —— 同一个镜像名
    在 `docker images` 里出现两次也只能产出一行。
    """
    keep = set(normalizeTag(name) for name in packaged)
    out = []
    for name in imageNames:
        key = normalizeTag(name)
        if key in keep and key not in out:
            out.append(key)
    return out


def shortImageId(inspectOutput: str) -> str:
    """把 `docker image inspect --format '{{.Id}}'` 的输出压成 12 位短 ID。

    两步都要做：去 `sha256:` 前缀 **且** 截断到 12 位。
    Step 5 的消费侧是 `docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}"`，
    那一列是 12 位无前缀的 —— 只去前缀不去长度，得到的是 64 位全串，
    `comm -13` 就 11 行全部对不上 ⇒ DIGEST-MISMATCH 假红。
    """
    raw = inspectOutput.strip()
    if raw.startswith(SHA256_PREFIX):
        raw = raw[len(SHA256_PREFIX):]
    return raw[:SHORT_ID_LENGTH]


def collectDigests(packaged: "Iterable[str]", inspectFn) -> "list[str]":
    """逐 tag 取 ID —— 天然每个 tag 一行，重复 tag 不可能产生重复行。

    返回 ["<repo:tag> <12位短ID不带前缀>", ...]，按 repo:tag 排序。
    逐 tag inspect 而不是 `docker images` 全量导出：后者的输出里同一 tag
    可能出现两行（多平台 manifest），会让 Step 5 门禁 3 的 `n -eq 11` 挂掉。
    """
    out = []
    for name in sorted(set(normalizeTag(item) for item in packaged)):
        out.append(f"{name} {shortImageId(inspectFn(name))}")
    return out


def parseImageList(stdout: str) -> "list[str]":
    """把命令输出切成镜像名列表，丢掉空行与 `<none>`（悬空镜像）。"""
    names = []
    for line in stdout.splitlines():
        name = line.strip()
        if not name or "<none>" in name:
            continue
        names.append(name)
    return names


def selectBuiltImages(composeImages: "Iterable[str]", externalImages: "Iterable[str]") -> "list[str]":
    """compose 用到的镜像里减去外部镜像，剩下的就是本地 build 的那两个。

    `config --images` 对没有 `image:` 字段的服务给的是**裸名**
    （compose 按 <project>-<service> 命名、tag 默认 latest），
    所以这里顺手补上 :latest。
    """
    external = set(externalImages)
    out = []
    for name in composeImages:
        if name in external or name in out:
            continue
        out.append(normalizeTag(name))
    return out


# ----------------------------------------------------------------- 命令形状


def composeArgs() -> "list[str]":
    """compose 的两份文件：基线 + CentOS overlay。"""
    return ["-f", COMPOSE_BASE, "-f", COMPOSE_CENTOS]


def pullCommand(image: str) -> "list[str]":
    """拉镜像必须显式钉平台（Mac 是 arm64，CentOS 是 x86_64）。"""
    return ["docker", "pull", "--platform", PLATFORM, image]


def buildCommand(npmRegistry: str) -> "list[str]":
    """本地 build backend / frontend。

    ⚠️ 这里**没有** `--platform`：`docker compose build` 根本没有这个 flag
    （compose v2.27.1 / v5.6.0 的 build 子命令选项里都没有），写成它会
    `unknown flag: --platform` 直接中断脚本。钉平台靠 buildEnv()。

    ⚠️ NPM_REGISTRY 必须走 `--build-arg`：`NPM_REGISTRY=xxx docker compose build`
    的环境变量前缀传不进 buildkit —— 宿主环境变量只参与 compose 自身的 ${} 插值，
    而 compose 文件里没引用它（实测 `--print` 的 args 只有 VITE_API_BASE_URL）。
    """
    args = ["docker", "compose"] + composeArgs()
    args += ["build", "--build-arg", "NPM_REGISTRY=" + npmRegistry]
    return args + list(BUILT_SERVICES)


def buildEnv() -> dict:
    """build 的环境变量：DOCKER_DEFAULT_PLATFORM 是钉平台唯一有效的办法。

    不设它时 compose 的 `build --print` 里**根本没有 platforms 字段**
    ⇒ 静默按宿主架构构建，arm64 镜像进不了 x86_64。
    这是**静默失败**，比 build 报错危险得多。
    """
    env = dict(os.environ)
    env["DOCKER_DEFAULT_PLATFORM"] = PLATFORM
    return env


def composeImagesCommand() -> "list[str]":
    """`config --images` 给镜像**名**（docker save 也吃名）。

    ⚠️ 不要用 `docker compose images -q` —— 它列出**所有**服务的镜像
    （含刚 pull 的 9 个外部镜像），离线包凭空多几个 GB。
    """
    return ["docker", "compose"] + composeArgs() + ["config", "--images"]


def localImagesCommand() -> "list[str]":
    """本机镜像清单（repo:tag），用于核对打包的那几个是不是真的都在。"""
    return ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"]


def inspectCommand(tag: str) -> "list[str]":
    """逐 tag 取镜像 ID。

    用 `.Id`（image config 的 sha）而不是 `RepoDigest`：从 tar `docker load`
    进来的镜像**没有** RepoDigests，比对它必然两边都空 ⇒ 恒过。
    """
    return ["docker", "image", "inspect", "--format", "{{.Id}}", tag]


def saveCommand(outPath: str, images: "Iterable[str]") -> "list[str]":
    """`docker save` 一个 tar。"""
    return ["docker", "save", "-o", outPath] + list(images)


# ----------------------------------------------------------------- 执行


def runCommand(args, env=None) -> str:
    """跑一条命令；非零退出就抛错并带上 stderr，不静默吞。"""
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(f"命令失败（exit {proc.returncode}）："
                           f"{' '.join(args)}\n{(proc.stderr or '').strip()}")
    return proc.stdout


def collectOfflineImages(run, outDir: str, npmRegistry: str = NPM_REGISTRY_DEFAULT,
                       tag=None) -> OfflineImageBundle:
    """跑完整条 docker 管线（不写文件），返回 OfflineImageBundle。

    run 是注入点：测试传假 docker，不真调 docker。
    """
    for image in IMAGES:
        run(pullCommand(image))

    run(buildCommand(npmRegistry), buildEnv())

    composeImages = parseImageList(run(composeImagesCommand()))
    built = selectBuiltImages(composeImages, IMAGES)
    if not built:
        raise RuntimeError(
            "拿不到本地构建的镜像名：`compose config --images` 减去外部镜像后是空的"
            f"（compose 给出：{', '.join(composeImages)}）。是不是 compose 文件路径不对，"
            "或者服务全都带 image: 字段了？")

    stamp = tag or datetime.now().strftime("%Y%m%d-%H%M")
    allTar = os.path.join(outDir, f"all-{stamp}.tar")
    builtTar = os.path.join(outDir, f"built-{stamp}.tar")
    run(saveCommand(allTar, IMAGES))
    run(saveCommand(builtTar, built))

    packaged = list(IMAGES) + list(built)
    present = set(filterPackaged(packaged, parseImageList(run(localImagesCommand()))))
    missing = [name for name in packaged if name not in present]
    if missing:
        raise RuntimeError(
            f"本地找不到这些镜像：{', '.join(missing)}。是不是 compose build 没成功，"
            "或者 pull 没拉全？")

    digests = collectDigests(packaged, lambda name: run(inspectCommand(name)))
    # ⚠️ 必须硬断言，不要只打个数字：清单为空/过短时，下游所有按它做的比对
    # 都会「空空如也 ⇒ 通过」（假绿）。Step 5 门禁 3 是 n -eq 11。
    if len(digests) < MIN_IMAGE_COUNT:
        raise RuntimeError(f"images.digests.txt 只有 {len(digests)} 行，"
                           f"期望 >= {MIN_IMAGE_COUNT}")

    return OfflineImageBundle(stamp, allTar, builtTar, packaged, built, digests)


def writeDigests(outDir: str, lines: "Iterable[str]") -> str:
    """写镜像 ID 清单：一行一个 `<repo:tag> <12位短ID>`。"""
    path = os.path.join(outDir, "images.digests.txt")
    with open(path, "w") as handle:
        for line in lines:
            handle.write(line + "\n")
    return path


def writeManifest(outDir: str, tarPaths: "Iterable[str]") -> str:
    """写 tar 的校验和清单（`sha256sum -c` 能吃的格式）。

    ⚠️ 只算**本次产出**的 tar，不能扫整个目录：$OUT_DIR 里可能留着上一次的 tar，
    清单会列进不属于本次转移包的文件，Step 5 的 `sha256sum -c` 必然报缺文件。
    """
    entries = []
    for tarPath in tarPaths:
        digest = hashlib.sha256()
        with open(tarPath, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(f"{digest.hexdigest()}  ./{os.path.basename(tarPath)}")
    path = os.path.join(outDir, "images.manifest.sha256")
    with open(path, "w") as handle:
        handle.write("\n".join(entries) + "\n")
    return path


def main() -> int:
    outDir = os.environ.get("OUT_DIR", OUT_DIR_DEFAULT)
    npmRegistry = os.environ.get("NPM_REGISTRY", NPM_REGISTRY_DEFAULT)

    if not os.path.exists(COMPOSE_BASE):
        raise RuntimeError(
            f"请从**仓库根**运行：compose 文件在 docker/ 下（{COMPOSE_BASE} 不存在）。"
            "用法：cd <repo> && python3 backend/scripts/build_offline_images.py")

    os.makedirs(outDir, exist_ok=True)
    bundle = collectOfflineImages(runCommand, outDir, npmRegistry)
    digestsPath = writeDigests(outDir, bundle.digests)
    manifestPath = writeManifest(outDir, [bundle.allTar, bundle.builtTar])

    print(f"镜像 ID 记录 {len(bundle.digests)} 行 -> {digestsPath}")
    print(f"校验和清单 -> {manifestPath}")
    print("把 images.manifest.sha256 和 images.digests.txt 一起带走，CentOS 侧要复核")
    return 0


if __name__ == "__main__":
    sys.exit(main())
