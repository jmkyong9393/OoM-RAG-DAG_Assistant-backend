package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.contains;
import static org.hamcrest.Matchers.empty;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import org.junit.jupiter.api.Test;
import org.springframework.dao.DataIntegrityViolationException;

/** 사례 체인 기록 구조 1단계: 간선의 연결 사유, 장애 건을 해결한 기록(여러 개, 지금 유효한 것만 표시). */
class ChainRecordApiTest extends AbstractApiTest {

  private long created(String json) throws Exception {
    String body = mvc.perform(as(post("/logs"), userBackend, wsSw).content(json)).andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
    return JsonPath.parse(body).read("$.id", Long.class);
  }

  @Test
  void edgeReasonIsStoredOnRegisterAndLinkAndShownInChain() throws Exception {
    long root = node(wsSw, "fail", "포털 정시 500", userBackend);
    long cause = created("{\"type\":\"fail\",\"title\":\"원인: 캐시 동시 만료\",\"body\":\"b\",\"parent_id\":" + root
        + ",\"edge_reason\":\"  정시 직전 캐시 키 만료 시각이 같았다  \"}");
    long other = node(wsSw, "ref", "캐시 키 설계 가이드", userBackend);
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + cause + ",\"child_id\":" + other + ",\"reason\":\"만료 분산 기준이 여기 있다\"}"))
        .andExpect(status().isCreated());
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$.edges", hasSize(2)))
        .andExpect(jsonPath("$.edges[?(@.child_id == " + cause + ")].reason").value("정시 직전 캐시 키 만료 시각이 같았다"))
        .andExpect(jsonPath("$.edges[?(@.child_id == " + other + ")].reason").value("만료 분산 기준이 여기 있다"));
    // 300자 초과는 거절하고 로그도 만들지 않는다. 사유 없는 연결은 그대로 된다
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"fail\",\"title\":\"t\",\"body\":\"b\",\"parent_id\":" + root
            + ",\"edge_reason\":\"" + "가".repeat(301) + "\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes WHERE title = 't'").query(Long.class).single()).isZero();
    long plain = created("{\"type\":\"fail\",\"title\":\"원인2\",\"body\":\"b\",\"parent_id\":" + root + "}");
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(jsonPath("$.edges[?(@.child_id == " + plain + ")].reason").value(contains((Object) null)));
  }

  @Test
  void oneShotAndResolveRootRecordTheResolvingLog() throws Exception {
    long fix = created("{\"type\":\"success\",\"title\":\"해결: 재시작\",\"body\":\"b\",\"problem_title\":\"배치 멈춤\"}");
    long root = jdbc.sql("SELECT parent_id FROM log_edges WHERE child_id = ?").param(fix).query(Long.class).single();
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw))
        .andExpect(jsonPath("$.nodes[?(@.id == " + fix + ")].resolves_root").value(contains(true)))
        .andExpect(jsonPath("$.nodes[?(@.id == " + root + ")].resolves_root").value(contains(false)));

    long root2 = node(wsSw, "fail", "로그인 지연", userBackend);
    long cause = node(wsSw, "fail", "원인: 세션 저장소 지연", userBackend);
    edge(wsSw, root2, cause);
    long fix2 = created("{\"type\":\"success\",\"title\":\"해결: 인덱스 추가\",\"body\":\"b\",\"parent_id\":" + cause + ",\"resolve_root\":true}");
    assertThat(jdbc.sql("SELECT node_id FROM log_resolutions WHERE root_id = ?").param(root2).query(Long.class).list()).containsExactly(fix2);
    // resolve_root 없이 붙인 해결은 해결 기록으로 남기지 않는다
    long fix3 = created("{\"type\":\"success\",\"title\":\"해결: 캐시\",\"body\":\"b\",\"parent_id\":" + cause + "}");
    assertThat(jdbc.sql("SELECT count(*) FROM log_resolutions WHERE node_id = ?").param(fix3).query(Long.class).single()).isZero();
  }

  @Test
  void patchResolvedValidatesAndReplacesResolvingLogs() throws Exception {
    long root = node(wsSw, "fail", "E-403", userBackend);
    long c1 = node(wsSw, "fail", "원인 ① 배치 누락", userBackend);
    long c2 = node(wsSw, "fail", "원인 ② 시간 오차", userBackend);
    long f1 = node(wsSw, "success", "해결: 갱신 주기 30분", userBackend);
    long f2 = node(wsSw, "success", "해결: NTP 동기화", userBackend);
    long note = node(wsSw, "success", "해결: 사후분석 작성", userBackend);
    edge(wsSw, root, c1); edge(wsSw, root, c2); edge(wsSw, c1, f1); edge(wsSw, c2, f2); edge(wsSw, root, note);
    long outside = node(wsSw, "success", "다른 건의 해결", userBackend);
    long foreign = node(wsHr, "success", "다른 부서", userBackend);

    for (long bad : new long[] {c1, outside, foreign, 999999L}) {   // 실패 기록 · 이 건 밖 · 다른 부서 · 없는 기록
      mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"resolved_node_ids\":[" + bad + "]}"))
          .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("RESOLVED_NODE_INVALID"));
    }
    assertThat(jdbc.sql("SELECT resolved_at IS NULL FROM log_nodes WHERE id = ?").param(root).query(Boolean.class).single()).isTrue();

    // 원인마다 해결이 따로 — 둘 다 해결 기록. 사후분석 작성은 후속 절차라 넣지 않는다
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"resolved_node_ids\":[" + f2 + "," + f1 + "," + f1 + "]}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_at").isNotEmpty())
        .andExpect(jsonPath("$.resolved_node_ids", hasSize(2)));
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw))
        .andExpect(jsonPath("$.nodes[?(@.resolves_root == true)].id", hasSize(2)))
        .andExpect(jsonPath("$.nodes[?(@.id == " + note + ")].resolves_root").value(contains(false)));

    // 해결 기록이 폐기되거나 연결이 끊기면 표시에서 빠지고, 복원하면 돌아온다
    mvc.perform(as(patch("/logs/" + f1), userBackend, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"옛 조치\"}")).andExpect(status().isOk());
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(jsonPath("$.nodes[?(@.resolves_root == true)].id").value(contains((int) f2)));
    mvc.perform(as(patch("/logs/" + f1), userBackend, wsSw).content("{\"status\":\"active\"}")).andExpect(status().isOk());
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(jsonPath("$.nodes[?(@.resolves_root == true)].id", hasSize(2)));
    mvc.perform(as(delete("/edges"), userBackend, wsSw).content("{\"parent_id\":" + c2 + ",\"child_id\":" + f2 + "}")).andExpect(status().isNoContent());
    mvc.perform(as(get("/chains/" + root), userBackend, wsSw)).andExpect(jsonPath("$.nodes[?(@.resolves_root == true)].id").value(contains((int) f1)));

    // 목록을 주면 통째로 바꾸고, 안 주면 그대로 둔다. 폐기된 기록은 새로 지정할 수 없다
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true}")).andExpect(jsonPath("$.resolved_node_ids").value(contains((int) f1)));
    mvc.perform(as(patch("/logs/" + note), userBackend, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"x\"}")).andExpect(status().isOk());
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"resolved_node_ids\":[" + note + "]}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("RESOLVED_NODE_INVALID"));
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"resolved_node_ids\":[]}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_node_ids", empty()));

    // 완료를 되돌리면 해결 기록도 비운다
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"resolved_node_ids\":[" + f1 + "]}")).andExpect(status().isOk());
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":false}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_at").isEmpty()).andExpect(jsonPath("$.resolved_node_ids", empty()));
    assertThat(jdbc.sql("SELECT count(*) FROM log_resolutions WHERE root_id = ?").param(root).query(Long.class).single()).isZero();
  }

  @Test
  void bulkCarriesParentReasonAndExplicitResolves() throws Exception {
    String bulk = """
        {"items": [
          {"ref": "r", "type": "fail", "title": "결재 지연", "body": "b", "source_ref": "cr-1"},
          {"ref": "c", "type": "fail", "title": "원인: 대결자 미지정", "body": "b", "parent_ref": "r", "parent_reason": "휴가 중 결재가 멈췄다", "source_ref": "cr-2"},
          {"ref": "f", "type": "success", "title": "해결: 대결자 필수", "body": "b", "parent_ref": "c", "resolves": true, "source_ref": "cr-3"},
          {"ref": "g", "type": "success", "title": "해결: 기한 알림 봇", "body": "b", "parent_ref": "c", "source_ref": "cr-4"},
          {"ref": "o", "type": "success", "title": "해결: 한 번에", "body": "b", "problem_title": "메일 반송", "source_ref": "cr-5"}]}""";
    mvc.perform(as(post("/logs/bulk"), userBackend, wsSw).content(bulk)).andExpect(status().isCreated()).andExpect(jsonPath("$.created").value(5));
    long r = id("cr-1"), c = id("cr-2"), f = id("cr-3"), g = id("cr-4"), o = id("cr-5");
    assertThat(jdbc.sql("SELECT reason FROM log_edges WHERE parent_id = ? AND child_id = ?").params(r, c).query(String.class).single()).isEqualTo("휴가 중 결재가 멈췄다");
    assertThat(jdbc.sql("SELECT node_id FROM log_resolutions WHERE root_id = ?").param(r).query(Long.class).list()).containsExactly(f);
    assertThat(jdbc.sql("SELECT count(*) FROM log_resolutions WHERE node_id = ?").param(g).query(Long.class).single()).isZero();
    assertThat(jdbc.sql("SELECT count(*) FROM log_resolutions WHERE node_id = ?").param(o).query(Long.class).single()).isEqualTo(1L);
  }

  @Test
  void dbRejectsResolutionOfWrongType() {
    long root = node(wsSw, "fail", "R", userBackend);
    long cause = node(wsSw, "fail", "C", userBackend);
    org.assertj.core.api.Assertions.assertThatThrownBy(() -> jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)")
        .params(wsSw, root, cause).update()).isInstanceOf(DataIntegrityViolationException.class).hasMessageContaining("RESOLUTION_TYPE");
  }

  @Test
  void dbRejectsResolutionAcrossWorkspaces() {
    long root = node(wsSw, "fail", "R", userBackend);
    long foreign = node(wsHr, "success", "F", userBackend);
    org.assertj.core.api.Assertions.assertThatThrownBy(() -> jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)")
        .params(wsSw, root, foreign).update()).isInstanceOf(DataIntegrityViolationException.class).hasMessageContaining("RESOLUTION_CROSS_WORKSPACE");
  }

  private long id(String sourceRef) {
    return jdbc.sql("SELECT id FROM log_nodes WHERE source_ref = ?").param(sourceRef).query(Long.class).single();
  }
}
