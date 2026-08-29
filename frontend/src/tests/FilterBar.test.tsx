import { describe, it, expect, vi } from "vitest";
import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import FilterBar from "../components/ontology/FilterBar";
import type { FilterField } from "../components/ontology/FilterBar";
import type { FilterValues } from "../utils/ontologyFilter";

const fields: FilterField[] = [
  { key: "className", label: "类名", type: "input" },
  {
    key: "classId",
    label: "所属类",
    type: "select",
    options: [{ value: "1", label: "Customer（客户）" }],
  },
];

/** 受控封装：像 OntologyPage 一样把值提升到上层 state，测试真实输入链路。 */
function StatefulBar({ onChange }: { onChange: (key: string, value: string) => void }) {
  const [values, setValues] = useState<FilterValues>({});
  return (
    <FilterBar
      fields={fields}
      values={values}
      onChange={(k, v) => {
        onChange(k, v);
        setValues((prev) => ({ ...prev, [k]: v }));
      }}
      onReset={() => setValues({})}
    />
  );
}

describe("FilterBar", () => {
  it("渲染输入框、下拉与重置按钮", () => {
    render(<FilterBar fields={fields} values={{}} onChange={vi.fn()} onReset={vi.fn()} />);
    expect(screen.getByPlaceholderText("类名")).toBeInTheDocument();
    // Select 不加 aria-label（避免与编辑弹窗同名下拉冲突），占位文案即其可见标签
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(screen.getByText("所属类")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /重置/ })).toBeInTheDocument();
  });

  it("无筛选条件时重置按钮禁用", () => {
    render(<FilterBar fields={fields} values={{}} onChange={vi.fn()} onReset={vi.fn()} />);
    expect(screen.getByRole("button", { name: /重置/ })).toBeDisabled();
  });

  it("输入内容触发 onChange(key, value) 且值累积", async () => {
    const onChange = vi.fn();
    render(<StatefulBar onChange={onChange} />);
    await userEvent.type(screen.getByPlaceholderText("类名"), "Cust");
    expect(onChange).toHaveBeenLastCalledWith("className", "Cust");
  });

  it("选中下拉触发 onChange 且值为字符串", async () => {
    const onChange = vi.fn();
    render(<StatefulBar onChange={onChange} />);
    await userEvent.click(screen.getByRole("combobox"));
    await userEvent.click(screen.getByText("Customer（客户）"));
    expect(onChange).toHaveBeenLastCalledWith("classId", "1");
  });

  it("有筛选条件时重置可用，点击触发 onReset", async () => {
    const onReset = vi.fn();
    render(
      <FilterBar fields={fields} values={{ className: "C" }} onChange={vi.fn()} onReset={onReset} />
    );
    const resetBtn = screen.getByRole("button", { name: /重置/ });
    expect(resetBtn).toBeEnabled();
    await userEvent.click(resetBtn);
    expect(onReset).toHaveBeenCalledTimes(1);
  });
});
