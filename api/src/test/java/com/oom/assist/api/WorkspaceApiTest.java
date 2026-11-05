package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.common.context.TenantContextFilter;
import org.junit.jupiter.api.Test;

/** 워크스페이스 목록(로그인 전/후)·모드 토글·멤버십 없는 접근. 응답 키가 snake_case 인지도 여기서 확인한다. */
class WorkspaceApiTest extends AbstractApiTest {

  @org.springframework.beans.factory.annotation.Autowired com.oom.assist.workspace.WorkspaceController controller;

  @Test
  void workspacesListAnonymousShowsDeptsAndLoggedInShowsMemberships() throws Exception {
    // 로그인 전: 데모 회사를 지정하지 않으면 빈 목록, 지정하면 그 회사의 부서만 (다른 회사 부서는 드러나지 않는다)
    mvc.perform(get("/workspaces")).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(0)));
    long other = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('other', 'Other') RETURNING id").query(Long.class).single();
    jdbc.sql("INSERT INTO workspaces (tenant_id, kind, name, policy_enabled) VALUES (?, 'dept', '남의 부서', true)").param(other).update();
    org.springframework.test.util.ReflectionTestUtils.setField(controller, "publicTenant", "t");
    try {
      mvc.perform(get("/workspaces")).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)))
          .andExpect(jsonPath("$[0].kind").value("dept")).andExpect(jsonPath("$[0].parent_id").value(company));
    } finally {
      org.springframework.test.util.ReflectionTestUtils.setField(controller, "publicTenant", "");
    }
    mvc.perform(asUser(get("/workspaces"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(wsSw)).andExpect(jsonPath("$[0].suggestions", hasSize(0)));
    // 첫 화면 추천 질문은 부서 설정에서 온다
    jdbc.sql("UPDATE workspaces SET settings = settings || '{\"suggestions\": [\"질문 하나\", \"질문 둘\"]}'::jsonb WHERE id = ?").param(wsSw).update();
    mvc.perform(asUser(get("/workspaces"), userLead)).andExpect(status().isOk())
        .andExpect(jsonPath("$[0].suggestions", hasSize(2))).andExpect(jsonPath("$[0].suggestions[0]").value("질문 하나"));
  }

  @Test
  void meWorksWithoutWorkspaceHeader() throws Exception {
    mvc.perform(asUser(get("/me"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$.title").value("팀장"))
        .andExpect(jsonPath("$.workspace_id").value(wsSw));
  }

  @Test
  void patchModeRequiresAdminOfThatWorkspace() throws Exception {
    String body = "{\"policy_enabled\": true}";
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content(body))
        .andExpect(status().isOk()).andExpect(jsonPath("$.policy_enabled").value(true));
    mvc.perform(as(patch("/workspaces/" + wsSw), userBackend, wsSw).content(body))
        .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("FORBIDDEN"));
    // 헤더의 워크스페이스와 경로가 다르면 존재를 숨긴다 (R1)
    mvc.perform(as(patch("/workspaces/" + wsHr), userLead, wsSw).content(body)).andExpect(status().isNotFound());
  }

  @Test
  void patchChangesOnlySentSwitchesAndRefusesBothOff() throws Exception {
    // wsSw: 규정 off · 사례 on (사례 단일형). 사례까지 끄는 요청은 거절
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content("{\"case_enabled\": false}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("NO_SOURCE"));
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content("{}")).andExpect(status().isBadRequest());
    // 규정을 켠 뒤에는 사례를 끌 수 있다 (규정 참조형). 보내지 않은 스위치는 그대로
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content("{\"policy_enabled\": true}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.policy_enabled").value(true)).andExpect(jsonPath("$.case_enabled").value(true));
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content("{\"case_enabled\": false}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.policy_enabled").value(true)).andExpect(jsonPath("$.case_enabled").value(false));
    // 변경 2건이 감사 로그에 남는다. 같은 값을 다시 보내면 남지 않는다
    mvc.perform(as(patch("/workspaces/" + wsSw), userLead, wsSw).content("{\"case_enabled\": false}")).andExpect(status().isOk());
    var audit = jdbc.sql("SELECT actor, reason, before::text AS before FROM audit_log WHERE table_name = 'workspaces' AND row_id = ? ORDER BY id")
        .param(String.valueOf(wsSw)).query().listOfRows();
    assertThat(audit).hasSize(2);
    assertThat(audit.get(1).get("actor")).isEqualTo("배종현");
    assertThat((String) audit.get(1).get("reason")).isEqualTo("case_enabled: true → false");
    assertThat((String) audit.get(1).get("before")).contains("\"case_enabled\": true");
  }

  @Test
  void companyAdminConfiguresEveryDeptOfTheCompanyOnly() throws Exception {
    // userLead: wsSw 의 admin 이자 전사 공통의 admin(회사 관리자). wsHr 에는 소속이 없다
    member(company, userLead, "admin");
    mvc.perform(as(patch("/workspaces/" + wsHr), userLead, wsSw).content("{\"case_enabled\": false}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.id").value(wsHr)).andExpect(jsonPath("$.case_enabled").value(false));
    mvc.perform(asUser(get("/workspaces/manageable"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)))
        .andExpect(jsonPath("$[0].id").value(wsSw)).andExpect(jsonPath("$[1].id").value(wsHr))
        .andExpect(jsonPath("$[1].case_enabled").value(false)).andExpect(jsonPath("$[0].cases").value(0)).andExpect(jsonPath("$[0].policy_docs").value(0));
    // 부서 관리자는 자기 부서만, 일반 구성원은 없음
    mvc.perform(asUser(get("/workspaces/manageable"), userBackend)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(wsHr));
    mvc.perform(get("/workspaces/manageable")).andExpect(status().isUnauthorized());
    // 전사 공통 자체와 다른 회사의 부서는 설정 대상이 아니다
    mvc.perform(as(patch("/workspaces/" + company), userLead, wsSw).content("{\"case_enabled\": false}")).andExpect(status().isNotFound());
    long otherTenant = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('o', 'O') RETURNING id").query(Long.class).single();
    long otherDept = jdbc.sql("INSERT INTO workspaces (tenant_id, kind, name, policy_enabled) VALUES (?, 'dept', '남의 부서', true) RETURNING id")
        .param(otherTenant).query(Long.class).single();
    mvc.perform(as(patch("/workspaces/" + otherDept), userLead, wsSw).content("{\"case_enabled\": false}")).andExpect(status().isNotFound());
    // /me: 소속 부서 목록에 전사 공통은 넣지 않고 company_admin 으로 알린다
    mvc.perform(asUser(get("/me"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$.company_admin").value(true))
        .andExpect(jsonPath("$.workspaces", hasSize(1))).andExpect(jsonPath("$.workspaces[0].can_configure").value(true))
        .andExpect(jsonPath("$.workspaces[0].case_enabled").value(true)).andExpect(jsonPath("$.workspace_id").value(wsSw));
    mvc.perform(asUser(get("/me"), userBackend)).andExpect(status().isOk()).andExpect(jsonPath("$.company_admin").value(false))
        .andExpect(jsonPath("$.workspaces[?(@.id == " + wsSw + ")].can_configure").value(false));
    // 부서 헤더 없이도 된다 — 권한은 로그인한 사용자로만 본다 (회사 관리자는 소속하지 않은 부서도 고친다)
    mvc.perform(asUser(patch("/workspaces/" + wsHr), userLead).contentType(org.springframework.http.MediaType.APPLICATION_JSON).content("{\"case_enabled\": true}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$.case_enabled").value(true));
    assertThat(jdbc.sql("SELECT actor FROM audit_log WHERE table_name = 'workspaces' AND row_id = ? ORDER BY id DESC LIMIT 1")
        .param(String.valueOf(wsHr)).query(String.class).single()).isEqualTo("배종현");
  }

  @Test
  void protectedEndpointWithoutMembershipIs401() throws Exception {
    // userLead 는 HR 소속이 아니다 → 컨텍스트 없음
    mvc.perform(asUser(get("/logs"), userLead).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isUnauthorized());
    mvc.perform(asUser(get("/logs"), userLead).header(TenantContextFilter.WORKSPACE_HEADER, "abc")).andExpect(status().isUnauthorized());
    mvc.perform(asUser(get("/logs"), userLead)).andExpect(status().isUnauthorized());
  }
}
