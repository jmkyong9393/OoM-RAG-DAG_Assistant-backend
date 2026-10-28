package com.oom.assist.common.context;

/**
 * 요청당 1회 결정되는 실행 컨텍스트. 쿠키(사용자) + X-Workspace-Id(워크스페이스) + workspace_members(소속·역할).
 * 모든 Repository 는 이 값으로만 범위를 잡는다. v1.1 JWT 전환 시 TenantContextFilter 한 곳만 바뀐다.
 */
public record TenantContext(long tenantId, long workspaceId, long userId, String userName, String role) {
  public boolean isAdmin() { return "admin".equals(role); }
}
