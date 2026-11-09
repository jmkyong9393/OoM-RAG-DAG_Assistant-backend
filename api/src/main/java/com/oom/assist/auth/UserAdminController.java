package com.oom.assist.auth;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Mask;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import java.security.SecureRandom;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 계정은 관리자 초대로만 만든다(자가 가입 없음). 현재 워크스페이스의 admin 이 사번으로 초대하면
 * 같은 테넌트에 사용자를 만들고(임시 비밀번호 1회 반환, must_change_password) 그 워크스페이스 멤버로 넣는다.
 * 이미 테넌트에 있는 사번이면 멤버십만 추가한다. 목록의 이메일은 마스킹(보호조치 기준 §10).
 */
@RestController
public class UserAdminController {

  private static final String UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ", LOWER = "abcdefghjkmnpqrstuvwxyz", DIGIT = "23456789", SPECIAL = "!@#$%*-_";
  private final JdbcClient jdbc;
  private final PasswordEncoder encoder;
  private final LoginAttempts attempts;
  private final SecureRandom random = new SecureRandom();

  public UserAdminController(JdbcClient jdbc, PasswordEncoder encoder, LoginAttempts attempts) {
    this.jdbc = jdbc;
    this.encoder = encoder;
    this.attempts = attempts;
  }

  public record InviteRequest(@NotBlank @Pattern(regexp = "[A-Za-z0-9-]{3,32}") String employeeNo, @NotBlank String name,
                              @Email String email, String grade, String position, String title, String role) {}
  public record InviteResponse(long id, String employeeNo, String name, long workspaceId, String role, boolean created, String tempPassword) {}
  /** title = 표시용 "직급 · 직책"(예전 화면 호환). */
  public record MemberDto(long id, String employeeNo, String name, String title, String grade, String position, String emailMasked, String role, boolean locked) {}

  @PostMapping("/users")
  @Transactional
  public ResponseEntity<InviteResponse> invite(@Valid @RequestBody InviteRequest body, TenantContext ctx) {
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    String role = body.role() == null ? "member" : body.role();
    if (!role.equals("member") && !role.equals("admin")) throw ApiException.badRequest("VALIDATION", "role must be member|admin");
    String empNo = body.employeeNo().trim().toUpperCase();
    String email = body.email() == null || body.email().isBlank() ? null : body.email().trim().toLowerCase();

    Optional<Long> existing = jdbc.sql("SELECT id FROM users WHERE tenant_id = ? AND lower(employee_no) = lower(?)").params(ctx.tenantId(), empNo)
        .query(Long.class).optional();
    boolean created = existing.isEmpty();
    String temp = null;
    long userId;
    if (created) {
      if (jdbc.sql("SELECT count(*) FROM users WHERE lower(employee_no) = lower(?)").param(empNo).query(Long.class).single() > 0)
        throw ApiException.conflict("EMPLOYEE_NO_TAKEN", "employee_no already used in another tenant");
      temp = tempPassword(empNo);
      // 직급 · 직책이 오면 그대로, 예전 화면처럼 title 만 오면 DB 규칙(title_parts)으로 나눈다
      Map<String, Object> gp = jdbc.sql("""
            SELECT CASE WHEN ?::text IS NULL AND ?::text IS NULL THEN p.grade ELSE nullif(btrim(?), '') END AS grade,
                   CASE WHEN ?::text IS NULL AND ?::text IS NULL THEN p.duty ELSE nullif(btrim(?), '') END AS duty
            FROM title_parts(?) p""")
          .params(body.grade(), body.position(), body.grade(), body.grade(), body.position(), body.position(), body.title()).query().singleRow();
      String grade = (String) gp.get("grade"), position = (String) gp.get("duty");
      userId = jdbc.sql("""
            INSERT INTO users (tenant_id, name, grade, duty, title, email, employee_no, password_hash, must_change_password, password_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, true, now()) RETURNING id""")
          .params(ctx.tenantId(), body.name().trim(), grade, position, AuthController.display(grade, position), email, empNo, encoder.encode(temp))
          .query(Long.class).single();
    } else {
      userId = existing.get();
    }
    int added = jdbc.sql("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?) ON CONFLICT DO NOTHING")
        .params(ctx.workspaceId(), userId, role).update();
    if (!created && added == 0) throw ApiException.conflict("ALREADY_MEMBER", "already a member");
    jdbc.sql("""
          INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
          VALUES (?, ?, 'users', ?, ?, ?, 'ui', ?)""")
        .params(ctx.tenantId(), ctx.workspaceId(), String.valueOf(userId), created ? "invite" : "add_member", ctx.userName(), "role=" + role).update();
    return ResponseEntity.status(HttpStatus.CREATED)
        .body(new InviteResponse(userId, empNo, body.name().trim(), ctx.workspaceId(), role, created, temp));
  }

  /** 현재 워크스페이스 멤버 목록 (초대 화면·작성자 선택용). 이메일은 마스킹. */
  @GetMapping("/users")
  public List<MemberDto> members(TenantContext ctx) {
    return jdbc.sql("""
          SELECT u.id, u.employee_no, u.email, u.name, u.grade, u.duty, coalesce(nullif(concat_ws(' · ', u.grade, u.duty), ''), u.title) AS title,
                 m.role, (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked
          FROM workspace_members m JOIN users u ON u.id = m.user_id
          WHERE m.workspace_id = ? AND u.status = 'active' ORDER BY (m.role = 'admin') DESC, u.name""")
        .param(ctx.workspaceId())
        .query((rs, i) -> new MemberDto(rs.getLong("id"), rs.getString("employee_no"), rs.getString("name"), rs.getString("title"),
            rs.getString("grade"), rs.getString("duty"), Mask.email(rs.getString("email")), rs.getString("role"), rs.getBoolean("locked"))).list();
  }

  /** 잠금 해제 (현재 워크스페이스 admin, 대상은 그 워크스페이스 멤버). */
  @PostMapping("/users/{id}/unlock")
  public ResponseEntity<Void> unlock(@PathVariable long id, TenantContext ctx) {
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    int n = jdbc.sql("""
          UPDATE users u SET locked_until = NULL, failed_attempts = 0
          FROM workspace_members m WHERE m.user_id = u.id AND m.workspace_id = ? AND u.id = ?""").params(ctx.workspaceId(), id).update();
    if (n == 0) throw ApiException.notFound("user");
    String empNo = jdbc.sql("SELECT employee_no FROM users WHERE id = ?").param(id).query(String.class).single();
    attempts.audit(empNo, "unlocked_by_admin", ctx.userName());
    return ResponseEntity.noContent().build();
  }

  public record RoleRequest(String role) {}

  /** 역할 변경(현재 워크스페이스 admin). 자기 자신 강등과 마지막 admin 강등은 409 — 부서에 관리자가 없어지면 아무도 되돌릴 수 없다. */
  @PatchMapping("/users/{id}")
  @Transactional
  public MemberDto changeRole(@PathVariable long id, @RequestBody RoleRequest body, TenantContext ctx) {
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    String role = body == null ? null : body.role();
    if (!"member".equals(role) && !"admin".equals(role)) throw ApiException.badRequest("VALIDATION", "role must be member|admin");
    lockMembers(ctx.workspaceId());
    String cur = memberRole(ctx.workspaceId(), id);
    if (!cur.equals(role)) {
      if ("member".equals(role)) {
        if (id == ctx.userId()) throw ApiException.conflict("SELF_DEMOTION", "cannot demote yourself");
        if (adminCount(ctx.workspaceId()) <= 1) throw ApiException.conflict("LAST_ADMIN", "workspace needs at least one admin");
      }
      jdbc.sql("UPDATE workspace_members SET role = ? WHERE workspace_id = ? AND user_id = ?").params(role, ctx.workspaceId(), id).update();
      auditMember(ctx, id, "role_changed", cur + "->" + role);
    }
    return members(ctx).stream().filter(m -> m.id() == id).findFirst().orElseThrow(() -> ApiException.notFound("user"));
  }

  /** 멤버 제외(계정은 남긴다 — 작성한 사례의 작성자로 계속 보인다). 자기 자신 · 마지막 admin 제외는 409. 경로의 부서와 X-Workspace-Id 가 같아야 한다. */
  @DeleteMapping("/workspaces/{ws}/members/{uid}")
  @Transactional
  public ResponseEntity<Void> removeMember(@PathVariable long ws, @PathVariable long uid, TenantContext ctx) {
    if (ws != ctx.workspaceId()) throw ApiException.badRequest("VALIDATION", "path workspace must match X-Workspace-Id");
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    lockMembers(ws);
    String cur = memberRole(ws, uid);
    if (uid == ctx.userId()) throw ApiException.conflict("SELF_REMOVAL", "cannot remove yourself");
    if ("admin".equals(cur) && adminCount(ws) <= 1) throw ApiException.conflict("LAST_ADMIN", "workspace needs at least one admin");
    jdbc.sql("DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?").params(ws, uid).update();
    auditMember(ctx, uid, "member_removed", "role=" + cur);
    return ResponseEntity.noContent().build();
  }

  /** 같은 부서의 역할 변경 · 제외를 줄 세운다 — 관리자 둘이 서로를 동시에 강등해 admin 이 0명이 되는 틈을 막는다. */
  private void lockMembers(long ws) {
    jdbc.sql("SELECT pg_advisory_xact_lock(?, ?)").params(7, (int) ws).query(Object.class).optional();
  }

  private String memberRole(long ws, long uid) {
    return jdbc.sql("SELECT m.role FROM workspace_members m JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? AND m.user_id = ? AND u.status = 'active'")
        .params(ws, uid).query(String.class).optional().orElseThrow(() -> ApiException.notFound("member"));
  }

  private long adminCount(long ws) {
    return jdbc.sql("SELECT count(*) FROM workspace_members m JOIN users u ON u.id = m.user_id WHERE m.workspace_id = ? AND m.role = 'admin' AND u.status = 'active'")
        .param(ws).query(Long.class).single();
  }

  private void auditMember(TenantContext ctx, long uid, String action, String reason) {
    jdbc.sql("""
          INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
          VALUES (?, ?, 'users', ?, ?, ?, 'ui', ?)""")
        .params(ctx.tenantId(), ctx.workspaceId(), String.valueOf(uid), action, ctx.userName(), reason).update();
  }

  /** 작성규칙을 만족하는 임시 비밀번호: 대문자·소문자·숫자·특수 4종 14자, 연속 문자가 생기면 다시 뽑는다. */
  private String tempPassword(String employeeNo) {
    while (true) {
      StringBuilder sb = new StringBuilder(14);
      sb.append(UPPER.charAt(random.nextInt(UPPER.length()))).append(LOWER.charAt(random.nextInt(LOWER.length())))
        .append(DIGIT.charAt(random.nextInt(DIGIT.length()))).append(SPECIAL.charAt(random.nextInt(SPECIAL.length())));
      String all = UPPER + LOWER + DIGIT;
      for (int i = 0; i < 10; i++) sb.append(all.charAt(random.nextInt(all.length())));
      String pw = sb.toString();
      if (PasswordPolicy.isValid(pw, employeeNo)) return pw;
    }
  }
}
