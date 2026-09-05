import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import TermDictionaryButton from "../components/chat/TermDictionaryButton";

const api = vi.hoisted(() => ({
  createTerm: vi.fn(),
}));
vi.mock("../api/termDictionary", () => api);

function renderButton(props: { initialTerm?: string } = {}) {
  return render(
    <ConfigProvider locale={zhCN}>
      <TermDictionaryButton {...props} />
    </ConfigProvider>,
  );
}

describe("TermDictionaryButton", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.createTerm.mockResolvedValue({
      id: 1,
      term: "OTD",
      definition: "On-Time Delivery",
      mappedClassName: null,
      mappedPropertyName: null,
      formulaHint: null,
      createdBy: "u1",
      createdTime: "2026-09-01T00:00:00Z",
    });
  });

  it("渲染按钮：点击打开模态框", async () => {
    const user = userEvent.setup();
    renderButton();
    await user.click(screen.getByRole("button", { name: /添加到术语词典/ }));

    expect(
      await screen.findByText(/添加术语/),
    ).toBeInTheDocument();
  });

  it("initialTerm 预填到表单 term 字段", async () => {
    const user = userEvent.setup();
    renderButton({ initialTerm: "OTD" });
    await user.click(screen.getByRole("button", { name: /添加到术语词典/ }));

    const termInput = await screen.findByPlaceholderText(/占比/);
    expect((termInput as HTMLInputElement).value).toBe("OTD");
  });

  it("提交：必填字段填写后调用 createTerm 并 toast success", async () => {
    const user = userEvent.setup();
    const successSpy = vi.spyOn(message, "success").mockReturnValue(1 as unknown as ReturnType<typeof message.success>);
    renderButton();
    await user.click(screen.getByRole("button", { name: /添加到术语词典/ }));

    const termInput = await screen.findByPlaceholderText(/占比/);
    const defInput = screen.getByPlaceholderText(/占总量的比例/);
    await user.type(termInput, "OTD");
    await user.type(defInput, "On-Time Delivery");
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createTerm).toHaveBeenCalledTimes(1);
      expect(api.createTerm.mock.calls[0][0]).toMatchObject({
        term: "OTD",
        definition: "On-Time Delivery",
      });
      expect(successSpy).toHaveBeenCalled();
    });
    successSpy.mockRestore();
  });

  it("createTerm 失败时 catch 静默", async () => {
    const user = userEvent.setup();
    api.createTerm.mockRejectedValue(new Error("创建失败"));
    renderButton();
    await user.click(screen.getByRole("button", { name: /添加到术语词典/ }));

    const termInput = await screen.findByPlaceholderText(/占比/);
    const defInput = screen.getByPlaceholderText(/占总量的比例/);
    await user.type(termInput, "OTD");
    await user.type(defInput, "On-Time Delivery");
    await user.click(screen.getByRole("button", { name: /确\s?定/ }));

    await waitFor(() => {
      expect(api.createTerm).toHaveBeenCalled();
    });
  });
});
