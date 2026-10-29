package com.oom.assist.api;

import static org.hamcrest.Matchers.closeTo;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;

/** 검색 내역 재열람·피드백·대시보드 집계. 집계값이 세션·로그 원천과 일치하는지(인수 테스트 §7-7). */
class SessionFeedbackDashboardApiTest extends AbstractApiTest {

  private long session(long ws, String status, String intent, String answer, String[] verified, String[] unverified, Integer total) {
    return jdbc.sql("""
        INSERT INTO search_sessions (workspace_id, user_id, query, mode, status, intent, answer_text, verified_ids, unverified_ids, retrieved_ids,
          retrieval_ms, first_token_ms, total_ms, policy_refs, chain_ref)
        VALUES (?, ?, 'q', 'dag_only', ?, ?, ?, ?, ?, ?, 10, 20, ?,
          '[{"ref":"P1","chunk_id":"vpn-a12-1","doc_id":1,"doc_title":"VPN","version":"v2","article_label":"제12조","page":7,"text":"t"}]'::jsonb,
          '{"entry_id":101,"nodes":[{"ref":"N1","id":101,"type":"fail","status":"active","title":"E-403","depth":0,"path_label":"root.n101","last_confirmed_at":null}],"edges":[]}'::jsonb)
        RETURNING id
        """).params(ws, userBackend, status, intent, answer, verified, unverified, verified, total).query(Long.class).single();
  }

  @Test
  void sessionsListMapsStoredIdsBackToRefsAndIncludesScore() throws Exception {
    long s = session(wsSw, "done", "troubleshoot", "답변", new String[] {"n:101", "c:vpn-a12-1"}, new String[] {"n:999"}, 300);
    jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, 1)").param(s).update();
    session(wsHr, "done", "policy", "다른 부서", new String[0], new String[0], 100);
    mvc.perform(as(get("/sessions"), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(s))
        .andExpect(jsonPath("$[0].user_name").value("장문경"))
        .andExpect(jsonPath("$[0].answer_text").value("답변"))
        .andExpect(jsonPath("$[0].policy[0].ref").value("P1"))
        .andExpect(jsonPath("$[0].chain.entry_id").value(101))
        .andExpect(jsonPath("$[0].citations.verified[0]").value("N1"))
        .andExpect(jsonPath("$[0].citations.verified[1]").value("P1"))
        .andExpect(jsonPath("$[0].citations.unverified[0]").value("n:999"))   // 매핑 불가 id 는 그대로
        .andExpect(jsonPath("$[0].latency.total_ms").value(300))
        .andExpect(jsonPath("$[0].score").value(1));
    mvc.perform(as(get("/sessions/" + s), userBackend, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$.status").value("done"));
    mvc.perform(as(get("/sessions/" + s), userBackend, wsHr)).andExpect(status().isNotFound());
  }

  @Test
  void feedbackUpsertsAndNullWithdraws() throws Exception {
    long s = session(wsSw, "done", "troubleshoot", "a", new String[0], new String[0], 1);
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":" + s + ",\"score\":1}")).andExpect(status().isNoContent());
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":" + s + ",\"score\":-1,\"comment\":\"틀림\"}")).andExpect(status().isNoContent());
    mvc.perform(as(get("/sessions/" + s), userBackend, wsSw)).andExpect(jsonPath("$.score").value(-1));
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":" + s + ",\"score\":null}")).andExpect(status().isNoContent());
    mvc.perform(as(get("/sessions/" + s), userBackend, wsSw)).andExpect(jsonPath("$.score").isEmpty());
    mvc.perform(as(post("/feedback"), userBackend, wsSw).content("{\"session_id\":" + s + ",\"score\":5}")).andExpect(status().isBadRequest());
    mvc.perform(as(post("/feedback"), userBackend, wsHr).content("{\"session_id\":" + s + ",\"score\":1}")).andExpect(status().isNotFound());
  }

  @Test
  void dashboardAggregatesMatchSources() throws Exception {
    // 로그: 완료 루트 1(해결 소요 48h), 진행 루트 1, 정제 실패 1
    long resolved = node(wsSw, "fail", "완료 장애", userBackend);
    jdbc.sql("UPDATE log_nodes SET created_at = now() - interval '2 days', resolved_at = now() WHERE id = ?").param(resolved).update();
    long open = node(wsSw, "fail", "진행 장애", userBackend);
    long fix = node(wsSw, "success", "해결", userBackend);
    edge(wsSw, resolved, fix);
    jdbc.sql("UPDATE log_nodes SET refine_status = 'failed' WHERE id = ?").param(open).update();
    // 세션: 근거 있음 2(둘 다 n:resolved 인용), 근거 없음 1, 오류 1(집계 제외)
    long s1 = session(wsSw, "done", "troubleshoot", "a", new String[] {"n:" + resolved}, new String[0], 100);
    long s2 = session(wsSw, "done", "both", "b", new String[] {"n:" + resolved, "n:" + fix}, new String[0], 300);
    session(wsSw, "done", "policy", "c", new String[0], new String[0], 200);
    session(wsSw, "error", null, "", new String[0], new String[0], null);
    jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, 1), (?, -1)").params(s1, s2).update();
    // 평가: 예시 행
    jdbc.sql("INSERT INTO eval_runs (workspace_id, dataset_version, is_sample, faithfulness, answer_relevance, meta) VALUES (?, 'sample', true, 0.94, 0.91, "
        + "'{\"search_time\":{\"before_min\":5.5,\"after_min\":1.4,\"by_intent\":[]}}'::jsonb)").param(wsSw).update();

    mvc.perform(as(get("/dashboard/summary"), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.is_sample").value(true))
        .andExpect(jsonPath("$.search_time.before_min").value(5.5))
        .andExpect(jsonPath("$.eval.faithfulness").value(0.94))
        .andExpect(jsonPath("$.logs.total").value(3))
        .andExpect(jsonPath("$.logs.open_roots").value(1))
        .andExpect(jsonPath("$.logs.resolved_roots").value(1))
        .andExpect(jsonPath("$.logs.refine_failed").value(1))
        .andExpect(jsonPath("$.feedback.up").value(1))
        .andExpect(jsonPath("$.feedback.down").value(1))
        .andExpect(jsonPath("$.latency_ms.total").value(200))              // (100+300+200)/3, error 제외
        .andExpect(jsonPath("$.queries.total").value(3))
        .andExpect(jsonPath("$.queries.by_intent", hasSize(3)))
        .andExpect(jsonPath("$.queries.by_intent[1].intent").value("troubleshoot"))
        .andExpect(jsonPath("$.queries.by_intent[1].count").value(1))
        .andExpect(jsonPath("$.no_evidence.rate", closeTo(1.0 / 3, 1e-9)))
        .andExpect(jsonPath("$.no_evidence.recent", hasSize(1)))
        .andExpect(jsonPath("$.resolve_time.median_hours").value(48.0))
        .andExpect(jsonPath("$.resolve_time.sample").value(1))
        .andExpect(jsonPath("$.reused", hasSize(2)))
        .andExpect(jsonPath("$.reused[0].node_id").value(resolved))
        .andExpect(jsonPath("$.reused[0].count").value(2))
        .andExpect(jsonPath("$.reused[0].author_name").value("장문경"));
  }

  @Test
  void dashboardWithoutDataReturnsNullsNotZeros() throws Exception {
    mvc.perform(as(get("/dashboard/summary"), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.is_sample").value(false))
        .andExpect(jsonPath("$.search_time").isEmpty())
        .andExpect(jsonPath("$.eval.faithfulness").isEmpty())
        .andExpect(jsonPath("$.latency_ms.total").isEmpty())
        .andExpect(jsonPath("$.no_evidence.rate").isEmpty())
        .andExpect(jsonPath("$.resolve_time.median_hours").isEmpty())
        .andExpect(jsonPath("$.logs.total").value(0));
  }
}
