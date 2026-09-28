export interface AuthLoginRequest {
  username: string;
  password: string;
}

export interface UserSummaryRead {
  id: number;
  username: string;
  displayName: string;
  email: string | null;
  roles: string[];
  organizations: string[];
}

export interface AuthLoginResponse {
  accessToken: string;
  tokenType: "Bearer";
  expiresIn: number;
  mustChangePassword: boolean;
  user: UserSummaryRead;
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
  requireSpecial: boolean;
  minSpecialCount: number | null;
}
