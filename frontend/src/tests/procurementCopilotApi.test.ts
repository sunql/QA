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
  describeCopilotError,
  getCopilotConversation,
  listCopilotConversations,
} from "../api/procurementCopilot";

beforeEach(() => {
  httpMock.get.mockReset();
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
