/** dataUrlToBlob：把 PNG data URL 解码成真正的二进制 Blob。
 *
 * 既有 bug 的回归钉：导出曾把 base64 **字符串**原样写进 .png
 * （`new Blob([dataUrl])`），文件打不开 —— 解码必须发生在 Blob 之前。
 */
import { describe, expect, it } from "vitest";
import { dataUrlToBlob } from "../utils/download";

/** jsdom 的 Blob 未实现 .arrayBuffer()，用 FileReader 读字节。 */
function readBlobBytes(blob: Blob): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

describe("dataUrlToBlob", () => {
  it("PNG data URL 解码为 image/png Blob，字节是真正的 PNG 魔数", async () => {
    // 89 50 4E 47 0D 0A 1A 0A 是 PNG 文件头；字符串形式的 dataUrl 写进文件打不开
    const blob = dataUrlToBlob("data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==");

    expect(blob.type).toBe("image/png");
    const bytes = await readBlobBytes(blob);
    expect(Array.from(bytes.slice(0, 8))).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  });

  it("base64 之外的 payload 逐字节保真", async () => {
    const blob = dataUrlToBlob("data:image/png;base64,aGVsbG8=");

    const bytes = await readBlobBytes(blob);
    expect(new TextDecoder().decode(bytes)).toBe("hello");
  });

  it("非 PNG 前缀抛错（后端与前端只收 PNG）", () => {
    expect(() => dataUrlToBlob("data:image/svg+xml;base64,PHN2Zz4=")).toThrow();
    expect(() => dataUrlToBlob("not-a-data-url")).toThrow();
  });
});
