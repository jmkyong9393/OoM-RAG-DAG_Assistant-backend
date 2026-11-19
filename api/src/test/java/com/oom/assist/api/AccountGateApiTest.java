package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.oom.assist.common.context.TenantContextFilter;
import jakarta.servlet.http.Cookie;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.ResultActions;

/**
 * 계정 상태를 서버가 지킨다(AccountGate) — 처리방침에 동의하지 않았거나 비밀번호를 바꿔야 하는 계정은 정해진 API 만 쓴다.
 * 공통 기반의 계정(배종현 · 장문경)은 지금 판에 동의했고 비밀번호도 바꿀 필요가 없다. 지금 판은 data/legal/privacy_policy.md 의 "판: v2".
 */
class AccountGateApiTest extends AbstractApiTest {

  static final String NEW_PASSWORD = "Nv!pass-2026x";

  private MvcResult login(String employeeNo, String password) throws Exception {
    return mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON)
        .content("{\"employee_no\": \"" + employeeNo + "\", \"password\": \"" + password + "\"}")).andExpect(status().isOk()).andReturn();
  }

  private Cookie cookieOf(MvcResult r) {
    String h = r.getResponse().getHeaders("Set-Cookie").stream().filter(x -> x.startsWith("OOMSESSION=")).findFirst().orElseThrow();
    return new Cookie("OOMSESSION", h.substring("OOMSESSION=".length(), h.indexOf(';')));
  }

  private ResultActions logs(Cookie c) throws Exception {
    return mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsSw));
  }

  private ResultActions changePassword(Cookie c, String current, String next) throws Exception {
    return mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON)
        .content("{\"current_password\": \"" + current + "\", \"new_password\": \"" + next + "\"}"));
  }

  @Test
  void tempPasswordSessionUsesOnlyTheOpenApisUntilPasswordIsChanged() throws Exception {
    member(company, userLead, "admin");   // 장문경은 두 부서에 속해 있어 회사 관리자가 재발급한다
    MvcResult r = mvc.perform(as(post("/users/" + userBackend + "/reset-password"), userLead, wsSw)).andExpect(status().isOk()).andReturn();
    String temp = JsonPath.read(r.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.temp_password");
    Cookie c = cookieOf(login(empBackend, temp));   // 동의는 마친 계정이 임시 비밀번호로 로그인한 직후
    logs(c).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));
    mvc.perform(get("/users").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsSw))
        .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));
    mvc.perform(post("/chat").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsSw).contentType(MediaType.APPLICATION_JSON).content("{\"query\": \"VPN\"}"))
        .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));   // 스트림을 열기 전에 JSON 으로
    // 열어 두는 API 는 전과 같다
    mvc.perform(get("/me").cookie(c)).andExpect(status().isOk()).andExpect(jsonPath("$.must_change_password").value(true));
    mvc.perform(get("/auth/password-policy").cookie(c)).andExpect(status().isOk());
    mvc.perform(get("/auth/privacy-policy").cookie(c)).andExpect(status().isOk());
    mvc.perform(get("/workspaces").cookie(c)).andExpect(status().isOk());
    int health = mvc.perform(get("/actuator/health")).andReturn().getResponse().getStatus();   // 로그인 없이 부른 응답
    assertThat(health).isIn(200, 503);
    mvc.perform(get("/actuator/health").cookie(c)).andExpect(status().is(health));              // 로그인했다고 막지 않는다
    mvc.perform(post("/auth/consent").cookie(c)).andExpect(status().isNoContent());
    // 비밀번호를 바꾸면 같은 세션에서 바로 풀린다
    changePassword(c, temp, NEW_PASSWORD).andExpect(status().isNoContent());
    logs(c).andExpect(status().isOk());
    mvc.perform(post("/auth/logout").cookie(c)).andExpect(status().isNoContent());
  }

  @Test
  void expiredPasswordAndReactivatedAccountMustChangePassword() throws Exception {
    jdbc.sql("UPDATE users SET password_changed_at = now() - interval '181 days' WHERE id = ?").param(userBackend).update();   // 만료(180일)
    logs(cookieOf(login(empBackend, PASSWORD))).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));
    // 재활성화한 계정 — 예전 비밀번호로 들어오되 바꾸기 전에는 업무 API 를 쓰지 못한다
    member(company, userLead, "admin");
    long returned = user("복귀 직원", "사원", "T0005", "returned@test.local");
    member(wsSw, returned, "member");
    mvc.perform(asUser(post("/users/" + returned + "/deactivate"), userLead).contentType(MediaType.APPLICATION_JSON).content("{\"reason\": \"휴직\"}"))
        .andExpect(status().isNoContent());
    mvc.perform(asUser(post("/users/" + returned + "/reactivate"), userLead)).andExpect(status().isNoContent());
    logs(cookieOf(login("T0005", PASSWORD))).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));
  }

  @Test
  void accountWithoutCurrentConsentMustConsentFirst() throws Exception {
    clearConsent(userBackend);   // 동의 이력이 없는 계정
    Cookie first = cookieOf(login(empBackend, PASSWORD));
    logs(first).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("CONSENT_REQUIRED"));
    mvc.perform(post("/auth/consent").cookie(first)).andExpect(status().isNoContent());
    logs(first).andExpect(status().isOk());   // 동의하면 같은 세션에서 바로 풀린다
    clearConsent(userLead);      // 옛 판에만 동의한 계정
    jdbc.sql("UPDATE users SET consented_at = now() - interval '10 days' WHERE id = ?").param(userLead).update();
    jdbc.sql("INSERT INTO user_consents (user_id, policy_version, consented_at) VALUES (?, 'v1', now() - interval '10 days')").param(userLead).update();
    Cookie c = cookieOf(login(empLead, PASSWORD));
    logs(c).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("CONSENT_REQUIRED"));
    mvc.perform(post("/auth/consent").cookie(c)).andExpect(status().isNoContent());
    logs(c).andExpect(status().isOk());   // 동의하면 같은 세션에서 바로 풀린다
  }

  @Test
  void invitedAccountPassesConsentThenPasswordChange() throws Exception {
    MvcResult r = mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\": \"T0006\", \"name\": \"신규\"}")).andExpect(status().isCreated()).andReturn();
    String temp = JsonPath.read(r.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.temp_password");
    Cookie c = cookieOf(login("T0006", temp));   // 동의도 없고 임시 비밀번호다 — 화면 순서대로 동의가 먼저
    logs(c).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("CONSENT_REQUIRED"));
    mvc.perform(post("/auth/consent").cookie(c)).andExpect(status().isNoContent());
    logs(c).andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("PASSWORD_CHANGE_REQUIRED"));
    changePassword(c, temp, NEW_PASSWORD).andExpect(status().isNoContent());
    logs(c).andExpect(status().isOk());
  }

  @Test
  void inactiveAccountWithSurvivingSessionGets401AndTheSessionEnds() throws Exception {
    member(company, userLead, "admin");   // 배종현: 회사 관리자(GET /audit)이고 SW 개발의 admin(PATCH /workspaces/{id})
    Cookie a = cookieOf(login(empLead, PASSWORD)), b = cookieOf(login(empLead, PASSWORD)), c = cookieOf(login(empLead, PASSWORD));
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userLead).update();   // 세션 행은 남겨 둔다 — 비활성화와 겹친 로그인이 남긴 세션과 같은 상태
    long sessions = jdbc.sql("SELECT count(*) FROM spring_session WHERE principal_name = ?").param(empLead).query(Long.class).single();
    mvc.perform(get("/audit").cookie(a)).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    mvc.perform(patch("/workspaces/" + wsSw).cookie(b).contentType(MediaType.APPLICATION_JSON).content("{\"policy_enabled\": true}"))
        .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    mvc.perform(get("/me").cookie(c)).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    assertThat(jdbc.sql("SELECT count(*) FROM spring_session WHERE principal_name = ?").param(empLead).query(Long.class).single())
        .isEqualTo(sessions - 3);   // 요청에 쓴 세션 셋이 끝났다
  }

  @Test
  void loginIsRecheckedAgainstTheHashUsedForAuthentication() {
    // 인증과 세션 저장 사이에 재발급이 끝난 로그인은 요청 하나 안의 두 줄 사이라 API 로 겹치지 못한다 — 로그인이 끝에서 부르는 확인을 직접 본다
    String used = jdbc.sql("SELECT password_hash FROM users WHERE id = ?").param(userBackend).query(String.class).single();
    assertThat(accountGate.stillValid(userBackend, used)).isTrue();
    jdbc.sql("UPDATE users SET password_hash = ? WHERE id = ?").params(encoder.encode(NEW_PASSWORD), userBackend).update();   // 인증 뒤 재발급
    assertThat(accountGate.stillValid(userBackend, used)).isFalse();
    String lead = jdbc.sql("SELECT password_hash FROM users WHERE id = ?").param(userLead).query(String.class).single();
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userLead).update();                                   // 인증 뒤 비활성화
    assertThat(accountGate.stillValid(userLead, lead)).isFalse();
  }

  @Test
  void consentedAccountWithValidPasswordIsNotAffected() throws Exception {
    mvc.perform(as(get("/logs"), userLead, wsSw)).andExpect(status().isOk());
    mvc.perform(as(get("/users"), userLead, wsSw)).andExpect(status().isOk());
  }
}
