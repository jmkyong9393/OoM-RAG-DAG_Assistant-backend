package com.oom.assist.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import org.junit.jupiter.api.Test;

/** Flyway V1~V3 + R__ 가 적용되어 객체·역할·권한이 설계대로 존재하는지. */
class SchemaMigrationTest extends AbstractDbTest {

  @Test
  void tablesExist() {
    List<String> tables = jdbc.sql("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' AND table_name <> 'flyway_schema_history'")
        .query(String.class).list();
    assertThat(tables).containsExactlyInAnyOrder(
        "tenants", "workspaces", "users", "workspace_members",
        "log_nodes", "log_edges", "policy_documents", "policy_chunks",
        "search_sessions", "answer_feedback", "workspace_terms", "eval_runs", "audit_log",
        "spring_session", "spring_session_attributes");   // V4 세션 저장소
  }

  @Test
  void functionsAndViewsExist() {
    List<String> fns = jdbc.sql("SELECT proname FROM pg_proc WHERE proname = ANY (?)")
        .param(new String[] {"workspace_ancestors", "or_tsquery", "hybrid_search_chunks", "hybrid_search_nodes",
            "chain_at_depth", "get_chain", "would_create_cycle", "orphaned_descendants", "purge_document"})
        .query(String.class).list();
    assertThat(fns).hasSize(9);
    List<String> views = jdbc.sql("SELECT table_name FROM information_schema.views WHERE table_name LIKE 'v\\_%'").query(String.class).list();
    assertThat(views).containsExactlyInAnyOrder("v_log_nodes", "v_policy_documents", "v_search_sessions");
  }

  @Test
  void generatedTsvAndVectorColumns() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "d1-a1-0", "제1조", "본문", "접속 신청 기준", null);
    String tsv = jdbc.sql("SELECT tsv::text FROM policy_chunks WHERE id = 'd1-a1-0'").query(String.class).single();
    assertThat(tsv).contains("'접속'").contains("'신청'").contains("'기준'");
    int dim = jdbc.sql("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'policy_chunks'::regclass AND attname = 'embedding'").query(Integer.class).single();
    assertThat(dim).isEqualTo(1536);
  }

  @Test
  void rolesExistWithColumnLevelGrants() {
    List<String> roles = jdbc.sql("SELECT rolname FROM pg_roles WHERE rolname IN ('oom_api','oom_ai')").query(String.class).list();
    assertThat(roles).containsExactlyInAnyOrder("oom_api", "oom_ai");
    // Python: 정제 컬럼만 UPDATE, 제목·상태는 불가, 삭제는 청크만
    assertThat(priv("oom_ai", "log_nodes", "embedding", "UPDATE")).isTrue();
    assertThat(priv("oom_ai", "log_nodes", "title", "UPDATE")).isFalse();
    assertThat(priv("oom_ai", "log_nodes", "status", "UPDATE")).isFalse();
    assertThat(tablePriv("oom_ai", "policy_chunks", "DELETE")).isTrue();
    assertThat(tablePriv("oom_ai", "log_nodes", "DELETE")).isFalse();
    // Spring: 상태·수명 컬럼 UPDATE, 정제 결과는 불가, 로그 삭제 불가, 간선·피드백 삭제 가능
    assertThat(priv("oom_api", "log_nodes", "status", "UPDATE")).isTrue();
    assertThat(priv("oom_api", "log_nodes", "embedding", "UPDATE")).isFalse();
    assertThat(tablePriv("oom_api", "log_nodes", "DELETE")).isFalse();
    assertThat(tablePriv("oom_api", "log_edges", "DELETE")).isTrue();
    assertThat(tablePriv("oom_api", "policy_documents", "DELETE")).isFalse();
    // R__ 로 나중에 만든 뷰·함수에도 기본 권한이 적용된다
    assertThat(tablePriv("oom_api", "v_log_nodes", "SELECT")).isTrue();
    assertThat(jdbc.sql("SELECT has_function_privilege('oom_ai', 'get_chain(bigint,bigint,int,int)', 'EXECUTE')").query(Boolean.class).single()).isTrue();
  }

  private boolean priv(String role, String table, String col, String p) {
    return jdbc.sql("SELECT has_column_privilege(?, ?::regclass, ?, ?)").params(role, table, col, p).query(Boolean.class).single();
  }

  private boolean tablePriv(String role, String table, String p) {
    return jdbc.sql("SELECT has_table_privilege(?, ?::regclass, ?)").params(role, table, p).query(Boolean.class).single();
  }
}
