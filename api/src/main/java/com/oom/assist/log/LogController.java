package com.oom.assist.log;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PatchMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class LogController {

  private final LogService service;
  private final LogRepository repo;
  private final LogBulkService bulk;

  public LogController(LogService service, LogRepository repo, LogBulkService bulk) {
    this.service = service;
    this.repo = repo;
    this.bulk = bulk;
  }

  /** 상위 로그 자동완성: 추천(에러 코드·키워드·어휘 점수) + 검색(제목·코드 부분 일치). 두 섹션은 겹치지 않는다. */
  @GetMapping("/logs/suggest")
  public LogBulkService.SuggestResponse suggest(@RequestParam(defaultValue = "") String q, @RequestParam(defaultValue = "8") int limit, TenantContext ctx) {
    return bulk.suggest(ctx, q, limit);
  }

  /** 일괄 등록 (합성 로그·연구노트 실사례). 한 트랜잭션, source_ref 멱등. */
  @PostMapping("/logs/bulk")
  public ResponseEntity<LogBulkService.BulkResponse> bulkImport(@RequestBody LogBulkService.BulkRequest body, TenantContext ctx) {
    return ResponseEntity.status(HttpStatus.CREATED).body(bulk.bulk(ctx, body));
  }

  public record ListResponse(List<LogNodeDto> items, Map<String, Integer> counts) {}

  /** limit=0 은 전체. counts 는 active 기준. */
  @GetMapping("/logs")
  public ListResponse list(@RequestParam(defaultValue = "50") int limit, @RequestParam(required = false) String type,
      @RequestParam(required = false) String q, @RequestParam(required = false) String status, TenantContext ctx) {
    return new ListResponse(repo.list(ctx.workspaceId(), new LogRepository.ListFilter(type, q, status, limit)), repo.counts(ctx.workspaceId()));
  }

  @GetMapping("/logs/weekly-count")
  public Map<String, Integer> weekly(TenantContext ctx) {
    return Map.of("count", repo.weeklyCount(ctx.workspaceId()));
  }

  @GetMapping("/logs/{id}")
  public LogNodeDto get(@PathVariable long id, TenantContext ctx) {
    return service.get(ctx, id);
  }

  @PostMapping("/logs")
  public ResponseEntity<LogNodeDto> create(@RequestBody LogService.CreateRequest body, TenantContext ctx) {
    return ResponseEntity.status(HttpStatus.CREATED).body(service.create(ctx, body));
  }

  @PostMapping("/logs/{id}/refine")
  public ResponseEntity<LogNodeDto> refine(@PathVariable long id, TenantContext ctx) {
    return ResponseEntity.status(HttpStatus.ACCEPTED).body(service.requeueRefine(ctx, id));
  }

  @PostMapping("/logs/{id}/confirm")
  public LogNodeDto confirm(@PathVariable long id, TenantContext ctx) {
    return service.confirm(ctx, id);
  }

  @GetMapping("/logs/{id}/orphaned")
  public Map<String, List<Long>> orphaned(@PathVariable long id, TenantContext ctx) {
    return Map.of("orphaned", service.orphaned(ctx, id));
  }

  public record PatchRequest(Boolean resolved, String note, String status, String reason, Boolean cascade) {}

  /** 두 가지 모양을 받는다: {resolved, note?} 또는 {status, reason?, cascade?}. 상태 변경은 affected 를 함께 돌려준다. */
  @PatchMapping("/logs/{id}")
  public Map<String, Object> patch(@PathVariable long id, @RequestBody PatchRequest body, TenantContext ctx) {
    if (body.resolved() != null) {
      return asMap(service.setResolved(ctx, id, body.resolved(), body.note()), null);
    }
    if (body.status() != null) {
      LogService.StatusResult r = service.setStatus(ctx, id, body.status(), body.reason(), Boolean.TRUE.equals(body.cascade()));
      return asMap(r.node(), r.affected());
    }
    throw ApiException.badRequest("VALIDATION", "resolved or status is required");
  }

  /** LogNode 필드 + affected. 레코드를 그대로 쓰면 affected 를 끼울 수 없어 Map 으로 편다 (키는 snake_case). */
  private static Map<String, Object> asMap(LogNodeDto n, List<Long> affected) {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("id", n.id()); m.put("workspace_id", n.workspaceId()); m.put("type", n.type()); m.put("status", n.status());
    m.put("title", n.title()); m.put("body", n.body()); m.put("author_name", n.authorName()); m.put("path_label", n.pathLabel());
    m.put("error_code", n.errorCode()); m.put("keywords", n.keywords()); m.put("resolution_summary", n.resolutionSummary());
    m.put("refine_status", n.refineStatus()); m.put("last_confirmed_at", n.lastConfirmedAt()); m.put("deprecated_via", n.deprecatedVia());
    m.put("deprecate_reason", n.deprecateReason()); m.put("resolved_at", n.resolvedAt()); m.put("resolve_note", n.resolveNote());
    m.put("root_id", n.rootId()); m.put("depth", n.depth()); m.put("category", n.category()); m.put("tags", n.tags()); m.put("created_at", n.createdAt());
    if (affected != null) m.put("affected", affected);
    return m;
  }
}
