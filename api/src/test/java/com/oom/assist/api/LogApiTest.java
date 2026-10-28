package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import org.junit.jupiter.api.Test;
import org.springframework.test.web.servlet.MvcResult;

/** 업무 로그 API — 규칙 R2·R3·R4·R5·R12 와 응답 계약(snake_case, {items, counts}). */
class LogApiTest extends AbstractApiTest {

  // ── 등록 (R2 · R3 · R12) ───────────────────────────────────────────────

  @Test
  void createFailRootReturns201WithContractFields() throws Exception {
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"fail\",\"title\":\"VPN E-403 반복\",\"body\":\"오전 9시 이후 발생\",\"compose_ms\":12000}"))
        .andExpect(status().isCreated())
        .andExpect(jsonPath("$.type").value("fail"))
        .andExpect(jsonPath("$.status").value("active"))
        .andExpect(jsonPath("$.author_name").value("장문경"))
        .andExpect(jsonPath("$.refine_status").value("pending"))
        .andExpect(jsonPath("$.root_id").isNumber())
        .andExpect(jsonPath("$.depth").value(0))
        .andExpect(jsonPath("$.path_label").isString())
        .andExpect(jsonPath("$.keywords").isArray())
        .andExpect(jsonPath("$.resolved_at").isEmpty())
        .andExpect(jsonPath("$.workspace_id").value(wsSw));
  }

  @Test
  void createSuccessWithoutParentNeedsProblemTitle() throws Exception {
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"success\",\"title\":\"해결\",\"body\":\"b\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("PROBLEM_TITLE_REQUIRED"));
  }

  @Test
  void createSuccessWithProblemTitleAutoCreatesResolvedFailRoot() throws Exception {
    MvcResult r = mvc.perform(as(post("/logs"), userBackend, wsSw)
            .content("{\"type\":\"success\",\"title\":\"NTP 동기화\",\"body\":\"시간 오차\",\"problem_title\":\"토큰 만료 오류\"}"))
        .andExpect(status().isCreated())
        .andExpect(jsonPath("$.type").value("success"))
        .andExpect(jsonPath("$.depth").value(1))
        .andReturn();
    int rootId = JsonPath.read(r.getResponse().getContentAsString(), "$.root_id");
    mvc.perform(as(get("/logs/" + rootId), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.type").value("fail"))
        .andExpect(jsonPath("$.title").value("토큰 만료 오류"))
        .andExpect(jsonPath("$.resolved_at").isNotEmpty());
    // 감사 로그
    assertThat(jdbc.sql("SELECT action FROM audit_log WHERE table_name = 'log_nodes' AND row_id = ?").param(String.valueOf(rootId)).query(String.class).list())
        .containsExactly("resolved");
  }

  @Test
  void createWithParentInOtherWorkspaceOrDeprecatedIsRejected() throws Exception {
    long foreign = node(wsHr, "fail", "HR 장애", userBackend);
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"ref\",\"title\":\"x\",\"body\":\"y\",\"parent_id\":" + foreign + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("PARENT_NOT_FOUND"));
    long dep = node(wsSw, "fail", "폐기된 장애", userBackend);
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(dep).update();
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"ref\",\"title\":\"x\",\"body\":\"y\",\"parent_id\":" + dep + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("PARENT_DEPRECATED"));
  }

  @Test
  void successUnderSuccessIsRejected() throws Exception {
    long root = node(wsSw, "fail", "장애", userBackend);
    long s = node(wsSw, "success", "해결1", userBackend);
    edge(wsSw, root, s);
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"success\",\"title\":\"해결2\",\"body\":\"b\",\"parent_id\":" + s + "}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("EDGE_PARENT_TYPE"));
  }

  @Test
  void createSuccessUnderFailWithResolveRootResolvesTheRoot() throws Exception {
    long root = node(wsSw, "fail", "장애", userBackend);
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"success\",\"title\":\"해결\",\"body\":\"b\",\"parent_id\":" + root + ",\"resolve_root\":true}"))
        .andExpect(status().isCreated()).andExpect(jsonPath("$.root_id").value(root));
    mvc.perform(as(get("/logs/" + root), userBackend, wsSw)).andExpect(jsonPath("$.resolved_at").isNotEmpty()).andExpect(jsonPath("$.resolve_note").isEmpty());
  }

  @Test
  void invalidTypeAndMissingFields() throws Exception {
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"bug\",\"title\":\"x\",\"body\":\"y\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_TYPE"));
    mvc.perform(as(post("/logs"), userBackend, wsSw).content("{\"type\":\"fail\",\"title\":\" \",\"body\":\"y\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
  }

  // ── 조회 ───────────────────────────────────────────────────────────────

  @Test
  void listReturnsItemsAndActiveCountsScopedToWorkspace() throws Exception {
    long a = node(wsSw, "fail", "A 장애", userBackend);
    node(wsSw, "ref", "B 참고", userBackend);
    long dep = node(wsSw, "success", "C 해결", userBackend);
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated', error_code = 'E-9' WHERE id = ?").param(dep).update();
    node(wsHr, "fail", "HR 것", userBackend);
    mvc.perform(as(get("/logs?limit=0"), userBackend, wsSw))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.items", hasSize(3)))
        .andExpect(jsonPath("$.counts.all").value(2))
        .andExpect(jsonPath("$.counts.fail").value(1))
        .andExpect(jsonPath("$.counts.success").value(0))
        .andExpect(jsonPath("$.counts.ref").value(1));
    mvc.perform(as(get("/logs?type=fail&q=장애"), userBackend, wsSw)).andExpect(jsonPath("$.items", hasSize(1))).andExpect(jsonPath("$.items[0].id").value(a));
    mvc.perform(as(get("/logs?q=E-9"), userBackend, wsSw)).andExpect(jsonPath("$.items", hasSize(1)));
    mvc.perform(as(get("/logs?status=active"), userBackend, wsSw)).andExpect(jsonPath("$.items", hasSize(2)));
    mvc.perform(as(get("/logs/weekly-count"), userBackend, wsSw)).andExpect(jsonPath("$.count").value(3));
    mvc.perform(as(get("/logs/" + a), userBackend, wsHr)).andExpect(status().isNotFound());
  }

  // ── 완료 (R4) ──────────────────────────────────────────────────────────

  @Test
  void resolveRulesNotRootRefRootNoteRequired() throws Exception {
    long root = node(wsSw, "fail", "장애", userBackend);
    long cause = node(wsSw, "ref", "원인", userBackend);
    edge(wsSw, root, cause);
    long refRoot = node(wsSw, "ref", "참고 루트", userBackend);

    mvc.perform(as(patch("/logs/" + cause), userBackend, wsSw).content("{\"resolved\":true}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("NOT_ROOT"));
    mvc.perform(as(patch("/logs/" + refRoot), userBackend, wsSw).content("{\"resolved\":true}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REF_ROOT"));
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("NOTE_REQUIRED"));
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"note\":\"재현 안 됨\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_at").isNotEmpty()).andExpect(jsonPath("$.resolve_note").value("재현 안 됨"));
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":false}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_at").isEmpty()).andExpect(jsonPath("$.resolve_note").isEmpty());
  }

  @Test
  void resolveWithActiveSuccessDescendantNeedsNoNoteAndIgnoresGivenNote() throws Exception {
    long root = node(wsSw, "fail", "장애", userBackend);
    long fix = node(wsSw, "success", "해결", userBackend);
    edge(wsSw, root, fix);
    mvc.perform(as(patch("/logs/" + root), userBackend, wsSw).content("{\"resolved\":true,\"note\":\"무시됨\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.resolved_at").isNotEmpty()).andExpect(jsonPath("$.resolve_note").isEmpty());
  }

  // ── 폐기·복원 (R5) ────────────────────────────────────────────────────

  @Test
  void deprecateNeedsReasonAndCascadeFollowsOrphans() throws Exception {
    long root = node(wsSw, "fail", "장애", userBackend);
    long c1 = node(wsSw, "ref", "원인1", userBackend);
    long c2 = node(wsSw, "ref", "원인2", userBackend);
    long onlyC1 = node(wsSw, "success", "해결A", userBackend);
    long shared = node(wsSw, "success", "해결B", userBackend);
    edge(wsSw, root, c1); edge(wsSw, root, c2); edge(wsSw, c1, onlyC1); edge(wsSw, c1, shared); edge(wsSw, c2, shared);

    mvc.perform(as(patch("/logs/" + c1), userBackend, wsSw).content("{\"status\":\"deprecated\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("REASON_REQUIRED"));
    mvc.perform(as(get("/logs/" + c1 + "/orphaned"), userBackend, wsSw))
        .andExpect(status().isOk()).andExpect(jsonPath("$.orphaned", hasSize(1))).andExpect(jsonPath("$.orphaned[0]").value(onlyC1));
    mvc.perform(as(patch("/logs/" + c1), userBackend, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"오진\",\"cascade\":true}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("deprecated"))
        .andExpect(jsonPath("$.deprecate_reason").value("오진"))
        .andExpect(jsonPath("$.affected", hasSize(1))).andExpect(jsonPath("$.affected[0]").value(onlyC1));
    mvc.perform(as(get("/logs/" + onlyC1), userBackend, wsSw))
        .andExpect(jsonPath("$.status").value("deprecated")).andExpect(jsonPath("$.deprecated_via").value(c1)).andExpect(jsonPath("$.deprecate_reason").value("오진"));
    mvc.perform(as(get("/logs/" + shared), userBackend, wsSw)).andExpect(jsonPath("$.status").value("active"));
    // 복원은 연동 복원
    mvc.perform(as(patch("/logs/" + c1), userBackend, wsSw).content("{\"status\":\"active\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("active")).andExpect(jsonPath("$.affected[0]").value(onlyC1));
    mvc.perform(as(get("/logs/" + onlyC1), userBackend, wsSw)).andExpect(jsonPath("$.status").value("active")).andExpect(jsonPath("$.deprecated_via").isEmpty());
    assertThat(jdbc.sql("SELECT action FROM audit_log WHERE table_name = 'log_nodes' AND row_id = ? ORDER BY id").param(String.valueOf(c1)).query(String.class).list())
        .containsExactly("deprecated", "restored");
  }

  // ── 정제 재시도 · 유효 확인 ────────────────────────────────────────────

  @Test
  void refineRequeuesAndConfirmTouchesTimestamp() throws Exception {
    long n = node(wsSw, "fail", "장애", userBackend);
    jdbc.sql("UPDATE log_nodes SET refine_status = 'failed', refine_attempts = 3, refine_error = 'boom' WHERE id = ?").param(n).update();
    mvc.perform(as(post("/logs/" + n + "/refine"), userBackend, wsSw)).andExpect(status().isAccepted()).andExpect(jsonPath("$.refine_status").value("pending"));
    assertThat(jdbc.sql("SELECT refine_attempts FROM log_nodes WHERE id = ?").param(n).query(Integer.class).single()).isZero();
    mvc.perform(as(post("/logs/" + n + "/confirm"), userBackend, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$.last_confirmed_at").isNotEmpty());
  }

  @Test
  void patchWithoutResolvedOrStatusIs400() throws Exception {
    long n = node(wsSw, "fail", "장애", userBackend);
    mvc.perform(as(patch("/logs/" + n), userBackend, wsSw).content("{}")).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("VALIDATION"));
  }
}
