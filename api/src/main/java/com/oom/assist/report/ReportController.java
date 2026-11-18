package com.oom.assist.report;

import com.oom.assist.common.context.TenantContext;
import com.oom.assist.common.error.ApiException;
import com.oom.assist.common.util.Times;
import com.oom.assist.log.PolicyRefService;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;

/**
 * 장애 보고서(템플릿 R1, FE-REQ-005) — 장애 건(루트) 하나를 보고서 모양으로 만들어 incident_reports 에 저장한다.
 *   POST /reports {root_id, audience, force}   만들기. 같은 장애 건 · 같은 공개 범위 · 같은 내용의 보고서가 있으면 그것을 돌려준다(force=true 면 새로 만든다)
 *   GET  /reports/{id}                         한 건
 *   GET  /reports?root_id=                     그 장애 건의 보고서(최신순)
 * content 는 프론트 lib/types/report.ts 의 ReportContent. 사실 칸은 여기서 DB 로 조립한다(프론트 lib/report/assemble.ts 와 같은 규칙):
 *   근거 = 루트 + 그 아래 활성 기록, 번호 N1… = 작성 시각 오름차순(같으면 id), 구분 = 루트 발생 / 원인 표시 원인 / 해결 기록 해결 / 그 밖의 성공 조치 /
 *   그 밖의 실패 추가 문제 / 참고. 하위 기록 제목의 '원인:' · '해결:' 머리는 뗀다. external 은 작성자 · 보고자를 비운다(부서 관리자만 생성).
 * AI 칸(summary · impact · prevention · open_items · 원인 설명 · 조치와 추가 문제의 메모)은 비워 generating 으로 넣고 AI 워커가 채운다.
 *   oom.report.ai-enabled=false 면 AI 칸 없이 바로 done — LLM 호출을 멈출 때.
 * stale = 보고서를 만든 뒤 장애 건의 사실 칸이 바뀌었다(지금 다시 조립한 지문과 저장된 지문이 다르다).
 */
@RestController
public class ReportController {

  private static final int MAX_RECORDS = 200;
  private static final int SYMPTOM_MAX = 200;
  private static final Pattern HEAD = Pattern.compile("^(원인|해결\\(구\\)|해결|참고)\\s*:\\s*");

  private final JdbcClient jdbc;
  private final PolicyRefService policyRefs;
  private final ObjectMapper json;
  private final boolean aiEnabled;

  public ReportController(JdbcClient jdbc, PolicyRefService policyRefs, ObjectMapper json,
                          @Value("${oom.report.ai-enabled:true}") boolean aiEnabled) {
    this.jdbc = jdbc;
    this.policyRefs = policyRefs;
    this.json = json;
    this.aiEnabled = aiEnabled;
  }

  public record CreateRequest(Long rootId, String audience, Boolean force) {}

  public record Report(long id, long rootId, String status, String audience, String templateVersion, String promptVersion, String model,
                       OffsetDateTime generatedAt, String error, boolean stale, String pendingStatus, Object content) {}

  /** 보고서 행 + 응답에 보일 상태 — 10분 넘게 generating 이면 멈춘 것이라 failed 로 보인다(DB 값은 그대로). */
  private static final String ROWS = """
      SELECT *, CASE WHEN status = 'generating' AND created_at <= now() - interval '10 minutes' THEN 'failed' ELSE status END AS shown_status
      FROM incident_reports""";

  private record Facts(String content, String sig) {}

  @PostMapping("/reports")
  public Report create(@RequestBody CreateRequest req, TenantContext ctx) {
    if ("viewer".equals(ctx.role())) throw ApiException.forbidden("viewer cannot create a report");   // AI 비용이 드는 쓰기
    if (req.rootId() == null) throw ApiException.badRequest("VALIDATION", "root_id is required");
    String audience = req.audience() == null ? "internal" : req.audience();
    if (!audience.equals("internal") && !audience.equals("external")) throw ApiException.badRequest("VALIDATION", "audience must be internal or external");
    if (audience.equals("external") && !ctx.isAdmin()) throw ApiException.forbidden("workspace admin required for external report");
    long ws = ctx.workspaceId();
    Facts facts = facts(ws, req.rootId(), audience);
    // 새로 만들지 않고 돌려주는 경우 — AI 호출을 겹치거나 연달아 내지 않는다:
    //  만드는 중인 보고서가 있다(10분 넘게 generating 이면 멈춘 것으로 보고 무시) · 같은 내용의 완성본이 있다(force 여도 1분 안에 만든 것이면 그대로)
    boolean force = Boolean.TRUE.equals(req.force());
    Long same = jdbc.sql("""
        SELECT id FROM incident_reports WHERE workspace_id = ? AND root_id = ? AND audience = ?
          AND ((status = 'generating' AND created_at > now() - interval '10 minutes')
            OR (status = 'done' AND sig = ? AND (NOT ? OR generated_at > now() - interval '1 minute')))
        ORDER BY id DESC LIMIT 1
        """).params(ws, req.rootId(), audience, facts.sig(), force).query(Long.class).optional().orElse(null);
    if (same != null) return read(ws, same, facts.sig());
    // 끝나지 않은 이전 요청(멈춘 generating · failed)은 여기서 지운다 — 남겨 두면 워커가 그 수만큼 AI 를 부른다.
    // 완료된 이전 보고서는 새 것이 done 이 될 때 지운다 — AI 를 쓰면 AI 워커가, 끄면(바로 done) 여기서
    long id = jdbc.sql("""
        WITH gone AS (DELETE FROM incident_reports WHERE workspace_id = ? AND root_id = ? AND audience = ? AND (NOT ? OR status <> 'done'))
        INSERT INTO incident_reports (workspace_id, root_id, audience, status, sig, content, requested_by, generated_at)
        VALUES (?, ?, ?, ?, ?, ?::jsonb, ?, CASE WHEN ? THEN NULL ELSE now() END) RETURNING id
        """).params(ws, req.rootId(), audience, aiEnabled,
            ws, req.rootId(), audience, aiEnabled ? "generating" : "done", facts.sig(), facts.content(), ctx.userId(), aiEnabled)
        .query(Long.class).single();
    return read(ws, id, facts.sig());
  }

  @GetMapping("/reports/{id}")
  public Report get(@PathVariable long id, TenantContext ctx) {
    return read(ctx.workspaceId(), id, null);
  }

  @GetMapping("/reports")
  public List<Report> list(@RequestParam("root_id") long rootId, TenantContext ctx) {
    long ws = ctx.workspaceId();
    Map<String, List<Map<String, Object>>> byAudience = new LinkedHashMap<>();   // 공개 범위마다 최신순
    for (Map<String, Object> r : jdbc.sql(ROWS + " WHERE workspace_id = ? AND root_id = ? ORDER BY id DESC").params(ws, rootId).query().listOfRows())
      byAudience.computeIfAbsent((String) r.get("audience"), a -> new ArrayList<>()).add(r);
    // 공개 범위마다 지금 보여 줄 한 건 = 가장 최근에 완료된 것(없으면 가장 최근 행). 그보다 새 요청이 있으면 그 상태를 pending_status 로 알린다
    List<Report> out = new ArrayList<>();
    for (Map.Entry<String, List<Map<String, Object>>> e : byAudience.entrySet()) {
      Map<String, Object> latest = e.getValue().get(0);
      Map<String, Object> shown = e.getValue().stream().filter(r -> "done".equals(r.get("shown_status"))).findFirst().orElse(latest);
      out.add(report(shown, sigNow(ws, rootId, e.getKey()), shown == latest ? null : (String) latest.get("shown_status")));
    }
    return out;
  }

  private Report read(long ws, long id, String sigNow) {
    Map<String, Object> r = jdbc.sql(ROWS + " WHERE id = ? AND workspace_id = ?").params(id, ws)
        .query().listOfRows().stream().findFirst().orElseThrow(() -> ApiException.notFound("report"));
    return report(r, sigNow != null ? sigNow : sigNow(ws, ((Number) r.get("root_id")).longValue(), (String) r.get("audience")), null);
  }

  /** 지금 장애 건의 지문. 더 이상 장애 건이 아니면(상위가 생김) 빈 값 — 저장된 보고서는 stale 로 보인다. */
  private String sigNow(long ws, long rootId, String audience) {
    try {
      return facts(ws, rootId, audience).sig();
    } catch (ApiException e) {
      return "";
    }
  }

  private Report report(Map<String, Object> r, String sigNow, String pendingStatus) {
    return new Report(((Number) r.get("id")).longValue(), ((Number) r.get("root_id")).longValue(), (String) r.get("shown_status"), (String) r.get("audience"),
        (String) r.get("template_version"), (String) r.get("prompt_version"), (String) r.get("model"), Times.offset(r.get("generated_at")),
        (String) r.get("error"), !sigNow.equals(r.get("sig")), pendingStatus, json.readValue(r.get("content").toString(), Object.class));
  }

  /** 사실 칸을 조립한 content(JSON)와 그 지문. AI 칸은 빈 값으로 둔다. */
  private Facts facts(long ws, long rootId, String audience) {
    Map<String, Object> root = jdbc.sql("SELECT id, root_id FROM v_log_nodes WHERE id = ? AND workspace_id = ?").params(rootId, ws)
        .query().listOfRows().stream().findFirst().orElseThrow(() -> ApiException.notFound("log"));
    if (root.get("root_id") == null || ((Number) root.get("root_id")).longValue() != rootId) throw ApiException.badRequest("REPORT_NOT_ROOT", "root_id must be an incident root");

    List<Map<String, Object>> all = jdbc.sql("""
        SELECT v.id, v.type, v.title, v.body, v.created_at, v.author_name, v.error_code, v.resolution_summary, v.is_cause, v.resolved_at, v.resolve_note
        FROM bfs_levels(?, ARRAY[?]::bigint[], true) g JOIN v_log_nodes v ON v.id = g.node_id
        WHERE v.status = 'active' OR v.id = ?
        ORDER BY v.created_at, v.id
        """).params(ws, rootId, rootId).query().listOfRows();
    boolean truncated = all.size() > MAX_RECORDS;
    List<Map<String, Object>> rows = truncated ? all.subList(0, MAX_RECORDS) : all;
    List<Long> ids = rows.stream().map(r -> ((Number) r.get("id")).longValue()).toList();
    Long[] idArr = ids.toArray(Long[]::new);
    Set<Long> resolving = new HashSet<>(jdbc.sql("SELECT resolved_nodes(?, ?)").params(ws, rootId).query(Long.class).list());
    Map<Long, String> reason = new HashMap<>();
    jdbc.sql("SELECT child_id, reason FROM log_edges WHERE parent_id = ANY (?) AND child_id = ANY (?) AND reason IS NOT NULL ORDER BY parent_id")
        .params(idArr, idArr).query((rs, i) -> reason.putIfAbsent(rs.getLong("child_id"), rs.getString("reason"))).list();
    // 후속 조치가 달린 기록 = 아래(자손)에 활성 success 기록이 있다. 없는 원인 · 추가 문제는 '남은 확인'에 싣는다(AI 가 고르지 않는다)
    List<long[]> edges = jdbc.sql("SELECT parent_id, child_id FROM log_edges WHERE parent_id = ANY (?) AND child_id = ANY (?)")
        .params(idArr, idArr).query((rs, i) -> new long[] {rs.getLong("parent_id"), rs.getLong("child_id")}).list();
    Set<Long> followed = new HashSet<>();
    for (Map<String, Object> r : rows) if ("success".equals(r.get("type"))) followed.add(((Number) r.get("id")).longValue());
    for (boolean grew = true; grew; ) {
      grew = false;
      for (long[] e : edges) if (followed.contains(e[1]) && followed.add(e[0])) grew = true;
    }
    Map<Long, List<PolicyRefService.RefView>> refs = policyRefs.readMany(ws, ids);
    boolean internal = audience.equals("internal");

    Map<String, Object> rootRow = rows.stream().filter(r -> ((Number) r.get("id")).longValue() == rootId).findFirst().orElseThrow();
    OffsetDateTime reported = Times.offset(rootRow.get("created_at")), resolved = Times.offset(rootRow.get("resolved_at"));
    String status = "ref".equals(rootRow.get("type")) ? "reference" : resolved != null ? "resolved" : "open";
    String body = (String) rootRow.get("body");

    List<Map<String, Object>> timeline = new ArrayList<>(), causes = new ArrayList<>(), items = new ArrayList<>(), others = new ArrayList<>(),
        sides = new ArrayList<>(), policy = new ArrayList<>(), records = new ArrayList<>(), open = new ArrayList<>();
    Set<String> codes = new LinkedHashSet<>();
    int n = 0;
    for (Map<String, Object> r : rows) {
      long id = ((Number) r.get("id")).longValue();
      String ref = "N" + (++n), type = (String) r.get("type");
      String kind = id == rootId ? "occurrence" : Boolean.TRUE.equals(r.get("is_cause")) ? "cause" : resolving.contains(id) ? "resolution"
          : type.equals("success") ? "action" : type.equals("fail") ? "side_issue" : "reference";
      String title = id == rootId ? (String) r.get("title") : HEAD.matcher((String) r.get("title")).replaceFirst("");
      OffsetDateTime at = Times.offset(r.get("created_at"));
      String author = internal ? (String) r.get("author_name") : null;
      if (r.get("error_code") != null) codes.add((String) r.get("error_code"));
      timeline.add(map("ref", ref, "at", at, "kind", kind, "title", title, "author", author));
      records.add(map("ref", ref, "id", id, "kind", kind, "title", title, "at", at, "author", author));
      switch (kind) {
        case "cause" -> causes.add(map("ref", ref, "title", title, "link_reason", reason.get(id), "explanation", null));
        case "resolution" -> items.add(map("ref", ref, "title", title, "summary", r.get("resolution_summary")));
        case "action", "reference" -> others.add(map("ref", ref, "kind", kind, "title", title));
        case "side_issue" -> sides.add(map("ref", ref, "title", title, "note", null));
        default -> { }
      }
      if ((kind.equals("cause") || kind.equals("side_issue")) && !followed.contains(id)) open.add(map("text", title, "refs", List.of(ref)));
      for (PolicyRefService.RefView p : refs.getOrDefault(id, List.of())) {
        policy.add(map("ref", ref, "relation", p.relation(), "document", p.docTitle() != null ? p.docTitle() : p.policy(),
            "article", p.articleLabel() != null ? p.articleLabel() : p.article(),
            "note", p.note() != null ? p.note() : "gap".equals(p.relation()) ? p.topic() : null));
      }
    }

    Map<String, Object> content = map(
        "meta", map("title", rootRow.get("title"), "interim", status.equals("open"),
            "workspace_name", jdbc.sql("SELECT name FROM workspaces WHERE id = ?").param(ws).query(String.class).single(),
            "status", status, "root_id", rootId, "first_reported_at", reported, "resolved_at", resolved,
            "duration_minutes", resolved == null ? null : Math.max(0, Math.round(Duration.between(reported, resolved).toSeconds() / 60.0)),
            "record_count", rows.size(), "truncated", truncated),
        "occurrence", map("symptom", body.length() > SYMPTOM_MAX ? body.substring(0, SYMPTOM_MAX) + "…" : body, "error_codes", List.copyOf(codes),
            "reported_at", reported, "reported_by", internal ? rootRow.get("author_name") : null),
        "timeline", timeline, "causes", causes,
        "resolution", map("items", items, "resolve_note", rootRow.get("resolve_note"), "others", others, "note", null),
        "side_issues", sides, "policy", policy,
        "summary", List.of(), "impact", List.of(), "prevention", List.of(), "open_items", open,
        "records", records);
    String text = json.writeValueAsString(content);
    return new Facts(text, md5(text));
  }

  private static Map<String, Object> map(Object... kv) {   // 순서를 지키는 Map (null 값 허용) — 지문이 실행마다 같게
    Map<String, Object> m = new LinkedHashMap<>();
    for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
    return m;
  }

  private static String md5(String s) {
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("MD5").digest(s.getBytes(StandardCharsets.UTF_8)));
    } catch (java.security.NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }
}
