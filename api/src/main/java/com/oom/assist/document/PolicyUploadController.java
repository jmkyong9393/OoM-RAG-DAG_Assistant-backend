package com.oom.assist.document;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.DigestInputStream;
import java.security.MessageDigest;
import java.time.OffsetDateTime;
import java.util.HexFormat;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.core.io.FileSystemResource;
import org.springframework.core.io.Resource;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import tools.jackson.databind.ObjectMapper;

/**
 * 규정 원본 업로드 → (AI 워커) 자동 변환 → 검수 → 승인 → (AI 워커) 등록. 원본과 검수한 MD 는 저장소(oom.storage.uploads-dir)에 남긴다.
 * 권한은 문서 관리와 같다: 대상 워크스페이스(부서 또는 전사 공통) 또는 그 상위의 admin. 목록 · 상세는 이 부서와 상위의 업로드.
 */
@RestController
public class PolicyUploadController {

  static final Set<String> FORMATS = Set.of("pdf", "hwp", "hwpx", "docx", "md", "txt");
  private static final java.util.regex.Pattern SLUG = java.util.regex.Pattern.compile("[a-z0-9][a-z0-9-]{2,79}");
  private static final String COLS = """
      u.id, u.workspace_id, u.status, u.original_name, u.format, u.size_bytes, u.sha256, u.slug, u.title, u.version, u.doc_type, u.supersedes,
      u.report, u.error, u.reject_reason, u.document_id, u.created_at, u.updated_at, u.reviewed_at,
      (SELECT name FROM users WHERE id = u.uploaded_by) AS uploaded_by, (SELECT name FROM users WHERE id = u.reviewed_by) AS reviewed_by,
      EXISTS (SELECT 1 FROM workspace_members m WHERE m.user_id = ? AND m.role = 'admin'
              AND m.workspace_id IN (SELECT workspace_ancestors(u.workspace_id))) AS can_manage""";

  private final JdbcClient jdbc;
  private final ObjectMapper json;
  private final Path root;

  public PolicyUploadController(JdbcClient jdbc, ObjectMapper json, @Value("${oom.storage.uploads-dir}") String uploadsDir) {
    this.jdbc = jdbc;
    this.json = json;
    this.root = Path.of(uploadsDir).toAbsolutePath().normalize();
  }

  /** report = 워커가 채우는 변환 보고(쪽별 방법 · OCR 쪽 · 조문 미리보기 · 경고). markdown 은 상세에서만. */
  public record UploadDto(long id, long workspaceId, String status, String originalName, String format, long sizeBytes, String sha256,
                          String slug, String title, String version, String docType, String supersedes, Object report, String error,
                          String rejectReason, Long documentId, String uploadedBy, String reviewedBy, OffsetDateTime reviewedAt,
                          OffsetDateTime createdAt, OffsetDateTime updatedAt, boolean canManage, String markdown) {}
  public record MarkdownRequest(String markdown) {}
  public record RejectRequest(String reason) {}

  /** 업로드. scope = dept(이 부서 규정, 기본) · company(전사 공통 규정). 변환은 워커가 이어서 한다(status converting → review). */
  @PostMapping(value = "/documents/uploads", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
  @Transactional
  public ResponseEntity<UploadDto> upload(@RequestPart("file") MultipartFile file, @RequestParam String title, @RequestParam String version,
                                          @RequestParam String slug, @RequestParam(name = "doc_type", required = false) String docType,
                                          @RequestParam(required = false) String supersedes, @RequestParam(defaultValue = "dept") String scope,
                                          TenantContext ctx) {
    if (file == null || file.isEmpty()) throw ApiException.badRequest("VALIDATION", "file is required");
    String name = file.getOriginalFilename() == null ? "upload" : Path.of(file.getOriginalFilename()).getFileName().toString();
    String ext = name.contains(".") ? name.substring(name.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT) : "";
    if (!FORMATS.contains(ext)) throw ApiException.badRequest("UNSUPPORTED_FORMAT", "supported: " + String.join(", ", FORMATS.stream().sorted().toList()));
    if (title == null || title.isBlank() || version == null || version.isBlank()) throw ApiException.badRequest("VALIDATION", "title and version are required");
    if (slug == null || !SLUG.matcher(slug).matches()) throw ApiException.badRequest("VALIDATION", "slug must be lowercase letters, digits, hyphens (3~80)");
    long ws = targetWorkspace(scope, ctx);
    requireManager(ws, ctx);
    if (Boolean.TRUE.equals(jdbc.sql("""
          SELECT EXISTS (SELECT 1 FROM policy_documents WHERE slug = ?)
              OR EXISTS (SELECT 1 FROM policy_uploads WHERE slug = ? AND status IN ('converting', 'review', 'indexing'))""")   // 승인된 것은 문서 유무로 — 영구 삭제한 문서의 slug 는 다시 쓸 수 있다
        .params(slug, slug).query(Boolean.class).single()))
      throw ApiException.conflict("SLUG_TAKEN", "slug already used: " + slug);
    if (supersedes != null && !supersedes.isBlank() && !Boolean.TRUE.equals(jdbc.sql("""
          SELECT EXISTS (SELECT 1 FROM policy_documents d WHERE d.slug = ? AND d.status = 'active' AND d.workspace_id IN (SELECT workspace_ancestors(?)))""")
        .params(supersedes, ws).query(Boolean.class).single()))
      throw ApiException.badRequest("SUPERSEDES_NOT_FOUND", "no active document to replace: " + supersedes);

    String rel = UUID.randomUUID() + "/original." + ext;
    Path target = root.resolve(rel);
    String sha;
    try {
      Files.createDirectories(target.getParent());
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      try (InputStream in = new DigestInputStream(file.getInputStream(), md)) {
        Files.copy(in, target);
      }
      sha = HexFormat.of().formatHex(md.digest());
    } catch (IOException | java.security.NoSuchAlgorithmException e) {
      throw new IllegalStateException("cannot store upload: " + e.getMessage(), e);
    }
    long id = jdbc.sql("""
        INSERT INTO policy_uploads (workspace_id, original_name, format, size_bytes, sha256, original_path, slug, title, version, doc_type, supersedes, uploaded_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""")
        .params(ws, name, ext, file.getSize(), sha, rel, slug, title.strip(), version.strip(), blankToNull(docType), blankToNull(supersedes), ctx.userId())
        .query(Long.class).single();
    audit(ctx, ws, id, "uploaded", name);
    return ResponseEntity.status(HttpStatus.ACCEPTED).body(find(id, ctx, true));
  }

  @GetMapping("/documents/uploads")
  public List<UploadDto> list(@RequestParam(required = false) String status, TenantContext ctx) {
    return jdbc.sql("SELECT " + COLS + ", NULL AS markdown FROM policy_uploads u WHERE u.workspace_id IN (SELECT workspace_ancestors(?))"
            + " AND (?::text IS NULL OR u.status = ?) ORDER BY u.created_at DESC, u.id DESC LIMIT 200")
        .params(ctx.userId(), ctx.workspaceId(), status, status).query(this::map).list();
  }

  @GetMapping("/documents/uploads/{id}")
  public UploadDto get(@PathVariable long id, TenantContext ctx) {
    return find(id, ctx, true);
  }

  /** 검수 중 변환본을 고친다. 조문 미리보기는 워커가 다시 만든다(status review → converting → review, 글자 추출은 다시 하지 않음). */
  @PutMapping("/documents/uploads/{id}/markdown")
  @Transactional
  public UploadDto saveMarkdown(@PathVariable long id, @RequestBody MarkdownRequest body, TenantContext ctx) {
    UploadDto u = managed(id, ctx);
    if (!"review".equals(u.status())) throw ApiException.conflict("NOT_IN_REVIEW", "status is " + u.status());
    if (body == null || body.markdown() == null || body.markdown().isBlank()) throw ApiException.badRequest("VALIDATION", "markdown is required");
    jdbc.sql("UPDATE policy_uploads SET markdown = ?, status = 'converting', updated_at = now() WHERE id = ?").params(body.markdown(), id).update();
    audit(ctx, u.workspaceId(), id, "edited", null);
    return find(id, ctx, true);
  }

  /** 승인 → 워커가 검수한 MD 로 조문 · 임베딩을 만들어 규정 문서로 등록(status indexing → approved). 조문이 하나도 없으면 409. */
  @PostMapping("/documents/uploads/{id}/approve")
  @Transactional
  public UploadDto approve(@PathVariable long id, TenantContext ctx) {
    UploadDto u = managed(id, ctx);
    if (!"review".equals(u.status())) throw ApiException.conflict("NOT_IN_REVIEW", "status is " + u.status());
    Integer articles = jdbc.sql("SELECT (report->>'article_count')::int FROM policy_uploads WHERE id = ?").param(id).query(Integer.class).optional().orElse(null);
    if (articles == null || articles < 1) throw ApiException.conflict("NO_ARTICLES", "no articles in the converted text");
    jdbc.sql("UPDATE policy_uploads SET status = 'indexing', reviewed_by = ?, reviewed_at = now(), updated_at = now() WHERE id = ?").params(ctx.userId(), id).update();
    audit(ctx, u.workspaceId(), id, "approved", null);
    return find(id, ctx, true);
  }

  @PostMapping("/documents/uploads/{id}/reject")
  @Transactional
  public UploadDto reject(@PathVariable long id, @RequestBody(required = false) RejectRequest body, TenantContext ctx) {
    UploadDto u = managed(id, ctx);
    if (!Set.of("review", "failed").contains(u.status())) throw ApiException.conflict("NOT_IN_REVIEW", "status is " + u.status());
    String reason = body == null ? null : blankToNull(body.reason());
    if (reason == null) throw ApiException.badRequest("REASON_REQUIRED", "reason is required to reject");
    jdbc.sql("UPDATE policy_uploads SET status = 'rejected', reject_reason = ?, reviewed_by = ?, reviewed_at = now(), updated_at = now() WHERE id = ?")
        .params(reason, ctx.userId(), id).update();
    audit(ctx, u.workspaceId(), id, "rejected", reason);
    return find(id, ctx, true);
  }

  /** 변환 실패한 업로드를 처음부터 다시 변환. */
  @PostMapping("/documents/uploads/{id}/retry")
  @Transactional
  public UploadDto retry(@PathVariable long id, TenantContext ctx) {
    UploadDto u = managed(id, ctx);
    if (!"failed".equals(u.status())) throw ApiException.conflict("NOT_FAILED", "status is " + u.status());
    jdbc.sql("UPDATE policy_uploads SET status = 'converting', markdown = NULL, error = NULL, updated_at = now() WHERE id = ?").param(id).update();
    return find(id, ctx, true);
  }

  /** 원본 파일 내려받기(올린 이름 그대로). */
  @GetMapping("/documents/uploads/{id}/original")
  public ResponseEntity<Resource> original(@PathVariable long id, TenantContext ctx) {
    UploadDto u = find(id, ctx, false);
    String rel = jdbc.sql("SELECT original_path FROM policy_uploads WHERE id = ?").param(id).query(String.class).single();
    Path p = root.resolve(rel).normalize();
    if (!p.startsWith(root) || !Files.exists(p)) throw ApiException.notFound("original file");
    return ResponseEntity.ok()
        .header(HttpHeaders.CONTENT_DISPOSITION, ContentDisposition.attachment().filename(u.originalName(), StandardCharsets.UTF_8).build().toString())
        .contentType(MediaType.APPLICATION_OCTET_STREAM).body(new FileSystemResource(p));
  }

  /** 변환본(검수 중이면 지금 고친 것, 승인 뒤면 보관한 검수본과 같음) 내려받기. */
  @GetMapping("/documents/uploads/{id}/markdown")
  public ResponseEntity<String> markdown(@PathVariable long id, TenantContext ctx) {
    UploadDto u = find(id, ctx, true);
    if (u.markdown() == null) throw ApiException.notFound("markdown");
    String file = u.slug() + ".md";
    return ResponseEntity.ok()
        .header(HttpHeaders.CONTENT_DISPOSITION, ContentDisposition.attachment().filename(file, StandardCharsets.UTF_8).build().toString())
        .contentType(new MediaType("text", "markdown", StandardCharsets.UTF_8)).body(u.markdown());
  }

  private long targetWorkspace(String scope, TenantContext ctx) {
    if ("dept".equals(scope)) return ctx.workspaceId();
    if (!"company".equals(scope)) throw ApiException.badRequest("VALIDATION", "scope must be dept or company");
    return jdbc.sql("SELECT w.id FROM workspaces w WHERE w.kind <> 'dept' AND w.id IN (SELECT workspace_ancestors(?)) ORDER BY w.id LIMIT 1")
        .param(ctx.workspaceId()).query(Long.class).optional().orElseThrow(() -> ApiException.badRequest("VALIDATION", "no company workspace above this department"));
  }

  private void requireManager(long ws, TenantContext ctx) {
    boolean ok = jdbc.sql("""
        SELECT EXISTS (SELECT 1 FROM workspace_members m WHERE m.user_id = ? AND m.role = 'admin'
                       AND m.workspace_id IN (SELECT workspace_ancestors(?)))""").params(ctx.userId(), ws).query(Boolean.class).single();
    if (!ok) throw ApiException.forbidden("document admin required");
  }

  private UploadDto managed(long id, TenantContext ctx) {
    UploadDto u = find(id, ctx, false);
    if (!u.canManage()) throw ApiException.forbidden("document admin required");
    return u;
  }

  private UploadDto find(long id, TenantContext ctx, boolean withMarkdown) {
    return jdbc.sql("SELECT " + COLS + ", " + (withMarkdown ? "u.markdown" : "NULL AS markdown")
            + " FROM policy_uploads u WHERE u.id = ? AND u.workspace_id IN (SELECT workspace_ancestors(?))")
        .params(ctx.userId(), id, ctx.workspaceId()).query(this::map).optional().orElseThrow(() -> ApiException.notFound("upload"));
  }

  private void audit(TenantContext ctx, long ws, long id, String action, String reason) {
    jdbc.sql("""
        INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, reason, label)
        SELECT ?, ?, 'policy_uploads', ?, ?, ?, 'ui', ?, u.title || ' ' || u.version FROM policy_uploads u WHERE u.id = ?""")
        .params(ctx.tenantId(), ws, String.valueOf(id), action, ctx.userName(), reason, id).update();
  }

  private UploadDto map(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
    String report = rs.getString("report");
    Object parsed;
    try {
      parsed = report == null ? Map.of() : json.readValue(report, Object.class);
    } catch (RuntimeException e) {
      parsed = Map.of();
    }
    return new UploadDto(rs.getLong("id"), rs.getLong("workspace_id"), rs.getString("status"), rs.getString("original_name"), rs.getString("format"),
        rs.getLong("size_bytes"), rs.getString("sha256"), rs.getString("slug"), rs.getString("title"), rs.getString("version"), rs.getString("doc_type"),
        rs.getString("supersedes"), parsed, rs.getString("error"), rs.getString("reject_reason"), (Long) rs.getObject("document_id"),
        rs.getString("uploaded_by"), rs.getString("reviewed_by"), rs.getObject("reviewed_at", OffsetDateTime.class),
        rs.getObject("created_at", OffsetDateTime.class), rs.getObject("updated_at", OffsetDateTime.class), rs.getBoolean("can_manage"),
        rs.getString("markdown"));
  }

  private static String blankToNull(String s) {
    return s == null || s.isBlank() ? null : s.strip();
  }
}
