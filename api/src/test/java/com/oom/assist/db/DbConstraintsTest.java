package com.oom.assist.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.OffsetDateTime;
import org.junit.jupiter.api.Test;

/** 트리거·제약: 설계 규칙이 DB에서 강제되는지 한 건씩. */
class DbConstraintsTest extends AbstractDbTest {

  @Test
  void workspaceParentMustBeSameTenant() {
    long other = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('o', 'Other') RETURNING id").query(Long.class).single();
    assertFails(() -> workspace(other, company, "X", false), "WORKSPACE_CROSS_TENANT");
  }

  @Test
  void workspaceCannotBeItsOwnParent() {
    assertFails(() -> jdbc.sql("UPDATE workspaces SET parent_id = id WHERE id = ?").param(ws).update(), "WORKSPACE_SELF_PARENT");
  }

  @Test
  void edgeRejectsCrossWorkspace() {
    long other = workspace(tenant, company, "HR", true);
    long a = node(ws, "fail", "A", null);
    long b = node(other, "ref", "B", null);
    assertFails(() -> edge(a, b), "EDGE_CROSS_WORKSPACE");
  }

  @Test
  void edgeRejectsSuccessUnderSuccess() {
    long s1 = node("success", "A", null);
    long s2 = node("success", "B", null);
    assertFails(() -> edge(s1, s2), "EDGE_PARENT_TYPE");
  }

  @Test
  void edgeRejectsSelfLoopAndDuplicate() {
    long a = node("fail", "A", null);
    long b = node("ref", "B", null);
    assertFails(() -> edge(a, a), "check");
    edge(a, b);
    assertFails(() -> edge(a, b), "duplicate");
  }

  @Test
  void nodeTypeStatusAndRefineStatusAreChecked() {
    assertFails(() -> node("bug", "X", null), "check");
    long n = node("fail", "X", null);
    assertFails(() -> jdbc.sql("UPDATE log_nodes SET status = 'gone' WHERE id = ?").param(n).update(), "check");
    assertFails(() -> jdbc.sql("UPDATE log_nodes SET refine_status = 'queued' WHERE id = ?").param(n).update(), "check");
  }

  @Test
  void updatedAtIsTouchedOnUpdate() {
    long n = node("fail", "X", null);
    OffsetDateTime before = jdbc.sql("SELECT updated_at FROM log_nodes WHERE id = ?").param(n).query(OffsetDateTime.class).single();
    jdbc.sql("UPDATE log_nodes SET title = 'Y', updated_at = '2000-01-01' WHERE id = ?").param(n).update();
    OffsetDateTime after = jdbc.sql("SELECT updated_at FROM log_nodes WHERE id = ?").param(n).query(OffsetDateTime.class).single();
    assertThat(after).isAfterOrEqualTo(before); // 트리거가 now()로 덮어쓴다
  }

  @Test
  void documentSlugIsGloballyUnique() {
    long other = workspace(tenant, company, "HR", true);
    document(ws, "vpn-guide", "VPN", "v1", "active");
    assertFails(() -> document(other, "vpn-guide", "VPN", "v1", "active"), "duplicate");
  }

  @Test
  void chunkWorkspaceIsFilledFromDocumentAndMustMatch() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "d1-a1-0", "제1조", "본문", null, null);
    long filled = jdbc.sql("SELECT workspace_id FROM policy_chunks WHERE id = 'd1-a1-0'").query(Long.class).single();
    assertThat(filled).isEqualTo(ws);
    assertFails(() -> jdbc.sql("INSERT INTO policy_chunks (id, doc_id, workspace_id, article_label, text) VALUES ('d1-a1-1', ?, ?, 'x', 'y')")
        .params(doc, company).update(), "CHUNK_WORKSPACE_MISMATCH");
  }

  @Test
  void chunksCascadeWhenDocumentDeleted() {
    long doc = document(ws, "d1", "D", "v1", "retired");
    chunk(doc, "d1-a1-0", "제1조", "본문", null, null);
    jdbc.sql("DELETE FROM policy_documents WHERE id = ?").param(doc).update();
    assertThat(jdbc.sql("SELECT count(*) FROM policy_chunks WHERE doc_id = ?").param(doc).query(Long.class).single()).isZero();
  }

  @Test
  void sessionDefaultsAndFeedbackUniqueness() {
    long s = jdbc.sql("INSERT INTO search_sessions (workspace_id, query, mode) VALUES (?, 'q', 'dag_only') RETURNING id").param(ws).query(Long.class).single();
    assertThat(jdbc.sql("SELECT status FROM search_sessions WHERE id = ?").param(s).query(String.class).single()).isEqualTo("streaming");
    jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, 1)").param(s).update();
    assertFails(() -> jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, -1)").param(s).update(), "duplicate");
    assertFails(() -> jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, 0)").param(s).update(), "check");
  }

  @Test
  void userEmailUniquePerTenantButNullAllowedTwice() {
    jdbc.sql("INSERT INTO users (tenant_id, name, email) VALUES (?, 'a', 'x@y')").param(tenant).update();
    assertFails(() -> jdbc.sql("INSERT INTO users (tenant_id, name, email) VALUES (?, 'b', 'x@y')").param(tenant).update(), "duplicate");
    jdbc.sql("INSERT INTO users (tenant_id, name) VALUES (?, 'c')").param(tenant).update();
    jdbc.sql("INSERT INTO users (tenant_id, name) VALUES (?, 'd')").param(tenant).update();
  }
}
