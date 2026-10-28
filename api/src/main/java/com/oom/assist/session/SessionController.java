package com.oom.assist.session;

import com.oom.assist.common.context.TenantContextFilter;
import com.oom.assist.common.error.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.constraints.NotNull;
import java.util.List;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 데모 로그인. POST /session {user_id} → 서명 쿠키. GET /me 는 쿠키만 있으면 되고 워크스페이스 헤더가 없어도 된다
 * (로그인 직후 워크스페이스를 고르기 전에 호출되므로).
 */
@RestController
public class SessionController {

  private final JdbcClient jdbc;
  private final SessionCookie cookie;
  private final TenantContextFilter filter;

  public SessionController(JdbcClient jdbc, SessionCookie cookie, TenantContextFilter filter) {
    this.jdbc = jdbc;
    this.cookie = cookie;
    this.filter = filter;
  }

  public record LoginRequest(@NotNull Long userId) {}
  public record WorkspaceMembership(long id, String name, boolean policyEnabled, String role) {}
  public record Me(long id, String name, String title, Long workspaceId, List<WorkspaceMembership> workspaces) {}

  @PostMapping("/session")
  public Me login(@RequestBody LoginRequest body, HttpServletResponse res) {
    Me me = loadMe(body.userId()).orElseThrow(() -> ApiException.notFound("user"));
    res.addCookie(cookie.toCookie(cookie.issue(me.id()), false));
    return me;
  }

  @DeleteMapping("/session")
  public ResponseEntity<Void> logout(HttpServletResponse res) {
    res.addCookie(cookie.toCookie("", true));
    return ResponseEntity.noContent().build();
  }

  @GetMapping("/me")
  public Me me(HttpServletRequest req) {
    long userId = filter.readUserId(req).orElseThrow(ApiException::unauthorized);
    return loadMe(userId).orElseThrow(ApiException::unauthorized);
  }

  private java.util.Optional<Me> loadMe(long userId) {
    return jdbc.sql("SELECT id, name, title FROM users WHERE id = ? AND status = 'active'").param(userId).query().listOfRows().stream().findFirst()
        .map(u -> {
          List<WorkspaceMembership> ws = jdbc.sql("""
                SELECT w.id, w.name, w.policy_enabled, m.role
                FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                WHERE m.user_id = ? ORDER BY (m.role = 'admin') DESC, w.id
                """).param(userId).query((rs, i) -> new WorkspaceMembership(rs.getLong("id"), rs.getString("name"),
                  rs.getBoolean("policy_enabled"), rs.getString("role"))).list();
          Long home = ws.isEmpty() ? null : ws.get(0).id(); // admin 멤버십이 있으면 그것이 홈
          return new Me(((Number) u.get("id")).longValue(), (String) u.get("name"), (String) u.get("title"), home, ws);
        });
  }

  /** 로그인 화면용 사용자 목록 (데모 전용. 비밀번호가 없으므로 공개). */
  @GetMapping("/session/users")
  public List<Map<String, Object>> users() {
    return jdbc.sql("SELECT id, name, title FROM users WHERE status = 'active' ORDER BY id").query().listOfRows();
  }
}
