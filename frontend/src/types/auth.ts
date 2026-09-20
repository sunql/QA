// 认证 DTO（feat-user-auth，2026-09-20）
//
// 与后端 app/schemas/auth.py 对齐 —— 后端 snake_case 经 CamelModel 序列化为 camelCase
// JSON，前端字段全部用 camelCase。命名风格与同目录其它类型一致（chat.ts /
// userProfile.ts 等）。

export interface AuthLoginRequest {
  username: string;
  password: string;
}

export interface AuthLoginResponse {
  accessToken: string;
  expiresIn: number;
  mustChangePassword: boolean;
  user: UserSummary;
}

export interface UserSummary {
  id: number;
  username: string;
  displayName: string;
  email: string | null;
  roles: string[];
  organizations: string[];
}

export interface AuthMeRead {
  id: number;
  username: string;
  displayName: string;
  email: string | null;
  enabled: boolean;
  mustChangePassword: boolean;
  roles: string[];
  organizations: string[];
  tenantId: string;
  lastLoginAt: string | null;
}

export interface AuthChangePasswordRequest {
  oldPassword: string;
  newPassword: string;
}

export interface AdminResetPasswordRequest {
  newPassword: string;
  forceChangeOnNextLogin: boolean;
}

export interface PasswordPolicyRead {
  minLength: number;
  requireLetter: boolean;
  requireDigit: boolean;
  description: string;
}
