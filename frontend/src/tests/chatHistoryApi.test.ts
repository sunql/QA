import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({
  httpClient: {
    ...httpMock,
    defaults: {
      baseURL: "/api/v1",
      timeout: 30000,
      headers: { "X-Tenant-Id": "tenant-1", "X-User-Id": "user-1" },
    },
  },
}));

import {
  listChatSessions,
  loadSessionMessages,
  deleteSessionHistory,
  exportSessionPdf,
} from "../api/chatHistory";

describe("api/chatHistory", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listChatSessions 默认 limit=50 / offset=0", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listChatSessions();
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/chat-history", {
      params: { limit: 50, offset: 0 },
    });
  });

  it("listChatSessions 接受自定义 limit / offset", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    await listChatSessions(10, 20);
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/chat-history", {
      params: { limit: 10, offset: 20 },
    });
  });

  it("loadSessionMessages 默认 limit=200 / 无 beforeId", async () => {
    httpMock.get.mockResolvedValue({ data: { sessionId: "s-1", messages: [] } });
    await loadSessionMessages("s-1");
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s-1/messages", {
      params: { limit: 200 },
    });
  });

  it("loadSessionMessages 携带 beforeId cursor", async () => {
    httpMock.get.mockResolvedValue({ data: { sessionId: "s-1", messages: [] } });
    await loadSessionMessages("s-1", 50, 100);
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s-1/messages", {
      params: { limit: 50, beforeId: 100 },
    });
  });

  it("deleteSessionHistory DELETE /sessions/:sessionId", async () => {
    httpMock.delete.mockResolvedValue({ data: undefined });
    await deleteSessionHistory("s-1");
    expect(httpMock.delete).toHaveBeenCalledWith("/sessions/s-1");
  });

  it("exportSessionPdf 使用 raw axios（不走 httpClient）", async () => {
    // 此测试只需要验证调用成功 + 返回 Blob；具体实现细节见 localImportApi.test.ts
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "get").mockResolvedValue({
      data: new Blob(["pdf-content"], { type: "application/pdf" }),
    } as never);

    const blob = await exportSessionPdf("s-1");
    expect(blob).toBeInstanceOf(Blob);
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/sessions/s-1/export.pdf",
      expect.objectContaining({
        params: {},
        responseType: "blob",
      }),
    );

    spy.mockRestore();
  });

  it("exportSessionPdf 携带 messageId 参数", async () => {
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "get").mockResolvedValue({
      data: new Blob(["pdf"]),
    } as never);

    await exportSessionPdf("s-1", 42);
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/sessions/s-1/export.pdf",
      expect.objectContaining({ params: { messageId: 42 } }),
    );

    spy.mockRestore();
  });
});