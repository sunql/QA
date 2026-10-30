import { describe, it, expect, vi, beforeEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

import { listMessages, getUnreadCount, markMessageRead } from "../api/messages";

describe("api/messages", () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it("listMessages GET /messages（默认 unreadOnly=false & limit=20）", async () => {
    httpMock.get.mockResolvedValue({ data: [] });
    const result = await listMessages();
    expect(httpMock.get).toHaveBeenCalledWith("/messages", {
      params: { unreadOnly: false, limit: 20 },
    });
    expect(result).toEqual([]);
  });

  it("listMessages 携带 unreadOnly / limit", async () => {
    httpMock.get.mockResolvedValue({ data: [{ id: 1 }] });
    await listMessages(true, 5);
    expect(httpMock.get).toHaveBeenCalledWith("/messages", {
      params: { unreadOnly: true, limit: 5 },
    });
  });

  it("getUnreadCount GET /messages/unread-count 并解包 unreadCount", async () => {
    httpMock.get.mockResolvedValue({ data: { unreadCount: 4 } });
    const result = await getUnreadCount();
    expect(httpMock.get).toHaveBeenCalledWith("/messages/unread-count");
    expect(result).toBe(4);
  });

  it("markMessageRead POST /messages/:id/read（无 body）", async () => {
    httpMock.post.mockResolvedValue({});
    await markMessageRead(9);
    expect(httpMock.post).toHaveBeenCalledWith("/messages/9/read");
  });
});
