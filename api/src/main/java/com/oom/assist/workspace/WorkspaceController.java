package com.oom.assist.workspace;

import com.oom.assist.auth.CurrentUser;
import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import jakarta.validation.Valid;
import java.util.ArrayList;
import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class WorkspaceController {

  private static final String COLS = "id, name, policy_enabled, case_enabled, parent_id, kind";
  /** 부서 설정 권한: 그 부서 또는 상위(전사 공통)의 admin. workspace_ancestors 는 자기 자신을 포함한다. */
  private static final String CAN_CONFIGURE = """
      EXISTS (SELECT 1 FROM workspace_members m WHERE m.user_id = ? AND m.role = 'admin'
              AND m.workspace_id IN (SELECT workspace_ancestors(w.id)))""";

  private final JdbcClient jdbc;

  public WorkspaceController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public record WorkspaceDto(long id, String name, boolean policyEnabled, boolean caseEnabled, Long parentId, String kind) {}
  /** 설정 화면의 한 행. policyDocs = 이 부서에서 검색되는 활성 규정 수(상위 포함), cases = 이 부서의 활성 사례 수. */
  public record ManageableDto(long id, String name, boolean policyEnabled, boolean caseEnabled, Long parentId, long policyDocs, long cases) {}
  public record PatchRequest(Boolean policyEnabled, Boolean caseEnabled) {}

  /** 로그인 전에도 호출된다(앱 셸·로그인 화면). 로그인 상태면 소속 부서만, 아니면 부서 목록 전체(데모). */
  @GetMapping("/workspaces")
  public List<WorkspaceDto> list() {
    return CurrentUser.id()
        .map(uid -> jdbc.sql("""
              SELECT w.id, w.name, w.policy_enabled, w.case_enabled, w.parent_id, w.kind FROM workspaces w
              JOIN workspace_members m ON m.workspace_id = w.id AND m.user_id = ? WHERE w.kind = 'dept' ORDER BY w.id
              """).param(uid).query(this::map).list())
        .orElseGet(() -> jdbc.sql("SELECT " + COLS + " FROM workspaces WHERE kind = 'dept' ORDER BY id").query(this::map).list());
  }

  /** 설정을 바꿀 수 있는 부서. 부서 관리자는 자기 부서, 회사 관리자(전사 공통의 admin)는 회사의 모든 부서. 없으면 빈 목록. */
  @GetMapping("/workspaces/manageable")
  public List<ManageableDto> manageable() {
    long uid = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    return jdbc.sql("""
          SELECT w.id, w.name, w.policy_enabled, w.case_enabled, w.parent_id,
                 (SELECT count(*) FROM policy_documents d WHERE d.status = 'active' AND d.workspace_id IN (SELECT workspace_ancestors(w.id))) AS policy_docs,
                 (SELECT count(*) FROM log_nodes n WHERE n.workspace_id = w.id AND n.status = 'active') AS cases
          FROM workspaces w WHERE w.kind = 'dept'""" + " AND " + CAN_CONFIGURE + " ORDER BY w.id").param(uid)
        .query((rs, i) -> {
          long parent = rs.getLong("parent_id");
          return new ManageableDto(rs.getLong("id"), rs.getString("name"), rs.getBoolean("policy_enabled"), rs.getBoolean("case_enabled"),
              rs.wasNull() ? null : parent, rs.getLong("policy_docs"), rs.getLong("cases"));
        }).list();
  }

  /** 부서 유형 설정: 규정 사용·사례 사용. 보낸 항목만 바꾼다. 둘 다 끌 수는 없다. 변경은 감사 로그에 남는다. */
  @PatchMapping("/workspaces/{id}")
  @Transactional
  public WorkspaceDto patch(@PathVariable long id, @Valid @RequestBody PatchRequest body, TenantContext ctx) {
    if (body.policyEnabled() == null && body.caseEnabled() == null) {
      throw ApiException.badRequest("VALIDATION", "policy_enabled 또는 case_enabled 가 필요합니다");
    }
    boolean allowed = jdbc.sql("SELECT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = ? AND w.tenant_id = ? AND w.kind = 'dept' AND " + CAN_CONFIGURE + ")")
        .params(id, ctx.tenantId(), ctx.userId()).query(Boolean.class).single();
    if (!allowed) {
      // 자기 부서면 권한 부족, 그 밖의 부서는 존재를 숨긴다 (R1)
      if (ctx.workspaceId() == id) throw ApiException.forbidden("admin only");
      throw ApiException.notFound("workspace");
    }
    WorkspaceDto cur = jdbc.sql("SELECT " + COLS + " FROM workspaces WHERE id = ? FOR UPDATE").param(id).query(this::map).single();
    boolean policy = body.policyEnabled() != null ? body.policyEnabled() : cur.policyEnabled();
    boolean cases = body.caseEnabled() != null ? body.caseEnabled() : cur.caseEnabled();
    if (!policy && !cases) throw ApiException.badRequest("NO_SOURCE", "규정과 사례를 모두 끌 수는 없습니다");
    List<String> changes = new ArrayList<>();
    if (policy != cur.policyEnabled()) changes.add("policy_enabled: " + cur.policyEnabled() + " → " + policy);
    if (cases != cur.caseEnabled()) changes.add("case_enabled: " + cur.caseEnabled() + " → " + cases);
    if (changes.isEmpty()) return cur;
    jdbc.sql("UPDATE workspaces SET policy_enabled = ?, case_enabled = ? WHERE id = ?").params(policy, cases, id).update();
    jdbc.sql("""
          INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label, before)
          VALUES (?, ?, 'workspaces', ?, 'settings_changed', ?, 'ui', ?, ?, jsonb_build_object('policy_enabled', ?, 'case_enabled', ?))""")
        .params(ctx.tenantId(), id, String.valueOf(id), ctx.userName(), String.join(", ", changes), cur.name(), cur.policyEnabled(), cur.caseEnabled())
        .update();
    return new WorkspaceDto(cur.id(), cur.name(), policy, cases, cur.parentId(), cur.kind());
  }

  private WorkspaceDto map(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
    long parent = rs.getLong("parent_id");
    return new WorkspaceDto(rs.getLong("id"), rs.getString("name"), rs.getBoolean("policy_enabled"), rs.getBoolean("case_enabled"),
        rs.wasNull() ? null : parent, rs.getString("kind"));
  }
}
