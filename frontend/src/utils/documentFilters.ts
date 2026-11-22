/** 文档目录筛选字段（类型/状态下拉必须有 options，否则 antd Select 展开是空列表）。 */
import type { FilterField } from "../components/ontology/FilterBar";
import { DOCUMENT_STATUS_OPTIONS, DOCUMENT_TYPE_OPTIONS } from "../types/document";

export function documentFilterFields(): FilterField[] {
  return [
    { key: "documentId", label: "文档编号" },
    {
      key: "documentType",
      label: "类型",
      type: "select",
      options: DOCUMENT_TYPE_OPTIONS.map((item) => ({
        value: String(item.value),
        label: item.label,
      })),
    },
    {
      key: "status",
      label: "状态",
      type: "select",
      options: [
        ...DOCUMENT_STATUS_OPTIONS.map((item) => ({
          value: String(item.value),
          label: item.label,
        })),
        // 库 CHECK 允许 ARCHIVED，但类型词表没有它 —— 筛选仍应能选中已归档文档
        { value: "ARCHIVED", label: "已归档" },
      ],
    },
  ];
}
