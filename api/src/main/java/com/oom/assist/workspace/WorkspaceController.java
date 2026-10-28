package com.oom.assist.workspace;

import com.oom.assist.auth.CurrentUser;
import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotNull;
import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class WorkspaceController {

  private final JdbcClient jdbc;

  public WorkspaceController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public record WorkspaceDto(long id, String name, boolean policyEnabled, Long parentId, String kind) {}
  public record PatchRequest(@NotNull Boolean policyEnabled) {}

  /** 로그인 전에도 호출된다(앱 셸·로그인 화면). 로그인 상태면 소속 워크스페이스만, 아니면 부서 목록 전체(데모). */
  @GetMapping("/workspaces")
  public List<WorkspaceDto> list() {
    return CurrentUser.id()
        .map(uid -> jdbc.sql("""
              SELECT w.id, w.name, w.policy_enabled, w.parent_id, w.kind FROM workspaces w
              JOIN workspace_members m ON m.workspace_id = w.id AND m.user_id = ? ORDER BY w.id
              """).param(uid).query(this::map).list())
        .orElseGet(() -> jdbc.sql("SELECT id, name, policy_enabled, parent_id, kind FROM workspaces WHERE kind = 'dept' ORDER BY id")
            .query(this::map).list());
  }

  /** 모드 토글. 해당 워크스페이스의 admin 만. */
  @PatchMapping("/workspaces/{id}")
  public WorkspaceDto patch(@PathVariable long id, @Valid @RequestBody PatchRequest body, TenantContext ctx) {
    if (ctx.workspaceId() != id) throw ApiException.notFound("workspace");
    if (!ctx.isAdmin()) throw ApiException.forbidden("admin only");
    jdbc.sql("UPDATE workspaces SET policy_enabled = ? WHERE id = ?").params(body.policyEnabled(), id).update();
    return jdbc.sql("SELECT id, name, policy_enabled, parent_id, kind FROM workspaces WHERE id = ?").param(id).query(this::map).single();
  }

  private WorkspaceDto map(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
    long parent = rs.getLong("parent_id");
    return new WorkspaceDto(rs.getLong("id"), rs.getString("name"), rs.getBoolean("policy_enabled"), rs.wasNull() ? null : parent, rs.getString("kind"));
  }
}
