/**
 * 日期入参归一：antd `DatePicker`（Dayjs）或既有字符串 → 后端 `date` 契约。
 *
 * 背景（2026-11-21 实测）：文档编辑弹窗的「生效日期」原是**自由文本 Input**，
 * 后端只接受 ISO `YYYY-MM-DD`；用户输入 `2026/11/21`、`2026年11月21日` 甚至
 * 留空串时，pydantic 直接 422（`date_from_datetime_parsing`），前端只在控制台
 * 留一行 422，界面上什么都看不到。
 *
 * 口径：
 * - `undefined` / `null` / 空白串 ⇒ `undefined`（**不发这个字段**，保持"没填"语义）；
 * - 字符串只接受已符合 `YYYY-MM-DD` 的值（服务端返回值就是该格式，原样透传）；
 * - Dayjs ⇒ `format("YYYY-MM-DD")`；非法 Dayjs ⇒ `undefined`。
 *
 * 不做"猜格式"的宽松解析：把 `2026/11/21` 猜成日期，等于把用户的输入错误
 * 变成静默的另一个日期。
 */

import type { Dayjs } from "dayjs";

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Dayjs / 字符串 → `YYYY-MM-DD` 或 undefined。 */
export function toIsoDateParam(
  value: Dayjs | string | null | undefined,
): string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "string") {
    const trimmed = value.trim();
    return ISO_DATE_RE.test(trimmed) ? trimmed : undefined;
  }
  return value.isValid() ? value.format("YYYY-MM-DD") : undefined;
}
