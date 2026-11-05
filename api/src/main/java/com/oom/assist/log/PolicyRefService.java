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
  /**
   * docTitle·articleLabel 은 지금 유효한 조문(개정됐으면 개정판의 조문). 개정판에 대응 조문이 정해지지 않았으면 null.
   * revision: null(개정 없음·내용 그대로) · amended(조문 내용이 바뀜) · replaced(대응 조문 없음 — suggested* 는 내용이 가장 가까운 새 조문).
   * fromLabel 은 사례가 따른 개정 전 조문('환불 규정 2026-11 제9조 (신선식품 반품 제한)').
   */
  public record RefView(String relation, String policy, String article, String note, String topic, String docTitle, String articleLabel,
                        String revision, String fromLabel, String suggestedPolicy, String suggestedArticle, String suggestedLabel) {}

  /** resolve_policy_ref 한 행. status: current·same·amended·replaced·retired. */
  public record Resolved(String status, String slug, String docTitle, String version, String articleLabel,
                         String fromTitle, String fromVersion, String fromLabel, String suggestedLabel) {
    static final Resolved MISSING = new Resolved("missing", null, null, null, null, null, null, null, null);

    boolean stale() {
      return "replaced".equals(status) || "retired".equals(status);
    }

    String from() {
      return fromLabel == null ? null : fromTitle + " " + fromVersion + " " + fromLabel;
    }
  }
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
    Map<String, Resolved> labels = new java.util.HashMap<>();   // 같은 조문을 가리키는 노드가 많다 — 조문 조회를 한 번씩만
    jdbc.sql("SELECT id, attributes->'policy_refs' AS refs FROM log_nodes WHERE workspace_id = ? AND id = ANY (?) AND jsonb_typeof(attributes->'policy_refs') = 'array'")
        .params(ws, nodeIds.toArray(Long[]::new))
        .query((rs, i) -> Map.entry(rs.getLong("id"), rs.getString("refs"))).list()
        .forEach(e -> {
          List<RefView> v = views(ws, e.getValue(), labels);
          if (!v.isEmpty()) out.put(e.getKey(), v);
        });
    return out;
  }

  private List<RefView> views(long ws, String raw, Map<String, Resolved> cache) {
    List<Map<String, String>> refs;
    try {
      refs = json.readValue(raw, new TypeReference<>() {});
    } catch (JacksonException e) {
      refs = List.of();
    }
    List<RefView> out = new ArrayList<>();
    for (Map<String, String> r : refs) {
      Resolved v = r.get("policy") == null ? null : resolve(ws, r.get("policy"), r.get("article"), cache);
      boolean live = v != null && v != Resolved.MISSING && !v.stale();
      boolean replaced = v != null && v.stale();
      Matcher m = replaced && v.suggestedLabel() != null ? ARTICLE.matcher(v.suggestedLabel()) : null;
      out.add(new RefView(r.get("relation"), r.get("policy"), r.get("article"), r.get("note"), r.get("topic"),
          live ? v.docTitle() : null, live ? v.articleLabel() : null,
          replaced ? "replaced" : live && "amended".equals(v.status()) ? "amended" : null,
          v == null || v == Resolved.MISSING || "current".equals(v.status()) ? null : v.from(),
          replaced && v.suggestedLabel() != null ? v.slug() : null, m != null && m.find() ? m.group(1) : null, replaced ? v.suggestedLabel() : null));
    }
    return out;
  }

  /** 근거 조문(문서 slug + 조문)을 지금 유효한 조문으로 푼다 — 문서가 개정됐으면 개정 사슬을 따라간다. 원래 문서에 없는 조문은 MISSING. */
  public Resolved resolve(long ws, String slug, String article, Map<String, Resolved> cache) {
    if (slug == null || article == null) return Resolved.MISSING;
    return cache.computeIfAbsent(slug + "|" + article, k -> jdbc.sql("""
        SELECT status, slug, doc_title, version, article_label, from_title, from_version, from_label, suggested_label
        FROM resolve_policy_ref(?, ?, ?)""").params(ws, slug, article)
        .query((rs, i) -> new Resolved(rs.getString("status"), rs.getString("slug"), rs.getString("doc_title"), rs.getString("version"),
            rs.getString("article_label"), rs.getString("from_title"), rs.getString("from_version"), rs.getString("from_label"),
            rs.getString("suggested_label")))
        .optional().orElse(Resolved.MISSING));
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
