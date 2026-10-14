"""研究 payload JSON 编码器（Task 3）：驱动标量 → JSON 安全标量。

业务库驱动（Oracle/asyncpg/...）返回的行值不是 JSON 原生类型：``Decimal``
（numeric）、``date``/``datetime``（date/timestamp）、``uuid.UUID``（uuid）。
``json.dumps`` 对它们一律 ``TypeError``，而 SSE 帧的异常会炸掉**整帧**。

四类映射各自钉住**目标形态**（不是「只要不抛就算过」）：
- ``Decimal`` → ``str``（**不是 ``float``**：精度不可在线上丢）
- ``date`` / ``datetime`` → ``isoformat()``
- ``uuid.UUID`` → ``str``
- 未知类型 → ``TypeError`` 且消息点名类型（不静默 ``str()``）
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.research_json_encoder import research_json


def test_maps_decimal_to_lossless_string_not_float() -> None:
    """Decimal → str：`float` 会吃掉高精度尾数（known issue：Decimal/float 阈值边界）。"""
    assert json.loads(research_json({"amount": Decimal("123.45")}))["amount"] == "123.45"

    # 反向断言：这个值转 float 会失真 ⇒ 走 float 的实现过不了这一条
    lossy = Decimal("0.1000000000000000055511151231257827021181583404541015625")
    assert Decimal(str(float(lossy))) != lossy
    assert json.loads(research_json({"amount": lossy}))["amount"] == str(lossy)


def test_maps_date_and_datetime_to_isoformat() -> None:
    """date / datetime → ISO 8601 字符串（`datetime` 是 `date` 子类，分支顺序承重）。"""
    payload = {
        "createdOn": date(2024, 1, 15),
        "createdAt": datetime(2024, 1, 15, 10, 30, 0),
    }
    parsed = json.loads(research_json(payload))
    assert parsed["createdOn"] == "2024-01-15"
    assert parsed["createdAt"] == "2024-01-15T10:30:00"
    # 反向：date 不得被当成 datetime 加上 "T00:00:00"
    assert "T" not in parsed["createdOn"]


def test_maps_uuid_to_string() -> None:
    """UUID → 标准连字符字符串（不是 repr / 不是字段名）。"""
    value = uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert json.loads(research_json({"id": value}))["id"] == "11111111-1111-1111-1111-111111111111"

    randomValue = uuid.uuid4()
    assert json.loads(research_json({"id": randomValue}))["id"] == str(randomValue)


def test_maps_nested_containers_without_manual_walk() -> None:
    """嵌套 dict / list 内的驱动标量同样被映射（`json.dumps` 自行递归）。"""
    rowId = uuid.uuid4()
    payload = {
        "index": 0,
        "data": [
            {
                "AMOUNT": Decimal("1.5"),
                "CREATED_AT": datetime(2024, 3, 1, 8, 15, 30),
                "ROW_ID": rowId,
                "TAGS": [Decimal("2.25"), date(2024, 3, 2)],
            },
            {"AMOUNT": None},
        ],
        "rowCount": 2,
    }
    parsed = json.loads(research_json(payload))
    first = parsed["data"][0]
    assert first["AMOUNT"] == "1.5"
    assert first["CREATED_AT"] == "2024-03-01T08:15:30"
    assert first["ROW_ID"] == str(rowId)
    assert first["TAGS"] == ["2.25", "2024-03-02"]
    assert parsed["data"][1]["AMOUNT"] is None


@pytest.mark.parametrize("value", [b"bytes", {"a", "b"}, object(), complex(1, 2)])
def test_raises_type_error_naming_unknown_type(value: object) -> None:
    """未知类型显式 TypeError，且消息点名类型 —— 静默 str() 会把契约破坏伪装成正常。

    双向断言：既要求出现类型名，也要求是**本模块**的拒绝（`json.dumps` 自带的
    `Object of type ... is not JSON serializable` 同样带类型名 ⇒ 只断言类型名
    无法区分「mapper 拒绝了」与「json 自己拒绝了」）。
    """
    expectedTypeName = type(value).__name__
    with pytest.raises(TypeError) as excInfo:
        research_json({"unknown": value})
    message = str(excInfo.value)
    assert expectedTypeName in message
    # 下面这一行钉的是**判别器**，不是文案口味：`json.dumps` 自带的 TypeError 同样
    # 点名类型（"Object of type set is not JSON serializable"）⇒ 只断言类型名无法
    # 区分「mapper 拒绝」与「json 自己拒绝」，`research_json` 这个 token 才是分界线。
    # 改动错误文案时请同步改这里，**不要**因为它"只是文案"就删掉这条断言。
    assert "research_json" in message


def test_json_native_scalars_are_untouched() -> None:
    """JSON 原生类型（None/bool/int/float/str）原样透传，且输出与 json.dumps 逐字节一致。"""
    payload = {"none": None, "flag": True, "count": 7, "ratio": 1.5, "name": "供应商"}
    encoded = research_json(payload)
    assert encoded == json.dumps(payload, ensure_ascii=False)
    assert json.loads(encoded) == payload


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_fails_loudly_instead_of_emitting_invalid_json(value: float) -> None:
    """非有限 float 必须显式失败 —— 否则产出的是**浏览器读不了**的帧。

    `float` 走 json **原生**编码器（`default` 不触发），故这条只能靠 `allow_nan=False`
    兜住：关掉之前 `json.dumps` 会写出裸 `NaN` / `Infinity`，那不是合法 JSON，
    前端 `JSON.parse` 直接抛 ⇒ 整帧丢失，正是本任务要消灭的失败类。

    PG `double precision` 列可存 `'NaN'`、asyncpg 原样回 `float('nan')` ⇒ 可达。
    """
    with pytest.raises(ValueError, match="JSON compliant"):
        research_json({"ratio": value})
    # 嵌套同罪（容器由 json 自己递归，异常同样穿透）
    with pytest.raises(ValueError, match="JSON compliant"):
        research_json({"rows": [{"ratio": value}]})


def test_non_finite_decimal_keeps_its_string_form() -> None:
    """`Decimal('NaN')` 走 `default` ⇒ 字符串 `"NaN"`（**接受**，非拒绝）：与「Decimal 一律 str」同口径。

    刻意**不**与 float 的非有限值一刀切，理由是不对称的契约而非口味：
    - float 在线上是 JSON **数字字面量**，而 JSON 数字没有 NaN/Inf 的表示 ⇒ 只能失败；
    - Decimal 在本模块的线上形态**本来就是字符串**（精度原因，见模块 docstring），
      `"NaN"` 是它忠实且**可解析**的表示，浏览器拿到的是明确的 "NaN" 文本而非坏帧。
    另外 `numeric` 列合法地能存 `'NaN'`，把它升级成整帧失败会让**合法数据状态**拉黑通道。
    """
    assert research_json({"v": Decimal("NaN")}) == '{"v": "NaN"}'
    assert json.loads(research_json({"v": Decimal("NaN")}))["v"] == "NaN"
    assert json.loads(research_json({"v": Decimal("Infinity")}))["v"] == "Infinity"
    assert json.loads(research_json({"v": Decimal("-Infinity")}))["v"] == "-Infinity"


def test_keeps_non_ascii_readable() -> None:
    """`ensure_ascii=False`：中文不转义（SSE 帧里保持可读，与既有 `_sseFrame` 口径一致）。"""
    assert research_json({"name": "供应商"}) == '{"name": "供应商"}'
