/**
 * 标准化 antd Table 分页配置 hook（2026-09-19 复盘）。
 *
 * 历史 bug：OntologyPropertyAdminPage 等页用
 * `pagination={{ pageSize: 20, showSizeChanger: true }}` 硬编码，
 * 但 antd 5 不暴露 `pageSizeOptions` 时 showSizeChanger 下拉只能切到默认 pageSize，
 * 用户反馈「分页不可用、只能 2 条每页、修改不了」。实际是 dataSource 命中条数少
 * 时 antd 自动调整 pageSize 到「数据条数」并锁住，但下拉仍然只显示 20 一个选项。
 *
 * 修法：state 化 current + pageSize + 显式 pageSizeOptions + showQuickJumper，
 * 让用户能稳定切 10/20/50/100。showTotal 用 i18n `common.totalItems` 模板。
 *
 * 用法：
 *   const { pagination } = useTablePagination();
 *   <Table pagination={pagination} dataSource={items} columns={cols} />
 */

import { useState } from "react";
import type { TablePaginationConfig } from "antd";
import { useTranslation } from "../i18n";

/** 统一的页大小选项。导出常量以便测试和其他地方复用（如后端 limit 校验）。 */
export const TABLE_PAGE_SIZES: readonly number[] = [10, 20, 50, 100];

export interface TablePaginationState {
  current: number;
  pageSize: number;
}

/**
 * 返回 antd Table `pagination` 字段。
 * - 默认 current=1, pageSize=20
 * - showSizeChanger / showQuickJumper 都开
 * - 用户切换页/页大小后状态保留（弹窗关闭再开仍记得页大小）
 * - showTotal 用 i18n 文案
 */
export function useTablePagination(): {
  pagination: TablePaginationConfig;
  reset: () => void;
  setPage: (page: number) => void;
} {
  const { t } = useTranslation();
  const [state, setState] = useState<TablePaginationState>({
    current: 1,
    pageSize: 20,
  });

  const pagination: TablePaginationConfig = {
    current: state.current,
    pageSize: state.pageSize,
    showSizeChanger: true,
    pageSizeOptions: TABLE_PAGE_SIZES.map(String),
    showQuickJumper: true,
    showTotal: (total: number) => t("common.totalItems", { total }),
    onChange: (current, pageSize) => setState({ current, pageSize }),
  };

  return {
    pagination,
    reset: () => setState({ current: 1, pageSize: 20 }),
    // 切换过滤条件时回到第 1 页，但保留用户选的 pageSize。
    // 单独 reset() 会把 pageSize 也回到 20，影响用户已经调好的页大小。
    setPage: (page: number) => setState((s) => ({ ...s, current: page })),
  };
}