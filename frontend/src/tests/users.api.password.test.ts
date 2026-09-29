import { describe, it, expect, vi } from "vitest";
import type { Mock } from "vitest";
import { adminResetPassword } from "../api/users";
import { httpClient } from "../api/client";

vi.mock("../api/client", () => ({
  httpClient: { put: vi.fn() },
}));

describe("adminResetPassword", () => {
  it("PUT /users/{id}/password with newPassword + forceChangeOnNextLogin", async () => {
    (httpClient.put as Mock).mockResolvedValue({ status: 204 });
    await adminResetPassword(42, { newPassword: "NewPass456", forceChangeOnNextLogin: true });
    expect(httpClient.put).toHaveBeenCalledWith(
      "/users/42/password",
      { newPassword: "NewPass456", forceChangeOnNextLogin: true },
    );
  });
});