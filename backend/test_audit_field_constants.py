"""audit 字段（run_status/entity_type/action/error）必须走 chat_constants SSOT。

Static grep-based test: reads chat_domain.py directly, no DB needed.
Placed at backend/ root to avoid conftest.py autouse DB fixtures in tests/ subdirs.

覆盖范围与 _emitAgentRunAuditAfter 5 处 except 分支 + finally SUCCESS 分支对齐：
- entity_type="agent_run_log" × 6（5 except + 1 finally）
- action="CREATE" × 6
- run_status = "SUCCESS"（初值） + run_status = "FAILED" × 5 = 6
- run_status == "SUCCESS"（finally 比较）
- 5 error 字段各 1：AGENT_NOT_FOUND / PERMISSION_DENIED / AGENT_NOT_RUNNABLE /
  BAD_INPUT / UNEXPECTED
"""
from __future__ import annotations

import pathlib
import sys

# Bare string literals that must NOT appear as audit_log kwargs in production code.
BARE_LITERALS = [
    '"agent_run_log"',
    '"CREATE"',
    # run_status 既作为赋值字面量，也作为比较字面量（finally 检查 SUCCESS）。
    # 两种形态分别匹配以避免漏检；赋值形态覆盖 kwarg/变量初值，比较形态覆盖 ==。
    '"SUCCESS"',
    '"FAILED"',
    '"AGENT_NOT_FOUND"',
    '"PERMISSION_DENIED"',
    '"AGENT_NOT_RUNNABLE"',
    '"BAD_INPUT"',
    '"UNEXPECTED"',
]

TARGETS = ["chat_domain.py"]


def check() -> list[str]:
    """Return list of error messages; empty list means PASS."""
    base = pathlib.Path(__file__).parent / "app" / "services"
    errors = []
    for t in TARGETS:
        src = (base / t).read_text()
        for lit in BARE_LITERALS:
            if lit not in src:
                continue
            # Match audit_log 关键字面量：
            # 1) entity_type=<lit> / action=<lit>：直接命中 audit.record(...) kwarg
            # 2) run_status = <lit> / run_status == <lit>：赋值/比较形态
            # 3) "error": <lit>：audit_log after 字典里的 error 字段
            patterns = [
                f"entity_type={lit}",
                f"action={lit}",
                f'"error": {lit}',
                f"run_status = {lit}",
                f"run_status == {lit}",
            ]
            for pat in patterns:
                if pat in src:
                    errors.append(f"{t} still has audit literal: {pat}")
    return errors


if __name__ == "__main__":
    errors = check()
    if errors:
        for e in errors:
            print("FAIL:", e)
        sys.exit(1)
    print("PASS: no bare audit-field literals in chat_domain.py")
    sys.exit(0)


def test_no_bare_audit_field_literals():
    """audit 字段字面量必须替换为 chat_constants.AUDIT_* / AGENT_RUN_ERROR_* 常量。"""
    errors = check()
    assert not errors, "\n".join(errors)


def test_audit_field_constant_values():
    """SSOT 常量值必须与 DB 列现有数据保持一致（改名=改值=历史 group by 空集）。"""
    from app.services.chat_constants import (
        AUDIT_ENTITY_AGENT_RUN_LOG,
        AUDIT_ACTION_CREATE,
        AUDIT_RUN_STATUS_SUCCESS,
        AUDIT_RUN_STATUS_FAILED,
        AGENT_RUN_ERROR_AGENT_NOT_FOUND,
        AGENT_RUN_ERROR_PERMISSION_DENIED,
        AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE,
        AGENT_RUN_ERROR_BAD_INPUT,
        AGENT_RUN_ERROR_UNEXPECTED,
    )

    assert AUDIT_ENTITY_AGENT_RUN_LOG == "agent_run_log"
    assert AUDIT_ACTION_CREATE == "CREATE"
    assert AUDIT_RUN_STATUS_SUCCESS == "SUCCESS"
    assert AUDIT_RUN_STATUS_FAILED == "FAILED"
    assert AGENT_RUN_ERROR_AGENT_NOT_FOUND == "AGENT_NOT_FOUND"
    assert AGENT_RUN_ERROR_PERMISSION_DENIED == "PERMISSION_DENIED"
    assert AGENT_RUN_ERROR_AGENT_NOT_RUNNABLE == "AGENT_NOT_RUNNABLE"
    assert AGENT_RUN_ERROR_BAD_INPUT == "BAD_INPUT"
    assert AGENT_RUN_ERROR_UNEXPECTED == "UNEXPECTED"
