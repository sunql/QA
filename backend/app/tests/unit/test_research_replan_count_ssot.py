"""`replanCount` 只允许经 `OPT_REPLAN_COUNT` 出现（M1 的根因是双拼写）。

常量自己的定义在 `research_agent_ports.py`（那里字面量是**必须**的），消费者模块
一律不许再写一遍引号字面量 —— 否则「改常量值」与「写侧仍写老字符串」会静默分叉，
3 次 replan 上限随之失效（用户可无限循环 modify）。
"""

from pathlib import Path

SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"

CONSUMERS = (
    "research_agent_phases.py",
    "research_agent_service.py",
    "research_agent_stages.py",
    "research_agent_execution.py",
)


def test_no_bare_replan_count_literal_in_consumers() -> None:
    for name in CONSUMERS:
        text = (SERVICES_DIR / name).read_text(encoding="utf-8")
        for quote in ('"', "'"):
            literal = f"{quote}replanCount{quote}"
            assert literal not in text, (
                f"{name} 直写了 {literal}；请改用 OPT_REPLAN_COUNT（单一事实源）"
            )
