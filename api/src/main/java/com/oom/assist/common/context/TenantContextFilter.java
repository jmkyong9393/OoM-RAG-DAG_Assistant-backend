package com.oom.assist.common.context;

import com.oom.assist.auth.CurrentUser;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.util.Map;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 세션에서 복원된 로그인 사용자 + 헤더 X-Workspace-Id → workspace_members 조회 → TenantContext 를 요청 속성에 둔다.
 * Spring Security 필터 체인 안(익명 처리 뒤)에 끼워 넣는다(SecurityConfig). 헤더가 없거나 소속이 아니면 속성을 비워 두고,
 * 컨트롤러가 TenantContext 를 요구할 때 401 이 난다. 규칙 R1: 소속이 아닌 워크스페이스는 존재 자체를 숨긴다.
 */
public class TenantContextFilter extends OncePerRequestFilter {
  public static final String WORKSPACE_HEADER = "X-Workspace-Id";

  private final JdbcClient jdbc;

  public TenantContextFilter(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  @Override
  protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
    Optional<Long> userId = CurrentUser.id();
    String wsHeader = req.getHeader(WORKSPACE_HEADER);
    if (userId.isPresent() && wsHeader != null && !wsHeader.isBlank()) {
      try {
        long wsId = Long.parseLong(wsHeader.trim());
        jdbc.sql("""
              SELECT w.tenant_id, w.id AS workspace_id, u.id AS user_id, u.name AS user_name, m.role
              FROM workspace_members m
              JOIN workspaces w ON w.id = m.workspace_id
              JOIN users u ON u.id = m.user_id AND u.status = 'active'
              WHERE m.workspace_id = ? AND m.user_id = ?
              """).params(wsId, userId.get()).query().listOfRows().stream().findFirst()
            .ifPresent(r -> req.setAttribute(TenantContextArgumentResolver.ATTR, toContext(r)));
      } catch (NumberFormatException ignored) {
        // 잘못된 헤더는 컨텍스트 없음. 보호된 엔드포인트에서 401.
      }
    }
    chain.doFilter(req, res);
  }

  private static TenantContext toContext(Map<String, Object> r) {
    return new TenantContext(((Number) r.get("tenant_id")).longValue(), ((Number) r.get("workspace_id")).longValue(),
        ((Number) r.get("user_id")).longValue(), (String) r.get("user_name"), (String) r.get("role"));
  }
}
