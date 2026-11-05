package com.oom.assist.log;

import com.oom.assist.common.context.TenantContext;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 규정 개정 영향: 폐기(개정)된 규정의 조문을 근거로 적어 둔 이 부서의 사례와, 그 근거가 개정 뒤 어떻게 되는지.
 * same(조문 그대로) · amended(조문 내용이 바뀜 — 사례는 옛 기준) · replaced(개정판에 대응 조문이 정해지지 않음 — 다시 연결해야 한다).
 * 다시 연결은 PUT /logs/{id}/policy-refs. suggested* 는 내용이 가장 가까운 새 조문(제안일 뿐, 사람이 확인한다).
 */
@RestController
public class PolicyRevisionController {

  private final JdbcClient jdbc;
  private final PolicyRefService policyRefs;

  public PolicyRevisionController(JdbcClient jdbc, PolicyRefService policyRefs) {
    this.jdbc = jdbc;
    this.policyRefs = policyRefs;
  }

  public record Item(long nodeId, String title, String type, String relation, String policy, String article, String note, String status,
                     String fromLabel, String currentPolicy, String currentTitle, String currentVersion, String currentLabel,
                     String suggestedArticle, String suggestedLabel) {}
  public record Impact(int cases, Map<String, Integer> summary, List<Item> items) {}

  @GetMapping("/policy-revisions/impact")
  public Impact impact(TenantContext ctx) {
    long ws = ctx.workspaceId();
    Map<String, PolicyRefService.Resolved> cache = new HashMap<>();
    Map<String, Integer> summary = new LinkedHashMap<>(Map.of());
    for (String k : List.of("same", "amended", "replaced")) summary.put(k, 0);
    List<Item> items = new ArrayList<>();
    List<String> order = List.of("replaced", "amended", "same");   // 손봐야 하는 것부터
    Map<Long, String> worst = new LinkedHashMap<>();                // 사례마다 가장 나쁜 상태 — 요약은 사례 수로 센다
    jdbc.sql("""
        SELECT n.id, n.title, n.type, r->>'relation' AS relation, r->>'policy' AS policy, r->>'article' AS article, r->>'note' AS note
        FROM log_nodes n
        CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(n.attributes->'policy_refs') = 'array' THEN n.attributes->'policy_refs' ELSE '[]'::jsonb END) r
        JOIN policy_documents d ON d.slug = r->>'policy' AND d.status = 'retired'
        WHERE n.workspace_id = ? AND n.status = 'active'
        ORDER BY n.id
        """).param(ws).query().listOfRows().forEach(row -> {
          PolicyRefService.Resolved v = policyRefs.resolve(ws, (String) row.get("policy"), (String) row.get("article"), cache);
          if (v == PolicyRefService.Resolved.MISSING) return;
          String status = v.stale() ? "replaced" : v.status();
          worst.merge(((Number) row.get("id")).longValue(), status, (a, b) -> order.indexOf(a) <= order.indexOf(b) ? a : b);
          java.util.regex.Matcher m = v.suggestedLabel() == null ? null
              : PolicyRefService.ARTICLE.matcher(v.suggestedLabel());
          items.add(new Item(((Number) row.get("id")).longValue(), (String) row.get("title"), (String) row.get("type"), (String) row.get("relation"),
              (String) row.get("policy"), (String) row.get("article"), (String) row.get("note"), status, v.from(),
              v.slug(), v.docTitle(), v.version(), v.stale() ? null : v.articleLabel(),
              m != null && m.find() ? m.group(1) : null, v.suggestedLabel()));
        });
    worst.values().forEach(st -> summary.merge(st, 1, Integer::sum));
    items.sort(java.util.Comparator.comparingInt((Item i) -> order.indexOf(i.status())).thenComparingLong(Item::nodeId));
    return new Impact(worst.size(), summary, items);
  }
}
