/** 文档批量上传弹窗：多选 → 串行上传 → 逐文件结果 → 只重试失败项。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import DocumentUploadModal from "../components/documents/DocumentUploadModal";

const api = vi.hoisted(() => ({ uploadDocument: vi.fn() }));
vi.mock("../api/document", () => api);

function makeFile(name: string): File {
  return new File(["dummy"], name, { type: "application/pdf" });
}

function renderModal(onUploaded = vi.fn(), onClose = vi.fn()) {
  render(<DocumentUploadModal open onClose={onClose} onUploaded={onUploaded} />);
  return { onUploaded, onClose };
}

async function pickFiles(...names: string[]) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(input, { target: { files: names.map(makeFile) } });
  for (const name of names) {
    await waitFor(() => expect(screen.getByText(name)).toBeTruthy());
  }
}

describe("DocumentUploadModal", () => {
  beforeEach(() => vi.clearAllMocks());

  it("多选后串行上传，全部成功回调 onUploaded", async () => {
    api.uploadDocument
      .mockResolvedValueOnce({ documentId: "DOC1", chunks: 3 })
      .mockResolvedValueOnce({ documentId: "DOC2", chunks: 5 });
    const { onUploaded } = renderModal();

    await pickFiles("a.pdf", "b.pdf");
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));

    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledTimes(2));
    expect(onUploaded).toHaveBeenCalled();
    const firstFile = api.uploadDocument.mock.calls[0][0] as File;
    expect(firstFile.name).toBe("a.pdf");
  });

  it("部分失败时保留结果并可只重试失败项", async () => {
    api.uploadDocument
      .mockRejectedValueOnce({ response: { data: { error: "内容重复" } } })
      .mockResolvedValueOnce({ documentId: "DOC2", chunks: 1 })
      .mockResolvedValueOnce({ documentId: "DOC1", chunks: 2 });
    const { onUploaded } = renderModal();

    await pickFiles("a.pdf", "b.pdf");
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));

    await waitFor(() => expect(screen.getByText("内容重复")).toBeTruthy());
    expect(onUploaded).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /重试失败项/ })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /重试失败项/ }));
    await waitFor(() => expect(api.uploadDocument).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(onUploaded).toHaveBeenCalled());
  });

  it("未选文件直接提交给出提示，不调用上传", async () => {
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));
    expect(api.uploadDocument).not.toHaveBeenCalled();
  });
});


  it("部分成功也会触发列表刷新（弹窗保持打开）", async () => {
    const onPartial = vi.fn();
    api.uploadDocument
      .mockRejectedValueOnce({ response: { data: { error: "内容重复" } } })
      .mockResolvedValueOnce({ documentId: "DOC2", chunks: 1 });

    render(
      <DocumentUploadModal
        open
        onClose={vi.fn()}
        onUploaded={vi.fn()}
        onPartiallyUploaded={onPartial}
      />,
    );
    await pickFiles("a.pdf", "b.pdf");
    fireEvent.click(screen.getByRole("button", { name: /开始上传/ }));

    await waitFor(() => expect(onPartial).toHaveBeenCalled());
  });
