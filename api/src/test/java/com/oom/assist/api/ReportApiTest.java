package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.ObjectMapper;

/**
 * 장애 보고서(R1) — POST /reports 가 사실 칸을 DB 에서 조립해 generating 으로 저장하고(AI 칸은 AI 워커 몫), GET 이 그 행을 돌려준다.
 * 번호(시각순) · 구분 · 제목 머리 떼기 · 외부판(관리자만, 작성자 비움) · 같은 내용이면 재사용(force 면 새로) · 기록이 바뀌면 stale · 루트가 아니면 400 · 다른 부서 404.
 */
class ReportApiTest extends AbstractApiTest {

  @Autowired ObjectMapper json;

  long root, cause, fix, extra, side, memo, dead;

  @BeforeEach
  void seedIncident() {
    root = at(node(wsSw, "fail", "DB 커넥션 풀 고갈", userBackend), "2026-07-10 08:00+00");
    cause = at(node(wsSw, "fail", "원인: 풀 크기 10 고정", userBackend), "2026-07-10 09:00+00");
    fix = at(node(wsSw, "success", "해결: 풀 최대 20", userLead), "2026-07-12 08:00+00");
    extra = at(node(wsSw, "success", "런북 작성", userLead), "2026-07-13 08:00+00");
    side = at(node(wsSw, "fail", "풀 확대 후 DB 연결 한도 초과", userBackend), "2026-07-20 08:00+00");
    memo = at(node(wsSw, "ref", "참고: 진단 쿼리", userBackend), "2026-07-11 08:00+00");
    dead = at(node(wsSw, "success", "폐기된 옛 조치", userBackend), "2026-07-10 10:00+00");
    edge(wsSw, root, cause); edge(wsSw, cause, fix); edge(wsSw, root, extra); edge(wsSw, fix, side); edge(wsSw, root, memo); edge(wsSw, root, dead);
    jdbc.sql("UPDATE log_edges SET reason = '풀이 작아 대기가 쌓였다' WHERE child_id = ?").param(cause).update();
    jdbc.sql("UPDATE log_nodes SET category = 'cause', error_code = 'E-POOL' WHERE id = ?").param(cause).update();
    jdbc.sql("UPDATE log_nodes SET resolution_summary = '풀을 20 으로 늘려 해소' WHERE id = ?").param(fix).update();
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(dead).update();
    jdbc.sql("UPDATE log_nodes SET resolved_at = '2026-07-12 08:00+00' WHERE id = ?").param(root).update();
    jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)").params(wsSw, root, fix).update();
  }

  private long at(long id, String ts) {
    jdbc.sql("UPDATE log_nodes SET created_at = ?::timestamptz WHERE id = ?").params(ts, id).update();
    return id;
  }

  private ResultActions create(long user, long ws, long rootId, String audience, boolean force) throws Exception {
    return mvc.perform(as(post("/reports"), user, ws)
        .content("{\"root_id\": %d, \"audience\": \"%s\", \"force\": %s}".formatted(rootId, audience, force)));
  }

  private long idOf(ResultActions r) throws Exception {
    return json.readTree(r.andReturn().getResponse().getContentAsString()).get("id").asLong();
  }

  @Test
  void internalReportAssemblesFactsInTimeOrder() throws Exception {
    create(userBackend, wsSw, root, "internal", false).andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("generating")).andExpect(jsonPath("$.template_version").value("R1"))
        .andExpect(jsonPath("$.audience").value("internal")).andExpect(jsonPath("$.stale").value(false))
        .andExpect(jsonPath("$.generated_at").doesNotExist())
        // 머리말
        .andExpect(jsonPath("$.content.meta.title").value("DB 커넥션 풀 고갈")).andExpect(jsonPath("$.content.meta.status").value("resolved"))
        .andExpect(jsonPath("$.content.meta.interim").value(false)).andExpect(jsonPath("$.content.meta.workspace_name").value("SW 개발"))
        .andExpect(jsonPath("$.content.meta.duration_minutes").value(2880)).andExpect(jsonPath("$.content.meta.record_count").value(6))
        .andExpect(jsonPath("$.content.meta.truncated").value(false))
        .andExpect(jsonPath("$.content.occurrence.error_codes[0]").value("E-POOL")).andExpect(jsonPath("$.content.occurrence.reported_by").value("장문경"))
        // 남은 확인 = 아래에 조치(success) 기록이 없는 원인 · 추가 문제. 원인(N2)은 해결이 달려 있어 빠진다
        .andExpect(jsonPath("$.content.open_items.length()").value(1)).andExpect(jsonPath("$.content.open_items[0].refs[0]").value("N6"))
        .andExpect(jsonPath("$.content.open_items[0].text").value("풀 확대 후 DB 연결 한도 초과"))
        // 번호는 작성 시각순, 폐기된 기록은 빠진다. 하위 기록 제목의 머리는 뗀다
        .andExpect(jsonPath("$.content.records.length()").value(6))
        .andExpect(jsonPath("$.content.records[0].kind").value("occurrence")).andExpect(jsonPath("$.content.records[0].id").value(root))
        .andExpect(jsonPath("$.content.records[1].ref").value("N2")).andExpect(jsonPath("$.content.records[1].kind").value("cause"))
        .andExpect(jsonPath("$.content.records[1].title").value("풀 크기 10 고정"))
        .andExpect(jsonPath("$.content.records[2].kind").value("reference")).andExpect(jsonPath("$.content.records[2].title").value("진단 쿼리"))
        .andExpect(jsonPath("$.content.records[3].kind").value("resolution")).andExpect(jsonPath("$.content.records[4].kind").value("action"))
        .andExpect(jsonPath("$.content.records[5].kind").value("side_issue"))
        .andExpect(jsonPath("$.content.timeline[3].author").value("배종현"))
        // 섹션
        .andExpect(jsonPath("$.content.causes[0].ref").value("N2")).andExpect(jsonPath("$.content.causes[0].link_reason").value("풀이 작아 대기가 쌓였다"))
        .andExpect(jsonPath("$.content.resolution.items[0].ref").value("N4")).andExpect(jsonPath("$.content.resolution.items[0].title").value("풀 최대 20"))
        .andExpect(jsonPath("$.content.resolution.items[0].summary").value("풀을 20 으로 늘려 해소"))
        .andExpect(jsonPath("$.content.resolution.others.length()").value(2))
        .andExpect(jsonPath("$.content.side_issues[0].ref").value("N6"))
        // AI 칸은 비어 있다(AI 워커가 채운다)
        .andExpect(jsonPath("$.content.summary.length()").value(0)).andExpect(jsonPath("$.content.prevention.length()").value(0))
        .andExpect(jsonPath("$.content.causes[0].explanation").doesNotExist());
  }

  @Test
  void externalReportIsAdminOnlyAndHidesAuthors() throws Exception {
    create(userBackend, wsSw, root, "external", false).andExpect(status().isForbidden());   // 장문경은 SW 개발의 member
    create(userLead, wsSw, root, "external", false).andExpect(status().isOk())
        .andExpect(jsonPath("$.audience").value("external"))
        .andExpect(jsonPath("$.content.occurrence.reported_by").doesNotExist())
        .andExpect(jsonPath("$.content.timeline[0].author").doesNotExist()).andExpect(jsonPath("$.content.records[3].author").doesNotExist());
  }

  @Test
  void sameFactsReuseTheReportUnlessForced() throws Exception {
    long first = idOf(create(userBackend, wsSw, root, "internal", false));
    assertThat(idOf(create(userBackend, wsSw, root, "internal", false))).isEqualTo(first);
    assertThat(idOf(create(userBackend, wsSw, root, "internal", true))).isEqualTo(first);      // 만드는 중이면 force 여도 그 보고서
    jdbc.sql("UPDATE incident_reports SET status = 'done', generated_at = now() WHERE id = ?").param(first).update();
    assertThat(idOf(create(userBackend, wsSw, root, "internal", true))).isEqualTo(first);      // 방금(1분 안) 만든 것이면 force 여도 그대로
    jdbc.sql("UPDATE incident_reports SET generated_at = now() - interval '2 minutes' WHERE id = ?").param(first).update();
    assertThat(idOf(create(userBackend, wsSw, root, "internal", true))).isNotEqualTo(first);
    jdbc.sql("UPDATE incident_reports SET created_at = now() - interval '11 minutes' WHERE root_id = ? AND status = 'generating'").param(root).update();
    create(userBackend, wsSw, root, "internal", true);                                         // 10분 넘게 만드는 중이면 멈춘 것 — 새로 만든다
    assertThat(jdbc.sql("SELECT count(*) FROM incident_reports WHERE root_id = ? AND audience = 'internal'").param(root).query(Long.class).single()).isEqualTo(3);
    assertThat(idOf(create(userLead, wsSw, root, "external", false))).isNotEqualTo(first);   // 공개 범위가 다르면 다른 보고서
    mvc.perform(as(get("/reports").param("root_id", String.valueOf(root)), userBackend, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$.length()").value(4)).andExpect(jsonPath("$[3].id").value(first));   // 최신순
  }

  @Test
  void reportTurnsStaleWhenRecordsChange() throws Exception {
    long id = idOf(create(userBackend, wsSw, root, "internal", false));
    mvc.perform(as(get("/reports/" + id), userBackend, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$.stale").value(false));
    long more = at(node(wsSw, "success", "누수 감지 추가", userLead), "2026-07-21 08:00+00");
    edge(wsSw, root, more);
    mvc.perform(as(get("/reports/" + id), userBackend, wsSw)).andExpect(jsonPath("$.stale").value(true))
        .andExpect(jsonPath("$.content.meta.record_count").value(6));   // 저장된 내용은 만들 때 그대로
    jdbc.sql("UPDATE incident_reports SET status = 'done', generated_at = now() WHERE id = ?").param(id).update();
    assertThat(idOf(create(userBackend, wsSw, root, "internal", false))).isNotEqualTo(id);   // 내용이 바뀌었으니 새 보고서
  }

  @Test
  void openIncidentIsInterimAndFailedReportIsNotReused() throws Exception {
    jdbc.sql("UPDATE log_nodes SET resolved_at = NULL WHERE id = ?").param(root).update();
    long id = idOf(create(userBackend, wsSw, root, "internal", false).andExpect(jsonPath("$.content.meta.status").value("open"))
        .andExpect(jsonPath("$.content.meta.interim").value(true)).andExpect(jsonPath("$.content.meta.duration_minutes").doesNotExist()));
    jdbc.sql("UPDATE incident_reports SET status = 'failed', error = 'AI_BAD_OUTPUT' WHERE id = ?").param(id).update();
    mvc.perform(as(get("/reports/" + id), userBackend, wsSw)).andExpect(jsonPath("$.status").value("failed")).andExpect(jsonPath("$.error").value("AI_BAD_OUTPUT"))
        .andExpect(jsonPath("$.content.records.length()").value(6));   // 실패해도 사실 칸은 남는다
    assertThat(idOf(create(userBackend, wsSw, root, "internal", false))).isNotEqualTo(id);
  }

  @Test
  void rejectsNonRootUnknownAndOtherWorkspace() throws Exception {
    create(userBackend, wsSw, cause, "internal", false).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("REPORT_NOT_ROOT"));
    create(userBackend, wsSw, 999_999_999L, "internal", false).andExpect(status().isNotFound());
    create(userBackend, wsSw, root, "public", false).andExpect(status().isBadRequest());
    create(userBackend, wsHr, root, "internal", false).andExpect(status().isNotFound());   // 다른 부서의 장애 건
    long id = idOf(create(userBackend, wsSw, root, "internal", false));
    mvc.perform(as(get("/reports/" + id), userBackend, wsHr)).andExpect(status().isNotFound());
    mvc.perform(as(get("/reports").param("root_id", String.valueOf(root)), userBackend, wsHr)).andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(0));
  }
}
