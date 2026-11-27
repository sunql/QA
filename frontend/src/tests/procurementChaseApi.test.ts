/** 催货单 API client（feat/delivery-chase-closed-loop）：请求形状 + actor 不外传。 */

import { beforeEach, describe, expect, it, vi } from "vitest";

const postMock = vi.hoisted(() => vi.fn());
const getMock = vi.hoisted(() => vi.fn());

vi.mock("../api/client", () => ({
  httpClient: {
    post: (...args: unknown[]) => postMock(...args),
    get: (...args: unknown[]) => getMock(...args),
  },
}));

const { createChaseFromPending, decideChase, sendChaseDraft } = await import(
  "../api/procurementChase"
);

describe("procurementChase api", () => {
  beforeEach(() => {
    postMock.mockReset();
    getMock.mockReset();
  });

  it("createChaseFromPending 只发身份对（poNo/poLine），不带数量", async () => {
    postMock.mockResolvedValue({ data: { requestId: "R", drafts: [], unresolvedSuppliers: [], skippedLines: [] } });

    await createChaseFromPending({
      lines: [{ poNo: "PO1", poLine: "1" }],
      notes: "催一下",
    });

    const [url, body] = postMock.mock.calls[0] as [string, Record<string, unknown>];
    expect(url).toContain("/procurement/chase/from-pending");
    expect(body.lines).toEqual([{ poNo: "PO1", poLine: "1" }]);
    expect(JSON.stringify(body)).not.toContain("openQty");
  });

  it("decideChase 不传 actor（后端从登录身份取）", async () => {
    postMock.mockResolvedValue({ data: { draft: {}, lines: [], decisions: [] } });

    await decideChase("abc", "approve", "同意");

    const [url, body] = postMock.mock.calls[0] as [string, Record<string, unknown>];
    expect(url).toContain("/drafts/abc/approve");
    expect(body).toEqual({ note: "同意" });
    expect(JSON.stringify(body)).not.toContain("actor");
  });

  it("decideChase 无 note ⇒ 空 body", async () => {
    postMock.mockResolvedValue({ data: { draft: {}, lines: [], decisions: [] } });

    await decideChase("abc", "cancel");

    const body = postMock.mock.calls[0][1];
    expect(body).toEqual({});
  });

  it("sendChaseDraft 打到 send 端点", async () => {
    postMock.mockResolvedValue({ data: { draft: {}, lines: [], decisions: [] } });

    await sendChaseDraft("abc");

    expect(postMock.mock.calls[0][0]).toContain("/drafts/abc/send");
  });
});
