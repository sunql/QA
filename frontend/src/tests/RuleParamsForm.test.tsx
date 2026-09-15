import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { i18n } from "../i18n";
import { RuleParamsForm } from "../components/dq/RuleParamsForm";

const wrap = (ui: React.ReactNode) => (
  <I18nextProvider i18n={i18n}>{ui}</I18nextProvider>
);

describe("RuleParamsForm - VALIDITY range", () => {
  it("renders min/max and updates value", () => {
    const onChange = vi.fn();
    render(wrap(
      <RuleParamsForm
        ruleType="VALIDITY"
        columns={[{ name: "ORDER_QTY" }]}
        value={null}
        onChange={onChange}
      />,
    ));
    // antd InputNumber: native hidden input has role="spinbutton"
    const spinbuttons = document.querySelectorAll<HTMLInputElement>('input[role="spinbutton"]');
    expect(spinbuttons.length).toBeGreaterThanOrEqual(1);
    fireEvent.change(spinbuttons[0], { target: { value: "0" } });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "range" }),
    );
    // onChange called with an object that has a min property set
    const called = onChange.mock.calls[0][0] as Record<string, unknown>;
    expect(called).toHaveProperty("min");
  });
});

describe("RuleParamsForm - CONSISTENCY", () => {
  it("renders left/right select and factor", () => {
    render(wrap(
      <RuleParamsForm
        ruleType="CONSISTENCY"
        columns={[{ name: "RECEIVED_QTY" }, { name: "ORDER_QTY" }]}
        value={null}
        onChange={() => {}}
      />,
    ));
    // Check that left/right column labels are rendered (i18n: "左列" / "右列")
    expect(screen.getByTitle("左列")).toBeInTheDocument();
    expect(screen.getByTitle("右列")).toBeInTheDocument();
    expect(screen.getByTitle("系数")).toBeInTheDocument();
  });
});
