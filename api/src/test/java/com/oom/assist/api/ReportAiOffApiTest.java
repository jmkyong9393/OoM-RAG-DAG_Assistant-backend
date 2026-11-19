package com.oom.assist.api;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;

/** oom.report.ai-enabled=false — 보고서를 AI 칸 없이 바로 done 으로 만든다(AI 워커가 집을 generating 행을 남기지 않는다). */
class ReportAiOffApiTest extends AbstractApiTest {

  @DynamicPropertySource
  static void aiOff(DynamicPropertyRegistry r) {
    r.add("oom.report.ai-enabled", () -> "false");
  }

  @Test
  void markedPreventionRecordsAreListedInTheFacts() throws Exception {
    long root = node(wsSw, "fail", "위키 접속 불가", userBackend);
    long fix = node(wsSw, "success", "인증서 재발급", userBackend);
    long alarm = node(wsSw, "success", "만료 30일 전 경보 추가", userBackend);
    edge(wsSw, root, fix); edge(wsSw, root, alarm);
    String body = "{\"root_id\": %d, \"force\": true}".formatted(root);
    // 표시가 없으면 전과 같은 모양이다 — prevention_refs 키가 없어 지문이 달라지지 않는다
    mvc.perform(as(post("/reports"), userBackend, wsSw).content(body)).andExpect(status().isOk())
        .andExpect(jsonPath("$.content.prevention.length()").value(0)).andExpect(jsonPath("$.content.prevention_refs").doesNotExist());
    jdbc.sql("UPDATE log_nodes SET category = 'prevention' WHERE id = ?").param(alarm).update();
    mvc.perform(as(get("/reports").param("root_id", String.valueOf(root)), userBackend, wsSw)).andExpect(jsonPath("$[0].stale").value(true));   // 표시하면 저장된 보고서는 옛 것이 된다
    jdbc.sql("UPDATE incident_reports SET generated_at = now() - interval '2 minutes' WHERE root_id = ?").param(root).update();
    mvc.perform(as(post("/reports"), userBackend, wsSw).content(body)).andExpect(status().isOk())
        .andExpect(jsonPath("$.content.prevention_refs.length()").value(1)).andExpect(jsonPath("$.content.prevention_refs[0]").value("N3"))
        .andExpect(jsonPath("$.content.prevention[0].text").value("만료 30일 전 경보 추가")).andExpect(jsonPath("$.content.prevention[0].refs[0]").value("N3"));
  }

  @Test
  void reportIsDoneWithFactsOnly() throws Exception {
    long root = node(wsSw, "fail", "VPN 접속 시 E-403 반복", userBackend);
    mvc.perform(as(post("/reports"), userBackend, wsSw).content("{\"root_id\": %d}".formatted(root))).andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("done")).andExpect(jsonPath("$.audience").value("internal"))
        .andExpect(jsonPath("$.generated_at").exists()).andExpect(jsonPath("$.prompt_version").doesNotExist())
        .andExpect(jsonPath("$.content.records.length()").value(1)).andExpect(jsonPath("$.content.summary.length()").value(0));
    jdbc.sql("UPDATE incident_reports SET generated_at = now() - interval '2 minutes' WHERE root_id = ?").param(root).update();
    mvc.perform(as(post("/reports"), userBackend, wsSw).content("{\"root_id\": %d, \"force\": true}".formatted(root))).andExpect(status().isOk());
    mvc.perform(as(get("/reports").param("root_id", String.valueOf(root)), userBackend, wsSw))
        .andExpect(jsonPath("$.length()").value(1));   // 새로 만들면 이전 보고서는 지운다
  }
}
