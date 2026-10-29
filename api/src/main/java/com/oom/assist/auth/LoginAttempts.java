package com.oom.assist.auth;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;

/**
 * 접근통제(보호조치 기준 §4 ⑦): 연속 5회 실패 시 15분 잠금, 성공 시 초기화. 접속기록은 audit_log(table_name='auth') 에 남긴다.
 * 캡챠는 외부 서비스 의존이라 두지 않고 잠금으로 대신한다(개발문서 35).
 */
@Service
public class LoginAttempts {
  public static final int MAX_FAILURES = 5;
  public static final int LOCK_MINUTES = 15;

  private final JdbcClient jdbc;

  public LoginAttempts(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  /** 실패 기록. 임계치에 닿으면 잠그고 true 를 돌려준다. 없는 사번이면 기록만 남긴다. */
  public boolean recordFailure(String employeeNo, String ip) {
    Integer attempts = jdbc.sql("""
          UPDATE users SET failed_attempts = failed_attempts + 1
          WHERE lower(employee_no) = lower(?) RETURNING failed_attempts""").param(employeeNo).query(Integer.class).optional().orElse(null);
    audit(employeeNo, "login_failed", ip);
    if (attempts != null && attempts >= MAX_FAILURES) {
      jdbc.sql("UPDATE users SET locked_until = now() + make_interval(mins => ?), failed_attempts = 0 WHERE lower(employee_no) = lower(?)")
          .params(LOCK_MINUTES, employeeNo).update();
      audit(employeeNo, "account_locked", ip);
      return true;
    }
    return false;
  }

  public void recordSuccess(long userId, String employeeNo, String ip) {
    jdbc.sql("UPDATE users SET failed_attempts = 0, locked_until = NULL, last_login_at = now() WHERE id = ?").param(userId).update();
    audit(employeeNo, "login_success", ip);
  }

  public void audit(String employeeNo, String action, String detail) {
    jdbc.sql("""
          INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
          SELECT u.tenant_id, NULL, 'auth', coalesce(u.id::text, '-'), ?, ?, 'ui', ?
          FROM (SELECT 1) one LEFT JOIN users u ON lower(u.employee_no) = lower(?)""")
        .params(action, employeeNo, detail, employeeNo).update();
  }

  /** 접속 IP. 리버스 프록시(Caddy) 뒤에서는 X-Forwarded-For 의 첫 값. */
  public static String clientIp(HttpServletRequest req) {
    String xff = req.getHeader("X-Forwarded-For");
    if (xff != null && !xff.isBlank()) return xff.split(",")[0].trim();
    return req.getRemoteAddr();
  }
}
