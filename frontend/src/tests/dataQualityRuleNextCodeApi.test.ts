import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { fetchNextRuleCode } from "../api/dataQualityRuleNextCode";
import type { NextRuleCodeResponse } from "../api/dataQualityRuleNextCode";

const NEXT_CODE: NextRuleCodeResponse = { code: "DQ_AMOUNT_20260930_001", seq: 1 };

describe("api/dataQualityRuleNextCode", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("fetchNextRuleCode GET /data-quality/rules/next-code（不传 date 时 params 只有 className）", async () => {
    httpMock.get.mockResolvedValue({ data: NEXT_CODE });
    const result = await fetchNextRuleCode({ className: "金额" });
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/rules/next-code", {
      params: { className: "金额" },
    });
    expect(result).toEqual(NEXT_CODE);
  });

  it("fetchNextRuleCode 传 date 时 params 带 className + date", async () => {
    httpMock.get.mockResolvedValue({ data: NEXT_CODE });
    await fetchNextRuleCode({ className: "金额", date: "20260930" });
    expect(httpMock.get).toHaveBeenCalledWith("/data-quality/rules/next-code", {
      params: { className: "金额", date: "20260930" },
    });
  });

  it("请求失败时把后端错误原样抛出（不吞异常）", async () => {
    httpMock.get.mockRejectedValue(new Error("Request failed with status code 409"));
    await expect(fetchNextRuleCode({ className: "金额" })).rejects.toThrow("409");
  });
});
