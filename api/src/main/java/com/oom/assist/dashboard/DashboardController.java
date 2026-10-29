package com.oom.assist.dashboard;

import com.oom.assist.common.context.TenantContext;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.ObjectMapper;

/**
 * GET /dashboard/summary — 한 번의 호출로 대시보드 전체 집계 (FE-007, BE-HANDOFF-001 §3-5).
 * 실시간 집계(로그·세션·피드백)는 DB 값 그대로. search_time·eval 은 eval_runs 최신 행에서 오며, 실측 전에는 is_sample=true 인
 * 'sample' 행만 있고, 그것도 없으면 null (화면은 "측정 전"으로 표시한다). 0 을 지어내지 않는다.
 */
@RestController
public class DashboardController {

  private final JdbcClient jdbc;
  private final ObjectMapper json;

  public DashboardController(JdbcClient jdbc, ObjectMapper json) {
    this.jdbc = jdbc;
    this.json = json;
  }

  @GetMapping("/dashboard/summary")
  public Map<String, Object> summary(TenantContext ctx) {
    long ws = ctx.workspaceId();
    Map<String, Object> out = new LinkedHashMap<>();

    // eval_runs 최신 행 (워크스페이스 행 우선, 없으면 전역)
    Map<String, Object> eval = jdbc.sql("""
        SELECT is_sample, faithfulness, answer_relevance, dataset_version, run_at, meta::text AS meta
        FROM eval_runs WHERE workspace_id = ? OR workspace_id IS NULL
        ORDER BY (workspace_id IS NOT NULL) DESC, run_at DESC LIMIT 1
        """).param(ws).query().listOfRows().stream().findFirst().orElse(null);
    boolean isSample = eval != null && Boolean.TRUE.equals(eval.get("is_sample"));
    out.put("is_sample", isSample);
    out.put("search_time", eval == null ? null : searchTime(String.valueOf(eval.get("meta"))));
    out.put("eval", eval == null ? map("faithfulness", null, "answer_relevance", null, "dataset_version", null, "run_at", null)
        : map("faithfulness", eval.get("faithfulness"), "answer_relevance", eval.get("answer_relevance"),
            "dataset_version", eval.get("dataset_version"), "run_at", eval.get("run_at")));

    Map<String, Object> logs = jdbc.sql("""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS week,
               count(*) FILTER (WHERE type = 'fail' AND status = 'active' AND resolved_at IS NULL AND depth = 0) AS open_roots,
               count(*) FILTER (WHERE type = 'fail' AND resolved_at IS NOT NULL AND depth = 0) AS resolved_roots,
               count(*) FILTER (WHERE refine_status = 'failed') AS refine_failed
        FROM v_log_nodes WHERE workspace_id = ?
        """).param(ws).query().singleRow();
    out.put("logs", logs);

    out.put("feedback", jdbc.sql("""
        SELECT count(*) FILTER (WHERE f.score = 1) AS up, count(*) FILTER (WHERE f.score = -1) AS down
        FROM answer_feedback f JOIN search_sessions s ON s.id = f.session_id WHERE s.workspace_id = ?
        """).param(ws).query().singleRow());

    out.put("latency_ms", jdbc.sql("""
        SELECT round(avg(retrieval_ms))::int AS retrieval, round(avg(first_token_ms))::int AS first_token, round(avg(total_ms))::int AS total
        FROM search_sessions WHERE workspace_id = ? AND status = 'done'
        """).param(ws).query().singleRow());

    Map<String, Object> queries = new LinkedHashMap<>(jdbc.sql("""
        SELECT count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS week, count(*) AS total
        FROM search_sessions WHERE workspace_id = ? AND status <> 'error'
        """).param(ws).query().singleRow());
    queries.put("by_intent", jdbc.sql("""
        SELECT i.intent, count(s.id) AS count
        FROM unnest(ARRAY['policy','troubleshoot','both']) AS i(intent)
        LEFT JOIN search_sessions s ON s.intent = i.intent AND s.workspace_id = ? AND s.status <> 'error'
        GROUP BY i.intent ORDER BY array_position(ARRAY['policy','troubleshoot','both'], i.intent)
        """).param(ws).query().listOfRows());
    out.put("queries", queries);

    // 근거 없이 끝난 질문: done 인데 verified 0건. 중단(error)은 제외한다
    Map<String, Object> noEvidence = new LinkedHashMap<>();
    Map<String, Object> ne = jdbc.sql("""
        SELECT count(*) FILTER (WHERE cardinality(verified_ids) = 0) AS none, count(*) AS done_total
        FROM search_sessions WHERE workspace_id = ? AND status = 'done'
        """).param(ws).query().singleRow();
    long doneTotal = ((Number) ne.get("done_total")).longValue();
    noEvidence.put("rate", doneTotal == 0 ? null : ((Number) ne.get("none")).doubleValue() / doneTotal);
    noEvidence.put("recent", jdbc.sql("""
        SELECT id AS session_id, query, intent, created_at FROM search_sessions
        WHERE workspace_id = ? AND status = 'done' AND cardinality(verified_ids) = 0 ORDER BY created_at DESC LIMIT 5
        """).param(ws).query().listOfRows());
    out.put("no_evidence", noEvidence);

    out.put("resolve_time", jdbc.sql("""
        SELECT round((EXTRACT(EPOCH FROM percentile_cont(0.5) WITHIN GROUP (ORDER BY resolved_at - created_at)) / 3600)::numeric, 1)::double precision AS median_hours,
               count(*)::int AS sample
        FROM v_log_nodes WHERE workspace_id = ? AND type = 'fail' AND depth = 0 AND resolved_at IS NOT NULL
        """).param(ws).query().singleRow());

    out.put("reused", jdbc.sql("""
        SELECT v.id AS node_id, v.title, v.type, v.author_name, count(*)::int AS count
        FROM search_sessions s, unnest(s.verified_ids) AS cid
        JOIN v_log_nodes v ON cid LIKE 'n:%' AND v.id = substr(cid, 3)::bigint
        WHERE s.workspace_id = ? AND s.status = 'done' AND v.status = 'active'
        GROUP BY v.id, v.title, v.type, v.author_name ORDER BY count DESC, v.id LIMIT 5
        """).param(ws).query().listOfRows());
    return out;
  }

  @SuppressWarnings("unchecked")
  private Map<String, Object> searchTime(String metaJson) {
    Map<String, Object> meta = json.readValue(metaJson, Map.class);
    Object st = meta.get("search_time");
    return st instanceof Map<?, ?> m ? (Map<String, Object>) m : null;
  }

  private static Map<String, Object> map(Object... kv) {
    Map<String, Object> m = new LinkedHashMap<>();
    for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
    return m;
  }
}
