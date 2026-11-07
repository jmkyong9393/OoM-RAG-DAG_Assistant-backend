package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.hamcrest.Matchers.contains;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;

/** 사례 체인 기록 구조 2단계: 장애 건의 원인 표시(category='cause'), 한 번에 등록의 원인 한 줄. */
class CauseCategoryApiTest extends AbstractApiTest {

  private long created(String json) throws Exception {
    String body = mvc.perform(as(post("/logs"), userBackend, wsSw).content(json)).andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
    return JsonPath.parse(body).read("$.id", Long.class);
  }

  private String category(long id) {
    return jdbc.sql("SELECT category FROM log_nodes WHERE id = ?").param(id).query(String.class).optional().orElse(null);
  }

  @Test
  void causeIsMarkedOnRegisterAndShownInChainOnlyWhileItHasAParent() throws Exception {
    long root = node(wsSw, "fail", "포털 정시 500", userBackend);
    long cause = created("{\"type\":\"fail\",\"title\":\"캐시 동시 만료\",\"body\":\"b\",\"parent_id\":" + root + ",\"category\":\"cause\"}");
    long attempt = created("{\"type\":\"fail\",\"title\":\"재시작해 봄 — 그대로\",\"body\":\"b\",\"parent_id\":" + root + "}");
    mvc.perform(as(get("/logs/" + cause), userBackend, wsSw)).andExpect(jsonPath("$.category").value("cause"));
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw))
        .andExpect(jsonPath("$.nodes[?(@.id == " + cause + ")].is_cause").value(contains(true)))
        .andExpect(jsonPath("$.nodes[?(@.id == " + attempt + ")].is_cause").value(contains(false)))
        .andExpect(jsonPath("$.nodes[?(@.id == " + root + ")].is_cause").value(contains(false)));
    // 연결이 끊겨 루트가 되면 원인으로 보이지 않는다. 표시 자체는 남아 다시 이으면 돌아온다
    mvc.perform(as(delete("/edges"), userBackend, wsSw).content("{\"parent_id\":" + root + ",\"child_id\":" + cause + "}")).andExpect(status().isNoContent());
    mvc.perform(as(get("/chains/" + cause), userBackend, wsSw)).andExpect(jsonPath("$.nodes[?(@.id == " + cause + ")].is_cause").value(contains(false)));
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + root + ",\"child_id\":" + cause + "}")).andExpect(status().isCreated());
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(jsonPath("$.nodes[?(@.id == " + cause + ")].is_cause").value(contains(true)));
  }

  @Test
  void causeNeedsAFailWithParentAndAKnownValue() throws Exception {
    long root = node(wsSw, "fail", "R", userBackend);
    for (String bad : new String[] {
        "{\"type\":\"fail\",\"title\":\"t\",\"body\":\"b\",\"category\":\"cause\"}",                              // 상위 없음
        "{\"type\":\"success\",\"title\":\"t\",\"body\":\"b\",\"parent_id\":" + root + ",\"category\":\"cause\"}",  // fail 아님
        "{\"type\":\"fail\",\"title\":\"t\",\"body\":\"b\",\"parent_id\":" + root + ",\"category\":\"symptom\"}"}) { // 모르는 값
      mvc.perform(as(post("/logs"), userBackend, wsSw).content(bad)).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("CATEGORY_INVALID"));
    }
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes WHERE title = 't'").query(Long.class).single()).isZero();
  }

  @Test
  void patchSetsAndClearsCause() throws Exception {
    long root = node(wsSw, "fail", "R", userBackend);
    long c = node(wsSw, "fail", "원인: 인증서 만료", userBackend);
    long fix = node(wsSw, "success", "해결: 갱신", userBackend);
    edge(wsSw, root, c); edge(wsSw, c, fix);
    mvc.perform(as(patch("/logs/" + c), userBackend, wsSw).content("{\"category\":\"cause\"}")).andExpect(status().isOk()).andExpect(jsonPath("$.category").value("cause"));
    for (long bad : new long[] {root, fix}) {   // 루트 · success
      mvc.perform(as(patch("/logs/" + bad), userBackend, wsSw).content("{\"category\":\"cause\"}"))
          .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("CATEGORY_INVALID"));
    }
    mvc.perform(as(patch("/logs/" + c), userBackend, wsSw).content("{\"category\":\"\"}")).andExpect(status().isOk()).andExpect(jsonPath("$.category").isEmpty());
    assertThat(category(c)).isNull();
    long other = node(wsHr, "fail", "다른 부서", userBackend);
    mvc.perform(as(patch("/logs/" + other), userBackend, wsSw).content("{\"category\":\"cause\"}")).andExpect(status().isNotFound());
  }

  @Test
  void oneShotWithCauseTitleBuildsRootCauseFix() throws Exception {
    long fix = created("{\"type\":\"success\",\"title\":\"해결: 대결자 필수\",\"body\":\"b\",\"problem_title\":\"결재 지연\",\"cause_title\":\" 대결자 미지정 \","
        + "\"edge_reason\":\"휴가 중 결재가 멈췄다\"}");
    long cause = jdbc.sql("SELECT parent_id FROM log_edges WHERE child_id = ?").param(fix).query(Long.class).single();
    long root = jdbc.sql("SELECT parent_id FROM log_edges WHERE child_id = ?").param(cause).query(Long.class).single();
    assertThat(jdbc.sql("SELECT title FROM log_nodes WHERE id = ?").param(cause).query(String.class).single()).isEqualTo("대결자 미지정");
    assertThat(category(cause)).isEqualTo("cause");
    assertThat(jdbc.sql("SELECT reason FROM log_edges WHERE parent_id = ? AND child_id = ?").params(cause, fix).query(String.class).single()).isEqualTo("휴가 중 결재가 멈췄다");
    assertThat(jdbc.sql("SELECT resolved_at IS NOT NULL FROM log_nodes WHERE id = ?").param(root).query(Boolean.class).single()).isTrue();
    assertThat(jdbc.sql("SELECT node_id FROM log_resolutions WHERE root_id = ?").param(root).query(Long.class).list()).containsExactly(fix);
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw))
        .andExpect(jsonPath("$.nodes[?(@.id == " + cause + ")].is_cause").value(contains(true)))
        .andExpect(jsonPath("$.nodes[?(@.id == " + fix + ")].resolves_root").value(contains(true)));
    // cause_title 은 한 번에 등록에서만
    long r = node(wsSw, "fail", "R", userBackend);
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"success\",\"title\":\"x\",\"body\":\"b\",\"parent_id\":" + r + ",\"cause_title\":\"c\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
  }

  @Test
  void bulkMarksCausesAndFillsExistingOnlyWhenEmpty() throws Exception {
    String first = """
        {"items": [
          {"ref": "r", "type": "fail", "title": "메일 반송", "body": "b", "source_ref": "cc-1"},
          {"ref": "c", "type": "fail", "title": "원인: SPF 누락", "body": "b", "parent_ref": "r", "source_ref": "cc-2"}]}""";
    mvc.perform(as(post("/logs/bulk"), userBackend, wsSw).content(first)).andExpect(status().isCreated());
    long c = id("cc-2");
    assertThat(category(c)).isNull();
    String again = """
        {"items": [
          {"ref": "r", "type": "fail", "title": "메일 반송", "body": "b", "source_ref": "cc-1"},
          {"ref": "c", "type": "fail", "title": "원인: SPF 누락", "body": "b", "parent_ref": "r", "category": "cause", "source_ref": "cc-2"},
          {"ref": "d", "type": "fail", "title": "원인: DKIM 키 교체", "body": "b", "parent_ref": "r", "category": "cause", "source_ref": "cc-3"}]}""";
    mvc.perform(as(post("/logs/bulk"), userBackend, wsSw).content(again)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(1)).andExpect(jsonPath("$.existing").value(2));
    assertThat(category(c)).isEqualTo("cause");
    assertThat(category(id("cc-3"))).isEqualTo("cause");
    assertThat(category(id("cc-1"))).isNull();
    mvc.perform(as(post("/logs/bulk"), userBackend, wsSw).content("""
        {"items": [{"ref": "x", "type": "fail", "title": "루트 원인", "body": "b", "category": "cause", "source_ref": "cc-9"}]}"""))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("CATEGORY_INVALID"));
  }

  @Test
  void dbRejectsCauseOnNonFail() {
    long s = node(wsSw, "success", "S", userBackend);
    assertThatThrownBy(() -> jdbc.sql("UPDATE log_nodes SET category = 'cause' WHERE id = ?").param(s).update())
        .isInstanceOf(DataIntegrityViolationException.class).hasMessageContaining("log_nodes_category_chk");
  }

  private long id(String sourceRef) {
    return jdbc.sql("SELECT id FROM log_nodes WHERE source_ref = ?").param(sourceRef).query(Long.class).single();
  }
}
