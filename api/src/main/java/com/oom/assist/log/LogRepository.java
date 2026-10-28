package com.oom.assist.log;

import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** 로그·간선 SQL. 읽기는 v_log_nodes 뷰, 그래프 판정은 DB 함수. 범위는 항상 workspace_id 로 제한한다. */
@Repository
public class LogRepository {

  private static final String VIEW_COLS = """
      id, workspace_id, type, status, title, body, author_name, path_label, error_code, keywords, resolution_summary,
      refine_status, last_confirmed_at, deprecated_via, deprecate_reason, resolved_at, resolve_note, root_id, depth,
      category, tags, created_at
      """;

  private final JdbcClient jdbc;

  public LogRepository(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public Optional<LogNodeDto> find(long ws, long id) {
    return jdbc.sql("SELECT " + VIEW_COLS + " FROM v_log_nodes WHERE workspace_id = ? AND id = ?")
        .params(ws, id).query(LogRepository::map).optional();
  }

  public record ListFilter(String type, String q, String status, int limit) {}

  public List<LogNodeDto> list(long ws, ListFilter f) {
    StringBuilder sql = new StringBuilder("SELECT " + VIEW_COLS + " FROM v_log_nodes WHERE workspace_id = ?");
    java.util.List<Object> params = new java.util.ArrayList<>();
    params.add(ws);
    if (f.type() != null && !f.type().isBlank() && !"all".equals(f.type())) { sql.append(" AND type = ?"); params.add(f.type()); }
    if (f.status() != null && !f.status().isBlank() && !"all".equals(f.status())) { sql.append(" AND status = ?"); params.add(f.status()); }
    if (f.q() != null && !f.q().isBlank()) {
      sql.append(" AND (title ILIKE ? OR coalesce(error_code, '') ILIKE ? OR EXISTS (SELECT 1 FROM unnest(keywords) k WHERE k ILIKE ?))");
      String like = "%" + f.q().trim() + "%";
      params.add(like); params.add(like); params.add(like);
    }
    sql.append(" ORDER BY created_at DESC, id DESC");
    if (f.limit() > 0) { sql.append(" LIMIT ?"); params.add(f.limit()); }
    return jdbc.sql(sql.toString()).params(params).query(LogRepository::map).list();
  }

  /** counts 는 active 만 (프론트 계약). */
  public Map<String, Integer> counts(long ws) {
    Map<String, Object> r = jdbc.sql("""
        SELECT count(*) AS all_cnt,
               count(*) FILTER (WHERE type = 'fail') AS fail_cnt,
               count(*) FILTER (WHERE type = 'success') AS success_cnt,
               count(*) FILTER (WHERE type = 'ref') AS ref_cnt
        FROM log_nodes WHERE workspace_id = ? AND status = 'active'
        """).param(ws).query().singleRow();
    return Map.of("all", n(r, "all_cnt"), "fail", n(r, "fail_cnt"), "success", n(r, "success_cnt"), "ref", n(r, "ref_cnt"));
  }

  public int weeklyCount(long ws) {
    return jdbc.sql("SELECT count(*) FROM log_nodes WHERE workspace_id = ? AND created_at >= now() - interval '7 days'")
        .param(ws).query(Integer.class).single();
  }

  public long insertNode(long ws, String type, String title, String body, long authorId, Integer composeMs, String source) {
    return jdbc.sql("""
        INSERT INTO log_nodes (workspace_id, type, title, body, author_id, compose_ms, source)
        VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id
        """).params(ws, type, title, body, authorId, composeMs, source).query(Long.class).single();
  }

  public void insertEdge(long ws, long parent, long child, long createdBy, String origin) {
    jdbc.sql("INSERT INTO log_edges (workspace_id, parent_id, child_id, created_by, origin) VALUES (?, ?, ?, ?, ?)")
        .params(ws, parent, child, createdBy, origin).update();
  }

  public int deleteEdge(long ws, long parent, long child) {
    return jdbc.sql("DELETE FROM log_edges WHERE workspace_id = ? AND parent_id = ? AND child_id = ?").params(ws, parent, child).update();
  }

  public void lockWorkspace(long ws) {
    jdbc.sql("SELECT pg_advisory_xact_lock(?)").param(ws).query(Object.class).optional();
  }

  public boolean wouldCreateCycle(long parent, long child) {
    return jdbc.sql("SELECT would_create_cycle(?, ?)").params(parent, child).query(Boolean.class).single();
  }

  public boolean isRoot(long id) {
    return jdbc.sql("SELECT NOT EXISTS (SELECT 1 FROM log_edges WHERE child_id = ?)").param(id).query(Boolean.class).single();
  }

  /** 루트 아래(모든 경로) 활성 success 가 있는가 — R4 판정. */
  public boolean hasActiveSuccessDescendant(long ws, long root) {
    return jdbc.sql("""
        SELECT EXISTS (
          SELECT 1 FROM get_chain(?, ?, 32, 100000) g JOIN log_nodes n ON n.id = g.node_id
          WHERE g.depth > 0 AND n.type = 'success' AND n.status = 'active')
        """).params(ws, root).query(Boolean.class).single();
  }

  public List<Long> orphanedDescendants(long id) {
    return jdbc.sql("SELECT orphaned_descendants(?)").param(id).query(Long.class).list();
  }

  public void setResolved(long id, OffsetDateTime at, String note) {
    jdbc.sql("UPDATE log_nodes SET resolved_at = ?, resolve_note = ? WHERE id = ?").params(at, note, id).update();
  }

  public void setDeprecated(List<Long> ids, String reason, Long via) {
    if (ids.isEmpty()) return;
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated', deprecated_at = now(), deprecate_reason = ?, deprecated_via = ? WHERE id = ANY (?)")
        .params(reason, via, ids.toArray(Long[]::new)).update();
  }

  public List<Long> restoreCascade(long via) {
    return jdbc.sql("UPDATE log_nodes SET status = 'active', deprecated_at = NULL, deprecate_reason = NULL, deprecated_via = NULL "
        + "WHERE deprecated_via = ? AND status = 'deprecated' RETURNING id").param(via).query(Long.class).list();
  }

  public void restore(long id) {
    jdbc.sql("UPDATE log_nodes SET status = 'active', deprecated_at = NULL, deprecate_reason = NULL, deprecated_via = NULL WHERE id = ?").param(id).update();
  }

  public void confirm(long id) {
    jdbc.sql("UPDATE log_nodes SET last_confirmed_at = now() WHERE id = ?").param(id).update();
  }

  public void requeueRefine(long id) {
    jdbc.sql("UPDATE log_nodes SET refine_status = 'pending', refine_attempts = 0, refine_error = NULL WHERE id = ?").param(id).update();
  }

  public void audit(long tenant, long ws, String table, String rowId, String action, String actor, String reason, String label) {
    jdbc.sql("INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label) VALUES (?, ?, ?, ?, ?, ?, 'ui', ?, ?)")
        .params(tenant, ws, table, rowId, action, actor, reason, label).update();
  }

  static LogNodeDto map(ResultSet rs, int i) throws SQLException {
    return new LogNodeDto(
        rs.getLong("id"), rs.getLong("workspace_id"), rs.getString("type"), rs.getString("status"), rs.getString("title"), rs.getString("body"),
        rs.getString("author_name"), rs.getString("path_label"), rs.getString("error_code"), textArray(rs.getArray("keywords")),
        rs.getString("resolution_summary"), rs.getString("refine_status"), rs.getObject("last_confirmed_at", OffsetDateTime.class),
        rs.getObject("deprecated_via", Long.class), rs.getString("deprecate_reason"), rs.getObject("resolved_at", OffsetDateTime.class),
        rs.getString("resolve_note"), rs.getObject("root_id", Long.class), rs.getObject("depth", Integer.class),
        rs.getString("category"), textArray(rs.getArray("tags")), rs.getObject("created_at", OffsetDateTime.class));
  }

  private static List<String> textArray(Array a) throws SQLException {
    if (a == null) return List.of();
    Object[] arr = (Object[]) a.getArray();
    return Arrays.stream(arr).map(String::valueOf).toList();
  }

  private static int n(Map<String, Object> r, String k) {
    return ((Number) r.get(k)).intValue();
  }
}
