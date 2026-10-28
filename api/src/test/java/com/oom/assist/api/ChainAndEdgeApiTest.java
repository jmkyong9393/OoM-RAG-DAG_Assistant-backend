package com.oom.assist.api;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;

/** 체인 조회(루트 기준 요약)와 간선 연결·해제(사이클·타입 규칙). */
class ChainAndEdgeApiTest extends AbstractApiTest {

  @Test
  void chainReturnsNodesEdgesAndRootSummary() throws Exception {
    long root = node(wsSw, "fail", "E-403 인증 오류", userBackend);
    long cause = node(wsSw, "ref", "토큰 갱신 누락", userBackend);
    long fix = node(wsSw, "success", "갱신 주기 30분", userBackend);
    edge(wsSw, root, cause); edge(wsSw, cause, fix);
    mvc.perform(as(get("/chains/" + cause), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.entry_id").value(cause))
        .andExpect(jsonPath("$.nodes", hasSize(3)))
        .andExpect(jsonPath("$.nodes[0].id").value(root)).andExpect(jsonPath("$.nodes[0].depth").value(-1))
        .andExpect(jsonPath("$.nodes[1].depth").value(0))
        .andExpect(jsonPath("$.nodes[2].id").value(fix)).andExpect(jsonPath("$.nodes[2].depth").value(1))
        .andExpect(jsonPath("$.nodes[2].path_label").value("root.n" + root + ".n" + cause + ".n" + fix))
        .andExpect(jsonPath("$.edges", hasSize(2)))
        .andExpect(jsonPath("$.summary").value("E-403 인증 오류 장애 건: 기록 3건, 해결 1건, 진행 중"));
    jdbc.sql("UPDATE log_nodes SET resolved_at = now() WHERE id = ?").param(root).update();
    mvc.perform(as(get("/chains/" + root + "?depth=1"), userBackend, wsSw))
        .andExpect(jsonPath("$.nodes", hasSize(2)))
        .andExpect(jsonPath("$.summary").value("E-403 인증 오류 장애 건: 기록 3건, 해결 1건, 완료"));
  }

  @Test
  void chainUnknownOrForeignEntryIs404() throws Exception {
    long foreign = node(wsHr, "fail", "HR", userBackend);
    mvc.perform(as(get("/chains/" + foreign), userBackend, wsSw)).andExpect(status().isNotFound());
    mvc.perform(as(get("/chains/999999"), userBackend, wsSw)).andExpect(status().isNotFound());
  }

  @Test
  void linkRejectsCycleAndTypeRuleThenUnlinks() throws Exception {
    long a = node(wsSw, "fail", "A", userBackend);
    long b = node(wsSw, "ref", "B", userBackend);
    long s = node(wsSw, "success", "S", userBackend);
    edge(wsSw, a, b);
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + b + ",\"child_id\":" + a + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("CYCLE"));
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + s + ",\"child_id\":" + b + "}"))
        .andExpect(status().isCreated());
    long s2 = node(wsSw, "success", "S2", userBackend);
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + s + ",\"child_id\":" + s2 + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("EDGE_PARENT_TYPE"));
    mvc.perform(as(delete("/edges"), userBackend, wsSw).content("{\"parent_id\":" + s + ",\"child_id\":" + b + "}"))
        .andExpect(status().isNoContent());
    mvc.perform(as(delete("/edges"), userBackend, wsSw).content("{\"parent_id\":" + s + ",\"child_id\":" + b + "}"))
        .andExpect(status().isNotFound());
  }
}
