import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import axios from "axios";
import { createHttpClient } from "../api/client";

// mock message.error 以避免 jsdom 下 antd 报警
vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: { ...actual.message, error: vi.fn(), success: vi.fn() },
  };
});

// mock axios.create 返回我们可控的实例
vi.mock("axios", () => {
  const instance = {
    interceptors: {
      response: { use: vi.fn() },
    },
    get: vi.fn(),
    post: vi.fn(),
  };
  return {
    default: { create: vi.fn(() => instance) },
  };
});

describe("api/client", () => {
  let responseHandlers: { onFulfilled: Function; onRejected: Function };

  beforeEach(() => {
    responseHandlers = { onFulfilled: () => {}, onRejected: () => {} };
    const instance = (axios.create as ReturnType<typeof vi.fn>)();
    (instance.interceptors.response.use as ReturnType<typeof vi.fn>).mockImplementation(
      (onFulfilled: Function, onRejected: Function) => {
        responseHandlers.onFulfilled = onFulfilled;
        responseHandlers.onRejected = onRejected;
      }
    );
    createHttpClient();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("解包成功信封并返回 data", () => {
    const response = {
      data: { success: true, data: { id: 1 }, error: null, timestamp: "t" },
    } as any;
    const result = responseHandlers.onFulfilled(response);
    expect(result.data).toEqual({ id: 1 });
  });

  it("success=false 时抛出并提示错误", async () => {
    const response = {
      data: { success: false, data: null, error: "模型已存在", timestamp: "t" },
    } as any;
    await expect(responseHandlers.onFulfilled(response)).rejects.toThrow(
      "模型已存在"
    );
  });

  it("HTTP 错误时优先使用响应体 error 消息", async () => {
    const error = {
      response: {
        status: 404,
        data: { success: false, error: "未找到", data: null, timestamp: "t" },
      },
    };
    await expect(responseHandlers.onRejected(error)).rejects.toThrow("未找到");
  });

  it("网络异常时给出默认提示", async () => {
    const error = { response: undefined };
    await expect(responseHandlers.onRejected(error)).rejects.toThrow(
      "网络异常，请稍后重试"
    );
  });
});
