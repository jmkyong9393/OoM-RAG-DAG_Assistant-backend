package com.oom.assist.chain;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Times;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * GET /chains/{entry_id}?depth=3 — get_chain() 결과(진입 기준 음수=조상·양수=자손) + 그 집합 안의 간선 + 루트 기준 요약.
 * 요약은 장애 건(루트) 전체 기준: "{루트 제목} 장애 건: 기록 N건, 해결 M건, 완료|진행 중".
 */
@RestController
public class ChainController {

  private static final int MAX_NODES = 60;

  private final JdbcClient jdbc;

  public ChainController(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public record ChainNode(long id, String type, String status, String title, int depth, String pathLabel, String note, OffsetDateTime lastConfirmedAt) {}
  public record Edge(long parentId, long childId) {}
  public record Chain(long entryId, List<ChainNode> nodes, List<Edge> edges, String summary) {}

  @GetMapping("/chains/{entryId}")
  public Chain get(@PathVariable long entryId, @RequestParam(defaultValue = "3") int depth, TenantContext ctx) {
    long ws = ctx.workspaceId();
    List<Map<String, Object>> rows = jdbc.sql("""
        SELECT v.id, v.type, v.status, v.title, g.depth, v.path_label, v.resolution_summary, v.last_confirmed_at, v.root_id
        FROM get_chain(?, ?, ?, ?) g JOIN v_log_nodes v ON v.id = g.node_id
        ORDER BY g.depth, v.id
        """).params(ws, entryId, Math.max(1, Math.min(depth, 8)), MAX_NODES).query().listOfRows();
    if (rows.isEmpty()) throw ApiException.notFound("chain");

    List<ChainNode> nodes = rows.stream().map(r -> new ChainNode(num(r, "id"), (String) r.get("type"), (String) r.get("status"), (String) r.get("title"),
        ((Number) r.get("depth")).intValue(), (String) r.get("path_label"), (String) r.get("resolution_summary"),
        Times.offset(r.get("last_confirmed_at")))).toList();
    List<Long> ids = nodes.stream().map(ChainNode::id).toList();
    List<Edge> edges = jdbc.sql("SELECT parent_id, child_id FROM log_edges WHERE parent_id = ANY (?) AND child_id = ANY (?) ORDER BY parent_id, child_id")
        .params(ids.toArray(Long[]::new), ids.toArray(Long[]::new))
        .query((rs, i) -> new Edge(rs.getLong("parent_id"), rs.getLong("child_id"))).list();

    Long rootId = rows.stream().filter(r -> num(r, "id") == entryId).map(r -> (Long) r.get("root_id")).findFirst().orElse(null);
    return new Chain(entryId, nodes, edges, summary(ws, rootId));
  }

  private String summary(long ws, Long rootId) {
    if (rootId == null) return null;
    Map<String, Object> r = jdbc.sql("""
        SELECT root.title, root.resolved_at, root.type,
               (SELECT count(*) FROM get_chain(?, ?, 32, 100000) g WHERE g.depth >= 0) AS records,
               (SELECT count(*) FROM get_chain(?, ?, 32, 100000) g JOIN log_nodes n ON n.id = g.node_id
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
