package com.oom.assist.auth;

import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Mask;
import java.time.OffsetDateTime;
import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 접속기록 점검(보호조치 기준): 로그인 · 잠금 · 비밀번호 변경 · 내 정보 정정 · 멤버 초대 · 역할 변경 · 제외 · 부서 설정 변경.
 * 회사 관리자만, 자기 회사 기록만. IP 는 가려서 내준다. 부서 헤더 없이 부른다.
 */
@RestController
public class AuditController {

  private static final int LIMIT_MAX = 500;
  private final JdbcClient jdbc;

  public AuditController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  /** employeeNo = 기록의 대상 계정(없으면 null), actor = 한 사람, reason = 바뀐 항목 · 역할 · 설정 등. */
  public record AuditItem(long id, OffsetDateTime at, String category, String action, String employeeNo, String actor, String ipMasked, String reason) {}
  public record AuditPage(List<AuditItem> items) {}

  @GetMapping("/audit")
  public AuditPage list(@RequestParam(required = false) String from, @RequestParam(required = false) String to,
                        @RequestParam(required = false) String action, @RequestParam(name = "employee_no", required = false) String employeeNo,
                        @RequestParam(defaultValue = "100") int limit) {
    long uid = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    boolean companyAdmin = jdbc.sql("""
        SELECT EXISTS (SELECT 1 FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
                       WHERE m.user_id = ? AND m.role = 'admin' AND w.kind <> 'dept')""").param(uid).query(Boolean.class).single();
    if (!companyAdmin) throw ApiException.forbidden("company admin only");
    if (limit < 1 || limit > LIMIT_MAX) throw ApiException.badRequest("VALIDATION", "limit must be 1~" + LIMIT_MAX);
    OffsetDateTime f = time(from), t = time(to);
    return new AuditPage(jdbc.sql("""
          WITH a AS (
            SELECT a.id, a.at, a.table_name, a.action, a.actor, a.reason, a.label,
                   CASE a.table_name WHEN 'auth' THEN a.actor
                                     WHEN 'users' THEN (SELECT u.employee_no FROM users u WHERE u.id::text = a.row_id) END AS employee_no
            FROM audit_log a
            WHERE a.tenant_id = (SELECT tenant_id FROM users WHERE id = ?) AND a.table_name IN ('auth', 'users', 'workspaces')
              AND (?::timestamptz IS NULL OR a.at >= ?) AND (?::timestamptz IS NULL OR a.at < ?)
              AND (?::text IS NULL OR a.action = ?)
          )
          SELECT * FROM a WHERE (?::text IS NULL OR lower(employee_no) = lower(?))
          ORDER BY at DESC, id DESC LIMIT ?
          """).params(uid, f, f, t, t, action, action, employeeNo, employeeNo, limit)
          .query((rs, i) -> item(rs.getLong("id"), rs.getObject("at", OffsetDateTime.class), rs.getString("table_name"), rs.getString("action"),
              rs.getString("employee_no"), rs.getString("actor"), rs.getString("reason"), rs.getString("label"))).list());
  }

  /** "2026-11-09"(그날 0시, 한국 시각) 또는 ISO 시각. 형식이 틀리면 400. */
  private static OffsetDateTime time(String v) {
    if (v == null || v.isBlank()) return null;
    try {
      return v.length() == 10 ? java.time.LocalDate.parse(v).atStartOfDay(java.time.ZoneId.of("Asia/Seoul")).toOffsetDateTime() : OffsetDateTime.parse(v);
    } catch (java.time.format.DateTimeParseException e) {
      throw ApiException.badRequest("VALIDATION", "from/to must be YYYY-MM-DD or ISO datetime");
    }
  }

  /** auth 기록은 reason 칸에 접속 IP 를 담는다(관리자 잠금 해제만 해제한 관리자 이름). 테이블마다 칸 쓰임이 달라 여기서 한 모양으로 맞춘다. */
  public static AuditItem item(long id, OffsetDateTime at, String table, String action, String employeeNo, String actor, String reason, String label) {
    return switch (table) {
      case "auth" -> "unlocked_by_admin".equals(action)
          ? new AuditItem(id, at, "auth", action, employeeNo, reason, null, null)
          : new AuditItem(id, at, "auth", action, employeeNo, actor, Mask.ip(reason), label);
      case "users" -> new AuditItem(id, at, "member", action, employeeNo, actor, null, reason);
      default -> new AuditItem(id, at, "settings", action, null, actor, null, label == null ? reason : label + ": " + reason);
    };
  }
}
