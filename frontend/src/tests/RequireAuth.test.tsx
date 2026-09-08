import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import RequireAuth from "../components/common/RequireAuth";
import { useAuthStore } from "../stores/authStore";

describe("RequireAuth", () => {
  it("无 token 跳 /login", () => {
    useAuthStore.setState({ token: null });
    render(
      <MemoryRouter initialEntries={["/protected"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/protected" element={<div>PROTECTED</div>} />
          </Route>
          <Route path="/login" element={<div>LOGIN</div>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("LOGIN")).toBeTruthy();
    expect(screen.queryByText("PROTECTED")).toBeNull();
  });

  it("有 token 渲染 children", () => {
    useAuthStore.setState({ token: "t" });
    render(
      <MemoryRouter initialEntries={["/protected"]}>
        <Routes>
          <Route element={<RequireAuth />}>
            <Route path="/protected" element={<div>PROTECTED</div>} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("PROTECTED")).toBeTruthy();
  });
});
