"""A5：语义发布开关的**答案级**两态评估（M0.4 §4 的执行工具）。

与 D17 演练的分工
-----------------
`diag_semantic_switchover.py` 比的是 **SQL 文本**：证明「机制层是否确定」。
本脚本比的是 **口径与答案**：证明「开关打开后，回答的是不是同一件事」。

判定口径全部在 `answer_parity_compare.py`（纯函数，可 CI 化）：
L1 SQL 文本（仅定位）/ L2 结果集 / **L3 口径覆盖（硬红线 = 0）** / L4 数值 / L5 答案，
外加同态抖动基线 J（J > 0 ⇒ 不可归因 ⇒ 退出码 6）。

两种运行模式
------------
1. **实跑**（默认）：连库读 ACTIVE 发布 + 调 LLM，每题在 OFF/ON 两态各跑 2 遍，
   落 `off-1/off-2/on-1/on-2.jsonl`。**不执行生成的 SQL**（只读、不碰业务库）。
   ⇒ L2/L4/L5 无样本，只能判 L1/L3（默认层）；
2. **档案回放**（`--replay-dir`）：读已落盘的 4 个 jsonl 重新判定。
   这是唯一能判 L2–L5 的路径（需要记录里有 `rows` / `answer`）。

⚠️ **模型档位不可外推**（M0.4 §4.9 + 决策 D-2）：本地 27B 的结论只证**机制**，
不证生产 DeepSeek 的**幅度**。按 D-2，本地档通过即可开闸，**不要求**生产档复跑；
生产档差异留到 M7 统一暴露（排期 R9）。报告必须写明档位。

只读性
------
实跑模式只读治理表（`ontology_*` / `semantic_release`），**不执行**生成的 SQL，
对业务库零写入；开关状态在退出时恢复为「关」。

用法::

    # 阳性对照自检（不连库、不调模型）
    ./.venv/bin/python scripts/eval_semantic_answer_parity.py --self-test

    # 实跑（LLM key 与 DSN 只走环境变量名，不进 argv）
    export QA_LOCAL_LLM_KEY=...
    export QA_METADATA_DSN=...
    ./.venv/bin/python scripts/eval_semantic_answer_parity.py \\
        --database-url-env QA_METADATA_DSN --llm-key-env QA_LOCAL_LLM_KEY

    # 档案回放（可判 L2/L4/L5）
    ./.venv/bin/python scripts/eval_semantic_answer_parity.py \\
        --replay-dir docs/update/1118/M0/eval/runs/2026-11-18/qwen3.8-27b-4bit \\
        --layers L1,L2,L3,L4,L5

退出码（与 D17 同一套契约）
--------------------------
- ``0``：所有**被要求的层**通过；
- ``2``：两态 schema **内容**不等价（快照丢了 / 多了事实）⇒ 前置故障，不是评估结论；
- ``3``：跨态**口径 / 答案**不一致且超过抖动基线 ⇒ 不通过（L3 是硬红线）；
- ``4``：前置条件不满足 / 执行异常 / 参数要求了实跑不支持的层；
- ``5``：SQL Guard 链失效（合法 SQL 被拒，或违规 SQL 未被拒）；
- ``6``：**不可判定**（同态抖动 J > 0 / SQL 不可解析 / 某态产不出 SQL / 要求的层缺样本）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import answer_parity_cases as cases  # noqa: E402
from scripts import answer_parity_compare as parity  # noqa: E402
from scripts import answer_parity_runtime as runtime  # noqa: E402
from scripts.answer_parity_runtime import (  # noqa: F401 - 对测试与外部复用保持原入口
    DEFAULT_SUBSET as _DEFAULT_SUBSET,
)
from scripts.answer_parity_runtime import (
    applicableQuestions as _applicableQuestions,
)
from scripts.answer_parity_runtime import (
    evaluateRunDirectory,
    loadQuestions,
    runLive,
    runPrecheck,
)
from scripts.db_snapshot_guard import maskDatabaseUrl, resolveDatabaseUrl  # noqa: E402
from scripts.diag_semantic_switchover import (  # noqa: E402
    _failureMessage as _diagFailureMessage,
)
from scripts.diag_semantic_switchover import (
    _maskEndpoint,
    _warnIfExternalEndpoint,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_QUESTIONS = _REPO_ROOT / "docs/update/1118/M0/eval/questions.json"
_DEFAULT_EVAL_ROOT = _REPO_ROOT / "docs/update/1118/M0/eval"
# 实跑模式不执行 SQL、不合成答案 ⇒ 这些层没有样本（判了就是假绿）。
_LIVE_UNSUPPORTED_LAYERS = ("L2", "L4", "L5")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _tierSlug(model: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z._-]+", "-", model).strip("-")
    if not slug or slug in {".", ".."}:
        return "unknown-model"
    return slug


def _gitCommit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return completed.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _requireEnvName(flag: str, value: str) -> str | None:
    """argv 只接受环境变量名；**绝不回显**原值（可能是被别人误传的 DSN / key）。"""
    if _ENV_NAME_RE.fullmatch(value):
        return None
    return f"{flag} 只接受环境变量名（形如 QA_METADATA_DSN），收到的值已隐去"


def _safeWriteDir(path: Path) -> bool:
    """写盘边界：评测产物可以写到任意目录，但绝不允许落进只读素材区。"""
    rawDir = (_REPO_ROOT / "raw").resolve()
    resolved = path.resolve()
    return not (resolved == rawDir or rawDir in resolved.parents)


def _parseLayers(raw: str) -> tuple[str, ...]:
    layers = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    if not layers:
        raise ValueError("--layers 不能为空（可选 L1,L2,L3,L4,L5）")
    unknown = [layer for layer in layers if layer not in parity.ALL_LAYERS]
    if unknown:
        raise ValueError(f"未知层：{unknown}（可选 {parity.ALL_LAYERS}）")
    return layers


def _sanitizedFailure(exc: BaseException, databaseUrl: str, llmKey: str) -> str:
    return _diagFailureMessage(exc, databaseUrl, llmKey).replace("演练中止", "评估中止")


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

_LAYER_MEANING = {
    "L1": "SQL 文本（仅定位）",
    "L2": "结果集（行序无关，列名参与）",
    "L3": "口径覆盖（硬红线 0，**始终强制**）",
    "L4": "关键数值（金额 0.01 元 / 比率 0.1pp / 计数精确）",
    "L5": "答案等价（结论 + 关键数值，≥95%）",
}


def _statusCell(report: runtime.QuestionReport, layer: str) -> str:
    for verdict in report.evaluation.verdicts:
        if verdict.layer == layer:
            return {"PASS": "✅", "DIFF": "❌", "SKIP": "—", "INCONCLUSIVE": "⚠️"}.get(
                verdict.status, verdict.status
            )
    return "—"


def _isLiveMode(mode: str) -> bool:
    return mode.startswith("实跑")


def _layerState(layer: str, requiredLayers: tuple[str, ...]) -> str:
    if layer == "L3":
        return "强制（硬红线）"
    return "已判" if layer in requiredLayers else "未要求"


def _reportHeader(
    meta: dict[str, Any],
    questionsPayload: dict[str, Any],
    mode: str,
    exitCode: int,
) -> list[str]:
    tier = meta.get("tier", {})
    lines = [
        "# 语义发布开关 · 答案级一致性报告（A5）",
        "",
        f"- 生成时间：{meta.get('finishedAt') or meta.get('startedAt')}",
        *(
            [f"- 被回放档案：{meta.get('replayDir')}（实跑于 {meta.get('sourceFinishedAt')}）"]
            if meta.get("replayDir")
            else []
        ),
        f"- 模式：{mode}",
        f"- 代码：commit `{meta.get('commit')}`",
        f"- 模型档位：`{tier.get('model') or 'unknown'}` @ `{tier.get('endpoint') or 'unknown'}`"
        f"（temperature={tier.get('temperature')}）",
        f"- 问题集：`{questionsPayload.get('version')}` / sha256 `{questionsPayload.get('sha256')}`",
        f"- 语义发布：OFF={meta.get('release', {}).get('off')} / "
        f"ON={meta.get('release', {}).get('on')}"
        f"（version={meta.get('release', {}).get('activeVersion')}）",
        f"- **进程退出码：{exitCode}**，meta.status=`{meta.get('status')}`",
        "",
        "> ⚠️ **档位不可外推**（M0.4 §4.9 + 决策 D-2）：本地 27B 只证**机制**"
        "（顺序变化会不会改变答案），不证生产 DeepSeek 的**幅度**。"
        "按 D-2，本地档通过即可开闸，**不要求**生产档复跑；生产档差异留到 M7 统一暴露（排期 R9）。",
        "",
    ]
    if meta.get("reason"):
        lines += [f"> 中止原因：{meta['reason']}", ""]
    return lines


def _coverageLines(
    reports: list[runtime.QuestionReport],
    requiredLayers: tuple[str, ...],
    mode: str,
) -> list[str]:
    lines = ["## 1. 覆盖度", "", "| 层 | 含义 | 本轮 |", "|---|---|---|"]
    for layer in parity.ALL_LAYERS:
        lines.append(f"| {layer} | {_LAYER_MEANING[layer]} | {_layerState(layer, requiredLayers)} |")
    if _isLiveMode(mode):
        uncovered = list(_LIVE_UNSUPPORTED_LAYERS)
        lines += [
            "",
            f"> 实跑模式**不执行**生成的 SQL、不合成答案 ⇒ {uncovered} 无样本。"
            "本报告只证机制（L1/L3），**不证答案幅度**；L2–L5 需带 `rows` / `answer` 的档案回放。",
        ]
    jitter = [
        report.questionId
        for report in reports
        if report.evaluation.exitCode == parity.EXIT_INCONCLUSIVE
        and "抖动" in report.evaluation.reason
    ]
    lines += [
        "",
        "## 2. 同态抖动基线 J",
        "",
        "每个态各跑 2 遍；同态两次不一致 ⇒ J > 0 ⇒ 跨态差异**无法归因**到开关（退出码 6）。",
        f"- J > 0 的题目：{jitter or '无（J = 0）'}",
    ]
    return lines


def _parityTableLines(reports: list[runtime.QuestionReport]) -> list[str]:
    lines = [
        "## 3. 逐题对照",
        "",
        "| 题号 | 问题 | L1 | L2 | L3 | L4 | L5 | 退出码 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for report in reports:
        cells = " | ".join(_statusCell(report, layer) for layer in parity.ALL_LAYERS)
        lines.append(
            f"| {report.questionId} | {report.question} | {cells} | "
            f"{report.evaluation.exitCode} | {report.evaluation.reason} |"
        )
    return lines


_EXIT_MEANING = {
    parity.EXIT_OK: lambda layers: (
        f"- ✅ 所有被要求的层通过（{', '.join(layers)}）：开关两态在**口径层面**等价。"
    ),
    parity.EXIT_ANSWER_MISMATCH: lambda _layers: (
        "- ❌ 跨态口径 / 答案不一致 ⇒ **不允许开闸**（L3 是硬红线，不看数值接近程度）。"
    ),
    parity.EXIT_NOT_EQUIVALENT: lambda _layers: (
        "- ⛔ 两态 schema 内容不等价 ⇒ **前置故障**，不是评估结论；先修快照/直读一致性。"
    ),
    parity.EXIT_GUARD_BROKEN: lambda _layers: (
        "- ⛔ SQL Guard 链失效 ⇒ 先修守卫，评估结论无效。"
    ),
    parity.EXIT_PRECONDITION: lambda _layers: (
        "- ⛔ 前置条件不满足 / 执行异常 ⇒ 本轮**未产生**评估结论。"
    ),
}


def _conclusionLines(
    exitCode: int,
    requiredLayers: tuple[str, ...],
    questionsPayload: dict[str, Any],
    mode: str,
) -> list[str]:
    lines = ["", "## 4. 结论", ""]
    meaning = _EXIT_MEANING.get(exitCode)
    if meaning is None:
        lines.append(
            "- ⚠️ 不可判定：同态抖动或样本不足 ⇒ 先降抖动 / 补样本，**不得**按「差不多」放行。"
        )
    else:
        lines.append(meaning(requiredLayers))
    if _isLiveMode(mode):
        lines.append(
            "- 本报告**不含** L2/L4/L5 结论 ⇒ 只证机制，不证答案幅度；"
            "按 D-2 这不阻塞开闸，生产档差异由 M7 统一暴露（R9）。"
        )
    lines += ["", "## 5. 问题集说明", ""]
    for note in questionsPayload.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return lines


def buildReport(
    *,
    meta: dict[str, Any],
    reports: list[runtime.QuestionReport],
    questionsPayload: dict[str, Any],
    requiredLayers: tuple[str, ...],
    mode: str,
    exitCode: int,
) -> str:
    lines = _reportHeader(meta, questionsPayload, mode, exitCode)
    lines += _coverageLines(reports, requiredLayers, mode)
    lines.append("")
    lines += _parityTableLines(reports)
    lines += _conclusionLines(exitCode, requiredLayers, questionsPayload, mode)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _writeReport(reportDir: Path, report: str, suffix: str) -> Path:
    reportDir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    reportPath = reportDir / f"parity-{_today()}-{suffix}-{stamp}.md"
    reportPath.write_text(report, encoding="utf-8")
    return reportPath


def _baseMeta(*, mode: str, model: str | None, endpoint: str | None, temperature: float | None) -> dict[str, Any]:
    return {
        "tool": "eval_semantic_answer_parity",
        "mode": mode,
        "commit": _gitCommit(),
        "startedAt": datetime.now().isoformat(),
        "tier": {"model": model, "endpoint": endpoint, "temperature": temperature},
    }


def _runReplay(
    args: argparse.Namespace,
    questions: list[dict[str, Any]],
    questionsPayload: dict[str, Any],
    requiredLayers: tuple[str, ...],
    reportDir: Path,
) -> int:
    replayDir = Path(args.replay_dir)
    if not replayDir.is_dir():
        print(f"回放目录不存在：{replayDir}", file=sys.stderr)
        return parity.EXIT_PRECONDITION
    try:
        exitCode, reports = evaluateRunDirectory(replayDir, questions, requiredLayers)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"档案不可用：{exc}", file=sys.stderr)
        return parity.EXIT_PRECONDITION
    meta = _replayMeta(args, replayDir)
    return _finishReport(
        meta=meta,
        reports=reports,
        questionsPayload=questionsPayload,
        requiredLayers=requiredLayers,
        mode="档案回放",
        exitCode=exitCode,
        reportDir=reportDir,
        suffix=f"replay-{_tierSlug(args.llm_model or 'unknown')}",
    )


def _replayMeta(args: argparse.Namespace, replayDir: Path) -> dict[str, Any]:
    """回放元信息：**继承**被回放档案的档位 / commit / 发布 / schema。

    这些字段描述的是**证据**（哪次实跑产生的档案），不能改写成「本次回放的
    代码与模型」—— 那会让报告把档案归到错误的 commit 上。
    """
    loaded: dict[str, Any] | None = None
    metaPath = replayDir / "meta.json"
    if metaPath.exists():
        try:
            parsed = json.loads(metaPath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            loaded = parsed
    if loaded is None:
        meta = _baseMeta(
            mode="replay", model=args.llm_model, endpoint="replay", temperature=None
        )
    else:
        meta = dict(loaded)
        tier = meta.get("tier") or {}
        if not args.llm_model and tier.get("model"):
            args.llm_model = tier["model"]
    meta["mode"] = "replay"
    meta["sourceFinishedAt"] = loaded.get("finishedAt") if loaded else None
    meta["replayAt"] = datetime.now().isoformat()
    meta["finishedAt"] = meta["replayAt"]
    meta["replayDir"] = str(replayDir)
    meta["status"] = "finished"
    meta.pop("conclusion", None)  # 本次回放会按当前代码重算
    return meta


def _liveMeta(args: argparse.Namespace, databaseUrl: str, endpoint: str) -> dict[str, Any]:
    meta = _baseMeta(
        mode="precheck" if args.precheck_only else "live",
        model=args.llm_model,
        endpoint=_maskEndpoint(endpoint),
        temperature=0.0,
    )
    meta["database"] = maskDatabaseUrl(databaseUrl)
    return meta


def _finishReport(
    *,
    meta: dict[str, Any],
    reports: list[runtime.QuestionReport],
    questionsPayload: dict[str, Any],
    requiredLayers: tuple[str, ...],
    mode: str,
    exitCode: int,
    reportDir: Path,
    suffix: str,
) -> int:
    report = buildReport(
        meta=meta,
        reports=reports,
        questionsPayload=questionsPayload,
        requiredLayers=requiredLayers,
        mode=mode,
        exitCode=exitCode,
    )
    reportPath = _writeReport(reportDir, report, suffix)
    print(report)
    print(f"\n报告已写入：{reportPath}")
    return exitCode


def _runPrecheckCli(
    args: argparse.Namespace,
    databaseUrl: str,
    llmKey: str,
    meta: dict[str, Any],
    metaPath: Path,
    classNames: tuple[str, ...],
) -> int:
    try:
        exitCode, _meta = asyncio.run(
            runPrecheck(
                databaseUrl=databaseUrl,
                classNames=classNames,
                meta=meta,
                metaPath=metaPath,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 统一脱敏后映射到 4
        print(_sanitizedFailure(exc, databaseUrl, llmKey), file=sys.stderr)
        return parity.EXIT_PRECONDITION
    print(f"meta 已写入：{metaPath}")
    return exitCode


def _runLiveCli(
    args: argparse.Namespace,
    databaseUrl: str,
    llmKey: str,
    questions: list[dict[str, Any]],
    questionsPayload: dict[str, Any],
    requiredLayers: tuple[str, ...],
    runDir: Path,
    reportDir: Path,
    classNames: tuple[str, ...],
) -> int:
    from app.domain.exceptions import Nl2SqlError

    metaPath = runDir / "meta.json"
    meta = _liveMeta(args, databaseUrl, args.llm_endpoint)
    if args.precheck_only:
        return _runPrecheckCli(args, databaseUrl, llmKey, meta, metaPath, classNames)

    try:
        exitCode, meta = asyncio.run(
            runLive(
                databaseUrl=databaseUrl,
                llmKey=llmKey,
                llmEndpoint=args.llm_endpoint,
                llmModel=args.llm_model,
                questions=questions,
                classNames=classNames,
                runDir=runDir,
                requiredLayers=requiredLayers,
                meta=meta,
                metaPath=metaPath,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 出口统一脱敏；NL2SQL 报错⇒6，其余⇒4
        print(_sanitizedFailure(exc, databaseUrl, llmKey), file=sys.stderr)
        if isinstance(exc, Nl2SqlError):
            return parity.EXIT_INCONCLUSIVE
        return parity.EXIT_PRECONDITION

    return _reportAfterLive(
        args=args,
        runDir=runDir,
        meta=meta,
        exitCode=exitCode,
        questions=questions,
        questionsPayload=questionsPayload,
        requiredLayers=requiredLayers,
        reportDir=reportDir,
    )


def _reportAfterLive(
    *,
    args: argparse.Namespace,
    runDir: Path,
    meta: dict[str, Any],
    exitCode: int,
    questions: list[dict[str, Any]],
    questionsPayload: dict[str, Any],
    requiredLayers: tuple[str, ...],
    reportDir: Path,
) -> int:
    """实跑结束后的报告生成；前置失败 / 守卫失效时**不**出报告。"""
    if meta.get("status") != "finished":
        # 前置失败 / 守卫失效 ⇒ 本轮**没有**评估结论；写一份「全 6」的报告只会误导。
        print(
            f"本轮中止（status={meta.get('status')}）：{meta.get('reason')}；未生成 parity 报告。",
            file=sys.stderr,
        )
        return exitCode
    try:
        _exitCode, reports = evaluateRunDirectory(runDir, questions, requiredLayers)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"档案不可用：{exc}", file=sys.stderr)
        return parity.EXIT_PRECONDITION
    return _finishReport(
        meta=meta,
        reports=reports,
        questionsPayload=questionsPayload,
        requiredLayers=requiredLayers,
        mode="实跑（两态 × 两遍）",
        exitCode=exitCode,
        reportDir=reportDir,
        suffix=_tierSlug(args.llm_model),
    )


class _CliError(RuntimeError):
    """参数 / 前置错误；统一映射到退出码 4（不含敏感值，消息可直接打印）。"""


def _parseArgs(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="语义发布开关 · 答案级两态评估（A5）")
    parser.add_argument("--self-test", action="store_true", help="只跑阳性对照自检")
    parser.add_argument("--questions", default=str(_DEFAULT_QUESTIONS))
    parser.add_argument("--replay-dir", default=None, help="档案回放目录（含 4 个 jsonl）")
    parser.add_argument("--layers", default="L1,L3", help="要求的判定层，逗号分隔")
    parser.add_argument(
        "--database-url-env", default="QA_METADATA_DSN", help="存放 DSN 的环境变量名"
    )
    parser.add_argument(
        "--llm-key-env", default="QA_LOCAL_LLM_KEY", help="存放 LLM API key 的环境变量名"
    )
    parser.add_argument("--llm-endpoint", default="http://127.0.0.1:8888/v1")
    parser.add_argument("--llm-model", default=None, help="缺省 Qwen3.8-27B-4bit（仅实跑）")
    parser.add_argument(
        "--subset-class",
        action="append",
        default=None,
        help="类子集（可重复）；缺省 = 采购 7 类",
    )
    parser.add_argument("--out-dir", default=None, help="实跑产物目录")
    parser.add_argument("--report-dir", default=None, help="报告目录")
    parser.add_argument(
        "--precheck-only", action="store_true", help="只做两态 schema 前置检查，不调模型"
    )
    return parser.parse_args(argv)


def _resolveInputs(
    args: argparse.Namespace,
) -> tuple[tuple[str, ...], dict[str, Any], list[dict[str, Any]]]:
    for flag, value in (
        ("--database-url-env", args.database_url_env),
        ("--llm-key-env", args.llm_key_env),
    ):
        problem = _requireEnvName(flag, value)
        if problem:
            raise _CliError(problem)
    try:
        requiredLayers = _parseLayers(args.layers)
    except ValueError as exc:
        raise _CliError(str(exc)) from exc
    try:
        questionsPayload = loadQuestions(Path(args.questions))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _CliError(f"问题集不可用：{exc}") from exc
    questions = _applicableQuestions(questionsPayload)
    if not questions:
        raise _CliError("问题集里没有 parityApplicable=true 的题")
    return requiredLayers, questionsPayload, questions


def _resolveDirs(args: argparse.Namespace) -> tuple[Path, Path]:
    if not args.replay_dir:
        # 档位决定产物目录名 ⇒ 默认值必须在算路径**之前**定下来。
        args.llm_model = args.llm_model or "Qwen3.8-27B-4bit"
    runDir = Path(args.out_dir) if args.out_dir else (
        _DEFAULT_EVAL_ROOT / "runs" / _today() / _tierSlug(args.llm_model or "unknown")
    )
    reportDir = Path(args.report_dir) if args.report_dir else _DEFAULT_EVAL_ROOT / "reports"
    for target in (runDir, reportDir):
        if not _safeWriteDir(target):
            raise _CliError(f"拒绝写入只读素材目录：{target}")
    return runDir, reportDir


def _resolveLiveInputs(args: argparse.Namespace) -> tuple[str, str]:
    unsupported = [layer for layer in _parseLayers(args.layers) if layer in _LIVE_UNSUPPORTED_LAYERS]
    if unsupported:
        raise _CliError(
            f"实跑模式不支持判定 {unsupported}：不执行 SQL、不合成答案。"
            "请改用 `--replay-dir`（档案里带 rows / answer）。"
        )
    rawUrl = os.getenv(args.database_url_env)
    if not rawUrl:
        raise _CliError(f"环境变量 {args.database_url_env} 未设置")
    llmKey = os.getenv(args.llm_key_env, "")
    if not llmKey and not args.precheck_only:
        raise _CliError(f"环境变量 {args.llm_key_env} 未设置")
    parsedEndpoint = urlparse(args.llm_endpoint)
    if parsedEndpoint.scheme not in {"http", "https"} or not parsedEndpoint.hostname:
        raise _CliError(
            f"--llm-endpoint 不是合法的 http(s) 地址：{_maskEndpoint(args.llm_endpoint)}"
        )
    _warnIfExternalEndpoint(args.llm_endpoint)
    try:
        databaseUrl = resolveDatabaseUrl(None, args.database_url_env, allowInlinePassword=False)
    except SystemExit as exc:
        raise _CliError(str(exc)) from exc
    return databaseUrl, llmKey


def main(argv: list[str] | None = None) -> int:
    args = _parseArgs(argv)

    if args.self_test:
        results = cases.runSelfTest()
        print(cases.formatSelfTest(results))
        return cases.selfTestExitCode(results)

    try:
        requiredLayers, questionsPayload, questions = _resolveInputs(args)
        runDir, reportDir = _resolveDirs(args)
        if args.replay_dir:
            return _runReplay(args, questions, questionsPayload, requiredLayers, reportDir)
        databaseUrl, llmKey = _resolveLiveInputs(args)
    except _CliError as exc:
        print(str(exc), file=sys.stderr)
        return parity.EXIT_PRECONDITION

    classNames = tuple(args.subset_class) if args.subset_class else _DEFAULT_SUBSET
    print(f"目标库 DSN = {maskDatabaseUrl(databaseUrl)}")
    print(f"LLM = {args.llm_model} @ {_maskEndpoint(args.llm_endpoint)}")
    return _runLiveCli(
        args, databaseUrl, llmKey, questions, questionsPayload,
        requiredLayers, runDir, reportDir, classNames,
    )


if __name__ == "__main__":
    raise SystemExit(main())
