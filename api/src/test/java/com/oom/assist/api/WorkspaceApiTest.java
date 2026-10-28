package com.oom.assist.api;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.common.context.TenantContextFilter;
import org.junit.jupiter.api.Test;

/** 워크스페이스 목록(로그인 전/후)·모드 토글·멤버십 없는 접근. 응답 키가 snake_case 인지도 여기서 확인한다. */
class WorkspaceApiTest extends AbstractApiTest {

  @Test
  void workspacesListAnonymousShowsDeptsAndLoggedInShowsMemberships() throws Exception {
    mvc.perform(get("/workspaces")).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)))
        .andExpect(jsonPath("$[0].kind").value("dept")).andExpect(jsonPath("$[0].parent_id").value(company));
    mvc.perform(asUser(get("/workspaces"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(wsSw));
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
  void protectedEndpointWithoutMembershipIs401() throws Exception {
    // userLead 는 HR 소속이 아니다 → 컨텍스트 없음
    mvc.perform(asUser(get("/logs"), userLead).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isUnauthorized());
    mvc.perform(asUser(get("/logs"), userLead).header(TenantContextFilter.WORKSPACE_HEADER, "abc")).andExpect(status().isUnauthorized());
    mvc.perform(asUser(get("/logs"), userLead)).andExpect(status().isUnauthorized());
  }
}
