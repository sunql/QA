import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DocumentQaInput } from "../components/documents/DocumentQaInput";

describe("DocumentQaInput", () => {
  it("calls onSend with question and selected filters", async () => {
    const onSend = vi.fn();
    render(
      <DocumentQaInput
        sessionId="sess-1"
        loading={false}
        onSend={onSend}
      />
    );
    fireEvent.change(screen.getByPlaceholderText(/输入问题/), { target: { value: "什么是质量协议？" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(onSend).toHaveBeenCalledWith(
      "什么是质量协议？",
      expect.objectContaining({}),
    ));
  });

  it("does not call onSend when question is empty/whitespace", async () => {
    const onSend = vi.fn();
    render(
      <DocumentQaInput
        sessionId="sess-1"
        loading={false}
        onSend={onSend}
      />
    );
    fireEvent.change(screen.getByPlaceholderText(/输入问题/), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(onSend).not.toHaveBeenCalled());
  });

  it("clears the input after sending", async () => {
    const onSend = vi.fn();
    render(
      <DocumentQaInput
        sessionId="sess-1"
        loading={false}
        onSend={onSend}
      />
    );
    const textarea = screen.getByPlaceholderText(/输入问题/) as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "质量协议条款？" } });
    fireEvent.click(screen.getByRole("button", { name: /发送/ }));
    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(textarea.value).toBe("");
  });
});