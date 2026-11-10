package com.oom.assist.chain;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Times;
import com.oom.assist.log.PolicyRefService;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * GET /chains/{entry_id}[?depth=N] — get_chain() 결과(진입 기준 음수=조상·양수=자손) + 그 집합 안의 간선 + 루트 기준 요약.
 * depth 없으면 조상 · 자손 전체. MAX_NODES 를 넘으면 해결 · 원인 경로를 먼저 남기고 자른다(truncated, total = 전체 수).
 * summary = 저장된 장애 건 AI 요약만(없으면 null, LLM 은 부르지 않는다 — 만드는 건 POST /chains/{id}/summary),
 * summary_status = fresh | stale | generating | none | deprecated (ChainSummaryService.peek).
 * overview = 장애 건(루트) 현황 한 줄: "{루트 제목} 장애 건: 기록 N건, 해결 M건, 완료|진행 중".
 */
@RestController
public class ChainController {

  private static final int MAX_NODES = 200;

  private final JdbcClient jdbc;
  private final PolicyRefService policyRefs;
  private final ChainSummaryService summaries;

  public ChainController(JdbcClient jdbc, PolicyRefService policyRefs, ChainSummaryService summaries) {
    this.jdbc = jdbc;
    this.policyRefs = policyRefs;
    this.summaries = summaries;
  }

  /**
   * policyRefs = 그 기록이 따른(또는 벗어난) 규정 조문. 없으면 null. 어시스턴트의 체인과 같은 표시를 타임라인에서도 하게 한다.
   * resolvesRoot = 이 success 가 장애 건을 실제로 해결한 기록으로 남아 있다(지금 유효한 것만 — resolved_nodes()).
   * isCause = 장애 건의 원인으로 표시한 기록(상위가 있을 때만 — v_log_nodes.is_cause).
   */
  public record ChainNode(long id, String type, String status, String title, int depth, String pathLabel, String note, OffsetDateTime lastConfirmedAt,
                          List<PolicyRefService.RefView> policyRefs, boolean resolvesRoot, boolean isCause) {}
  /** reason = 이 기록을 그 상위에 이어 붙인 이유. 없으면 null. */
  public record Edge(long parentId, long childId, String reason) {}
  public record Chain(long entryId, List<ChainNode> nodes, List<Edge> edges, String summary, String summaryStatus, OffsetDateTime summaryAt,
                      String overview, int total, boolean truncated) {}

  @GetMapping("/chains/{entryId}")
  public Chain get(@PathVariable long entryId, @RequestParam(required = false) Integer depth, TenantContext ctx) {
    return build(ctx.workspaceId(), entryId, depth, true);
  }

  /** withSummary=false 는 요약 입력용(POST 요약이 체인을 모을 때) — 저장된 요약 상태를 읽지 않는다. */
  private Chain build(long ws, long entryId, Integer depth, boolean withSummary) {
    List<Map<String, Object>> rows = jdbc.sql("""
        SELECT v.id, v.type, v.status, v.title, g.depth, v.path_label, v.resolution_summary, v.last_confirmed_at, v.root_id, v.is_cause, g.total
        FROM get_chain(?, ?, ?::int, ?, ?) g JOIN v_log_nodes v ON v.id = g.node_id
        ORDER BY g.depth, v.id
        """).params(ws, entryId, depth == null ? null : Math.max(1, depth), MAX_NODES, entryId).query().listOfRows();
    if (rows.isEmpty()) throw ApiException.notFound("chain");

    List<Long> ids = rows.stream().map(r -> num(r, "id")).toList();
    Map<Long, List<PolicyRefService.RefView>> refs = policyRefs.readMany(ws, ids);
    // 체인에 든 장애 건(루트)들의 해결 기록
    Long[] roots = rows.stream().map(r -> (Long) r.get("root_id")).filter(java.util.Objects::nonNull).distinct().toArray(Long[]::new);
    java.util.Set<Long> resolving = new java.util.HashSet<>(jdbc.sql("SELECT DISTINCT x FROM unnest(?::bigint[]) AS rt(root), resolved_nodes(?, rt.root) AS x")
        .params(roots, ws).query(Long.class).list());
    List<ChainNode> nodes = rows.stream().map(r -> new ChainNode(num(r, "id"), (String) r.get("type"), (String) r.get("status"), (String) r.get("title"),
        ((Number) r.get("depth")).intValue(), (String) r.get("path_label"), (String) r.get("resolution_summary"),
        Times.offset(r.get("last_confirmed_at")), refs.get(num(r, "id")), resolving.contains(num(r, "id")), Boolean.TRUE.equals(r.get("is_cause")))).toList();
    List<Edge> edges = jdbc.sql("SELECT parent_id, child_id, reason FROM log_edges WHERE parent_id = ANY (?) AND child_id = ANY (?) ORDER BY parent_id, child_id")
        .params(ids.toArray(Long[]::new), ids.toArray(Long[]::new))
        .query((rs, i) -> new Edge(rs.getLong("parent_id"), rs.getLong("child_id"), rs.getString("reason"))).list();

    Long rootId = rows.stream().filter(r -> num(r, "id") == entryId).map(r -> (Long) r.get("root_id")).findFirst().orElse(null);
    int total = ((Number) rows.get(0).get("total")).intValue();
    ChainSummaryService.View ai = withSummary && rootId != null ? summaries.peek(ws, rootId) : null;
    return new Chain(entryId, nodes, edges, ai == null ? null : ai.summary(), ai == null ? null : ai.status(), ai == null ? null : ai.at(),
        summary(ws, rootId), total, total > nodes.size());
  }

  /**
   * POST /chains/{entry_id}/summary — 장애 건(진입 노드의 루트 ~ 모든 리프) AI 요약을 만든다. GET 은 저장된 것을 읽기만 한다.
   * 저장된 요약의 지문이 지금과 같으면 그대로, 바뀌었으면 Python 으로 다시 만든다(ChainSummaryService).
   */
  @PostMapping("/chains/{entryId}/summary")
  public ChainSummaryService.Result aiSummary(@PathVariable long entryId, TenantContext ctx) {
    return summaries.summarize(ctx.workspaceId(), entryId, root -> build(ctx.workspaceId(), root, null, false));
  }

  private String summary(long ws, Long rootId) {
    if (rootId == null) return null;
    Map<String, Object> r = jdbc.sql("""
        SELECT root.title, root.resolved_at, root.type,
               (SELECT count(*) FROM bfs_levels(?, ARRAY[?]::bigint[], true) g) AS records,
               (SELECT count(*) FROM bfs_levels(?, ARRAY[?]::bigint[], true) g JOIN log_nodes n ON n.id = g.node_id
                 WHERE g.depth > 0 AND n.type = 'success' AND n.status = 'active') AS solved
        FROM log_nodes root WHERE root.id = ?
        """).params(ws, rootId, ws, rootId, rootId).query().listOfRows().stream().findFirst().orElse(null);
    if (r == null) return null;
    String state = "ref".equals(r.get("type")) ? "참고" : r.get("resolved_at") != null ? "완료" : "진행 중";
    return "%s 장애 건: 기록 %d건, 해결 %d건, %s".formatted(r.get("title"), num(r, "records"), num(r, "solved"), state);
  }

  private static long num(Map<String, Object> r, String k) {
    return ((Number) r.get(k)).longValue();
  }
}
