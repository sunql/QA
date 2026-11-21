import dayjs from "dayjs";
import { describe, expect, it } from "vitest";
import { toIsoDateParam } from "./dateParam";

describe("toIsoDateParam", () => {
  it("Dayjs（DatePicker 选中值）⇒ YYYY-MM-DD", () => {
    expect(toIsoDateParam(dayjs("2026-11-21"))).toBe("2026-11-21");
  });

  it("带时间的 Dayjs ⇒ 只取日期部分", () => {
    expect(toIsoDateParam(dayjs("2026-11-21T13:45:00"))).toBe("2026-11-21");
  });

  it("非法 Dayjs ⇒ undefined（不发字段，而不是发空串）", () => {
    expect(toIsoDateParam(dayjs("not-a-date"))).toBeUndefined();
  });

  it("空 / 空白 / null / undefined ⇒ undefined", () => {
    expect(toIsoDateParam(undefined)).toBeUndefined();
    expect(toIsoDateParam(null)).toBeUndefined();
    expect(toIsoDateParam("")).toBeUndefined();
    expect(toIsoDateParam("   ")).toBeUndefined();
  });

  it("已是 ISO 的字符串原样透传（服务端返回值）", () => {
    expect(toIsoDateParam("2026-01-01")).toBe("2026-01-01");
    expect(toIsoDateParam(" 2026-01-01 ")).toBe("2026-01-01");
  });

  it("非 ISO 写法返回 undefined —— 不猜格式（猜 = 静默改成另一个日期）", () => {
    expect(toIsoDateParam("2026/11/21")).toBeUndefined();
    expect(toIsoDateParam("2026年11月21日")).toBeUndefined();
    expect(toIsoDateParam("11-21-2026")).toBeUndefined();
  });
});
