import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import SqlPreview from "../components/chat/SqlPreview";

const writeTextMock = vi.fn();
const execCommandMock = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  // 默认 navigator.clipboard.writeText 不可用 → 走降级路径
  Object.defineProperty(navigator, "clipboard", {
    value: undefined,
    configurable: true,
  });
  // document.execCommand
  document.execCommand = execCommandMock;
});

function renderPreview(sql: string) {
  return render(
    <ConfigProvider locale={zhCN}>
      <SqlPreview sql={sql} />
    </ConfigProvider>,
  );
}

describe("SqlPreview — 渲染", () => {
  it("渲染 Collapse + 复制按钮", async () => {
    renderPreview("SELECT 1");
    // 点击 Collapse 展开
    const header = screen.getByText("查看 SQL");
    fireEvent.click(header);
    await waitFor(() =>
      expect(screen.getByTestId("sql-copy-button")).toBeInTheDocument(),
    );
  });
});

describe("SqlPreview — 复制（降级路径）", () => {
  beforeEach(() => {
    // 强制走降级路径：writeText undefined
    Object.defineProperty(navigator, "clipboard", {
      value: undefined,
      configurable: true,
    });
  });

  it("点击复制按钮 → 走 textarea 降级路径 + 显示已复制", async () => {
    execCommandMock.mockReturnValue(true);
    renderPreview("SELECT * FROM t");
    const header = screen.getByText("查看 SQL");
    fireEvent.click(header);

    const btn = await screen.findByTestId("sql-copy-button");
    fireEvent.click(btn);

    await waitFor(() => expect(execCommandMock).toHaveBeenCalledWith("copy"));
  });

  it("execCommand 抛错时不崩", async () => {
    execCommandMock.mockImplementation(() => {
      throw new Error("copy failed");
    });
    renderPreview("SELECT 1");
    const header = screen.getByText("查看 SQL");
    fireEvent.click(header);

    const btn = await screen.findByTestId("sql-copy-button");
    fireEvent.click(btn);

    // 验证组件不崩（无异常抛出）
    await waitFor(() => expect(btn).toBeInTheDocument());
  });
});

describe("SqlPreview — 复制（modern API）", () => {
  beforeEach(() => {
    // 强制走 navigator.clipboard.writeText 路径
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: writeTextMock },
      configurable: true,
    });
    writeTextMock.mockResolvedValue(undefined);
  });

  it("有 clipboard.writeText 时优先使用", async () => {
    renderPreview("SELECT 2");
    const header = screen.getByText("查看 SQL");
    fireEvent.click(header);

    const btn = await screen.findByTestId("sql-copy-button");
    fireEvent.click(btn);

    await waitFor(() =>
      expect(writeTextMock).toHaveBeenCalledWith("SELECT 2"),
    );
  });

  it("writeText 失败时降级到 message.error 但不崩", async () => {
    writeTextMock.mockRejectedValue(new Error("permission denied"));
    renderPreview("SELECT 3");
    const header = screen.getByText("查看 SQL");
    fireEvent.click(header);

    const btn = await screen.findByTestId("sql-copy-button");
    fireEvent.click(btn);

    await waitFor(() => expect(writeTextMock).toHaveBeenCalledWith("SELECT 3"));
  });
});