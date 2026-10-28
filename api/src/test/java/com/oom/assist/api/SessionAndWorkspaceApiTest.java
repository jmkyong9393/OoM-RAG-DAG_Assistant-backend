package com.oom.assist.api;

import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.cookie;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.common.context.TenantContextFilter;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;

/** 데모 세션(쿠키)·내 정보·워크스페이스 목록·모드 토글. 응답 키가 snake_case 인지도 여기서 확인한다. */
class SessionAndWorkspaceApiTest extends AbstractApiTest {

  @Test
  void loginIssuesCookieAndReturnsMeWithMemberships() throws Exception {
    mvc.perform(post("/session").contentType(MediaType.APPLICATION_JSON).content("{\"user_id\": " + userBackend + "}"))
        .andExpect(status().isOk())
        .andExpect(cookie().exists("oom_u"))
        .andExpect(cookie().httpOnly("oom_u", true))
        .andExpect(jsonPath("$.id").value(userBackend))
        .andExpect(jsonPath("$.name").value("장문경"))
        .andExpect(jsonPath("$.workspace_id").value(wsHr))            // admin 멤버십이 홈
        .andExpect(jsonPath("$.workspaces", hasSize(2)))
        .andExpect(jsonPath("$.workspaces[0].policy_enabled").value(true));
  }

  @Test
  void loginUnknownUserIs404() throws Exception {
    mvc.perform(post("/session").contentType(MediaType.APPLICATION_JSON).content("{\"user_id\": 999999}"))
        .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }

  @Test
  void meRequiresCookieButNotWorkspaceHeader() throws Exception {
    mvc.perform(get("/me")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    mvc.perform(get("/me").cookie(loginAs(userLead))).andExpect(status().isOk()).andExpect(jsonPath("$.title").value("팀장"));
  }

  @Test
  void logoutClearsCookie() throws Exception {
    mvc.perform(delete("/session").cookie(loginAs(userLead))).andExpect(status().isNoContent()).andExpect(cookie().maxAge("oom_u", 0));
  }

  @Test
  void workspacesListAnonymousShowsDeptsAndLoggedInShowsMemberships() throws Exception {
    mvc.perform(get("/workspaces")).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)))
        .andExpect(jsonPath("$[0].kind").value("dept")).andExpect(jsonPath("$[0].parent_id").value(company));
    mvc.perform(get("/workspaces").cookie(loginAs(userLead))).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].id").value(wsSw));
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
    mvc.perform(get("/logs").cookie(loginAs(userLead)).header(TenantContextFilter.WORKSPACE_HEADER, wsHr))
        .andExpect(status().isUnauthorized());
    mvc.perform(get("/logs").cookie(loginAs(userLead)).header(TenantContextFilter.WORKSPACE_HEADER, "abc"))
        .andExpect(status().isUnauthorized());
    mvc.perform(get("/logs").cookie(loginAs(userLead))).andExpect(status().isUnauthorized());
  }
}
