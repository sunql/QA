/** 采购 Copilot 客户端：会话列表 / 会话详情 / 错误体压平。 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import {
  deleteCopilotConversation,
  describeCopilotError,
  getCopilotConversation,
  getDeliveryTrackingOverview,
  getPendingDeliverySummary,
  listCopilotConversations,
  listPendingDeliverySites,
} from "../api/procurementCopilot";

beforeEach(() => {
  httpMock.get.mockReset();
  httpMock.delete.mockReset();
});

describe("listCopilotConversations", () => {
  it("按 limit 拉取当前用户的历史会话", async () => {
    const rows = [
      {
        conversationId: "c-1",
        agentCode: "PROCUREMENT_SOURCING",
        modelConfigId: 3,
        turnCount: 2,
        lastQuestion: "最近一问",
        updatedAt: "2026-11-23T10:00:00Z",
      },
    ];
    httpMock.get.mockResolvedValue({ data: rows });

    const result = await listCopilotConversations(5);

    expect(httpMock.get).toHaveBeenCalledWith(
      "/procurement/copilot/conversations",
      { params: { limit: 5 } },
    );
    expect(result).toEqual(rows);
  });

  it("默认 limit = 20", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listCopilotConversations();
    expect(httpMock.get).toHaveBeenCalledWith(
      "/procurement/copilot/conversations",
      { params: { limit: 20 } },
    );
  });
});

describe("getCopilotConversation", () => {
  it("按 id 拉会话详情，并对 id 做 URL 编码", async () => {
    const detail = {
      conversationId: "c 1",
      agentCode: "PROCUREMENT_SOURCING",
      modelConfigId: 3,
      turns: [],
    };
    httpMock.get.mockResolvedValue({ data: detail });

    const result = await getCopilotConversation("c 1");

    expect(httpMock.get).toHaveBeenCalledWith(
      "/procurement/copilot/conversations/c%201",
    );
    expect(result).toEqual(detail);
  });
});

describe("describeCopilotError", () => {
  it("字符串错误体原样返回", () => {
    expect(describeCopilotError("boom", "fallback")).toBe("boom");
  });

  it("pydantic 数组压成一句", () => {
    expect(
      describeCopilotError({ detail: [{ msg: "a" }, { msg: "b" }] }, "fallback"),
    ).toBe("a；b");
  });

  it("无法识别时用 fallback", () => {
    expect(describeCopilotError({ nope: 1 }, "fallback")).toBe("fallback");
  });
});

describe("deleteCopilotConversation", () => {
  it("DELETE 会话（id 做 URL 编码）", async () => {
    httpMock.delete.mockResolvedValue({ data: null });
    await deleteCopilotConversation("c 1");
    expect(httpMock.delete).toHaveBeenCalledWith(
      "/procurement/copilot/conversations/c%201",
    );
  });
});

describe("getPendingDeliverySummary", () => {
  it("带过滤参数请求统计端点（多值逗号分隔）", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        summary: { openLines: 12, openQty: 500, overdueLines: 7, excludedRows: 0 },
        bySupplier: [],
        topLines: [],
      },
    });

    const result = await getPendingDeliverySummary({
      monthsBack: 6,
      supplierKeys: ["F109", "B125"],
    });

    expect(httpMock.get).toHaveBeenCalledWith(
      "/procurement/pending-delivery/summary",
      { params: { monthsBack: 6, supplierKeys: "F109,B125" } },
    );
    expect(result.summary.openLines).toBe(12);
  });
});

describe("listPendingDeliverySites", () => {
  it("请求基地字典时不带数据源参数（THBI 硬规则）", async () => {
    httpMock.get.mockResolvedValue({ data: [{ code: "T3", name: "模具厂" }] });
    const rows = await listPendingDeliverySites();
    expect(httpMock.get).toHaveBeenCalledWith("/procurement/pending-delivery/sites", {
      params: {},
    });
    expect(rows[0].code).toBe("T3");
  });
});

describe("getDeliveryTrackingOverview", () => {
  it("默认请求本期概览（不带窗口参数）", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
        summary: { totalOrders: 4843, orderArrivalRate: 83.67, notArrivedOrders: 357 },
        bySupplier: [],
      },
    });

    const result = await getDeliveryTrackingOverview();

    expect(httpMock.get).toHaveBeenCalledWith("/procurement/delivery-tracking/overview", {
      params: {},
    });
    expect(result.summary.orderArrivalRate).toBe(83.67);
  });

  it("供应商过滤用逗号分隔（axios 默认序列化成 key[]=v 会静默失效）", async () => {
    httpMock.get.mockResolvedValue({
      data: {
        window: { dateFrom: "2026-10-24", dateTo: "2026-11-24", mode: "anchorDay" },
        summary: { totalOrders: 1, orderArrivalRate: 0, notArrivedOrders: 1 },
        bySupplier: [],
      },
    });

    await getDeliveryTrackingOverview({ supplierKeys: ["B125", "B019"] });

    expect(httpMock.get).toHaveBeenCalledWith("/procurement/delivery-tracking/overview", {
      params: { supplierKeys: "B125,B019" },
    });
  });
});
