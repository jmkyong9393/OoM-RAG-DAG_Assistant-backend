package com.oom.assist.session;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Times;
import java.sql.Array;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;

/**
 * 검색 내역(답변 재열람). 프론트 계약 SearchSession (BE-HANDOFF-001 §3-4).
 * citations 는 저장된 verified/unverified_ids('c:'/'n:' 접두어)를 policy_refs·chain_ref 의 ref(P1/N1)로 되돌려 만든다.
 */
@RestController
public class SearchSessionController {

  private final JdbcClient jdbc;
  private final ObjectMapper json;

  public SearchSessionController(JdbcClient jdbc, ObjectMapper json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  public record Latency(Integer retrievalMs, Integer firstTokenMs, Integer totalMs) {}
  public record Citations(List<String> verified, List<String> unverified) {}
  public record SearchSession(long id, long workspaceId, String userName, String query, String intent, String mode, String status,
      String answerText, List<Map<String, Object>> policy, Map<String, Object> chain, Citations citations, Latency latency,
      Integer score, OffsetDateTime createdAt) {}

  @GetMapping("/sessions")
  public List<SearchSession> list(@RequestParam(defaultValue = "30") int limit, TenantContext ctx) {
    return jdbc.sql("SELECT * FROM v_search_sessions WHERE workspace_id = ? ORDER BY created_at DESC, id DESC LIMIT ?")
        .params(ctx.workspaceId(), Math.max(1, Math.min(limit, 200))).query().listOfRows().stream().map(this::map).toList();
  }

  @GetMapping("/sessions/{id}")
  public SearchSession get(@PathVariable long id, TenantContext ctx) {
    return jdbc.sql("SELECT * FROM v_search_sessions WHERE workspace_id = ? AND id = ?").params(ctx.workspaceId(), id)
        .query().listOfRows().stream().findFirst().map(this::map).orElseThrow(() -> ApiException.notFound("session"));
  }

  @SuppressWarnings("unchecked")
  private SearchSession map(Map<String, Object> r) {
    List<Map<String, Object>> policy = parseList(r.get("policy_refs"));
    Map<String, Object> chain = parseMap(r.get("chain_ref"));
    // id → ref 매핑
    Map<String, String> refOf = new java.util.HashMap<>();
    for (Map<String, Object> p : policy) refOf.put("c:" + p.get("chunk_id"), String.valueOf(p.get("ref")));
    if (chain != null) for (Map<String, Object> n : (List<Map<String, Object>>) chain.getOrDefault("nodes", List.of())) refOf.put("n:" + n.get("id"), String.valueOf(n.get("ref")));
    Citations citations = new Citations(toRefs(r.get("verified_ids"), refOf), toRefs(r.get("unverified_ids"), refOf));
    Latency latency = new Latency(intOrNull(r.get("retrieval_ms")), intOrNull(r.get("first_token_ms")), intOrNull(r.get("total_ms")));
    return new SearchSession(((Number) r.get("id")).longValue(), ((Number) r.get("workspace_id")).longValue(), (String) r.get("user_name"),
        (String) r.get("query"), (String) r.get("intent"), (String) r.get("mode"), (String) r.get("status"), (String) r.get("answer_text"),
        policy, chain, citations, latency, intOrNull(r.get("score")), Times.offset(r.get("created_at")));
  }

  private static List<String> toRefs(Object pgArray, Map<String, String> refOf) {
    List<String> out = new ArrayList<>();
    try {
      Object[] arr = pgArray instanceof Array a ? (Object[]) a.getArray() : pgArray instanceof Object[] o ? o : new Object[0];
      for (Object x : arr) out.add(refOf.getOrDefault(String.valueOf(x), String.valueOf(x)));
    } catch (SQLException e) {
      throw new IllegalStateException(e);
    }
    return out;
  }

  @SuppressWarnings("unchecked")
  private List<Map<String, Object>> parseList(Object jsonb) {
    if (jsonb == null) return List.of();
    return json.readValue(String.valueOf(jsonb), List.class);
  }

  @SuppressWarnings("unchecked")
  private Map<String, Object> parseMap(Object jsonb) {
    if (jsonb == null) return null;
    return json.readValue(String.valueOf(jsonb), Map.class);
  }

  private static Integer intOrNull(Object o) {
    return o instanceof Number n ? n.intValue() : null;
  }
}
