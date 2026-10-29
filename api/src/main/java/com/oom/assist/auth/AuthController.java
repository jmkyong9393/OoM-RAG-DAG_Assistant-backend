package com.oom.assist.auth;

import com.oom.assist.common.error.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.authentication.LockedException;
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
 * 사번+비밀번호 로그인. 성공하면 SecurityContext 를 세션에 저장하고(Spring Session → Postgres) 세션 쿠키 OOMSESSION 이 내려간다.
 * 접근통제: 5회 실패 잠금(LoginAttempts), 비밀번호 작성규칙·유효기간 180일(PasswordPolicy), 첫 로그인 개인정보 동의(consented_at).
 * GET /me 는 세션만 있으면 되고 워크스페이스 헤더가 없어도 된다.
 */
@RestController
public class AuthController {

  private final AuthenticationManager authManager;
  private final SecurityContextRepository contextRepo;
  private final PasswordEncoder encoder;
  private final JdbcClient jdbc;
  private final LoginAttempts attempts;

  public AuthController(AuthenticationManager authManager, SecurityContextRepository contextRepo, PasswordEncoder encoder, JdbcClient jdbc,
      LoginAttempts attempts) {
    this.authManager = authManager;
    this.contextRepo = contextRepo;
    this.encoder = encoder;
    this.jdbc = jdbc;
    this.attempts = attempts;
  }

  public record LoginRequest(@NotBlank String employeeNo, @NotBlank String password) {}
  public record PasswordChangeRequest(@NotBlank String currentPassword, @NotBlank String newPassword) {}
  public record WorkspaceMembership(long id, String name, boolean policyEnabled, String role) {}
  public record Me(long id, String employeeNo, String name, String title, String email, Long workspaceId, List<WorkspaceMembership> workspaces,
                   boolean mustChangePassword, boolean consentRequired, Instant passwordExpiresAt) {}

  @PostMapping("/auth/login")
  public Me login(@Valid @RequestBody LoginRequest body, HttpServletRequest req, HttpServletResponse res) {
    String employeeNo = body.employeeNo().trim();
    String ip = LoginAttempts.clientIp(req);
    Authentication auth;
    try {
      auth = authManager.authenticate(UsernamePasswordAuthenticationToken.unauthenticated(employeeNo, body.password()));
    } catch (LockedException e) {
      attempts.audit(employeeNo, "login_rejected_locked", ip);
      throw new ApiException(HttpStatus.LOCKED, "ACCOUNT_LOCKED",
          "로그인 " + LoginAttempts.MAX_FAILURES + "회 실패로 " + LoginAttempts.LOCK_MINUTES + "분간 잠겼습니다. 잠시 후 다시 시도하거나 관리자에게 해제를 요청하세요.");
    } catch (AuthenticationException e) {
      if (attempts.recordFailure(employeeNo, ip)) {
        throw new ApiException(HttpStatus.LOCKED, "ACCOUNT_LOCKED",
            "로그인 " + LoginAttempts.MAX_FAILURES + "회 실패로 " + LoginAttempts.LOCK_MINUTES + "분간 잠겼습니다.");
      }
      throw ApiException.unauthorized();                          // 계정 존재 여부를 드러내지 않는다
    }
    SecurityContext ctx = SecurityContextHolder.createEmptyContext();
    ctx.setAuthentication(auth);
    SecurityContextHolder.setContext(ctx);
    contextRepo.saveContext(ctx, req, res);                      // 세션 생성 → 쿠키
    AppPrincipal p = (AppPrincipal) auth.getPrincipal();
    attempts.recordSuccess(p.id(), p.employeeNo(), ip);
    return loadMe(p.id()).orElseThrow(ApiException::unauthorized);
  }

  @PostMapping("/auth/logout")
  public ResponseEntity<Void> logout(HttpServletRequest req, HttpServletResponse res) {
    CurrentUser.get().ifPresent(p -> attempts.audit(p.employeeNo(), "logout", LoginAttempts.clientIp(req)));
    new SecurityContextLogoutHandler().logout(req, res, SecurityContextHolder.getContext().getAuthentication()); // 세션 무효화 → Spring Session 이 쿠키 만료
    return ResponseEntity.noContent().build();
  }

  @GetMapping("/me")
  public Me me() {
    long userId = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    return loadMe(userId).orElseThrow(ApiException::unauthorized);
  }

  /** 비밀번호 작성규칙 (로그인 화면·변경 화면 안내문). 공개. */
  @GetMapping("/auth/password-policy")
  public Map<String, Object> passwordPolicy() {
    return Map.of("rule", PasswordPolicy.RULE_TEXT, "max_age_days", PasswordPolicy.MAX_AGE_DAYS,
        "lock_after_failures", LoginAttempts.MAX_FAILURES, "lock_minutes", LoginAttempts.LOCK_MINUTES);
  }

  @PatchMapping("/auth/password")
  public ResponseEntity<Void> changePassword(@Valid @RequestBody PasswordChangeRequest body, HttpServletRequest req) {
    AppPrincipal p = CurrentUser.get().orElseThrow(ApiException::unauthorized);
    String hash = jdbc.sql("SELECT password_hash FROM users WHERE id = ?").param(p.id()).query(String.class).optional().orElse(null);
    if (hash == null || !encoder.matches(body.currentPassword(), hash)) throw ApiException.forbidden("current password mismatch");
    List<String> bad = PasswordPolicy.violations(body.newPassword(), p.employeeNo());
    if (body.newPassword().equals(body.currentPassword())) bad.add("이전 비밀번호와 달라야 합니다");
    if (!bad.isEmpty()) throw ApiException.badRequest("PASSWORD_POLICY", String.join(" / ", bad));
    jdbc.sql("UPDATE users SET password_hash = ?, must_change_password = false, password_changed_at = now() WHERE id = ?")
        .params(encoder.encode(body.newPassword()), p.id()).update();
    attempts.audit(p.employeeNo(), "password_changed", LoginAttempts.clientIp(req));
    return ResponseEntity.noContent().build();
  }

  /** 개인정보 수집·이용 동의(첫 로그인). 동의 시각만 기록한다 — 문안은 프론트 처리방침 페이지. */
  @PostMapping("/auth/consent")
  public ResponseEntity<Void> consent(HttpServletRequest req) {
    AppPrincipal p = CurrentUser.get().orElseThrow(ApiException::unauthorized);
    jdbc.sql("UPDATE users SET consented_at = coalesce(consented_at, now()) WHERE id = ?").param(p.id()).update();
    attempts.audit(p.employeeNo(), "consent", LoginAttempts.clientIp(req));
    return ResponseEntity.noContent().build();
  }

  java.util.Optional<Me> loadMe(long userId) {
    return jdbc.sql("""
          SELECT id, employee_no, email, name, title, must_change_password, consented_at, password_changed_at
          FROM users WHERE id = ? AND status = 'active'""").param(userId)
        .query().listOfRows().stream().findFirst()
        .map(u -> {
          List<WorkspaceMembership> ws = jdbc.sql("""
                SELECT w.id, w.name, w.policy_enabled, m.role
                FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                WHERE m.user_id = ? ORDER BY (m.role = 'admin') DESC, w.id
                """).param(userId).query((rs, i) -> new WorkspaceMembership(rs.getLong("id"), rs.getString("name"),
                  rs.getBoolean("policy_enabled"), rs.getString("role"))).list();
          Long home = ws.isEmpty() ? null : ws.get(0).id(); // admin 멤버십이 있으면 그것이 홈
          Timestamp changed = (Timestamp) u.get("password_changed_at");
          Instant expiresAt = changed == null ? null : changed.toInstant().plus(Duration.ofDays(PasswordPolicy.MAX_AGE_DAYS));
          boolean expired = expiresAt == null || expiresAt.isBefore(Instant.now());   // 기준 시각이 없으면(구 계정) 만료로 본다
          return new Me(((Number) u.get("id")).longValue(), (String) u.get("employee_no"), (String) u.get("name"), (String) u.get("title"),
              (String) u.get("email"), home, ws, Boolean.TRUE.equals(u.get("must_change_password")) || expired,
              u.get("consented_at") == null, expiresAt);
        });
  }
}
