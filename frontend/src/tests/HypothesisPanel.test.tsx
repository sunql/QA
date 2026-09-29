import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import HypothesisPanel from "../components/chat/HypothesisPanel";
import type { HypothesisView } from "../types/chat";
import { HYPOTHESIS_MAX_COUNT } from "../api/chat";

function makeHypothesis(overrides: Partial<HypothesisView> = {}): HypothesisView {
  return {
    id: Math.floor(Math.random() * 100000),
    statement: "收货量下降可能与供应商交付延期有关",
    driver: "QTY",
    verificationSql: "SELECT NAME, SUM(QTY) FROM PRECEIPT GROUP BY NAME",
    turnQuestion: "本月收货数量为什么下降",
    createdTime: "2026-09-29T10:00:00",
    ...overrides,
  };
}

function renderPanel(hypotheses: HypothesisView[], onVerify = vi.fn()) {
  return render(<HypothesisPanel hypotheses={hypotheses} onVerify={onVerify} />);
}

describe("HypothesisPanel（v3.1 B6 可能原因区块）", () => {
  it("渲染假设文本 + driver 徽标 + 验证按钮", () => {
    renderPanel([makeHypothesis({ statement: "假设一", driver: "QTY" })]);
    expect(screen.getByText("假设一")).toBeTruthy();
    expect(screen.getByText("QTY")).toBeTruthy();
    // antd 按钮会在两字文案间插 U+0020（「验 证」），用 role + 宽松正则匹配
    expect(screen.getByRole("button", { name: /验\s*证$/ })).toBeTruthy();
  });

  it("无 driver 时不渲染徽标", () => {
    renderPanel([makeHypothesis({ driver: null })]);
    expect(screen.queryByText("QTY")).toBeNull();
  });

  it("验证按钮把 verificationSql 交给 onVerify（复用发送链路，不直接执行）", () => {
    const onVerify = vi.fn();
    renderPanel(
      [makeHypothesis({ verificationSql: "SELECT 1 FROM DUAL" })],
      onVerify
    );
    fireEvent.click(screen.getByRole("button", { name: /验\s*证$/ }));
    expect(onVerify).toHaveBeenCalledWith("SELECT 1 FROM DUAL");
  });

  it("展示上限裁剪到 3 条（与后端 HYPOTHESIS_MAX_COUNT 对齐）", () => {
    const many = Array.from({ length: 6 }, (_, i) =>
      makeHypothesis({ id: i + 1, statement: `假设${i + 1}` })
    );
    renderPanel(many);
    expect(screen.getAllByTestId("hypothesis-item").length).toBe(HYPOTHESIS_MAX_COUNT);
  });

  it("包含可折叠的验证 SQL 代码块", () => {
    renderPanel([makeHypothesis()]);
    expect(screen.getByText(/查看验证 SQL/)).toBeTruthy();
    // 折叠态下 SQL 原文不直接可见
    expect(screen.queryByText(/SUM\(QTY\)/)).toBeNull();
    fireEvent.click(screen.getByText(/查看验证 SQL/));
    expect(screen.getByText(/SUM\(QTY\)/)).toBeTruthy();
  });

  it("空列表渲染 null（无假设整块隐藏）", () => {
    const { container } = renderPanel([]);
    expect(container.querySelector('[data-testid="hypothesis-panel"]')).toBeNull();
  });
});
