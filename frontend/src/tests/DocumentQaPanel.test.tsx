import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DocumentQaPanel } from "../components/documents/DocumentQaPanel";

describe("DocumentQaPanel integration", () => {
  it("renders message list, input, and history panel", () => {
    render(<DocumentQaPanel />);
    expect(screen.getByPlaceholderText(/输入问题/)).toBeTruthy();
    expect(screen.getByText(/新对话/)).toBeTruthy();
  });
});
