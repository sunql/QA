/**
 * 上传白名单必须与**后端解析器**同源（跨层漂移守卫）。
 *
 * 2026-11-22 用户实测：文档中心不能上传 xlsx —— 解析器一直支持，
 * 但前端 `accept` 写死漏了三种 ⇒ 文件选择器里选不出来。
 */

import { describe, expect, it } from "vitest";
import { DOCUMENT_UPLOAD_ACCEPT, DOCUMENT_UPLOAD_HINT } from "./uploadAccept";

const accepted = DOCUMENT_UPLOAD_ACCEPT.split(",");

describe("DOCUMENT_UPLOAD_ACCEPT", () => {
  it("包含后端解析器支持的**全部**格式", () => {
    // 后端 parse_document 的分支：txt/md/markdown、pdf、docx、pptx、xlsx、xls
    for (const extension of [
      ".pdf",
      ".docx",
      ".pptx",
      ".xlsx",
      ".xls",
      ".txt",
      ".md",
      ".markdown",
    ]) {
      expect(accepted, `后端支持 ${extension}，前端必须能选`).toContain(extension);
    }
  });

  it("不包含后端**显式拒绝**的旧二进制格式", () => {
    // .doc / .ppt 是 OLE2 二进制，python-docx / python-pptx 读不了，
    // 解析器会 422 要求另存为新格式 ⇒ 文件选择器不该放行（选得进、传不上＝坏体验）
    expect(accepted).not.toContain(".doc");
    expect(accepted).not.toContain(".ppt");
  });

  it("不包含后端解析不了的类型（否则必然 422）", () => {
    for (const unsupported of [".csv", ".html", ".htm", ".svg", ".png", ".jpg"]) {
      expect(accepted).not.toContain(unsupported);
    }
  });

  it("人话提示覆盖同样范围（文案与控件不打架）", () => {
    expect(DOCUMENT_UPLOAD_HINT).toContain("Excel");
    expect(DOCUMENT_UPLOAD_HINT).toContain("PPT");
    expect(DOCUMENT_UPLOAD_HINT).toContain("Word");
  });
});
