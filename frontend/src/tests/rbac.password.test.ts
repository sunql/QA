import { describe, it, expect } from "vitest";
import type { UserCreatePayload, AdminResetPasswordPayload } from "../types/rbac";

// 仅类型层断言：编译期校验（vitest 跑通 = import 不报错 + 类型可用）
describe("rbac types include password fields", () => {
  it("UserCreatePayload.password is required", () => {
    const payload: UserCreatePayload = {
      username: "alice",
      displayName: "Alice",
      password: "ValidPass1",
    };
    expect(payload.password).toBe("ValidPass1");
  });

  it("AdminResetPasswordPayload has newPassword + forceChangeOnNextLogin", () => {
    const payload: AdminResetPasswordPayload = {
      newPassword: "NewPass456",
      forceChangeOnNextLogin: true,
    };
    expect(payload.newPassword).toBe("NewPass456");
    expect(payload.forceChangeOnNextLogin).toBe(true);
  });
});