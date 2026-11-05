package com.oom.assist.log;

import com.oom.assist.common.error.ApiException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import tools.jackson.core.JacksonException;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

/**
 * 사례의 근거 조문(log_nodes.attributes.policy_refs). 관계는 follows(규정을 따른 조치)·exception(규정과 달리 처리)·gap(규정에 기준 없음).
 * follows·exception 은 이 부서에서 검색되는 활성 규정의 조문이어야 한다. 형식은 scripts/load_alignment.py 가 쓰는 것과 같다.
 */
@Service
public class PolicyRefService {

  private static final List<String> RELATIONS = List.of("follows", "exception", "gap");
  private static final Pattern ARTICLE = Pattern.compile("^\\[?(제\\d+조(?:의\\d+)?|별표 ?\\d+)");
  private static final int MAX_REFS = 5;
  /** 이 부서(자기 + 상위)에서 검색되는 활성 규정의 청크. 조문의 첫 청크가 먼저 오도록 id 순. */
  private static final String VISIBLE_CHUNKS = """
      SELECT d.slug, d.title, c.article_label FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
      WHERE d.status = 'active' AND c.workspace_id IN (SELECT workspace_ancestors(?))""";

  private final JdbcClient jdbc;
  private final ObjectMapper json;

  public PolicyRefService(JdbcClient jdbc, ObjectMapper json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  public record RefInput(String relation, String policy, String article, String note, String topic) {}
  /** docTitle·articleLabel 은 지금 그 조문이 활성 규정에 있을 때만 채워진다(개정으로 사라졌으면 null). */
  private static final String[] NONE = new String[0];   // 조회했지만 없는 조문(개정으로 사라짐)

  public record RefView(String relation, String policy, String article, String note, String topic, String docTitle, String articleLabel) {}
  public record ArticleOption(String policy, String docTitle, String article, String articleLabel) {}

  /** 입력을 검증해 저장 형태로 바꾼다. 잘못된 관계·없는 조문은 400. null 은 '건드리지 않음', 빈 목록은 '비움'. */
  public List<Map<String, String>> normalize(long ws, List<RefInput> in) {
    if (in == null) return null;
    if (in.size() > MAX_REFS) throw ApiException.badRequest("VALIDATION", "policy_refs: at most " + MAX_REFS);
    List<Map<String, String>> out = new ArrayList<>();
    for (RefInput r : in) {
      if (r == null || r.relation() == null || !RELATIONS.contains(r.relation())) {
        throw ApiException.badRequest("VALIDATION", "policy_refs.relation must be follows|exception|gap");
      }
      Map<String, String> m = new LinkedHashMap<>();
      if (r.relation().equals("gap")) {
        if (blank(r.topic())) throw ApiException.badRequest("VALIDATION", "policy_refs: gap requires topic");
        m.put("relation", "gap");
        m.put("topic", r.topic().trim());
      } else {
        if (blank(r.policy()) || blank(r.article())) throw ApiException.badRequest("VALIDATION", "policy_refs: policy and article are required");
        String article = r.article().trim();
        if (labelOf(ws, r.policy().trim(), article) == null) {
          throw ApiException.badRequest("POLICY_REF_NOT_FOUND", r.policy().trim() + " " + article + " 을(를) 이 부서의 활성 규정에서 찾을 수 없습니다");
        }
        m.put("policy", r.policy().trim());
        m.put("article", article);
        m.put("relation", r.relation());
      }
      if (!blank(r.note())) m.put("note", r.note().trim());
      out.add(m);
    }
    return out;
  }

  public void write(long nodeId, List<Map<String, String>> refs) {
    if (refs == null) return;
    jdbc.sql("UPDATE log_nodes SET attributes = coalesce(attributes, '{}'::jsonb) || jsonb_build_object('policy_refs', CAST(? AS jsonb)) WHERE id = ?")
        .params(json.writeValueAsString(refs), nodeId).update();
  }

  public List<RefView> read(long ws, long nodeId) {
    String raw = jdbc.sql("SELECT coalesce(attributes->'policy_refs', '[]'::jsonb)::text FROM log_nodes WHERE id = ? AND workspace_id = ?")
        .params(nodeId, ws).query(String.class).optional().orElseThrow(() -> ApiException.notFound("log"));
    return views(ws, raw, new java.util.HashMap<>());
  }

  /** 여러 노드의 근거 조문을 한 번에(체인 조회). 근거가 없는 노드는 결과에 없다. */
  public Map<Long, List<RefView>> readMany(long ws, List<Long> nodeIds) {
    Map<Long, List<RefView>> out = new LinkedHashMap<>();
    if (nodeIds.isEmpty()) return out;
    Map<String, String[]> labels = new java.util.HashMap<>();   // 같은 조문을 가리키는 노드가 많다 — 조문 라벨 조회를 한 번씩만
    jdbc.sql("SELECT id, attributes->'policy_refs' AS refs FROM log_nodes WHERE workspace_id = ? AND id = ANY (?) AND jsonb_typeof(attributes->'policy_refs') = 'array'")
        .params(ws, nodeIds.toArray(Long[]::new))
        .query((rs, i) -> Map.entry(rs.getLong("id"), rs.getString("refs"))).list()
        .forEach(e -> {
          List<RefView> v = views(ws, e.getValue(), labels);
          if (!v.isEmpty()) out.put(e.getKey(), v);
        });
    return out;
  }

  private List<RefView> views(long ws, String raw, Map<String, String[]> labels) {
    List<Map<String, String>> refs;
    try {
      refs = json.readValue(raw, new TypeReference<>() {});
    } catch (JacksonException e) {
      refs = List.of();
    }
    List<RefView> out = new ArrayList<>();
    for (Map<String, String> r : refs) {
      String[] hit = r.get("policy") == null ? null
          : labels.computeIfAbsent(r.get("policy") + "|" + r.get("article"), k -> java.util.Optional.ofNullable(labelOf(ws, r.get("policy"), r.get("article"))).orElse(NONE));
      boolean found = hit != null && hit != NONE;
      out.add(new RefView(r.get("relation"), r.get("policy"), r.get("article"), r.get("note"), r.get("topic"),
          found ? hit[0] : null, found ? hit[1] : null));
    }
    return out;
  }

  /** 등록 화면의 조문 고르기: 문서 제목·조문 라벨에 q 의 모든 어절이 든 조문. 조문마다 한 줄. */
  public List<ArticleOption> options(long ws, String q, int limit) {
    List<String> words = blank(q) ? List.of() : List.of(q.trim().toLowerCase().split("\\s+"));
    List<ArticleOption> out = new ArrayList<>();
    java.util.Set<String> seen = new java.util.HashSet<>();
    int max = Math.min(Math.max(limit, 1), 50);
    for (String[] row : jdbc.sql(VISIBLE_CHUNKS + " ORDER BY d.title, c.id").param(ws)
        .query((rs, i) -> new String[] {rs.getString("slug"), rs.getString("title"), rs.getString("article_label")}).list()) {
      Matcher m = ARTICLE.matcher(row[2] == null ? "" : row[2]);
      if (!m.find() || !seen.add(row[0] + "|" + m.group(1))) continue;
      String hay = (row[1] + " " + row[2]).toLowerCase();
      if (!words.stream().allMatch(hay::contains)) continue;
      out.add(new ArticleOption(row[0], row[1], m.group(1), row[2]));
      if (out.size() >= max) break;
    }
    return out;
  }

  /** {문서 제목, 조문 라벨} 또는 null. */
  private String[] labelOf(long ws, String slug, String article) {
    if (slug == null || article == null) return null;
    return jdbc.sql(VISIBLE_CHUNKS + " AND d.slug = ? ORDER BY c.id").params(ws, slug)
        .query((rs, i) -> new String[] {rs.getString("title"), rs.getString("article_label")}).list().stream()
        .filter(r -> sameArticle(r[1], article)).findFirst().orElse(null);
  }

  /** 청크 라벨('제12조 (접근 토큰)', '[별표 2] 점검표')이 그 조문('제12조', '별표 2')인가. '제1조' 가 '제10조' 에 걸리지 않게 한다. */
  static boolean sameArticle(String label, String article) {
    String head = label == null ? "" : label.strip().replaceFirst("^\\[", "");
    if (!head.startsWith(article)) return false;
    return head.length() == article.length() || !Character.isDigit(head.charAt(article.length()));
  }

  private static boolean blank(String s) {
    return s == null || s.isBlank();
  }
}
