import { describe, it, expect, vi } from "vitest";
import { downloadCsv, readFileAsText } from "../utils/csvDownload";

describe("utils/csvDownload - readFileAsText", () => {
  it("rejects unsupported file extension", async () => {
    const file = new File(["x"], "data.xlsx", { type: "application/vnd.ms-excel" });
    await expect(readFileAsText(file)).rejects.toThrow(/不支持的文件后缀/);
  });

  it("rejects file exceeding maxBytes", async () => {
    // 真实大小验证：构造 2KB 文件但 maxBytes=1KB
    const big = "x".repeat(2 * 1024);
    const file = new File([big], "big.csv", { type: "text/csv" });
    await expect(readFileAsText(file, { maxBytes: 1024 })).rejects.toThrow(
      /文件过大/,
    );
  });

  it("accepts .csv / .tsv / .txt by default", async () => {
    for (const ext of ["csv", "tsv", "txt"]) {
      const file = new File(["hello"], `data.${ext}`, { type: "text/plain" });
      // jsdom FileReader 是 stub；不实际读，重点是**没因后缀拒绝**
      const result = await readFileAsText(file).catch((e) => `ERR:${e}`);
      expect(result).toBeDefined();
      expect(String(result)).not.toMatch(/不支持的文件后缀/);
    }
  });

  it("uppercase file extension accepted (case-insensitive)", async () => {
    const file = new File(["x"], "DATA.CSV", { type: "text/csv" });
    const result = await readFileAsText(file).catch((e) => `ERR:${e}`);
    expect(String(result)).not.toMatch(/不支持的文件后缀/);
  });
});

describe("utils/csvDownload - downloadCsv", () => {
  it("creates a Blob with text/csv mime and triggers a[download]", () => {
    const createUrl = vi.fn(() => "blob:fake-url");
    const revokeUrl = vi.fn();
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    URL.createObjectURL = createUrl;
    URL.revokeObjectURL = revokeUrl;

    const clickMock = vi.fn();
    const appendChildSpy = vi.spyOn(document.body, "appendChild");
    const removeChildSpy = vi.spyOn(document.body, "removeChild");
    // 替换 a.click 的副作用
    const origCreateElement = document.createElement.bind(document);
    const createElementSpy = vi
      .spyOn(document, "createElement")
      .mockImplementation((tag: string) => {
        const el = origCreateElement(tag) as HTMLElement;
        if (tag === "a") {
          (el as HTMLAnchorElement).click = clickMock;
        }
        return el;
      });

    try {
      downloadCsv("a,b\n1,2", "out.csv");
      expect(createUrl).toHaveBeenCalledOnce();
      expect(clickMock).toHaveBeenCalledOnce();
      expect(appendChildSpy).toHaveBeenCalledOnce();
      expect(removeChildSpy).toHaveBeenCalledOnce();
      expect(revokeUrl).toHaveBeenCalledOnce();
    } finally {
      URL.createObjectURL = originalCreate;
      URL.revokeObjectURL = originalRevoke;
      createElementSpy.mockRestore();
      appendChildSpy.mockRestore();
      removeChildSpy.mockRestore();
    }
  });

  it("withBom=false omits the UTF-8 BOM", () => {
    let captured: Blob | null = null;
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn((blob: Blob) => {
      captured = blob;
      return "blob:fake";
    });
    URL.revokeObjectURL = vi.fn();

    try {
      downloadCsv("a,b", "out.csv", { withBom: false });
      expect(captured).not.toBeNull();
      // Blob 不可直接读 text，但 size + type 可查
      expect(captured!.type).toBe("text/csv;charset=utf-8");
      expect(captured!.size).toBe("a,b".length);
    } finally {
      URL.createObjectURL = origCreate;
    }
  });

  it("withBom=true prepends UTF-8 BOM (size grows by 3 bytes)", () => {
    let captured: Blob | null = null;
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = vi.fn((blob: Blob) => {
      captured = blob;
      return "blob:fake";
    });
    URL.revokeObjectURL = vi.fn();

    try {
      downloadCsv("a,b", "out.csv", { withBom: true });
      expect(captured!.size).toBe("a,b".length + 3); // BOM 是 3 字节 UTF-8
    } finally {
      URL.createObjectURL = origCreate;
    }
  });
});