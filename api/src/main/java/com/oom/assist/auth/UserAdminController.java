package com.oom.assist.auth;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import jakarta.validation.Valid;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import java.security.SecureRandom;
import java.util.List;
import java.util.Optional;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * 계정은 관리자 초대로만 만든다(자가 가입 없음). 현재 워크스페이스의 admin 이 이메일로 초대하면
 * 같은 테넌트에 사용자를 만들고(임시 비밀번호 1회 반환, must_change_password) 그 워크스페이스 멤버로 넣는다.
 * 이미 테넌트에 있는 이메일이면 멤버십만 추가한다.
 */
@RestController
public class UserAdminController {

  private static final String ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  private final JdbcClient jdbc;
  private final PasswordEncoder encoder;
  private final SecureRandom random = new SecureRandom();

  public UserAdminController(JdbcClient jdbc, PasswordEncoder encoder) {
    this.jdbc = jdbc;
    this.encoder = encoder;
  }

  public record InviteRequest(@NotBlank @Email String email, @NotBlank String name, String title, String role) {}
  public record InviteResponse(long id, String email, String name, long workspaceId, String role, boolean created, String tempPassword) {}
  public record MemberDto(long id, String email, String name, String title, String role) {}

  @PostMapping("/users")
  @Transactional
  public ResponseEntity<InviteResponse> invite(@Valid @RequestBody InviteRequest body, TenantContext ctx) {
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    String role = body.role() == null ? "member" : body.role();
    if (!role.equals("member") && !role.equals("admin")) throw ApiException.badRequest("VALIDATION", "role must be member|admin");
    String email = body.email().trim().toLowerCase();

    Optional<Long> existing = jdbc.sql("SELECT id FROM users WHERE tenant_id = ? AND lower(email) = ?").params(ctx.tenantId(), email)
        .query(Long.class).optional();
    boolean created = existing.isEmpty();
    String temp = null;
    long userId;
    if (created) {
      if (jdbc.sql("SELECT count(*) FROM users WHERE lower(email) = ?").param(email).query(Long.class).single() > 0)
        throw ApiException.conflict("EMAIL_TAKEN", "email already used in another tenant");
      temp = tempPassword();
      userId = jdbc.sql("""
            INSERT INTO users (tenant_id, name, title, email, password_hash, must_change_password)
            VALUES (?, ?, ?, ?, ?, true) RETURNING id""")
          .params(ctx.tenantId(), body.name().trim(), body.title(), email, encoder.encode(temp)).query(Long.class).single();
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
        .body(new InviteResponse(userId, email, body.name().trim(), ctx.workspaceId(), role, created, temp));
  }

  /** 현재 워크스페이스 멤버 목록 (초대 화면·작성자 선택용). */
  @GetMapping("/users")
  public List<MemberDto> members(TenantContext ctx) {
    return jdbc.sql("""
          SELECT u.id, u.email, u.name, u.title, m.role FROM workspace_members m JOIN users u ON u.id = m.user_id
          WHERE m.workspace_id = ? AND u.status = 'active' ORDER BY (m.role = 'admin') DESC, u.name""")
        .param(ctx.workspaceId())
        .query((rs, i) -> new MemberDto(rs.getLong("id"), rs.getString("email"), rs.getString("name"), rs.getString("title"), rs.getString("role"))).list();
  }

  private String tempPassword() {
    StringBuilder sb = new StringBuilder(14);
    for (int i = 0; i < 14; i++) sb.append(ALPHABET.charAt(random.nextInt(ALPHABET.length())));
    return sb.toString();
  }
}
