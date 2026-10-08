import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

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
      // 真实 httpClient 的 defaults 里**只有** X-Tenant-Id（见 api/client.ts:44-47）——
      // 这里曾经写着 X-User-Id: "user-1"，是让导出缺陷隐身的那份虚构。
      headers: { "X-Tenant-Id": "tenant-1" },
    },
  },
}));

import {
  listChatSessions,
  loadSessionMessages,
  deleteSessionHistory,
  exportSessionPdf,
} from "../api/chatHistory";
import { useAuthStore } from "../stores/authStore";
import { DEFAULT_TENANT_ID } from "../config";

describe("api/chatHistory", () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { useAuthStore.setState({ token: null }); });

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

  it("loadSessionMessages 默认 limit=200 / 无游标", async () => {
    httpMock.get.mockResolvedValue({ data: { sessionId: "s-1", messages: [] } });
    await loadSessionMessages("s-1");
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s-1/messages", {
      params: { limit: 200 },
    });
  });

  it("loadSessionMessages 携带 before_id cursor（参数名与后端 alias 一致）", async () => {
    // 曾经发的是 beforeId，而后端 alias 是 before_id —— 游标被 FastAPI 静默忽略，
    // 分页永远停在第一页且不报错。这里钉住线上真正生效的那个名字。
    httpMock.get.mockResolvedValue({ data: { sessionId: "s-1", messages: [] } });
    await loadSessionMessages("s-1", 50, { beforeId: 100 });
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s-1/messages", {
      params: { limit: 50, before_id: 100 },
    });
  });

  it("loadSessionMessages 的 tail 取最新一批（导出配图用）", async () => {
    httpMock.get.mockResolvedValue({ data: { sessionId: "s-1", messages: [] } });
    await loadSessionMessages("s-1", 1000, { tail: true });
    expect(httpMock.get).toHaveBeenCalledWith("/sessions/s-1/messages", {
      params: { limit: 1000, tail: true },
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
    const spy = vi.spyOn(axiosMock.default, "post").mockResolvedValue({
      data: new Blob(["pdf-content"], { type: "application/pdf" }),
    } as never);

    const blob = await exportSessionPdf("s-1");
    expect(blob).toBeInstanceOf(Blob);
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/sessions/s-1/export.pdf",
      {},
      expect.objectContaining({ responseType: "blob" }),
    );

    spy.mockRestore();
  });

  it("exportSessionPdf 携带 messageId 参数", async () => {
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "post").mockResolvedValue({
      data: new Blob(["pdf"]),
    } as never);

    await exportSessionPdf("s-1", { messageId: 42 });
    expect(spy).toHaveBeenCalledWith(
      "/api/v1/sessions/s-1/export.pdf",
      { messageId: 42 },
      expect.anything(),
    );

    spy.mockRestore();
  });

  it("exportSessionPdf 注入 Bearer（裸 axios 不走拦截器，靠 authHeaders SSOT）", async () => {
    // 为什么必须钉住：`sessions` router 是 **router 级** 鉴权
    // （api/v1/session.py:59 `APIRouter(dependencies=[Depends(getCurrentUser)])`）。
    // 这条路径刻意绕开 httpClient（PDF 是二进制，走不了信封解包），于是请求拦截器
    // 的 Bearer 注入也一并绕开了；nginx 又把 X-User-* 剥掉 ⇒ 线上表现为导出 **403
    // 请先登录**，且不报错到页面之外。SSOT 是 api/authHeaders.ts。
    useAuthStore.setState({ token: "test-jwt" });
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "post").mockResolvedValue({
      data: new Blob(["pdf"]),
    } as never);

    await exportSessionPdf("s-1");

    const headers = spy.mock.calls[0]?.[2]?.headers;
    expect(headers).toMatchObject({
      Authorization: "Bearer test-jwt",
      "X-Tenant-Id": DEFAULT_TENANT_ID,
    });
    // 反向守卫：不能退回去发那条伪造的 X-User-Id（nginx 会剥掉，等于没发）
    expect(headers).not.toHaveProperty("X-User-Id");

    spy.mockRestore();
  });

  it("exportSessionPdf 未登录时不注入 Authorization", async () => {
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "post").mockResolvedValue({
      data: new Blob(["pdf"]),
    } as never);

    await exportSessionPdf("s-1");

    expect(spy.mock.calls[0]?.[2]?.headers).not.toHaveProperty("Authorization");

    spy.mockRestore();
  });

  it("exportSessionPdf 把图表位图放进请求体（0105 图表进最终报告）", async () => {
    const axiosMock = await import("axios");
    const spy = vi.spyOn(axiosMock.default, "post").mockResolvedValue({
      data: new Blob(["pdf"]),
    } as never);
    const charts = [{ messageId: 7, imagePng: "data:image/png;base64,AA==" }];

    await exportSessionPdf("s-1", { charts });

    expect(spy).toHaveBeenCalledWith(
      "/api/v1/sessions/s-1/export.pdf",
      { charts },
      expect.anything(),
    );

    spy.mockRestore();
  });
});