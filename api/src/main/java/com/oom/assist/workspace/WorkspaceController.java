package com.oom.assist.workspace;

import com.oom.assist.auth.CurrentUser;
import com.oom.assist.common.error.ApiException;
import jakarta.validation.Valid;
import java.util.ArrayList;
import java.util.List;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class WorkspaceController {

  /** suggestions = 첫 화면 추천 질문(settings.suggestions). 부서마다 규정·사례가 달라 화면이 고정 문구를 쓰면 근거 없는 질문이 뜬다. */
  private static final String COLS = """
      w.id, w.name, w.policy_enabled, w.case_enabled, w.parent_id, w.kind,
      ARRAY(SELECT jsonb_array_elements_text(CASE WHEN jsonb_typeof(w.settings->'suggestions') = 'array'
                                                  THEN w.settings->'suggestions' ELSE '[]'::jsonb END)) AS suggestions""";
  /** 부서 설정 권한: 그 부서 또는 상위(전사 공통)의 admin. workspace_ancestors 는 자기 자신을 포함한다. */
  private static final String CAN_CONFIGURE = """
      EXISTS (SELECT 1 FROM workspace_members m WHERE m.user_id = ? AND m.role = 'admin'
              AND m.workspace_id IN (SELECT workspace_ancestors(w.id)))""";

  private final JdbcClient jdbc;
  /** 로그인 전 화면에 부서를 보여 줄 데모 회사(tenants.slug). 비어 있으면 로그인 전에는 아무 부서도 주지 않는다. */
  private String publicTenant;

  public WorkspaceController(JdbcClient jdbc, @Value("${oom.public-tenant:}") String publicTenant) {
    this.jdbc = jdbc;
    this.publicTenant = publicTenant;
  }

  public record WorkspaceDto(long id, String name, boolean policyEnabled, boolean caseEnabled, Long parentId, String kind, List<String> suggestions) {}
  /** 설정 화면의 한 행. policyDocs = 이 부서에서 검색되는 활성 규정 수(상위 포함), cases = 이 부서의 활성 사례 수. */
  public record ManageableDto(long id, String name, boolean policyEnabled, boolean caseEnabled, Long parentId, long policyDocs, long cases) {}
  public record PatchRequest(Boolean policyEnabled, Boolean caseEnabled) {}

  /** 로그인 전에도 호출된다(앱 셸·로그인 화면). 로그인 상태면 소속 부서만. 로그인 전에는 데모 회사(oom.public-tenant)의 부서만 —
   *  설정이 없으면 빈 목록. 다른 회사의 조직 구조가 로그인 없이 드러나지 않게 한다. */
  @GetMapping("/workspaces")
  public List<WorkspaceDto> list() {
    return CurrentUser.id()
        .map(uid -> jdbc.sql("SELECT " + COLS + """
               FROM workspaces w
              JOIN workspace_members m ON m.workspace_id = w.id AND m.user_id = ? WHERE w.kind = 'dept' ORDER BY w.id
              """).param(uid).query(this::map).list())
        .orElseGet(() -> publicTenant == null || publicTenant.isBlank() ? List.of()
            : jdbc.sql("SELECT " + COLS + " FROM workspaces w WHERE w.kind = 'dept' AND w.tenant_id = (SELECT id FROM tenants WHERE slug = ?) ORDER BY w.id")
                .param(publicTenant).query(this::map).list());
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

  /** 부서 유형 설정: 규정 사용·사례 사용. 보낸 항목만 바꾼다. 둘 다 끌 수는 없다. 변경은 감사 로그에 남는다.
   *  목록(manageable)과 같이 로그인한 사용자만으로 판단한다 — X-Workspace-Id 가 없거나, 회사 관리자가 소속하지 않은 하위 부서를 고쳐도 된다. */
  @PatchMapping("/workspaces/{id}")
  @Transactional
  public WorkspaceDto patch(@PathVariable long id, @Valid @RequestBody PatchRequest body) {
    long uid = CurrentUser.id().orElseThrow(ApiException::unauthorized);
    if (body.policyEnabled() == null && body.caseEnabled() == null) {
      throw ApiException.badRequest("VALIDATION", "policy_enabled 또는 case_enabled 가 필요합니다");
    }
    // 권한은 그 부서 또는 조상의 admin 소속으로 본다. 다른 회사의 부서에는 그런 소속이 있을 수 없다
    boolean allowed = jdbc.sql("SELECT EXISTS (SELECT 1 FROM workspaces w WHERE w.id = ? AND w.kind = 'dept' AND " + CAN_CONFIGURE + ")")
        .params(id, uid).query(Boolean.class).single();
    if (!allowed) {
      // 자기 부서면 권한 부족, 그 밖의 부서는 존재를 숨긴다 (R1)
      boolean member = jdbc.sql("SELECT EXISTS (SELECT 1 FROM workspace_members m JOIN workspaces w ON w.id = m.workspace_id AND w.kind = 'dept' WHERE m.workspace_id = ? AND m.user_id = ?)")
          .params(id, uid).query(Boolean.class).single();
      if (member) throw ApiException.forbidden("admin only");
      throw ApiException.notFound("workspace");
    }
    WorkspaceDto cur = jdbc.sql("SELECT " + COLS + " FROM workspaces w WHERE w.id = ? FOR UPDATE").param(id).query(this::map).single();
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
          VALUES ((SELECT tenant_id FROM workspaces WHERE id = ?), ?, 'workspaces', ?, 'settings_changed', (SELECT name FROM users WHERE id = ?), 'ui', ?, ?,
                  jsonb_build_object('policy_enabled', ?, 'case_enabled', ?))""")
        .params(id, id, String.valueOf(id), uid, String.join(", ", changes), cur.name(), cur.policyEnabled(), cur.caseEnabled())
        .update();
    return new WorkspaceDto(cur.id(), cur.name(), policy, cases, cur.parentId(), cur.kind(), cur.suggestions());
  }

  private WorkspaceDto map(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
    long parent = rs.getLong("parent_id");
    Long parentId = rs.wasNull() ? null : parent;
    java.sql.Array sug = rs.getArray("suggestions");
    return new WorkspaceDto(rs.getLong("id"), rs.getString("name"), rs.getBoolean("policy_enabled"), rs.getBoolean("case_enabled"),
        parentId, rs.getString("kind"), sug == null ? List.of() : List.of((String[]) sug.getArray()));
  }
}
