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
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.MediaType;
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
  private final Path privacyPolicyPath;

  public AuthController(AuthenticationManager authManager, SecurityContextRepository contextRepo, PasswordEncoder encoder, JdbcClient jdbc,
      LoginAttempts attempts, @Value("${oom.legal.privacy-policy-path}") String privacyPolicyPath) {
    this.authManager = authManager;
    this.contextRepo = contextRepo;
    this.encoder = encoder;
    this.jdbc = jdbc;
    this.attempts = attempts;
    this.privacyPolicyPath = Path.of(privacyPolicyPath);
  }

  public record LoginRequest(@NotBlank String employeeNo, @NotBlank String password) {}
  public record PasswordChangeRequest(@NotBlank String currentPassword, @NotBlank String newPassword) {}
  /** canConfigure: 이 부서의 설정(규정 사용·사례 사용)을 바꿀 수 있는가 — 부서 admin 이거나 상위(전사 공통)의 admin. */
  /** permissions: 이 부서에서 할 수 있는 관리 동작(admin.members · admin.settings · docs.manage). 화면 노출용이고 서버는 요청마다 다시 검사한다. */
  public record WorkspaceMembership(long id, String name, boolean policyEnabled, boolean caseEnabled, String role, boolean canConfigure, List<String> permissions) {}
  /** permissions: 부서와 무관한 동작(audit.read = 회사 관리자). title = 표시용 "직급 · 직책"(예전 화면 호환).
   *  consentVersion = 지금 처리방침 판, consentedVersion = 이 사용자가 마지막으로 동의한 판. 둘이 다르면 consentRequired. */
  public record Me(long id, String employeeNo, String name, String title, String grade, String position, String email, Long workspaceId,
                   List<WorkspaceMembership> workspaces, boolean mustChangePassword, boolean consentRequired, Instant passwordExpiresAt,
                   boolean companyAdmin, List<String> permissions, String consentVersion, String consentedVersion) {}
  /** null 은 그대로, 빈 문자열은 지움(직급 · 직책 · 이메일). 이름은 지울 수 없다. title 은 예전 화면용 — grade · position 이 없을 때만 직급/직책으로 나눠 넣는다. */
  public record ProfileRequest(String name, String grade, String position, String title, String email) {}
  /** version = 화면이 보여 준 처리방침 판. 다르면 그사이 문안이 바뀐 것이다. */
  public record ConsentRequest(String version) {}

  private static final java.util.regex.Pattern POLICY_VERSION = java.util.regex.Pattern.compile("판:\\s*([A-Za-z0-9._-]+)");

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

  /**
   * 개인정보 처리방침 문안 (동의 화면·푸터). 공개. 원문 data/legal/privacy_policy.md 를 요청 때 읽는다(수정이 재기동 없이 반영) — 동의 화면이 문안을 하드코딩하지 않게 (F-3).
   * 경로는 oom.legal.privacy-policy-path (기본 ../data/legal — make api·테스트의 작업 폴더가 api/). 파일이 없으면 404 (운영 이미지엔 data/ 가 없으므로 마운트 또는 PRIVACY_POLICY_PATH 필요).
   * produces 는 쓰지 않는다 — WebConfig 가 Accept 를 무시하고 JSON 을 기본으로 두어 406 이 난다. 응답 Content-Type 만 지정.
   */
  @GetMapping("/auth/privacy-policy")
  public ResponseEntity<String> privacyPolicy() {
    try {
      String text = Files.readString(privacyPolicyPath, StandardCharsets.UTF_8);
      ResponseEntity.BodyBuilder ok = ResponseEntity.ok().contentType(new MediaType("text", "markdown", StandardCharsets.UTF_8));
      String version = versionOf(text);
      if (version != null) ok.header("X-Policy-Version", version);
      return ok.body(text);
    } catch (IOException e) {
      throw ApiException.notFound("privacy policy (" + privacyPolicyPath + ")");
    }
  }

  /** 지금 처리방침 판(문안 머리의 "판: v2"). 파일이 없거나 판 표시가 없으면 null — 그때는 판 없이 "동의한 적 있는가"만 본다. */
  String policyVersion() {
    try {
      return versionOf(Files.readString(privacyPolicyPath, StandardCharsets.UTF_8));
    } catch (IOException e) {
      return null;
    }
  }

  private static String versionOf(String text) {
    java.util.regex.Matcher m = POLICY_VERSION.matcher(text);
    return m.find() ? m.group(1) : null;
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

  /** 개인정보 수집·이용 동의(첫 로그인 · 처리방침 판이 바뀐 뒤). 지금 판에 대한 동의를 판 단위로 남긴다. 화면이 보여 준 판과 지금 판이 다르면 409. */
  @PostMapping("/auth/consent")
  public ResponseEntity<Void> consent(@RequestBody(required = false) ConsentRequest body, HttpServletRequest req) {
    AppPrincipal p = CurrentUser.get().orElseThrow(ApiException::unauthorized);
    String current = policyVersion();
    if (body != null && body.version() != null && current != null && !current.equals(body.version()))
      throw ApiException.conflict("CONSENT_VERSION_MISMATCH", "privacy policy is now " + current + "; reload and consent again");
    jdbc.sql("UPDATE users SET consented_at = coalesce(consented_at, now()) WHERE id = ?").param(p.id()).update();
    if (current != null)
      jdbc.sql("INSERT INTO user_consents (user_id, policy_version) VALUES (?, ?) ON CONFLICT DO NOTHING").params(p.id(), current).update();
    attempts.audit(p.employeeNo(), "consent", LoginAttempts.clientIp(req), current);
    return ResponseEntity.noContent().build();
  }

  java.util.Optional<Me> loadMe(long userId) {
    return jdbc.sql("""
          SELECT id, employee_no, email, name, grade, duty, coalesce(nullif(concat_ws(' · ', grade, duty), ''), title) AS title,
                 must_change_password, consented_at, password_changed_at,
                 (SELECT c.policy_version FROM user_consents c WHERE c.user_id = users.id ORDER BY c.consented_at DESC LIMIT 1) AS consented_version,
                 EXISTS (SELECT 1 FROM user_consents c WHERE c.user_id = users.id AND c.policy_version = ?::text) AS consented_current
          FROM users WHERE id = ? AND status = 'active'""").params(policyVersion(), userId)
        .query().listOfRows().stream().findFirst()
        .map(u -> {
          List<WorkspaceMembership> ws = jdbc.sql("""
                SELECT w.id, w.name, w.policy_enabled, w.case_enabled, m.role,
                       EXISTS (SELECT 1 FROM workspace_members a WHERE a.user_id = m.user_id AND a.role = 'admin'
                               AND a.workspace_id IN (SELECT workspace_ancestors(w.id))) AS can_configure
                FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                WHERE m.user_id = ? AND w.kind = 'dept' ORDER BY (m.role = 'admin') DESC, w.id
                """).param(userId).query((rs, i) -> new WorkspaceMembership(rs.getLong("id"), rs.getString("name"),
                  rs.getBoolean("policy_enabled"), rs.getBoolean("case_enabled"), rs.getString("role"), rs.getBoolean("can_configure"),
                  permissions("admin".equals(rs.getString("role")), rs.getBoolean("can_configure")))).list();
          // 회사 관리자: 부서가 아닌 상위 워크스페이스(전사 공통)의 admin. 소속 부서 목록에는 넣지 않는다
          boolean companyAdmin = jdbc.sql("""
                SELECT EXISTS (SELECT 1 FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                               WHERE m.user_id = ? AND m.role = 'admin' AND w.kind <> 'dept')""").param(userId).query(Boolean.class).single();
          Long home = ws.isEmpty() ? null : ws.get(0).id(); // admin 멤버십이 있으면 그것이 홈
          Timestamp changed = (Timestamp) u.get("password_changed_at");
          Instant expiresAt = changed == null ? null : changed.toInstant().plus(Duration.ofDays(PasswordPolicy.MAX_AGE_DAYS));
          boolean expired = expiresAt == null || expiresAt.isBefore(Instant.now());   // 기준 시각이 없으면(구 계정) 만료로 본다
          String current = policyVersion();
          boolean consentRequired = current == null ? u.get("consented_at") == null : !Boolean.TRUE.equals(u.get("consented_current"));
          return new Me(((Number) u.get("id")).longValue(), (String) u.get("employee_no"), (String) u.get("name"), (String) u.get("title"),
              (String) u.get("grade"), (String) u.get("duty"), (String) u.get("email"), home, ws,
              Boolean.TRUE.equals(u.get("must_change_password")) || expired, consentRequired, expiresAt, companyAdmin,
              companyAdmin ? List.of("audit.read") : List.of(), current, (String) u.get("consented_version"));
        });
  }

  private static List<String> permissions(boolean wsAdmin, boolean canConfigure) {
    List<String> p = new java.util.ArrayList<>();
    if (wsAdmin) p.add("admin.members");
    if (canConfigure) { p.add("admin.settings"); p.add("docs.manage"); }
    return p;
  }

  /** 내 정보 정정(처리방침 §4: 이름 · 직급 · 직책 · 이메일은 본인이 직접 고친다). 사번 · 소속 · 역할은 바꾸지 않는다. 접속기록에는 바뀐 항목 이름만 남긴다. */
  @PatchMapping("/me")
  public Me updateProfile(@RequestBody ProfileRequest body, HttpServletRequest req) {
    AppPrincipal p = CurrentUser.get().orElseThrow(ApiException::unauthorized);
    if (body == null) throw ApiException.badRequest("VALIDATION", "body is required");
    Map<String, Object> cur = jdbc.sql("SELECT name, grade, duty, email FROM users WHERE id = ?").param(p.id()).query().singleRow();
    String name = body.name() == null ? (String) cur.get("name") : body.name().strip();
    if (name.isEmpty() || name.length() > 50) throw ApiException.badRequest("VALIDATION", "name must be 1~50 characters");
    String[] gp = gradePosition(body.grade(), body.position(), body.title(), (String) cur.get("grade"), (String) cur.get("duty"));
    String grade = gp[0], position = gp[1];
    String email = body.email() == null ? (String) cur.get("email") : blankToNull(body.email());
    if (email != null) {
      email = email.toLowerCase();
      if (email.length() > 254 || !email.matches("[^@\\s]+@[^@\\s]+\\.[^@\\s]+")) throw ApiException.badRequest("VALIDATION", "invalid email");
    }
    List<String> changed = new java.util.ArrayList<>();
    if (!name.equals(cur.get("name"))) changed.add("name");
    if (!java.util.Objects.equals(grade, cur.get("grade"))) changed.add("grade");
    if (!java.util.Objects.equals(position, cur.get("duty"))) changed.add("position");
    if (!java.util.Objects.equals(email, cur.get("email"))) changed.add("email");
    if (changed.contains("email") && email != null && jdbc.sql("""
          SELECT EXISTS (SELECT 1 FROM users o WHERE o.tenant_id = (SELECT tenant_id FROM users WHERE id = ?) AND o.email = ? AND o.id <> ?)""")
        .params(p.id(), email, p.id()).query(Boolean.class).single())
      throw ApiException.conflict("EMAIL_TAKEN", "email already used by another account");
    if (!changed.isEmpty()) {
      try {
        jdbc.sql("UPDATE users SET name = ?, grade = ?, duty = ?, title = ?, email = ? WHERE id = ?")
            .params(name, grade, position, display(grade, position), email, p.id()).update();
      } catch (org.springframework.dao.DuplicateKeyException e) {
        throw ApiException.conflict("EMAIL_TAKEN", "email already used by another account");
      }
      attempts.audit(p.employeeNo(), "profile_changed", LoginAttempts.clientIp(req), String.join(",", changed));
    }
    return loadMe(p.id()).orElseThrow(ApiException::unauthorized);
  }

  /** 요청의 직급 · 직책을 정한다. grade · position 이 오면 그대로(빈 문자열은 지움), 둘 다 없고 예전 title 만 오면 DB 규칙(title_parts)으로 나눈다. */
  String[] gradePosition(String grade, String position, String title, String curGrade, String curPosition) {
    String g, pos;
    if (grade != null || position != null) {
      g = grade == null ? curGrade : blankToNull(grade);
      pos = position == null ? curPosition : blankToNull(position);
    } else if (title != null) {
      Map<String, Object> parts = jdbc.sql("SELECT grade, duty FROM title_parts(?)").param(title).query().singleRow();
      g = (String) parts.get("grade");
      pos = (String) parts.get("duty");
    } else {
      g = curGrade;
      pos = curPosition;
    }
    if ((g != null && g.length() > 30) || (pos != null && pos.length() > 30)) throw ApiException.badRequest("VALIDATION", "grade/position must be at most 30 characters");
    return new String[] {g, pos};
  }

  /** 표시용 직함: "매니저 · 팀장". 둘 다 없으면 null. */
  static String display(String grade, String position) {
    String s = java.util.stream.Stream.of(grade, position).filter(java.util.Objects::nonNull).collect(java.util.stream.Collectors.joining(" · "));
    return s.isEmpty() ? null : s;
  }

  private static String blankToNull(String s) {
    return s == null || s.isBlank() ? null : s.strip();
  }
}
