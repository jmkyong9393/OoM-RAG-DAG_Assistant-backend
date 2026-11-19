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
 * 이미 테넌트에 있는 사번이면 멤버십만 추가한다(비활성 계정은 409 — 먼저 재활성화). 목록의 이메일은 마스킹(보호조치 기준 §10).
 * 계정 관리: 임시 비밀번호 재발급은 부서 단위(그 부서의 admin 이면서 대상이 속한 모든 워크스페이스의 admin 이거나 회사 관리자, X-Workspace-Id 필요), 비활성화 · 재활성화 · 비활성 목록은 회사 단위
 * (회사 관리자, 부서 헤더와 무관 — AuditController 처럼 로그인한 사람으로만 본다).
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
  /** title = 표시용 "직급 · 직책"(예전 화면 호환). canManageAccount = 요청한 사람이 이 계정을 재발급 · 잠금 해제할 수 있는가(화면이 버튼을 미리 끄는 데 쓴다). */
  public record MemberDto(long id, String employeeNo, String name, String title, String grade, String position, String emailMasked, String role, boolean locked,
                          boolean canManageAccount) {}

  @PostMapping("/users")
  @Transactional
  public ResponseEntity<InviteResponse> invite(@Valid @RequestBody InviteRequest body, TenantContext ctx) {
    requireMemberAdmin(ctx);
    String role = body.role() == null ? "member" : body.role();
    if (!role.equals("member") && !role.equals("admin")) throw ApiException.badRequest("VALIDATION", "role must be member|admin");
    String empNo = body.employeeNo().trim().toUpperCase();
    String email = body.email() == null || body.email().isBlank() ? null : body.email().trim().toLowerCase();

    Optional<Long> existing = jdbc.sql("SELECT id FROM users WHERE tenant_id = ? AND lower(employee_no) = lower(?)").params(ctx.tenantId(), empNo)
        .query(Long.class).optional();
    if (existing.isPresent() && !"active".equals(jdbc.sql("SELECT status FROM users WHERE id = ? FOR SHARE").param(existing.get()).query(String.class).single()))
      throw ApiException.conflict("ALREADY_INACTIVE", "reactivate the account first");   // 비활성(퇴사 처리) 계정이 그 상태로 다른 부서에 들어가지 않게. FOR SHARE = 확인과 소속 추가 사이에 비활성화가 끼어들지 못하게(lockUser 와 같은 줄)
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

  /**
   * 현재 워크스페이스 멤버 목록 (초대 화면·작성자 선택용). 이메일은 마스킹.
   * can_manage_account 는 재발급 · 잠금 해제가 거치는 두 확인(requireMemberAdmin · requireManagesAccount)과 같은 조건이다. 본인 재발급의 400 은 여기에 담지 않는다.
   */
  @GetMapping("/users")
  public List<MemberDto> members(TenantContext ctx) {
    return jdbc.sql("""
          SELECT u.id, u.employee_no, u.email, u.name, u.grade, u.duty, coalesce(nullif(concat_ws(' · ', u.grade, u.duty), ''), u.title) AS title,
                 m.role, (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,
                 (? OR (? AND NOT (""" + outsideMyAdmin("u.id") + """
                 ))) AS can_manage_account
          FROM workspace_members m JOIN users u ON u.id = m.user_id
          WHERE m.workspace_id = ? AND u.status = 'active' ORDER BY (m.role = 'admin') DESC, u.name""")
        .params(companyAdmin(ctx.userId()), ctx.isAdmin(), ctx.userId(), ctx.workspaceId())
        .query((rs, i) -> new MemberDto(rs.getLong("id"), rs.getString("employee_no"), rs.getString("name"), rs.getString("title"),
            rs.getString("grade"), rs.getString("duty"), Mask.email(rs.getString("email")), rs.getString("role"), rs.getBoolean("locked"),
            rs.getBoolean("can_manage_account"))).list();
  }

  /**
   * 잠금 해제(이 부서의 멤버를 관리할 수 있는 사람, 대상은 그 워크스페이스 멤버). 재발급과 같은 조건을 건다(requireManagesAccount) —
   * 다른 부서의 계정을 자기 부서에 초대한 뒤 잠금을 계속 풀어 주면, 로그인 5회 실패 잠금이 그 계정에는 듣지 않게 된다.
   * 순서: 멤버 관리 권한(403) → 대상이 이 부서의 멤버인가(404) → 이 계정을 넘겨받아도 되는가(403) → 해제. 권한이 없는 것과 대상이 없는 것을 구분한다.
   * 여러 부서에 속한 계정은 회사 관리자가 풀거나 15분 뒤 저절로 풀리기를 기다린다. 풀었을 때만 접속기록에 남긴다.
   */
  @PostMapping("/users/{id}/unlock")
  public ResponseEntity<Void> unlock(@PathVariable long id, TenantContext ctx) {
    requireMemberAdmin(ctx);
    if (!jdbc.sql("SELECT EXISTS (SELECT 1 FROM workspace_members WHERE workspace_id = ? AND user_id = ?)").params(ctx.workspaceId(), id)
        .query(Boolean.class).single()) throw ApiException.notFound("user");
    requireManagesAccount(ctx.userId(), id);
    int n = jdbc.sql("""
          UPDATE users u SET locked_until = NULL, failed_attempts = 0
          FROM workspace_members m WHERE m.user_id = u.id AND m.workspace_id = ? AND u.id = ?""").params(ctx.workspaceId(), id).update();
    if (n == 0) throw ApiException.notFound("user");
    String empNo = jdbc.sql("SELECT employee_no FROM users WHERE id = ?").param(id).query(String.class).single();
    attempts.audit(empNo, "unlocked_by_admin", ctx.userName());
    return ResponseEntity.noContent().build();
  }

  public record TempPasswordResponse(String tempPassword) {}

  /**
   * 임시 비밀번호 재발급(현재 워크스페이스 admin, 대상은 그 워크스페이스 멤버). 다음 로그인에서 비밀번호를 바꾸게 하고 잠금을 풀고,
   * 대상의 로그인 세션을 지운다 — 비밀번호가 새어 나갔을 때도 쓰는 기능이라, 지우지 않으면 새어 나간 쪽이 예전 세션을 계속 쓴다.
   * 임시 비밀번호는 이 응답에 한 번만 나온다 — 접속기록에도 남기지 않는다.
   * 임시 비밀번호가 요청한 사람에게 가므로 재발급은 "그 계정으로 로그인할 수 있게 되는 일"이다. 그래서 요청한 사람이 회사 관리자이거나
   * 대상이 속한 모든 워크스페이스의 admin 일 때만 한다(아니면 403) — 재발급으로 얻는 권한이 이미 가진 권한을 넘지 않게.
   * 권한이 아니라 소속을 본다: 대상이 다른 부서의 일반 멤버여도 그 부서의 기록을 읽게 된다. 대상을 자기 부서에 초대해 두고 재발급하는 길도
   * 이 조건으로 막힌다(초대해도 대상은 원래 부서에 그대로 속해 있다). 회사 관리자의 계정은 전사 공통 워크스페이스에 속하므로 회사 관리자만 재발급한다.
   */
  @PostMapping("/users/{id}/reset-password")
  @Transactional
  public TempPasswordResponse resetPassword(@PathVariable long id, TenantContext ctx) {
    requireMemberAdmin(ctx);
    if (id == ctx.userId()) throw ApiException.badRequest("VALIDATION", "change your own password with PATCH /auth/password");
    Map<String, Object> u = jdbc.sql("""
          SELECT u.employee_no, u.status FROM workspace_members m JOIN users u ON u.id = m.user_id
          WHERE m.workspace_id = ? AND m.user_id = ?""").params(ctx.workspaceId(), id).query().listOfRows().stream().findFirst()
        .orElseThrow(() -> ApiException.notFound("user"));
    if (!"active".equals(u.get("status"))) throw ApiException.conflict("ALREADY_INACTIVE", "reactivate the account first");
    requireManagesAccount(ctx.userId(), id);
    String temp = tempPassword((String) u.get("employee_no"));
    jdbc.sql("""
          UPDATE users SET password_hash = ?, must_change_password = true, password_changed_at = now(), locked_until = NULL, failed_attempts = 0
          WHERE id = ?""").params(encoder.encode(temp), id).update();
    endSessions(id);
    auditMember(ctx, id, "password_reset_by_admin", null);
    return new TempPasswordResponse(temp);
  }

  public record DeactivateRequest(String reason) {}

  /**
   * 계정 비활성화(회사 관리자). 부서 소속과 작성한 기록은 그대로 둔다 — 로그인이 status = 'active' 를 보므로 새로 로그인할 수 없고,
   * 이미 로그인해 있던 세션은 여기서 지운다(다음 요청부터 401). 지우지 않으면 계정 상태를 보지 않는 API(/audit, 부서 설정 변경)를 세션이 끝날 때까지 쓸 수 있다.
   * 자기 자신, 마지막 회사 관리자, 어떤 부서의 마지막 관리자는 409.
   */
  @PostMapping("/users/{id}/deactivate")
  @Transactional
  public ResponseEntity<Void> deactivate(@PathVariable long id, @RequestBody(required = false) DeactivateRequest body) {
    Actor actor = companyActor();
    String reason = body == null || body.reason() == null ? "" : body.reason().strip();
    if (reason.isEmpty()) throw ApiException.badRequest("REASON_REQUIRED", "reason is required");
    lockCompany(actor.tenantId());
    if (!"active".equals(status(id, actor.tenantId()))) throw ApiException.conflict("ALREADY_INACTIVE", "already inactive");
    // 위 status() 가 대상 계정의 행을 잠갔으므로(FOR UPDATE) 여기서부터 대상의 역할 · 소속은 바뀌지 않는다 — 역할 변경 · 제외 · 초대가
    // 같은 행을 FOR SHARE 로 잡는다(lockUser). 그래서 대상이 admin 인 부서는 한 번만 조회하면 된다.
    // 그 부서들은 잠가서, 같은 부서의 다른 admin 을 강등 · 제외하는 요청과 줄을 세운 뒤 인원을 센다
    List<Map<String, Object>> adminOf = jdbc.sql("""
          SELECT m.workspace_id, w.kind FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id
          WHERE m.user_id = ? AND m.role = 'admin' ORDER BY m.workspace_id""").param(id).query().listOfRows();
    for (Map<String, Object> w : adminOf) lockMembers(((Number) w.get("workspace_id")).longValue());
    if (adminOf.stream().anyMatch(w -> !"dept".equals(w.get("kind"))) && companyAdminCount(actor.tenantId()) <= 1)
      throw ApiException.conflict("LAST_COMPANY_ADMIN", "company needs at least one admin");
    if (id == actor.id()) throw ApiException.conflict("SELF_DEACTIVATION", "cannot deactivate yourself");
    for (Map<String, Object> w : adminOf)
      if ("dept".equals(w.get("kind")) && adminCount(((Number) w.get("workspace_id")).longValue()) <= 1)
        throw ApiException.conflict("LAST_ADMIN", "workspace needs at least one admin");
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(id).update();
    endSessions(id);
    auditCompany(actor, id, "user_deactivated", reason);
    return ResponseEntity.noContent().build();
  }

  /**
   * 재활성화(회사 관리자). 잠금을 풀고, 다음 로그인에서 비밀번호를 바꾸게 한다 — 퇴사 · 사고로 막았던 계정이 예전 비밀번호 그대로 살아나지 않게.
   * 이미 활성이면 아무것도 바꾸지 않고 204.
   */
  @PostMapping("/users/{id}/reactivate")
  @Transactional
  public ResponseEntity<Void> reactivate(@PathVariable long id) {
    Actor actor = companyActor();
    if (!"active".equals(status(id, actor.tenantId()))) {
      jdbc.sql("UPDATE users SET status = 'active', locked_until = NULL, failed_attempts = 0, must_change_password = true WHERE id = ?").param(id).update();
      auditCompany(actor, id, "user_reactivated", null);
    }
    return ResponseEntity.noContent().build();
  }

  /** 비활성 계정 한 줄 — 멤버 목록 항목(MemberDto)에서 role · locked 를 뺀 모양. */
  public record InactiveUserDto(long id, String employeeNo, String name, String title, String grade, String position, String emailMasked) {}

  /** 같은 회사의 비활성 계정(회사 관리자). 이메일은 마스킹. */
  @GetMapping("/users/inactive")
  public List<InactiveUserDto> inactive() {
    Actor actor = companyActor();
    return jdbc.sql("""
          SELECT id, employee_no, email, name, grade, duty, coalesce(nullif(concat_ws(' · ', grade, duty), ''), title) AS title
          FROM users WHERE tenant_id = ? AND status = 'inactive' ORDER BY name, id""").param(actor.tenantId())
        .query((rs, i) -> new InactiveUserDto(rs.getLong("id"), rs.getString("employee_no"), rs.getString("name"), rs.getString("title"),
            rs.getString("grade"), rs.getString("duty"), Mask.email(rs.getString("email")))).list();
  }

  private record Actor(long id, long tenantId, String name) {}

  /** 회사 단위 API 의 요청자. TenantContext 를 받지 않는다 — 받으면 부서 헤더에 묶여, 회사 관리자가 소속이 아닌 부서 화면이나 헤더 없이 부를 때 403 이 난다. */
  private Actor companyActor() {
    long uid = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    if (!companyAdmin(uid)) throw ApiException.forbidden("company admin only");
    Map<String, Object> me = jdbc.sql("SELECT tenant_id, name FROM users WHERE id = ?").param(uid).query().singleRow();
    return new Actor(uid, ((Number) me.get("tenant_id")).longValue(), (String) me.get("name"));
  }

  /**
   * 이 부서의 멤버를 관리할 수 있는가 — 그 부서의 admin 이거나 회사 관리자. 초대 · 역할 변경 · 제외 · 재발급 · 잠금 해제가 쓴다. 아니면 403.
   * 회사 관리자를 넣은 이유: 한 부서에만 속한 그 부서의 하나뿐인 admin 은 재발급해 줄 사람이 없었고, 하나뿐인 admin 이 떠난 부서는 후임을 지정할 사람이 없었다.
   * 회사 관리자도 부서 헤더는 필요하다 — 자기가 속한 부서에서 부른다(속하지 않은 부서는 부서 정보가 만들어지지 않아 여전히 403).
   * SELF_DEMOTION · SELF_REMOVAL · LAST_ADMIN 은 회사 관리자에게도 그대로다 — 후임을 먼저 지정하고 내린다.
   */
  private void requireMemberAdmin(TenantContext ctx) {
    if (!ctx.isAdmin() && !companyAdmin(ctx.userId())) throw ApiException.forbidden("admin only");
  }

  /**
   * 이 계정을 넘겨받아도 되는가 — 요청한 사람이 회사 관리자이거나 대상이 속한 모든 워크스페이스의 admin. 재발급 · 잠금 해제가 쓴다. 아니면 403.
   * 재발급은 임시 비밀번호가 요청한 사람에게 가고, 잠금 해제는 비밀번호를 계속 맞혀 볼 수 있게 한다 — 둘 다 그 계정에 들어가는 길이라
   * 그것으로 얻는 권한이 요청한 사람이 이미 가진 권한을 넘지 않을 때만 한다. 권한이 아니라 소속을 본다(대상이 다른 부서의 일반 멤버여도 그 부서의 기록을 읽게 된다).
   * 초대 · 역할 변경 · 제외는 계정에 들어가는 일이 아니라 쓰지 않는다(requireMemberAdmin 만).
   */
  private void requireManagesAccount(long requester, long target) {
    if (!companyAdmin(requester) && jdbc.sql("SELECT " + outsideMyAdmin("?")).params(target, requester).query(Boolean.class).single())
      throw ApiException.forbidden("the account belongs to a workspace you do not manage");
  }

  /** SQL 조건: 대상(target 식)이 속한 워크스페이스 가운데 요청한 사람(? 하나)이 admin 이 아닌 곳이 있다. 재발급 · 잠금 해제의 확인과 멤버 목록이 같이 쓴다. */
  private static String outsideMyAdmin(String target) {
    return "EXISTS (SELECT 1 FROM workspace_members t WHERE t.user_id = " + target
        + " AND NOT EXISTS (SELECT 1 FROM workspace_members r WHERE r.workspace_id = t.workspace_id AND r.user_id = ? AND r.role = 'admin'))";
  }

  /** 회사 관리자 = 부서가 아닌 상위(전사 공통) 워크스페이스의 admin 인 활성 계정 (AuditController 와 같은 판정). */
  private boolean companyAdmin(long uid) {
    return jdbc.sql("""
          SELECT EXISTS (SELECT 1 FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id JOIN users u ON u.id = m.user_id
                         WHERE m.user_id = ? AND m.role = 'admin' AND w.kind <> 'dept' AND u.status = 'active')""").param(uid).query(Boolean.class).single();
  }

  private long companyAdminCount(long tenantId) {
    return jdbc.sql("""
          SELECT count(DISTINCT m.user_id) FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id JOIN users u ON u.id = m.user_id
          WHERE w.tenant_id = ? AND w.kind <> 'dept' AND m.role = 'admin' AND u.status = 'active'""").param(tenantId).query(Long.class).single();
  }

  /**
   * 대상 계정의 상태 — 그 행을 잠그고 읽는다(FOR UPDATE): 비활성화 · 재활성화끼리, 그리고 그 계정의 역할 변경 · 제외 · 초대(lockUser)와 줄을 선다.
   * 다른 회사의 계정 · 없는 번호는 같은 404 — 있는지를 알리지 않는다.
   */
  private String status(long id, long tenantId) {
    return jdbc.sql("SELECT status FROM users WHERE id = ? AND tenant_id = ? FOR UPDATE").params(id, tenantId).query(String.class).optional()
        .orElseThrow(() -> ApiException.notFound("user"));
  }

  /** 같은 회사의 비활성화를 줄 세운다 — 회사 관리자 둘이 서로를 동시에 비활성화해 0명이 되는 틈을 막는다(lockMembers 와 같은 방식). */
  private void lockCompany(long tenantId) {
    jdbc.sql("SELECT pg_advisory_xact_lock(?, ?)").params(8, (int) tenantId).query(Object.class).optional();
  }

  /**
   * 그 계정의 로그인 세션을 지운다(다음 요청부터 401) — 비활성화 · 임시 비밀번호 재발급에서 쓴다.
   * 세션의 principal_name 은 사번(AppPrincipal.getUsername)이고 사번은 전체에서 유일하다(V5). 속성 행은 FK 로 함께 지워진다.
   */
  private void endSessions(long uid) {
    jdbc.sql("DELETE FROM spring_session WHERE principal_name = (SELECT employee_no FROM users WHERE id = ?)").param(uid).update();
  }

  /** 회사 단위 접속기록 — auditMember 와 같은 모양(table_name = 'users', row_id = 대상), 부서는 비운다. */
  private void auditCompany(Actor actor, long uid, String action, String reason) {
    jdbc.sql("""
          INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
          VALUES (?, NULL, 'users', ?, ?, ?, 'ui', ?)""")
        .params(actor.tenantId(), String.valueOf(uid), action, actor.name(), reason).update();
  }

  public record RoleRequest(String role) {}

  /** 역할 변경(현재 워크스페이스 admin). 자기 자신 강등과 마지막 admin 강등은 409 — 부서에 관리자가 없어지면 아무도 되돌릴 수 없다. */
  @PatchMapping("/users/{id}")
  @Transactional
  public MemberDto changeRole(@PathVariable long id, @RequestBody RoleRequest body, TenantContext ctx) {
    requireMemberAdmin(ctx);
    String role = body == null ? null : body.role();
    if (!"member".equals(role) && !"admin".equals(role)) throw ApiException.badRequest("VALIDATION", "role must be member|admin");
    lockUser(id, ctx.tenantId());
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
    requireMemberAdmin(ctx);
    lockUser(uid, ctx.tenantId());
    lockMembers(ws);
    String cur = memberRole(ws, uid);
    if (uid == ctx.userId()) throw ApiException.conflict("SELF_REMOVAL", "cannot remove yourself");
    if ("admin".equals(cur) && adminCount(ws) <= 1) throw ApiException.conflict("LAST_ADMIN", "workspace needs at least one admin");
    jdbc.sql("DELETE FROM workspace_members WHERE workspace_id = ? AND user_id = ?").params(ws, uid).update();
    auditMember(ctx, uid, "member_removed", "role=" + cur);
    return ResponseEntity.noContent().build();
  }

  /**
   * 회사 관리자가 자기 회사의 부서에 일반 멤버로 들어간다. 부서 헤더는 그 부서의 멤버만 통과하므로 헤더 없이 받는다.
   * 멤버 관리는 속한 부서에서만 되므로(requireMemberAdmin) 속하지 않은 부서는 먼저 들어간 뒤 같은 규칙으로 관리한다. 이미 멤버면 아무것도 하지 않는다.
   */
  @PostMapping("/workspaces/{ws}/join")
  @Transactional
  public ResponseEntity<Void> join(@PathVariable long ws) {
    Actor actor = companyActor();
    if (!jdbc.sql("SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = ? AND tenant_id = ? AND kind = 'dept')").params(ws, actor.tenantId())
        .query(Boolean.class).single()) throw ApiException.notFound("workspace");   // 다른 회사의 부서 · 부서가 아닌 곳 · 없는 번호는 같은 404
    int added = jdbc.sql("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, 'member') ON CONFLICT DO NOTHING")
        .params(ws, actor.id()).update();
    if (added > 0)
      jdbc.sql("""
            INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason)
            VALUES (?, ?, 'users', ?, 'add_member', ?, 'ui', 'role=member (company admin joined)')""")
          .params(actor.tenantId(), ws, String.valueOf(actor.id()), actor.name()).update();
    return ResponseEntity.noContent().build();
  }

  /** 같은 부서의 역할 변경 · 제외를 줄 세운다 — 관리자 둘이 서로를 동시에 강등해 admin 이 0명이 되는 틈을 막는다. */
  private void lockMembers(long ws) {
    jdbc.sql("SELECT pg_advisory_xact_lock(?, ?)").params(7, (int) ws).query(Object.class).optional();
  }

  /**
   * 대상 계정의 행을 공유 잠금(FOR SHARE) — 그 계정의 비활성화(status() 의 FOR UPDATE)와 줄을 세운다. 비활성화가 대상이 admin 인 부서를
   * 조회한 뒤에 대상이 다른 부서의 admin 으로 올라가고 기존 admin 이 내려가면, 그 부서는 확인 없이 admin 0명이 된다.
   * 잠그는 순서는 어디서나 계정 → 부서(lockMembers) — 순서가 같아야 서로 기다리다 멈추지 않는다.
   * 같은 회사의 계정만 잠근다 — 없는 번호 · 다른 회사의 계정은 아무것도 잠그지 않고 뒤에서 404.
   */
  private void lockUser(long uid, long tenantId) {
    jdbc.sql("SELECT 1 FROM users WHERE id = ? AND tenant_id = ? FOR SHARE").params(uid, tenantId).query(Integer.class).optional();
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
