package com.oom.assist.document;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 규정 문서 관리: 목록 · 조문 · 이력 조회(부서 소속이면 누구나), 폐기 · 복원 · 삭제(문서가 속한 워크스페이스 또는 상위의 admin).
 * 범위는 질문 검색과 같다 — 이 부서와 상위(전사 공통)의 문서. 적재 · 개정은 스크립트(index_policy.py) 몫이다.
 */
@RestController
public class DocumentController {

  private static final int REASON_MAX = 300;
  private static final String COLS = """
      d.id, d.workspace_id, d.slug, d.title, d.version, d.doc_type, d.source_type, (d.file_path IS NOT NULL) AS has_file,
      d.status, d.retired_at, d.retire_reason, d.retired_by, d.superseded_by,
      CASE WHEN w.kind = 'dept' THEN 'dept' ELSE 'company' END AS visibility,
      (SELECT count(*) FROM policy_chunks c WHERE c.doc_id = d.id)::int AS chunk_count,
      d.indexed_at, d.uploaded_by, d.uploaded_at,
      EXISTS (SELECT 1 FROM workspace_members m WHERE m.user_id = ? AND m.role = 'admin'
              AND m.workspace_id IN (SELECT workspace_ancestors(d.workspace_id))) AS can_manage""";
  private static final String IN_SCOPE = "d.workspace_id IN (SELECT workspace_ancestors(?))";

  private final JdbcClient jdbc;

  public DocumentController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  /** approvedBy = 검수 승인자(업로드 검수가 없어 항상 null). supersededBy = 이 문서를 대체한 새 판. canManage = 요청자가 폐기 · 복원 · 삭제할 수 있는가. */
  public record DocumentDto(long id, long workspaceId, String slug, String title, String version, String docType, String sourceType, boolean hasFile,
                            String status, OffsetDateTime retiredAt, String retireReason, String retiredBy, Long supersededBy, String visibility,
                            int chunkCount, OffsetDateTime indexedAt, String uploadedBy, OffsetDateTime uploadedAt, String approvedBy, boolean canManage) {}
  public record ChunkDto(String id, String articleLabel, Integer page, String text) {}
  public record EventDto(long id, long workspaceId, Long documentId, String documentLabel, String action, String actor, String source, String reason, OffsetDateTime at) {}
  public record StatusRequest(String status, String reason) {}

  @GetMapping("/documents")
  public List<DocumentDto> list(@RequestParam(required = false) String status, TenantContext ctx) {
    if (status != null && !Set.of("active", "retired").contains(status)) throw ApiException.badRequest("VALIDATION", "status must be active or retired");
    return jdbc.sql("SELECT " + COLS + " FROM policy_documents d JOIN workspaces w ON w.id = d.workspace_id WHERE " + IN_SCOPE
            + " AND (?::text IS NULL OR d.status = ?) ORDER BY d.status, d.title, d.version, d.id")
        .params(ctx.userId(), ctx.workspaceId(), status, status).query(DocumentController::map).list();
  }

  @GetMapping("/documents/{id}")
  public DocumentDto get(@PathVariable long id, TenantContext ctx) {
    return find(id, ctx);
  }

  @GetMapping("/documents/{id}/chunks")
  public List<ChunkDto> chunks(@PathVariable long id, TenantContext ctx) {
    find(id, ctx);
    // 청크 id = "{slug}-{a|s…}{번호}-{순번}" — 문자열 정렬이면 제10조가 제2조 앞에 와서 번호를 숫자로 정렬한다
    return jdbc.sql("""
        SELECT id, article_label, page, text FROM policy_chunks WHERE doc_id = ?
        ORDER BY page NULLS LAST, substring(id FROM '-[a-z]+([0-9]+)-[0-9]+$')::int NULLS LAST, substring(id FROM '-([0-9]+)$')::int NULLS LAST, id
        """).param(id).query((rs, i) -> new ChunkDto(rs.getString("id"), rs.getString("article_label"), (Integer) rs.getObject("page"), rs.getString("text"))).list();
  }

  /** 문서 이력(적재 · 개정 대체 · 폐기 · 복원 · 삭제). 삭제된 문서도 label 로 남는다. document_id 를 주면 그 문서만. */
  @GetMapping("/documents/events")
  public List<EventDto> events(@RequestParam(name = "document_id", required = false) Long documentId, TenantContext ctx) {
    return jdbc.sql("""
        SELECT a.id, a.workspace_id, a.row_id, a.label, a.action, a.actor, a.source, a.reason, a.at FROM audit_log a
        WHERE a.table_name = 'policy_documents' AND a.workspace_id IN (SELECT workspace_ancestors(?))
          AND (?::text IS NULL OR a.row_id = ?::text)
        ORDER BY a.at DESC, a.id DESC LIMIT 500
        """).params(ctx.workspaceId(), documentId == null ? null : documentId.toString(), documentId == null ? null : documentId.toString())
        .query((rs, i) -> new EventDto(rs.getLong("id"), rs.getLong("workspace_id"), parseId(rs.getString("row_id")), rs.getString("label"),
            rs.getString("action"), rs.getString("actor"), rs.getString("source"), rs.getString("reason"), rs.getObject("at", OffsetDateTime.class)))
        .list();
  }

  /** 폐기(status=retired) · 복원(status=active). 같은 상태면 그대로 돌려준다. 새 판으로 대체된 문서는 복원하지 않는다 — 두 판이 함께 검색된다. */
  @PatchMapping("/documents/{id}")
  @Transactional
  public DocumentDto patch(@PathVariable long id, @RequestBody StatusRequest body, TenantContext ctx) {
    if (body == null || body.status() == null || !Set.of("active", "retired").contains(body.status()))
      throw ApiException.badRequest("VALIDATION", "status must be active or retired");
    String reason = body.reason() == null || body.reason().isBlank() ? null : body.reason().strip();
    if (reason != null && reason.length() > REASON_MAX) throw ApiException.badRequest("VALIDATION", "reason must be at most " + REASON_MAX + " characters");
    DocumentDto doc = find(id, ctx);
    requireManager(doc);
    if (doc.status().equals(body.status())) return doc;
    if ("retired".equals(body.status())) {
      if (reason == null) throw ApiException.badRequest("REASON_REQUIRED", "reason is required to retire a document");
      jdbc.sql("UPDATE policy_documents SET status = 'retired', retired_at = now(), retire_reason = ?, retired_by = ? WHERE id = ?")
          .params(reason, ctx.userName(), id).update();
      audit(ctx, doc, "retired", reason);
    } else {
      if (doc.supersededBy() != null) throw ApiException.conflict("SUPERSEDED", "replaced by a newer version (superseded_by=" + doc.supersededBy() + "); restore is not allowed");
      jdbc.sql("UPDATE policy_documents SET status = 'active', retired_at = NULL, retire_reason = NULL, retired_by = NULL WHERE id = ?").param(id).update();
      audit(ctx, doc, "restored", reason);
    }
    return find(id, ctx);
  }

  /** 영구 삭제: 폐기된 문서만(purge_document 가 청크까지 지우고 감사 기록을 남긴다). 원본 파일(file_path)은 레포 데이터라 지우지 않는다.
   *  다른 판이 이 문서를 대체 대상으로 가리키거나 활성 사례가 이 문서를 근거로 적어 두었으면 막는다 — 개정 영향 화면의 근거가 사라진다. */
  @DeleteMapping("/documents/{id}")
  @Transactional
  public org.springframework.http.ResponseEntity<Void> delete(@PathVariable long id, TenantContext ctx) {
    DocumentDto doc = find(id, ctx);
    requireManager(doc);
    if (!"retired".equals(doc.status())) throw ApiException.conflict("NOT_RETIRED", "only retired documents can be deleted");
    if (Boolean.TRUE.equals(jdbc.sql("SELECT EXISTS (SELECT 1 FROM policy_documents WHERE superseded_by = ?)").param(id).query(Boolean.class).single()))
      throw ApiException.conflict("DOCUMENT_IN_USE", "an older version points to this document");
    long cases = jdbc.sql("""
        SELECT count(DISTINCT n.id) FROM log_nodes n
        CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(n.attributes->'policy_refs') = 'array' THEN n.attributes->'policy_refs' ELSE '[]'::jsonb END) r
        WHERE n.status = 'active' AND r->>'policy' = ?
          AND n.workspace_id IN (SELECT id FROM workspaces WHERE tenant_id = ?)
        """).params(doc.slug(), ctx.tenantId()).query(Long.class).single();
    if (cases > 0) throw ApiException.conflict("DOCUMENT_IN_USE", cases + " active cases cite this document");
    jdbc.sql("SELECT purge_document(?, ?)").params(id, ctx.userName()).query(Object.class).optional();
    return org.springframework.http.ResponseEntity.noContent().build();
  }

  private DocumentDto find(long id, TenantContext ctx) {
    return jdbc.sql("SELECT " + COLS + " FROM policy_documents d JOIN workspaces w ON w.id = d.workspace_id WHERE d.id = ? AND " + IN_SCOPE)
        .params(ctx.userId(), id, ctx.workspaceId()).query(DocumentController::map).optional().orElseThrow(() -> ApiException.notFound("document"));
  }

  /** 관리 권한: 문서가 속한 워크스페이스 또는 그 상위의 admin (전사 공통 문서는 회사 관리자만). */
  private void requireManager(DocumentDto doc) {
    if (!doc.canManage()) throw ApiException.forbidden("document admin required");
  }

  private void audit(TenantContext ctx, DocumentDto doc, String action, String reason) {
    jdbc.sql("""
        INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label)
        VALUES (?, ?, 'policy_documents', ?, ?, ?, 'ui', ?, ?)""")
        .params(ctx.tenantId(), doc.workspaceId(), String.valueOf(doc.id()), action, ctx.userName(), reason, doc.title() + " " + doc.version()).update();
  }

  private static Long parseId(String s) {
    try { return s == null ? null : Long.valueOf(s); } catch (NumberFormatException e) { return null; }
  }

  static DocumentDto map(ResultSet rs, int i) throws SQLException {
    return new DocumentDto(rs.getLong("id"), rs.getLong("workspace_id"), rs.getString("slug"), rs.getString("title"), rs.getString("version"),
        rs.getString("doc_type"), rs.getString("source_type"), rs.getBoolean("has_file"), rs.getString("status"),
        rs.getObject("retired_at", OffsetDateTime.class), rs.getString("retire_reason"), rs.getString("retired_by"), (Long) rs.getObject("superseded_by"),
        rs.getString("visibility"), rs.getInt("chunk_count"), rs.getObject("indexed_at", OffsetDateTime.class), rs.getString("uploaded_by"),
        rs.getObject("uploaded_at", OffsetDateTime.class), null, rs.getBoolean("can_manage"));
  }
}
