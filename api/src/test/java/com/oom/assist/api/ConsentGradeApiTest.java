package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.nullValue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Map;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;

/** 처리방침 판 단위 동의(V13 user_consents)와 직급 · 직책. 지금 판은 data/legal/privacy_policy.md 머리의 "판: v2". */
class ConsentGradeApiTest extends AbstractApiTest {

  @Test
  void privacyPolicyCarriesVersion() throws Exception {
    mvc.perform(get("/auth/privacy-policy")).andExpect(status().isOk()).andExpect(header().string("X-Policy-Version", "v2"));
  }

  @Test
  void consentToOldVersionRequiresConsentAgain() throws Exception {
    jdbc.sql("UPDATE users SET consented_at = now() - interval '10 days' WHERE id = ?").param(userBackend).update();
    jdbc.sql("INSERT INTO user_consents (user_id, policy_version, consented_at) VALUES (?, 'v1', now() - interval '10 days')").param(userBackend).update();
    mvc.perform(asUser(get("/me"), userBackend))
        .andExpect(jsonPath("$.consent_required").value(true))
        .andExpect(jsonPath("$.consent_version").value("v2"))
        .andExpect(jsonPath("$.consented_version").value("v1"));
    // 화면이 옛 문안을 보여 주고 있었다면 막는다
    mvc.perform(asUser(post("/auth/consent"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"version\":\"v1\"}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("CONSENT_VERSION_MISMATCH"));
    mvc.perform(asUser(post("/auth/consent"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"version\":\"v2\"}"))
        .andExpect(status().isNoContent());
    mvc.perform(asUser(get("/me"), userBackend))
        .andExpect(jsonPath("$.consent_required").value(false)).andExpect(jsonPath("$.consented_version").value("v2"));
    assertThat(jdbc.sql("SELECT policy_version FROM user_consents WHERE user_id = ? ORDER BY policy_version").param(userBackend).query(String.class).list())
        .containsExactly("v1", "v2");   // 판마다 남는다
    assertThat(jdbc.sql("SELECT label FROM audit_log WHERE table_name = 'auth' AND action = 'consent'").query(String.class).list()).containsExactly("v2");
  }

  @Test
  void firstConsentWithoutBodyRecordsCurrentVersion() throws Exception {
    mvc.perform(asUser(get("/me"), userLead)).andExpect(jsonPath("$.consent_required").value(true)).andExpect(jsonPath("$.consented_version").value(nullValue()));
    mvc.perform(asUser(post("/auth/consent"), userLead)).andExpect(status().isNoContent());
    mvc.perform(asUser(get("/me"), userLead)).andExpect(jsonPath("$.consent_required").value(false)).andExpect(jsonPath("$.consented_version").value("v2"));
  }

  @Test
  void titleSplitRule() {
    assertThat(parts("팀장")).containsEntry("grade", null).containsEntry("duty", "팀장");
    assertThat(parts(" 매니저 ")).containsEntry("grade", "매니저").containsEntry("duty", null);
    assertThat(parts("")).containsEntry("grade", null).containsEntry("duty", null);
  }

  @Test
  void profileAndInviteUseGradeAndPosition() throws Exception {
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"grade\":\"책임\",\"position\":\"파트장\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.grade").value("책임")).andExpect(jsonPath("$.position").value("파트장"))
        .andExpect(jsonPath("$.title").value("책임 · 파트장"));   // 예전 화면용 표시
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"position\":\"\"}"))
        .andExpect(jsonPath("$.grade").value("책임")).andExpect(jsonPath("$.position").value(nullValue())).andExpect(jsonPath("$.title").value("책임"));
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"팀장\"}"))   // 예전 화면은 title 만 보낸다
        .andExpect(jsonPath("$.grade").value(nullValue())).andExpect(jsonPath("$.position").value("팀장"));
    assertThat(jdbc.sql("SELECT label FROM audit_log WHERE action = 'profile_changed' ORDER BY id").query(String.class).list())
        .containsExactly("grade,position", "position", "grade,position");

    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\":\"T0101\",\"name\":\"새직원\",\"grade\":\"사원\"}")).andExpect(status().isCreated());
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\":\"T0102\",\"name\":\"예전화면\",\"title\":\"센터장\"}")).andExpect(status().isCreated());
    mvc.perform(as(get("/users"), userLead, wsSw))
        .andExpect(jsonPath("$[?(@.employee_no == 'T0101')].grade").value(org.hamcrest.Matchers.contains("사원")))
        .andExpect(jsonPath("$[?(@.employee_no == 'T0102')].position").value(org.hamcrest.Matchers.contains("센터장")))
        .andExpect(jsonPath("$[?(@.employee_no == 'T0102')].title").value(org.hamcrest.Matchers.contains("센터장")));
  }

  private Map<String, Object> parts(String title) {
    return jdbc.sql("SELECT grade, duty FROM title_parts(?)").param(title).query().singleRow();
  }
}
