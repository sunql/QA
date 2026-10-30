import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const httpMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock("../api/client", () => ({ httpClient: httpMock }));

// previewImportFile 走裸 axios.postForm（multipart），绕开 httpClient 的
// 全局 Content-Type: application/json —— 对齐 api/document.ts 的上传约定。
const axiosMock = vi.hoisted(() => ({ postForm: vi.fn() }));
vi.mock("axios", () => ({ default: axiosMock }));

import {
  listImportModels,
  previewImport,
  previewImportFile,
  executeImport,
  listImportTasks,
  getImportTask,
  retryImportTask,
} from "../api/wikiImport";
import { useAuthStore } from "../stores/authStore";
import { API_BASE_URL, DEFAULT_TENANT_ID } from "../config";

describe("api/wikiImport", () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => {
    useAuthStore.setState({ token: null });
  });

  it("listImportModels GET /wiki/import/models", async () => {
    httpMock.get.mockResolvedValue({
      data: [
        { id: 1, modelName: "gpt-4o", provider: "openai", usable: true, isActive: true },
      ],
    });
    const result = await listImportModels();
    expect(httpMock.get).toHaveBeenCalledWith("/wiki/import/models");
    expect(result).toHaveLength(1);
    expect(result[0].modelName).toBe("gpt-4o");
  });

  it("previewImport POST /wiki/import/preview（body 只带 source）", async () => {
    httpMock.post.mockResolvedValue({ data: { drafts: [], total: 0 } });
    const result = await previewImport("# 标题\n正文");
    expect(httpMock.post).toHaveBeenCalledWith("/wiki/import/preview", {
      source: "# 标题\n正文",
    });
    expect(result.total).toBe(0);
  });

  it("previewImportFile 走裸 axios.postForm + FormData + auth headers", async () => {
    useAuthStore.setState({ token: "test-jwt" });
    axiosMock.postForm.mockResolvedValue({
      data: { text: "纯文本", sourceType: "markdown" },
    });
    const file = new File(["# 内容"], "note.md", { type: "text/markdown" });

    const result = await previewImportFile(file);

    expect(axiosMock.postForm).toHaveBeenCalledTimes(1);
    expect(httpMock.post).not.toHaveBeenCalled();

    const call = axiosMock.postForm.mock.calls[0];
    expect(call[0]).toBe(`${API_BASE_URL}/wiki/import/preview-file`);
    // multipart body：FormData 里带 "file" 字段，且不能带 httpClient 的 json Content-Type
    const form = call[1] as FormData;
    expect(form).toBeInstanceOf(FormData);
    expect(form.get("file")).toBe(file);
    expect(call[2].headers["X-Tenant-Id"]).toBe(DEFAULT_TENANT_ID);
    expect(call[2].headers["Authorization"]).toBe("Bearer test-jwt");
    expect(result).toEqual({ text: "纯文本", sourceType: "markdown" });
  });

  it("previewImportFile 无登录态时不注入 Authorization", async () => {
    axiosMock.postForm.mockResolvedValue({ data: { text: "", sourceType: "txt" } });
    await previewImportFile(new File(["x"], "x.txt"));
    const call = axiosMock.postForm.mock.calls[0];
    expect(call[2].headers["Authorization"]).toBeUndefined();
    expect(call[2].headers["X-Tenant-Id"]).toBe(DEFAULT_TENANT_ID);
  });

  it("executeImport POST /wiki/import/execute", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 9, status: "PENDING" } });
    const payload = {
      drafts: [{ pageId: null, title: "T", content: "C" }],
      modelId: 3,
      autoClassify: true,
    };
    const result = await executeImport(payload);
    expect(httpMock.post).toHaveBeenCalledWith("/wiki/import/execute", payload);
    expect(result.id).toBe(9);
  });

  it("listImportTasks GET /wiki/import/tasks（默认 limit=20 offset=0）", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listImportTasks();
    expect(httpMock.get).toHaveBeenCalledWith("/wiki/import/tasks", {
      params: { limit: 20, offset: 0 },
    });
  });

  it("listImportTasks 携带自定义 limit/offset", async () => {
    httpMock.get.mockResolvedValue({ data: { rows: [], total: 0 } });
    await listImportTasks({ limit: 5, offset: 10 });
    expect(httpMock.get).toHaveBeenCalledWith("/wiki/import/tasks", {
      params: { limit: 5, offset: 10 },
    });
  });

  it("getImportTask GET /wiki/import/tasks/:taskId", async () => {
    httpMock.get.mockResolvedValue({ data: { id: 7, status: "RUNNING" } });
    const result = await getImportTask(7);
    expect(httpMock.get).toHaveBeenCalledWith("/wiki/import/tasks/7");
    expect(result.status).toBe("RUNNING");
  });

  it("retryImportTask POST /wiki/import/tasks/:taskId/retry（无 body）", async () => {
    httpMock.post.mockResolvedValue({ data: { id: 8, retryOfTaskId: 7 } });
    const result = await retryImportTask(7);
    // 源实现只传 URL 一个参数（无 body），断言必须严格 1 参
    expect(httpMock.post).toHaveBeenCalledWith("/wiki/import/tasks/7/retry");
    expect(result.retryOfTaskId).toBe(7);
  });
});
