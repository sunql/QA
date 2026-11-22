"""WP-ONT-AI ↔ V0.3 融合契约守卫（F1）。

把两份设计之间的**接缝**变成可执行断言，防止后续实现"各写一半"：

1. **表命名空间**：发现域的表只能是 `discovery_*`（+ 已建的 `metadata_snapshot`），
   **绝不能**用 `ontology_*` 前缀 —— 那是正式本体表，候选混进去 = "AI 写本体"红线；
2. **发现域不得写治理表**：发现域模块不许 import 本体/语义发布的**写路径**服务
   （编译/差异/Registry **读**是允许的）；
3. **计量守卫**：发现域一旦引入 LLM 调用，同一文件必须出现 token/cost 计量字段
   （V0.3 强制「每次 LLM 调用都计量」）；
4. **状态映射**：`APPROVED/VALIDATED → release DRAFT`、只有 `activate` 才 ACTIVE、
   `isPublished` 必须双条件。

用 **AST** 不用正则：正则会被自己写的注释命中（本仓既有教训），
`test_source_guard_ast_not_regex` 也钉住这一点。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.domain import discovery_status_contract as contract

_BACKEND = pathlib.Path(__file__).resolve().parents[3]
_DISCOVERY_FILES = (
    _BACKEND / "app" / "domain" / "discovery_models.py",
    _BACKEND / "app" / "services" / "discovery_snapshot_service.py",
    _BACKEND / "app" / "services" / "discovery_diff_service.py",
    _BACKEND / "app" / "api" / "v1" / "ontology_discovery.py",
)

#: 发现域允许声明的表名（其余一律红）。
ALLOWED_DISCOVERY_TABLES = frozenset({"discovery_source", "metadata_snapshot"})

#: 发现域**禁止** import 的写路径（读路径如 semantic_compiler / registry 允许）。
FORBIDDEN_IMPORTS = frozenset(
    {
        "app.services.semantic_release_service",
        "app.services.ontology_service",  # 本体 CRUD（写）
        "app.services.semantic_release_support",
    }
)

#: LLM 调用符号（一旦出现，同文件必须有计量字段）。
_LLM_SYMBOLS = frozenset({"LlmClient", "completeWithTransientRetry", "llm_factory"})
_METERING_FIELDS = frozenset({"prompt_tokens", "completion_tokens", "cost", "total_tokens"})


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _iterFiles() -> list[pathlib.Path]:
    files = [path for path in _DISCOVERY_FILES if path.exists()]
    assert files, "发现域文件一个都没找到 ⇒ 守卫空集（路径漂移？）"
    return files


# ---------------------------------------------------------------------------
# 1. 表命名空间
# ---------------------------------------------------------------------------


def test_discoveryTablesUseDiscoveryNamespace() -> None:
    """发现域声明的表名必须落在白名单内（不得出现 ontology_*）。"""
    names: set[str] = set()
    for path in _iterFiles():
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Name)
                        and target.id == "__tablename__"
                        and isinstance(node.value, ast.Constant)
                    ):
                        names.add(str(node.value.value))
    assert names, "没有扫描到任何 __tablename__ ⇒ 守卫空集"
    assert names <= ALLOWED_DISCOVERY_TABLES, (
        f"发现域声明了白名单外的表：{sorted(names - ALLOWED_DISCOVERY_TABLES)}"
    )
    assert not any(name.startswith("ontology_") for name in names), (
        "红线：发现域不得把自己写进 ontology_* 命名空间"
    )


# ---------------------------------------------------------------------------
# 2. 不得 import 治理写路径
# ---------------------------------------------------------------------------


def test_discoveryDomainDoesNotImportWritePaths() -> None:
    """发现域只产出候选；发布必须走既有发布链（不允许直接调写服务）。"""
    offenders: list[str] = []
    for path in _iterFiles():
        for node in ast.walk(_parse(path)):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in FORBIDDEN_IMPORTS:
                        offenders.append(f"{path.name}: {alias.name}")
            if module in FORBIDDEN_IMPORTS:
                offenders.append(f"{path.name}: {module}")
    assert offenders == [], f"发现域 import 了治理写路径：{offenders}"


# ---------------------------------------------------------------------------
# 3. 计量守卫
# ---------------------------------------------------------------------------


def test_llmUsageInDiscoveryRequiresMetering() -> None:
    """发现域一旦调 LLM，同文件必须出现 token/cost 计量字段（V0.3 强制）。"""
    offenders: list[str] = []
    llmFiles = 0
    for path in _iterFiles():
        tree = _parse(path)
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        if not (identifiers & _LLM_SYMBOLS):
            continue
        llmFiles += 1
        if not (identifiers & _METERING_FIELDS):
            offenders.append(path.name)
    # 当前 A1 没有 LLM 调用 ⇒ 0 个文件；守卫保持"一旦出现就必须计量"的语义。
    # 同时断言扫描面非空（防空集假绿）。
    assert _iterFiles(), "扫描面为空"
    assert offenders == [], f"以下文件调用 LLM 却没有计量：{offenders}"
    assert llmFiles == 0, (
        "发现域当前不应有 LLM 调用（A1 只做采集/快照/差异）；"
        "若引入了请同时补计量并更新本断言"
    )


# ---------------------------------------------------------------------------
# 4. 状态映射契约
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("discoveryStatus", "expected"),
    [
        (contract.DISCOVERY_DISCOVERED, None),
        (contract.DISCOVERY_PROPOSED, None),
        (contract.DISCOVERY_IN_REVIEW, None),
        (contract.DISCOVERY_APPROVED, contract.RELEASE_DRAFT),
        (contract.DISCOVERY_VALIDATED, contract.RELEASE_DRAFT),
        # 已发布 ⇒ 对应 release 必须 ACTIVE（派生视图）
        (contract.DISCOVERY_PUBLISHED, contract.RELEASE_ACTIVE),
        (contract.DISCOVERY_REJECTED, None),
        (contract.DISCOVERY_NEEDS_EVIDENCE, None),
    ],
)
def test_stateMapping(discoveryStatus: str, expected: str | None) -> None:
    assert contract.toReleaseStatus(discoveryStatus) == expected


def test_unknownStateIsRejected() -> None:
    with pytest.raises(ValueError):
        contract.toReleaseStatus("WHATEVER")


def test_publishOnlyThroughActivatePath() -> None:
    """发布动作只有一个入口：既有发布链的 `activate`。"""
    assert contract.PUBLISH_ACTION_PATH.endswith("/activate")
    assert "/semantic/releases/" in contract.PUBLISH_ACTION_PATH


def test_isPublishedRequiresBothSides() -> None:
    """只有「提案 PUBLISHED **且** release ACTIVE」才算发布（防单边自我宣布）。"""
    assert contract.isPublished(contract.DISCOVERY_PUBLISHED, contract.RELEASE_ACTIVE)
    assert not contract.isPublished(contract.DISCOVERY_PUBLISHED, contract.RELEASE_DRAFT)
    assert not contract.isPublished(contract.DISCOVERY_APPROVED, contract.RELEASE_ACTIVE)


def test_statesAreOrderedAndComplete() -> None:
    assert contract.DISCOVERY_STATES[0] == contract.DISCOVERY_DISCOVERED
    assert contract.DISCOVERY_STATES[-1] == contract.DISCOVERY_PUBLISHED
    assert contract.DISCOVERY_OFF_TRACK_STATES.isdisjoint(contract.DISCOVERY_STATES)
