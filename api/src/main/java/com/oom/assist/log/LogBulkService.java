package com.oom.assist.log;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 로그 등록 보조 2종.
 *  suggest — 등록 화면의 상위 로그 선택: 추천(hybrid_search_nodes: 에러 코드 정확 일치·키워드·어휘) + 검색(제목·코드 부분 일치).
 *            Spring 에는 kiwi 가 없으므로 질의 토큰은 공백 분리 + 소문자. 사용자가 명사 위주로 치는 짧은 질의에서는 충분하다.
 *  bulk    — 합성 로그·연구노트 실사례 일괄 등록. 배열 안 참조(parent_ref = 다른 항목의 ref) 또는 기존 id. source_ref 로 멱등.
 *            규칙은 단건 등록과 같다(R2·R3·R12). 정제 결과가 포함된 항목은 refine_status=done 으로 넣고 워커가 토큰·임베딩만 채운다.
 */
@Service
public class LogBulkService {

  static final Pattern ERROR_CODE = Pattern.compile("\\b(?:[A-Z]{1,4}-?\\d{3,4}|HTTP\\s?\\d{3})\\b", Pattern.CASE_INSENSITIVE);
  private static final Set<String> TYPES = Set.of("fail", "success", "ref");
  private static final Set<String> SOURCES = Set.of("bulk", "synthetic");

  private final LogRepository repo;

  public LogBulkService(LogRepository repo) {
    this.repo = repo;
  }

  // ── suggest ────────────────────────────────────────────────────────────────

  public record SuggestResponse(List<LogNodeDto> recommended, List<LogNodeDto> matched) {}

  public SuggestResponse suggest(TenantContext ctx, String q, int limit) {
    if (q == null || q.isBlank()) return new SuggestResponse(List.of(), List.of());
    int n = Math.max(1, Math.min(limit, 20));
    String tokens = String.join(" ", q.trim().toLowerCase(Locale.ROOT).split("\\s+"));
    List<LogNodeDto> recommended = repo.suggestByScore(ctx.workspaceId(), tokens, extractErrorCode(q), n);
    List<Long> seen = recommended.stream().map(LogNodeDto::id).toList();
    List<LogNodeDto> matched = repo.suggestByText(ctx.workspaceId(), q, n, seen);
    return new SuggestResponse(recommended, matched);
  }

  public static String extractErrorCode(String text) {
    Matcher m = ERROR_CODE.matcher(text == null ? "" : text);
    if (!m.find()) return null;
    String c = m.group().toUpperCase(Locale.ROOT).replaceAll("\\s+", " ");
    return c.replaceFirst("^HTTP ?", "HTTP ");
  }

  // ── bulk ───────────────────────────────────────────────────────────────────

  public record Item(String ref, String type, String title, String body, String parentRef, List<String> parentRefs, String problemTitle, String source,
                     String sourceRef, String errorCode, List<String> keywords, String resolutionSummary, OffsetDateTime createdAt) {}
  public record BulkRequest(List<Item> items) {}
  public record ItemResult(String ref, long id, String status) {}
  public record BulkResponse(int created, int existing, List<ItemResult> items) {}

  @Transactional
  public BulkResponse bulk(TenantContext ctx, BulkRequest req) {
    if (req == null || req.items() == null || req.items().isEmpty()) throw ApiException.badRequest("VALIDATION", "items required");
    if (req.items().size() > 500) throw ApiException.badRequest("VALIDATION", "at most 500 items per request");
    long ws = ctx.workspaceId();
    repo.lockWorkspace(ws);
    Map<String, Long> byRef = new HashMap<>();
    List<ItemResult> out = new ArrayList<>();
    int created = 0, existing = 0;
    for (int i = 0; i < req.items().size(); i++) {
      Item it = req.items().get(i);
      String ref = it.ref() == null || it.ref().isBlank() ? "#" + i : it.ref().trim();
      if (byRef.containsKey(ref)) throw ApiException.badRequest("VALIDATION", "duplicate ref " + ref);
      if (it.type() == null || !TYPES.contains(it.type())) throw ApiException.badRequest("INVALID_TYPE", ref + ": type must be fail|success|ref");
      if (blank(it.title()) || blank(it.body())) throw ApiException.badRequest("VALIDATION", ref + ": title and body are required");
      String source = it.source() == null ? "bulk" : it.source();
      if (!SOURCES.contains(source)) throw ApiException.badRequest("VALIDATION", ref + ": source must be bulk|synthetic");

      // 멱등: 같은 source_ref 가 이미 있으면 재사용
      if (!blank(it.sourceRef())) {
        var found = repo.findBySourceRef(ws, it.sourceRef().trim());
        if (found.isPresent()) {
          byRef.put(ref, found.get());
          out.add(new ItemResult(ref, found.get(), "existing"));
          existing++;
          continue;
        }
      }

      // 다중 부모(합류 노드): parent_ref 하나 + parent_refs 여러 개를 합쳐 중복 없이. 새 노드는 자식이 없어 사이클이 생길 수 없다
      List<Long> parentIds = new ArrayList<>();
      for (String p : parentRefsOf(it)) {
        Long pid = resolveParent(ws, p, byRef, ref);
        if (pid != null && !parentIds.contains(pid)) parentIds.add(pid);
      }
      long id;
      String sourceRef = blank(it.sourceRef()) ? null : it.sourceRef().trim();
      // 해결 시각은 success 노드의 created_at(과거 로그를 적재할 때 대시보드 해결 시간이 "지금 - 7월" 로 왜곡되지 않게)
      OffsetDateTime resolvedAt = it.createdAt() != null ? it.createdAt() : OffsetDateTime.now();
      if (parentIds.isEmpty() && "success".equals(it.type())) {
        // R2: success 루트 → problem_title 로 fail 루트 생성 + 완료 (단건 등록과 같은 규칙. 정제 컬럼은 INSERT 시에만 쓸 수 있어 여기서 직접 넣는다)
        if (blank(it.problemTitle())) throw ApiException.badRequest("PROBLEM_TITLE_REQUIRED", ref + ": success without parent needs problem_title");
        long root = repo.insertNodeFull(ws, "fail", it.problemTitle().trim(), it.problemTitle().trim(), ctx.userId(), source, null, null, null, null, it.createdAt());
        id = repo.insertNodeFull(ws, "success", it.title().trim(), it.body().trim(), ctx.userId(), source, sourceRef,
            it.errorCode(), it.keywords(), it.resolutionSummary(), it.createdAt());
        repo.insertEdge(ws, root, id, ctx.userId(), "bulk");
        repo.setResolved(root, resolvedAt, null);
        repo.audit(ctx.tenantId(), ws, "log_nodes", String.valueOf(root), "resolved", ctx.userName(), null, it.problemTitle().trim());
      } else {
        for (Long parentId : parentIds) {
          LogNodeDto parent = repo.find(ws, parentId).orElseThrow(() -> ApiException.conflict("PARENT_NOT_FOUND", ref + ": parent not in this workspace"));
          if (!"active".equals(parent.status())) throw ApiException.conflict("PARENT_DEPRECATED", ref + ": parent is deprecated");
          if ("success".equals(it.type()) && !Set.of("fail", "ref").contains(parent.type()))
            throw ApiException.conflict("EDGE_PARENT_TYPE", ref + ": success can only follow fail/ref");
        }
        id = repo.insertNodeFull(ws, it.type(), it.title().trim(), it.body().trim(), ctx.userId(), source,
            sourceRef, it.errorCode(), it.keywords(), it.resolutionSummary(), it.createdAt());
        for (Long parentId : parentIds) repo.insertEdge(ws, parentId, id, ctx.userId(), "bulk");
        // 체인에 success 가 붙으면 그 체인의 루트(들)는 해결된 것 — 단건 등록(R3)과 같은 의미. 이미 해결된 루트는 그대로
        if ("success".equals(it.type()))
          for (Long parentId : parentIds) repo.setResolvedIfOpen(repo.rootIdOf(parentId), resolvedAt);
      }
      byRef.put(ref, id);
      out.add(new ItemResult(ref, id, "created"));
      created++;
    }
    repo.audit(ctx.tenantId(), ws, "log_nodes", "-", "bulk_import", ctx.userName(), "created=" + created + " existing=" + existing, null);
    return new BulkResponse(created, existing, out);
  }

  private static List<String> parentRefsOf(Item it) {
    List<String> refs = new ArrayList<>();
    if (!blank(it.parentRef())) refs.add(it.parentRef());
    if (it.parentRefs() != null) for (String p : it.parentRefs()) if (!blank(p)) refs.add(p);
    return refs;
  }

  /** parent_ref: 배열 안 ref("#0", "root-a") 또는 기존 id 문자열("101"). */
  private Long resolveParent(long ws, String parentRef, Map<String, Long> byRef, String ref) {
    if (blank(parentRef)) return null;
    String p = parentRef.trim();
    if (byRef.containsKey(p)) return byRef.get(p);
    if (p.matches("\\d+")) return Long.parseLong(p);
    throw ApiException.badRequest("VALIDATION", ref + ": parent_ref '" + p + "' is neither an earlier item ref nor an id");
  }

  private static boolean blank(String s) {
    return s == null || s.isBlank();
  }
}
