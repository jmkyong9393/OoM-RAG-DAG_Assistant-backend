package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.oom.assist.log.LogBulkService;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MvcResult;

/** 상위 로그 자동완성(GET /logs/suggest)과 일괄 등록(POST /logs/bulk). */
class LogSuggestBulkApiTest extends AbstractApiTest {

  @Test
  void suggestSplitsRecommendedAndMatchedWithoutOverlap() throws Exception {
    long exact = node(wsSw, "fail", "VPN 접속 시 E-403 반복", userLead);
    long kw = node(wsSw, "fail", "토큰 갱신 배치 누락", userLead);
    long titleOnly = node(wsSw, "ref", "VPN 게이트웨이 점검 방법", userLead);
    long dep = node(wsSw, "fail", "VPN 구버전 (폐기)", userLead);
    node(wsHr, "fail", "VPN E-403 (다른 부서)", userBackend);
    jdbc.sql("UPDATE log_nodes SET error_code = 'E-403' WHERE id = ?").param(exact).update();
    jdbc.sql("UPDATE log_nodes SET keywords = ARRAY['토큰 갱신','VPN'] WHERE id = ?").param(kw).update();
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(dep).update();

    MvcResult r = mvc.perform(as(get("/logs/suggest").param("q", "vpn e-403 토큰 갱신"), userLead, wsSw)).andExpect(status().isOk()).andReturn();
    String b = r.getResponse().getContentAsString(StandardCharsets.UTF_8);
    java.util.List<Integer> rec = JsonPath.read(b, "$.recommended[*].id");
    java.util.List<Integer> mat = JsonPath.read(b, "$.matched[*].id");
    assertThat(rec.get(0)).isEqualTo((int) exact);                       // 에러 코드 정확 일치가 1위
    assertThat(rec).contains((int) kw);                                   // 복합어 키워드 '토큰 갱신' 부스팅
    assertThat(mat).contains((int) titleOnly).doesNotContainAnyElementsOf(rec);   // 제목 부분 일치, 추천과 겹치지 않음
    assertThat(rec).doesNotContain((int) dep);
    assertThat(mat).doesNotContain((int) dep);
    assertThat((String) JsonPath.read(b, "$.recommended[0].path_label")).startsWith("root.n");   // LogNode 계약 필드
    mvc.perform(as(get("/logs/suggest").param("q", ""), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$.recommended", hasSize(0))).andExpect(jsonPath("$.matched", hasSize(0)));
  }

  @Test
  void errorCodeExtractionNormalizes() {
    assertThat(LogBulkService.extractErrorCode("http504 가 떴다")).isEqualTo("HTTP 504");
    assertThat(LogBulkService.extractErrorCode("e-403 반복")).isEqualTo("E-403");
    assertThat(LogBulkService.extractErrorCode("제12조")).isNull();
  }

  @Test
  void bulkCreatesChainWithRefsAndIsIdempotentBySourceRef() throws Exception {
    String body = """
        {"items": [
          {"ref": "root", "type": "fail", "title": "FAQ API 504", "body": "타임아웃", "source": "synthetic", "source_ref": "syn-1",
           "error_code": "HTTP 504", "keywords": ["FAQ", "타임아웃"], "created_at": "2026-10-01T09:00:00Z"},
          {"ref": "cause", "type": "ref", "title": "커넥션 풀 고갈", "body": "원인", "parent_ref": "root", "source_ref": "syn-2"},
          {"type": "success", "title": "풀 크기 50 으로", "body": "해결", "parent_ref": "cause", "source_ref": "syn-3",
           "resolution_summary": "풀 확장으로 해결"}
        ]}""";
    MvcResult r = mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content(body))
        .andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(3)).andExpect(jsonPath("$.existing").value(0))
        .andExpect(jsonPath("$.items[0].ref").value("root")).andExpect(jsonPath("$.items[2].ref").value("#2")).andReturn();
    String b = r.getResponse().getContentAsString(StandardCharsets.UTF_8);
    int root = JsonPath.read(b, "$.items[0].id"), cause = JsonPath.read(b, "$.items[1].id"), fix = JsonPath.read(b, "$.items[2].id");
    // 체인·정제 상태·출처·시각
    mvc.perform(as(get("/logs/" + fix), userLead, wsSw)).andExpect(jsonPath("$.path_label").value("root.n" + root + ".n" + cause + ".n" + fix))
        .andExpect(jsonPath("$.refine_status").value("done")).andExpect(jsonPath("$.resolution_summary").value("풀 확장으로 해결"));
    mvc.perform(as(get("/logs/" + cause), userLead, wsSw)).andExpect(jsonPath("$.refine_status").value("pending"));   // 정제 결과 없음 → 워커 대상
    mvc.perform(as(get("/logs/" + root), userLead, wsSw)).andExpect(jsonPath("$.error_code").value("HTTP 504"))
        .andExpect(jsonPath("$.created_at").value(org.hamcrest.Matchers.startsWith("2026-10-01")));
    assertThat(jdbc.sql("SELECT source FROM log_nodes WHERE id = ?").param(root).query(String.class).single()).isEqualTo("synthetic");
    assertThat(jdbc.sql("SELECT origin FROM log_edges WHERE child_id = ?").param(fix).query(String.class).single()).isEqualTo("bulk");
    // 같은 요청을 다시 보내면 전부 existing, 새 행 없음
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content(body)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(0)).andExpect(jsonPath("$.existing").value(3)).andExpect(jsonPath("$.items[0].id").value(root));
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes WHERE workspace_id = ? AND source_ref LIKE 'syn-%'").param(wsSw).query(Long.class).single()).isEqualTo(3L);
    assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'bulk_import' AND workspace_id = ?").param(wsSw).query(Long.class).single()).isEqualTo(2L);
  }

  @Test
  void bulkAppliesSameRulesAsSingleCreate() throws Exception {
    // success 루트 → problem_title 필수, 있으면 fail 루트 자동 생성 + 완료
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content("{\"items\":[{\"type\":\"success\",\"title\":\"s\",\"body\":\"b\"}]}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("PROBLEM_TITLE_REQUIRED"));
    MvcResult r = mvc.perform(as(post("/logs/bulk"), userLead, wsSw)
        .content("{\"items\":[{\"type\":\"success\",\"title\":\"s\",\"body\":\"b\",\"problem_title\":\"장애 P\",\"error_code\":\"E-9\"}]}"))
        .andExpect(status().isCreated()).andReturn();
    int fix = JsonPath.read(r.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.items[0].id");
    mvc.perform(as(get("/logs/" + fix), userLead, wsSw)).andExpect(jsonPath("$.depth").value(1)).andExpect(jsonPath("$.error_code").value("E-9"));
    int root = JsonPath.read(mvc.perform(as(get("/logs/" + fix), userLead, wsSw)).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8), "$.root_id");
    mvc.perform(as(get("/logs/" + root), userLead, wsSw)).andExpect(jsonPath("$.title").value("장애 P")).andExpect(jsonPath("$.resolved_at").isNotEmpty());
    // R12: success 의 부모가 success 면 409. 잘못된 parent_ref 는 400. 다른 워크스페이스 id 는 409
    String badType = "{\"items\":[{\"ref\":\"a\",\"type\":\"success\",\"title\":\"s\",\"body\":\"b\",\"problem_title\":\"P\"},{\"type\":\"success\",\"title\":\"t\",\"body\":\"b\",\"parent_ref\":\"a\"}]}";
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content(badType)).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("EDGE_PARENT_TYPE"));
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content("{\"items\":[{\"type\":\"fail\",\"title\":\"t\",\"body\":\"b\",\"parent_ref\":\"nope\"}]}"))
        .andExpect(status().isBadRequest());
    long hrNode = node(wsHr, "fail", "HR", userBackend);
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content("{\"items\":[{\"type\":\"fail\",\"title\":\"t\",\"body\":\"b\",\"parent_ref\":\"" + hrNode + "\"}]}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("PARENT_NOT_FOUND"));
    // 실패한 요청의 전체 롤백은 @Transactional 이 보장한다 (테스트 자체가 한 트랜잭션 안이라 여기서는 행이 보여 단언하지 않는다)
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content("{\"items\":[]}")).andExpect(status().isBadRequest());
  }
}
