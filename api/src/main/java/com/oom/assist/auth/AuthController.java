package com.oom.assist.auth;

import com.oom.assist.common.error.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import java.util.List;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.core.context.SecurityContext;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.authentication.logout.SecurityContextLogoutHandler;
import org.springframework.security.web.context.SecurityContextRepository;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 이메일+비밀번호 로그인. 성공하면 SecurityContext 를 세션에 저장하고(Spring Session → Postgres) 세션 쿠키 OOMSESSION 이 내려간다.
 * GET /me 는 세션만 있으면 되고 워크스페이스 헤더가 없어도 된다(로그인 직후 워크스페이스를 고르기 전에 호출).
 */
@RestController
public class AuthController {

  private final AuthenticationManager authManager;
  private final SecurityContextRepository contextRepo;
  private final PasswordEncoder encoder;
  private final JdbcClient jdbc;

  public AuthController(AuthenticationManager authManager, SecurityContextRepository contextRepo, PasswordEncoder encoder, JdbcClient jdbc) {
    this.authManager = authManager;
    this.contextRepo = contextRepo;
    this.encoder = encoder;
    this.jdbc = jdbc;
  }

  public record LoginRequest(@NotBlank String email, @NotBlank String password) {}
  public record PasswordChangeRequest(@NotBlank String currentPassword, @NotBlank @Size(min = 8, max = 128) String newPassword) {}
  public record WorkspaceMembership(long id, String name, boolean policyEnabled, String role) {}
  public record Me(long id, String email, String name, String title, Long workspaceId, List<WorkspaceMembership> workspaces, boolean mustChangePassword) {}

  @PostMapping("/auth/login")
  public Me login(@Valid @RequestBody LoginRequest body, HttpServletRequest req, HttpServletResponse res) {
    Authentication auth;
    try {
      auth = authManager.authenticate(UsernamePasswordAuthenticationToken.unauthenticated(body.email().trim(), body.password()));
    } catch (AuthenticationException e) {
      throw ApiException.unauthorized();                          // 계정 존재 여부를 드러내지 않는다
    }
    SecurityContext ctx = SecurityContextHolder.createEmptyContext();
    ctx.setAuthentication(auth);
    SecurityContextHolder.setContext(ctx);
    contextRepo.saveContext(ctx, req, res);                      // 세션 생성 → 쿠키
    AppPrincipal p = (AppPrincipal) auth.getPrincipal();
    jdbc.sql("UPDATE users SET last_login_at = now() WHERE id = ?").param(p.id()).update();
    return loadMe(p.id()).orElseThrow(ApiException::unauthorized);
  }

  @PostMapping("/auth/logout")
  public ResponseEntity<Void> logout(HttpServletRequest req, HttpServletResponse res) {
    new SecurityContextLogoutHandler().logout(req, res, SecurityContextHolder.getContext().getAuthentication()); // 세션 무효화 → Spring Session 이 쿠키 만료
    return ResponseEntity.noContent().build();
  }

  @GetMapping("/me")
  public Me me() {
    long userId = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    return loadMe(userId).orElseThrow(ApiException::unauthorized);
  }

  @PatchMapping("/auth/password")
  public ResponseEntity<Void> changePassword(@Valid @RequestBody PasswordChangeRequest body) {
    long userId = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    String hash = jdbc.sql("SELECT password_hash FROM users WHERE id = ?").param(userId).query(String.class).optional().orElse(null);
    if (hash == null || !encoder.matches(body.currentPassword(), hash)) throw ApiException.forbidden("current password mismatch");
    jdbc.sql("UPDATE users SET password_hash = ?, must_change_password = false WHERE id = ?")
        .params(encoder.encode(body.newPassword()), userId).update();
    return ResponseEntity.noContent().build();
  }

  java.util.Optional<Me> loadMe(long userId) {
    return jdbc.sql("SELECT id, email, name, title, must_change_password FROM users WHERE id = ? AND status = 'active'").param(userId)
        .query().listOfRows().stream().findFirst()
        .map(u -> {
          List<WorkspaceMembership> ws = jdbc.sql("""
                SELECT w.id, w.name, w.policy_enabled, m.role
                FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                WHERE m.user_id = ? ORDER BY (m.role = 'admin') DESC, w.id
                """).param(userId).query((rs, i) -> new WorkspaceMembership(rs.getLong("id"), rs.getString("name"),
                  rs.getBoolean("policy_enabled"), rs.getString("role"))).list();
          Long home = ws.isEmpty() ? null : ws.get(0).id(); // admin 멤버십이 있으면 그것이 홈
          return new Me(((Number) u.get("id")).longValue(), (String) u.get("email"), (String) u.get("name"), (String) u.get("title"), home, ws,
              Boolean.TRUE.equals(u.get("must_change_password")));
        });
  }
}
