package com.oom.assist.api;

import static org.hamcrest.Matchers.contains;
import static org.hamcrest.Matchers.containsInAnyOrder;
import static org.hamcrest.Matchers.empty;
import static org.hamcrest.Matchers.everyItem;
import static org.hamcrest.Matchers.hasItem;
import static org.hamcrest.Matchers.not;
import static org.hamcrest.Matchers.nullValue;
import static org.hamcrest.Matchers.startsWith;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.auth.AuditController;
import com.oom.assist.common.util.Mask;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;

/** 계정 · 관리자: 내 정보 정정(PATCH /me), 권한 목록, 역할 변경, 멤버 제외, 접속기록(GET /audit). */
class AccountAdminApiTest extends AbstractApiTest {

  @Test
  void profileUpdateChangesOnlyGivenFieldsAndLogsFieldNames() throws Exception {
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"책임\",\"email\":\"New@Test.local\"}"))
        .andExpect(status().isOk())
        .andExpect(jsonPath("$.name").value("장문경"))
        .andExpect(jsonPath("$.title").value("책임"))
        .andExpect(jsonPath("$.email").value("new@test.local"));
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"\"}"))
        .andExpect(jsonPath("$.title").value(nullValue())).andExpect(jsonPath("$.email").value("new@test.local"));
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"name\":\"  \"}")).andExpect(status().isBadRequest());
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"not-an-email\"}")).andExpect(status().isBadRequest());
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"email\":\"" + emailLead + "\"}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("EMAIL_TAKEN"));
    // 값이 아니라 바뀐 항목 이름만 남는다
    org.assertj.core.api.Assertions.assertThat(jdbc.sql("SELECT label FROM audit_log WHERE table_name = 'auth' AND action = 'profile_changed' ORDER BY id")
        .query(String.class).list()).containsExactly("title,email", "title");
  }

  @Test
  void meCarriesPermissionsPerWorkspaceAndCompany() throws Exception {
    mvc.perform(asUser(get("/me"), userLead)).andExpect(status().isOk())
        .andExpect(jsonPath("$.workspaces[0].permissions", containsInAnyOrder("admin.members", "admin.settings", "docs.manage")))
        .andExpect(jsonPath("$.permissions", empty()));
    mvc.perform(asUser(get("/me"), userBackend))   // 경영지원 admin, SW 개발 member
        .andExpect(jsonPath("$.workspaces[?(@.id == " + wsSw + ")].permissions[*]", empty()));
    long boss = companyAdmin();
    mvc.perform(asUser(get("/me"), boss)).andExpect(jsonPath("$.permissions", contains("audit.read")))
        .andExpect(jsonPath("$.workspaces[0].permissions", containsInAnyOrder("admin.settings", "docs.manage")));   // 부서 member 지만 상위 admin
  }

  @Test
  void roleChangeNeedsAdminAndKeepsAnAdmin() throws Exception {
    mvc.perform(as(patch("/users/" + userLead), userBackend, wsSw).content("{\"role\":\"member\"}")).andExpect(status().isForbidden());
    mvc.perform(as(patch("/users/" + userBackend), userLead, wsSw).content("{\"role\":\"admin\"}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.role").value("admin")).andExpect(jsonPath("$.email_masked", startsWith("b***@")));
    mvc.perform(as(patch("/users/" + userLead), userLead, wsSw).content("{\"role\":\"member\"}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SELF_DEMOTION"));
    mvc.perform(as(patch("/users/" + userBackend), userLead, wsSw).content("{\"role\":\"member\"}")).andExpect(jsonPath("$.role").value("member"));
    mvc.perform(as(patch("/users/" + userBackend), userLead, wsSw).content("{\"role\":\"owner\"}")).andExpect(status().isBadRequest());
    long outsider = user("외부", null, "T0007", "x@test.local");
    mvc.perform(as(patch("/users/" + outsider), userLead, wsSw).content("{\"role\":\"admin\"}")).andExpect(status().isNotFound());
  }

  @Test
  void removeMemberKeepsAccountAndBlocksSelf() throws Exception {
    mvc.perform(as(delete("/workspaces/" + wsSw + "/members/" + userLead), userLead, wsSw))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SELF_REMOVAL"));
    mvc.perform(as(delete("/workspaces/" + wsHr + "/members/" + userBackend), userLead, wsSw)).andExpect(status().isBadRequest());   // 경로 ≠ 헤더
    mvc.perform(as(delete("/workspaces/" + wsSw + "/members/" + userLead), userBackend, wsSw)).andExpect(status().isForbidden());
    mvc.perform(as(delete("/workspaces/" + wsSw + "/members/" + userBackend), userLead, wsSw)).andExpect(status().isNoContent());
    mvc.perform(as(get("/users"), userLead, wsSw)).andExpect(jsonPath("$[*].id", not(hasItem((int) userBackend))));
    mvc.perform(as(get("/users"), userBackend, wsHr)).andExpect(status().isOk());   // 계정과 다른 부서 소속은 그대로
  }

  @Test
  void auditIsCompanyAdminOnlyAndMasksIp() throws Exception {
    mvc.perform(as(patch("/users/" + userBackend), userLead, wsSw).content("{\"role\":\"admin\"}")).andExpect(status().isOk());
    mvc.perform(asUser(patch("/me"), userBackend).contentType(MediaType.APPLICATION_JSON).content("{\"title\":\"책임\"}")).andExpect(status().isOk());
    mvc.perform(asUser(get("/audit"), userLead)).andExpect(status().isForbidden());
    long boss = companyAdmin();
    mvc.perform(asUser(get("/audit?action=role_changed"), boss)).andExpect(status().isOk())
        .andExpect(jsonPath("$.items[0].category").value("member"))
        .andExpect(jsonPath("$.items[0].employee_no").value(empBackend))
        .andExpect(jsonPath("$.items[0].actor").value("배종현"))
        .andExpect(jsonPath("$.items[0].reason").value("member->admin"));
    mvc.perform(asUser(get("/audit?action=profile_changed&employee_no=" + empBackend), boss))
        .andExpect(jsonPath("$.items[0].reason").value("title"))
        .andExpect(jsonPath("$.items[0].ip_masked").value("127.0.0.*"));
    mvc.perform(asUser(get("/audit?employee_no=" + empLead), boss)).andExpect(jsonPath("$.items[*].employee_no", everyItem(org.hamcrest.Matchers.equalToIgnoringCase(empLead))));
    mvc.perform(asUser(get("/audit?from=어제"), boss)).andExpect(status().isBadRequest());
    mvc.perform(asUser(get("/audit?limit=0"), boss)).andExpect(status().isBadRequest());
  }

  @Test
  void ipMaskAndItemShape() {
    org.assertj.core.api.Assertions.assertThat(Mask.ip("10.0.3.7")).isEqualTo("10.0.3.*");
    org.assertj.core.api.Assertions.assertThat(Mask.ip("0:0:0:0:0:0:0:1")).isEqualTo("0:0:0:0:*");
    org.assertj.core.api.Assertions.assertThat(Mask.ip(null)).isNull();
    var unlock = AuditController.item(1, null, "auth", "unlocked_by_admin", "T0002", "T0002", "배종현", null);
    org.assertj.core.api.Assertions.assertThat(unlock.actor()).isEqualTo("배종현");
    org.assertj.core.api.Assertions.assertThat(unlock.ipMasked()).isNull();
  }

  private long companyAdmin() {
    long boss = user("관리자", "팀장", "T0009", "boss@test.local");
    member(company, boss, "admin");
    member(wsSw, boss, "member");
    return boss;
  }
}
