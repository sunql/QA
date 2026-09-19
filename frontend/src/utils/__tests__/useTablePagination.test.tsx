/**
 * useTablePagination hook 单测（2026-09-19 复盘）。
 *
 * 历史 bug：硬编码 `pagination={{ pageSize: 20, showSizeChanger: true }}`
 * 缺乏 pageSizeOptions 时 antd 5 下拉只显示当前 pageSize，用户反馈
 * 「分页不可用、只能 2 条每页、修改不了」。本测试锁定：
 * 1. 默认 pageSize=20、pageSizeOptions=[10,20,50,100]、showSizeChanger/showQuickJumper 都开
 * 2. onChange 触发后 state 同步 current + pageSize，弹窗关闭再开不丢状态
 * 3. reset() 回退到 current=1 + pageSize=20
 */

import { describe, it, expect, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useTablePagination, TABLE_PAGE_SIZES } from "../useTablePagination";

describe("useTablePagination", () => {
  it("默认 pageSize=20 + options=[10,20,50,100] + showSizeChanger/QuickJumper", () => {
    const { result } = renderHook(() => useTablePagination());
    const p = result.current.pagination;

    expect(p.pageSize).toBe(20);
    expect(p.pageSizeOptions).toEqual(["10", "20", "50", "100"]);
    expect(p.showSizeChanger).toBe(true);
    expect(p.showQuickJumper).toBe(true);
    expect(typeof p.showTotal).toBe("function");
    expect(p.current).toBe(1);
  });

  it("onChange 切到 page=3 pageSize=50 后 state 同步", () => {
    const { result } = renderHook(() => useTablePagination());

    act(() => {
      result.current.pagination.onChange!(3, 50);
    });

    expect(result.current.pagination.current).toBe(3);
    expect(result.current.pagination.pageSize).toBe(50);
  });

  it("切页大小后弹窗关闭再开仍保留（state 持久）", () => {
    const { result, rerender } = renderHook(() => useTablePagination());

    act(() => {
      result.current.pagination.onChange!(2, 100);
    });
    // 模拟弹窗打开/关闭导致的父组件 rerender（hook 自身不被卸载）
    rerender();
    expect(result.current.pagination.pageSize).toBe(100);
    expect(result.current.pagination.current).toBe(2);
  });

  it("reset() 回退到 current=1 + pageSize=20", () => {
    const { result } = renderHook(() => useTablePagination());

    act(() => {
      result.current.pagination.onChange!(5, 100);
    });
    expect(result.current.pagination.current).toBe(5);

    act(() => {
      result.current.reset();
    });
    expect(result.current.pagination.current).toBe(1);
    expect(result.current.pagination.pageSize).toBe(20);
  });

  it("showTotal 用 i18n common.totalItems 模板渲染中文「共 N 条」", () => {
    const { result } = renderHook(() => useTablePagination());
    // react-i18next init 已挂载（i18n.test.ts 验证），i18next instance 用 zh-CN
    const text = result.current.pagination.showTotal!(1571, [1, 20]);
    expect(text).toMatch(/1571/);
  });

  it("TABLE_PAGE_SIZES 导出常量与 pageSizeOptions 字符串列表对齐", () => {
    expect(TABLE_PAGE_SIZES).toEqual([10, 20, 50, 100]);
    // 防回归：改了常量忘了改 hook 默认值
    const { result } = renderHook(() => useTablePagination());
    expect(result.current.pagination.pageSizeOptions).toEqual(
      TABLE_PAGE_SIZES.map(String)
    );
  });

  it("重复 onChange（模拟 antd 内部触发）不抛错且状态稳定", () => {
    const { result } = renderHook(() => useTablePagination());

    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      act(() => {
        result.current.pagination.onChange!(1, 20);
        result.current.pagination.onChange!(2, 20);
        result.current.pagination.onChange!(3, 20);
      });
      expect(result.current.pagination.current).toBe(3);
    } finally {
      spy.mockRestore();
    }
  });

  it("setPage(1) 回到第 1 页但保留 pageSize（过滤条件变化时用）", () => {
    const { result } = renderHook(() => useTablePagination());

    // 用户切到 page=4 pageSize=50
    act(() => {
      result.current.pagination.onChange!(4, 50);
    });
    expect(result.current.pagination.current).toBe(4);
    expect(result.current.pagination.pageSize).toBe(50);

    // 切换过滤条件后只回到第 1 页，pageSize 保留
    act(() => {
      result.current.setPage(1);
    });
    expect(result.current.pagination.current).toBe(1);
    expect(result.current.pagination.pageSize).toBe(50);
  });

  it("setPage(5) 跳到第 5 页（任意页码），pageSize 不变", () => {
    const { result } = renderHook(() => useTablePagination());

    act(() => {
      result.current.pagination.onChange!(2, 100);
      result.current.setPage(5);
    });
    expect(result.current.pagination.current).toBe(5);
    expect(result.current.pagination.pageSize).toBe(100);
  });
});