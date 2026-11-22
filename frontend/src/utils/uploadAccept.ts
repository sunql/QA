/**
 * 文档上传的 `accept` 白名单（**与后端解析器同源**）。
 *
 * 为什么要有这个常量：2026-11-22 用户实测「文档中心不能上传 xlsx」——
 * 后端起解器**一直支持** xlsx/xls/pptx，但文档中心的上传控件把
 * `accept` 写死成 `.pdf,.docx,.doc,.txt,.md`（漏了三种、还多了不支持的
 * `.doc`）⇒ 文件选择器里这些格式根本选不出来。wiki 导入页的列表是对的，
 * 两份各写一份 ⇒ 必然漂移。
 *
 * ⚠️ 与 `backend/app/services/document_parser.py::parse_document` 的
 * 分支**一一对应**（改这里就必须改那边，反之亦然）：
 *
 * | 扩展名 | 后端解析 |
 * |---|---|
 * | txt / md / markdown | 纯文本 |
 * | pdf | pypdf |
 * | docx | python-docx |
 * | pptx | python-pptx |
 * | xlsx | openpyxl |
 * | xls | xlrd |
 *
 * **不包含**：`doc` / `ppt`（旧 OLE2 二进制，解析器显式拒绝并要求另存为新格式）、
 * csv / html / 图片（解析器不支持 ⇒ 入库必然 422）。
 */
export const DOCUMENT_UPLOAD_ACCEPT = [
  ".pdf",
  ".docx",
  ".pptx",
  ".xlsx",
  ".xls",
  ".txt",
  ".md",
  ".markdown",
].join(",");

/** 给用户看的人话提示（与上面的白名单同源，避免文案与控件不一致）。 */
export const DOCUMENT_UPLOAD_HINT = "PDF / Word(docx) / PPT(pptx) / Excel(xlsx·xls) / TXT / Markdown";
