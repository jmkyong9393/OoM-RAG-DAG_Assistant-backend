package com.oom.assist.api;

import static org.hamcrest.Matchers.contains;
import static org.hamcrest.Matchers.containsInAnyOrder;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.nullValue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** /documents: 범위(이 부서 + 상위), 조문 순서, 폐기 · 복원 · 삭제 권한과 막는 조건, 이력. */
class DocumentApiTest extends AbstractApiTest {

  long companyAdmin, companyDoc, hrDoc;

  @BeforeEach
  void seedDocs() {
    companyAdmin = user("관리자", "팀장", "T0009", "admin@test.local");
    member(company, companyAdmin, "admin");
    member(wsSw, companyAdmin, "member");
    companyDoc = doc(company, "vpn-guide", "VPN 지침", "v1", "active");
    hrDoc = doc(wsHr, "hr-rule", "인사 규정", "v1", "active");
    chunk(companyDoc, "vpn-guide-a10-1", "제10조", 1);   // 같은 쪽 — 문자열 정렬이면 a10 이 a2 앞
    chunk(companyDoc, "vpn-guide-a2-1", "제2조", 1);
    chunk(companyDoc, "vpn-guide-a2-2", "제2조", 1);
  }

  @Test
  void listShowsThisDeptAndCompanyDocumentsOnly() throws Exception {
    mvc.perform(as(get("/documents"), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(companyDoc))
        .andExpect(jsonPath("$[0].visibility").value("company"))
        .andExpect(jsonPath("$[0].chunk_count").value(3))
        .andExpect(jsonPath("$[0].has_file").value(false))
        .andExpect(jsonPath("$[0].approved_by").value(nullValue()))
        .andExpect(jsonPath("$[0].can_manage").value(false));   // 부서 관리자는 전사 공통 문서를 못 바꾼다
    mvc.perform(as(get("/documents/" + companyDoc), companyAdmin, wsSw)).andExpect(jsonPath("$.can_manage").value(true));
    mvc.perform(as(get("/documents"), userBackend, wsHr)).andExpect(jsonPath("$[*].id", containsInAnyOrder((int) companyDoc, (int) hrDoc)));
    mvc.perform(as(get("/documents?status=retired"), userLead, wsSw)).andExpect(jsonPath("$", hasSize(0)));
    mvc.perform(as(get("/documents/" + hrDoc), userLead, wsSw)).andExpect(status().isNotFound());   // 다른 부서 문서
  }

  @Test
  void chunksAreOrderedByArticleNumber() throws Exception {
    mvc.perform(as(get("/documents/" + companyDoc + "/chunks"), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$[*].id", contains("vpn-guide-a2-1", "vpn-guide-a2-2", "vpn-guide-a10-1")));
  }

  @Test
  void retireAndRestoreNeedAdminOfDocumentWorkspace() throws Exception {
    // 부서 관리자는 전사 공통 문서를 못 바꾼다, 일반 구성원도
    mvc.perform(as(patch("/documents/" + companyDoc), userLead, wsSw).content("{\"status\":\"retired\"}")).andExpect(status().isForbidden());
    mvc.perform(as(patch("/documents/" + companyDoc), userBackend, wsSw).content("{\"status\":\"retired\"}")).andExpect(status().isForbidden());
    mvc.perform(as(patch("/documents/" + companyDoc), companyAdmin, wsSw).content("{\"status\":\"retired\",\"reason\":\"개정 예정\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.status").value("retired"))
        .andExpect(jsonPath("$.retire_reason").value("개정 예정"))
        .andExpect(jsonPath("$.retired_by").value("관리자"));
    mvc.perform(as(patch("/documents/" + companyDoc), companyAdmin, wsSw).content("{\"status\":\"active\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("active")).andExpect(jsonPath("$.retired_at").value(nullValue()));
    mvc.perform(as(get("/documents/events?document_id=" + companyDoc), userLead, wsSw))
        .andExpect(jsonPath("$[*].action", contains("restored", "retired")))
        .andExpect(jsonPath("$[1].reason").value("개정 예정"))
        .andExpect(jsonPath("$[1].document_label").value("VPN 지침 v1"));
    // 부서 문서는 그 부서 관리자가 바꾼다
    mvc.perform(as(patch("/documents/" + hrDoc), userBackend, wsHr).content("{\"status\":\"retired\"}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("REASON_REQUIRED"));   // 폐기는 사유 필수
    mvc.perform(as(patch("/documents/" + hrDoc), userBackend, wsHr).content("{\"status\":\"retired\",\"reason\":\"통합\"}")).andExpect(status().isOk());
    mvc.perform(as(patch("/documents/" + hrDoc), userBackend, wsHr).content("{\"status\":\"gone\"}")).andExpect(status().isBadRequest());
  }

  @Test
  void supersededDocumentCannotBeRestored() throws Exception {
    long v2 = doc(company, "vpn-guide-v2", "VPN 지침", "v2", "active");
    jdbc.sql("UPDATE policy_documents SET status = 'retired', superseded_by = ? WHERE id = ?").params(v2, companyDoc).update();
    mvc.perform(as(patch("/documents/" + companyDoc), companyAdmin, wsSw).content("{\"status\":\"active\"}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPERSEDED"));
  }

  @Test
  void deleteOnlyRetiredAndUnreferenced() throws Exception {
    mvc.perform(as(delete("/documents/" + companyDoc), companyAdmin, wsSw)).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("NOT_RETIRED"));
    jdbc.sql("UPDATE policy_documents SET status = 'retired' WHERE id = ?").param(companyDoc).update();
    long cited = node(wsSw, "success", "VPN 조치", userLead);
    jdbc.sql("UPDATE log_nodes SET attributes = jsonb_build_object('policy_refs', jsonb_build_array(jsonb_build_object('relation', 'follows', 'policy', 'vpn-guide', 'article', '제2조'))) WHERE id = ?")
        .param(cited).update();
    mvc.perform(as(delete("/documents/" + companyDoc), companyAdmin, wsSw)).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("DOCUMENT_IN_USE"));
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(cited).update();
    mvc.perform(as(delete("/documents/" + companyDoc), userLead, wsSw)).andExpect(status().isForbidden());
    mvc.perform(as(delete("/documents/" + companyDoc), companyAdmin, wsSw)).andExpect(status().isNoContent());
    mvc.perform(as(get("/documents/" + companyDoc), companyAdmin, wsSw)).andExpect(status().isNotFound());
    org.assertj.core.api.Assertions.assertThat(jdbc.sql("SELECT count(*) FROM policy_chunks WHERE doc_id = ?").param(companyDoc).query(Long.class).single()).isZero();
    mvc.perform(as(get("/documents/events"), userLead, wsSw))
        .andExpect(jsonPath("$[0].action").value("deleted")).andExpect(jsonPath("$[0].document_label").value("VPN 지침 v1"));
  }

  private long doc(long ws, String slug, String title, String version, String status) {
    return jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, ?, ?, ?, 'markdown', ?) RETURNING id")
        .params(ws, slug, title, version, status).query(Long.class).single();
  }

  private void chunk(long doc, String id, String label, int page) {
    long ws = jdbc.sql("SELECT workspace_id FROM policy_documents WHERE id = ?").param(doc).query(Long.class).single();
    jdbc.sql("INSERT INTO policy_chunks (id, doc_id, workspace_id, article_label, page, text) VALUES (?, ?, ?, ?, ?, ?)")
        .params(id, doc, ws, label, page, label + " 본문").update();
  }
}
