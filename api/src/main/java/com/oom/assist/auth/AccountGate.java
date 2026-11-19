package com.oom.assist.auth;

import com.oom.assist.common.error.ApiException;
import jakarta.servlet.DispatcherType;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

/**
 * 로그인한 요청마다 계정 상태를 한 곳에서 확인한다 — 화면의 게이트 둘(처리방침 동의 → 비밀번호 변경)을 서버도 같은 순서로 지킨다.
 * 전에는 화면만 막아서, 임시 비밀번호를 받은 사람이 비밀번호를 바꾸지 않고, 동의하지 않은 사람이 동의 없이 모든 API 를 쓸 수 있었다.
 * 그 상태의 계정은 OPEN 의 API 만 통과하고 나머지는 403(CONSENT_REQUIRED → PASSWORD_CHANGE_REQUIRED 순)을 받는다. 401 이 아니다 —
 * 프론트는 401 을 로그인 화면으로 보내는데, 다시 로그인해도 같은 상태라 끝없이 돈다.
 * 컨트롤러마다 넣지 않고 인터셉터 한 곳에 둔다(새 API 를 만들 때 빠지지 않게). 컨트롤러 전에 돌아서 스트리밍(POST /chat)도 스트림을 열기 전에 JSON 으로 거절된다.
 * 값은 요청마다 DB 에서 읽는다 — 세션에 넣어 두면 동의하거나 비밀번호를 바꾼 직후 같은 세션에서 풀리지 않고, 만료 · 판 변경은 로그인한 뒤에도 생긴다.
 * 판정은 GET /me 의 consent_required · must_change_password 와 같은 메서드를 쓴다(AuthController.loadMe) — 화면은 게이트로 보내는데 서버는 통과시키는 경우가 없게.
 */
@Component
public class AccountGate implements HandlerInterceptor {

  /** 두 상태에서 모두 열어 두는 API: 상태를 읽는 길, 동의하는 길, 비밀번호를 바꾸는 길, 나가는 길, 그리고 로그인 없이도 되는 것. */
  private static final Set<String> OPEN = Set.of(
      "GET /me",
      "POST /auth/consent", "GET /auth/privacy-policy",
      "PATCH /auth/password", "GET /auth/password-policy",
      "POST /auth/logout",
      "POST /auth/login", "GET /workspaces");

  private static final Pattern POLICY_VERSION = Pattern.compile("판:\\s*([A-Za-z0-9._-]+)");

  private record CachedVersion(long modified, long size, String version) {}

  private final JdbcClient jdbc;
  private final Path privacyPolicyPath;
  private volatile CachedVersion cached;

  public AccountGate(JdbcClient jdbc, @Value("${oom.legal.privacy-policy-path}") String privacyPolicyPath) {
    this.jdbc = jdbc;
    this.privacyPolicyPath = Path.of(privacyPolicyPath);
  }

  @Override
  public boolean preHandle(HttpServletRequest req, HttpServletResponse res, Object handler) {
    if (req.getDispatcherType() != DispatcherType.REQUEST) return true;   // 스트리밍의 두 번째 디스패치 · 오류 디스패치는 이미 확인을 거쳤다
    Optional<Long> uid = CurrentUser.id();
    if (uid.isEmpty() || open(req)) return true;
    String version = policyVersion();
    Optional<Map<String, Object>> row = jdbc.sql("""
          SELECT must_change_password, password_changed_at, consented_at,
                 EXISTS (SELECT 1 FROM user_consents c WHERE c.user_id = users.id AND c.policy_version = ?::text) AS consented_current
          FROM users WHERE id = ?""").params(version, uid.get()).query().listOfRows().stream().findFirst();
    if (row.isEmpty()) return true;
    Map<String, Object> u = row.get();
    if (consentRequired(version, u.get("consented_at"), Boolean.TRUE.equals(u.get("consented_current"))))
      throw new ApiException(HttpStatus.FORBIDDEN, "CONSENT_REQUIRED", "consent to the privacy policy first (POST /auth/consent)");
    if (mustChangePassword(Boolean.TRUE.equals(u.get("must_change_password")), (Timestamp) u.get("password_changed_at")))
      throw new ApiException(HttpStatus.FORBIDDEN, "PASSWORD_CHANGE_REQUIRED", "change your password first (PATCH /auth/password)");
    return true;
  }

  private static boolean open(HttpServletRequest req) {
    String path = req.getRequestURI().substring(req.getContextPath().length());
    return OPEN.contains(req.getMethod() + " " + path) || path.startsWith("/actuator/");
  }

  /** 처리방침에 동의해야 하는가 — 지금 판에 대한 동의가 없다. 판을 알 수 없으면(문안 파일 없음 · 판 표시 없음) 동의한 적이 있는지만 본다. */
  static boolean consentRequired(String currentVersion, Object consentedAt, boolean consentedCurrent) {
    return currentVersion == null ? consentedAt == null : !consentedCurrent;
  }

  /** 비밀번호를 바꿔야 하는가 — 표시가 있거나(임시 비밀번호 · 재활성화) 만료됐다. 기준 시각이 없으면(구 계정) 만료로 본다. */
  static boolean mustChangePassword(boolean flagged, Timestamp changedAt) {
    Instant expiresAt = passwordExpiresAt(changedAt);
    return flagged || expiresAt == null || expiresAt.isBefore(Instant.now());
  }

  static Instant passwordExpiresAt(Timestamp changedAt) {
    return changedAt == null ? null : changedAt.toInstant().plus(Duration.ofDays(PasswordPolicy.MAX_AGE_DAYS));
  }

  /**
   * 지금 처리방침 판(문안 머리의 "판: v2"). 파일이 없거나 판 표시가 없으면 null.
   * 문안 파일을 고치면 재시작 없이 반영된다 — 다만 이 확인이 모든 요청에 걸리므로 요청마다 파일을 읽지 않고, 수정 시각이나 크기가 바뀔 때만 다시 읽는다.
   */
  public String policyVersion() {
    try {
      long modified = Files.getLastModifiedTime(privacyPolicyPath).toMillis();
      long size = Files.size(privacyPolicyPath);
      CachedVersion c = cached;
      if (c == null || c.modified() != modified || c.size() != size) {
        c = new CachedVersion(modified, size, versionOf(Files.readString(privacyPolicyPath, StandardCharsets.UTF_8)));
        cached = c;
      }
      return c.version();
    } catch (IOException e) {
      return null;
    }
  }

  static String versionOf(String text) {
    Matcher m = POLICY_VERSION.matcher(text);
    return m.find() ? m.group(1) : null;
  }
}
