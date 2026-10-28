package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.hamcrest.Matchers.nullValue;
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

/**
 * 사번+비밀번호 로그인 → 세션 쿠키(OOMSESSION, Spring Session JDBC) → /me → 로그아웃 → 비밀번호 규칙·변경·만료 → 잠금 → 동의 → 관리자 초대·마스킹.
 * 규제 가이드 대조는 개발문서 35.
 */
class AuthApiTest extends AbstractApiTest {

  static final String COOKIE = "OOMSESSION";
  static final String GOOD_NEW = "Nv!pass-2026x";

  private MvcResult login(String employeeNo, String password) throws Exception {
    return mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON)
        .content("{\"employee_no\": \"" + employeeNo + "\", \"password\": \"" + password + "\"}")).andReturn();
  }

  private String setCookieHeader(MvcResult r) {
    return r.getResponse().getHeaders("Set-Cookie").stream().filter(h -> h.startsWith(COOKIE + "=")).findFirst().orElse(null);
  }

  private Cookie sessionCookie(MvcResult r) {
    String h = setCookieHeader(r);
    assertThat(h).as("session cookie").isNotNull();
    return new Cookie(COOKIE, h.substring(COOKIE.length() + 1, h.indexOf(';')));
  }

  private String body(MvcResult r) throws Exception {
    return r.getResponse().getContentAsString(StandardCharsets.UTF_8);
  }

  private long auditCount(String action, String actor) {
    return jdbc.sql("SELECT count(*) FROM audit_log WHERE table_name = 'auth' AND action = ? AND actor = ?").params(action, actor).query(Long.class).single();
  }

  @Test
  void loginSetsHttpOnlySessionCookieAndReturnsMe() throws Exception {
    MvcResult r = login(empBackend, PASSWORD);
    assertThat(r.getResponse().getStatus()).isEqualTo(200);
    Cookie c = sessionCookie(r);
    assertThat(setCookieHeader(r)).contains("HttpOnly").contains("SameSite=Lax").doesNotContain("Secure");
    String b = body(r);
    assertThat((int) JsonPath.read(b, "$.id")).isEqualTo((int) userBackend);
    assertThat((String) JsonPath.read(b, "$.employee_no")).isEqualTo(empBackend);
    assertThat((int) JsonPath.read(b, "$.workspace_id")).isEqualTo((int) wsHr);   // admin 멤버십이 홈
    assertThat((java.util.List<?>) JsonPath.read(b, "$.workspaces")).hasSize(2);
    assertThat((boolean) JsonPath.read(b, "$.must_change_password")).isFalse();
    assertThat((boolean) JsonPath.read(b, "$.consent_required")).isTrue();      // 첫 로그인: 개인정보 동의 전
    assertThat((String) JsonPath.read(b, "$.password_expires_at")).isNotBlank();
    // 세션은 Postgres 에, 미사용 만료 30분, 접속기록 남김
    assertThat(jdbc.sql("SELECT max(max_inactive_interval) FROM spring_session").query(Integer.class).single()).isEqualTo(30 * 60);
    assertThat(jdbc.sql("SELECT last_login_at IS NOT NULL FROM users WHERE id = ?").param(userBackend).query(Boolean.class).single()).isTrue();
    assertThat(auditCount("login_success", empBackend)).isEqualTo(1L);
    mvc.perform(get("/me").cookie(c)).andExpect(status().isOk()).andExpect(jsonPath("$.name").value("장문경"));
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isOk());
  }

  @Test
  void failuresAre401WithoutRevealingWhichAndAreAudited() throws Exception {
    assertThat(login(empBackend, "nope").getResponse().getStatus()).isEqualTo(401);
    MvcResult unknown = login("ZZZ9999", PASSWORD);
    assertThat(unknown.getResponse().getStatus()).isEqualTo(401);
    assertThat(body(unknown)).contains("\"code\":\"UNAUTHORIZED\"").doesNotContain("ZZZ9999");
    assertThat(auditCount("login_failed", empBackend)).isEqualTo(1L);
    assertThat(auditCount("login_failed", "ZZZ9999")).isEqualTo(1L);          // 없는 사번도 접속기록은 남는다
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userLead).update();
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
    jdbc.sql("UPDATE users SET password_hash = NULL, status = 'active' WHERE id = ?").param(userLead).update();
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
    assertThat(login(empBackend.toLowerCase(), PASSWORD).getResponse().getStatus()).isEqualTo(200);   // 사번 대소문자 무시
  }

  @Test
  void fiveFailuresLockTheAccountForFifteenMinutes() throws Exception {
    for (int i = 1; i <= 4; i++) assertThat(login(empLead, "wrong-" + i).getResponse().getStatus()).isEqualTo(401);
    MvcResult fifth = login(empLead, "wrong-5");
    assertThat(fifth.getResponse().getStatus()).isEqualTo(423);
    assertThat(body(fifth)).contains("ACCOUNT_LOCKED");
    // 잠긴 동안은 맞는 비밀번호도 423
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(423);
    assertThat(auditCount("account_locked", empLead)).isEqualTo(1L);
    assertThat(auditCount("login_rejected_locked", empLead)).isEqualTo(1L);
    // 멤버 목록에 잠금 표시
    mvc.perform(as(get("/users"), userBackend, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$[?(@.employee_no == '" + empLead + "')].locked").value(true));
    // 시간이 지나면 풀린다 (locked_until 을 과거로)
    jdbc.sql("UPDATE users SET locked_until = now() - interval '1 minute' WHERE id = ?").param(userLead).update();
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(200);
    assertThat(jdbc.sql("SELECT failed_attempts FROM users WHERE id = ?").param(userLead).query(Integer.class).single()).isZero();
  }

  @Test
  void adminCanUnlock() throws Exception {
    jdbc.sql("UPDATE users SET locked_until = now() + interval '10 minute', failed_attempts = 3 WHERE id = ?").param(userBackend).update();
    assertThat(login(empBackend, PASSWORD).getResponse().getStatus()).isEqualTo(423);
    mvc.perform(as(post("/users/" + userBackend + "/unlock"), userLead, wsSw)).andExpect(status().isNoContent());        // SW admin 배종현이 해제
    assertThat(login(empBackend, PASSWORD).getResponse().getStatus()).isEqualTo(200);
    assertThat(auditCount("unlocked_by_admin", empBackend)).isEqualTo(1L);
  }

  @Test
  void unauthenticatedRequestsGet401Json() throws Exception {
    mvc.perform(get("/me")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    mvc.perform(get("/logs").header(TenantContextFilter.WORKSPACE_HEADER, wsSw)).andExpect(status().isUnauthorized());
    mvc.perform(get("/me").cookie(new Cookie(COOKIE, "forged-session-id"))).andExpect(status().isUnauthorized());
    mvc.perform(get("/auth/password-policy")).andExpect(status().isOk()).andExpect(jsonPath("$.max_age_days").value(180))
        .andExpect(jsonPath("$.lock_after_failures").value(5));
  }

  @Test
  void logoutInvalidatesSessionServerSide() throws Exception {
    Cookie c = sessionCookie(login(empLead, PASSWORD));
    mvc.perform(get("/me").cookie(c)).andExpect(status().isOk());
    MvcResult out = mvc.perform(post("/auth/logout").cookie(c)).andExpect(status().isNoContent()).andReturn();
    assertThat(setCookieHeader(out)).as("expired cookie").contains("Max-Age=0");
    mvc.perform(get("/me").cookie(c)).andExpect(status().isUnauthorized());   // 재사용 불가 (서버 세션 삭제)
    assertThat(auditCount("logout", empLead)).isEqualTo(1L);
  }

  @Test
  void passwordChangeEnforcesPolicyAndTakesEffectImmediately() throws Exception {
    Cookie c = sessionCookie(login(empLead, PASSWORD));
    String tpl = "{\"current_password\": \"%s\", \"new_password\": \"%s\"}";
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON).content(tpl.formatted("wrong", GOOD_NEW)))
        .andExpect(status().isForbidden());
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON).content(tpl.formatted(PASSWORD, "short1!")))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("PASSWORD_POLICY"));
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON).content(tpl.formatted(PASSWORD, "Abc!" + empLead + "xyz")))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.message").value(org.hamcrest.Matchers.containsString("사번")));
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON).content(tpl.formatted(PASSWORD, PASSWORD)))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.message").value(org.hamcrest.Matchers.containsString("이전")));
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON).content(tpl.formatted(PASSWORD, GOOD_NEW)))
        .andExpect(status().isNoContent());
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
    assertThat(login(empLead, GOOD_NEW).getResponse().getStatus()).isEqualTo(200);
    assertThat(auditCount("password_changed", empLead)).isEqualTo(1L);
  }

  @Test
  void expiredPasswordForcesChange() throws Exception {
    jdbc.sql("UPDATE users SET password_changed_at = now() - interval '181 days' WHERE id = ?").param(userLead).update();
    assertThat((boolean) JsonPath.read(body(login(empLead, PASSWORD)), "$.must_change_password")).isTrue();
    jdbc.sql("UPDATE users SET password_changed_at = now() - interval '10 days' WHERE id = ?").param(userLead).update();
    assertThat((boolean) JsonPath.read(body(login(empLead, PASSWORD)), "$.must_change_password")).isFalse();
  }

  @Test
  void consentIsRecordedOnce() throws Exception {
    Cookie c = sessionCookie(login(empLead, PASSWORD));
    mvc.perform(post("/auth/consent").cookie(c)).andExpect(status().isNoContent());
    mvc.perform(get("/me").cookie(c)).andExpect(jsonPath("$.consent_required").value(false));
    String first = jdbc.sql("SELECT consented_at::text FROM users WHERE id = ?").param(userLead).query(String.class).single();
    mvc.perform(post("/auth/consent").cookie(c)).andExpect(status().isNoContent());
    assertThat(jdbc.sql("SELECT consented_at::text FROM users WHERE id = ?").param(userLead).query(String.class).single()).isEqualTo(first);
    assertThat(auditCount("consent", empLead)).isEqualTo(2L);
  }

  @Test
  void adminInvitesNewUserWithTempPasswordAndMembership() throws Exception {
    String req = "{\"employee_no\": \"t0003\", \"name\": \"김하늘\", \"email\": \"New.Person@test.local\", \"title\": \"상담사\", \"role\": \"member\"}";
    MvcResult r = mvc.perform(as(post("/users"), userLead, wsSw).content(req)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(true)).andExpect(jsonPath("$.employee_no").value("T0003"))
        .andExpect(jsonPath("$.workspace_id").value(wsSw)).andReturn();
    String temp = JsonPath.read(body(r), "$.temp_password");
    int newId = JsonPath.read(body(r), "$.id");
    assertThat(temp).hasSize(14);
    // 임시 비밀번호는 작성규칙을 만족하고, 첫 로그인에 변경을 요구한다
    MvcResult login = login("T0003", temp);
    assertThat(login.getResponse().getStatus()).isEqualTo(200);
    assertThat((boolean) JsonPath.read(body(login), "$.must_change_password")).isTrue();
    Cookie c = sessionCookie(login);
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsSw)).andExpect(status().isOk());
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isUnauthorized());
    assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE table_name = 'users' AND row_id = ? AND action = 'invite'").param(String.valueOf(newId))
        .query(Long.class).single()).isEqualTo(1L);
    // 같은 사번을 HR 로 다시 초대하면 계정 재사용, 멤버십만 추가 (임시 비밀번호 없음) → 또 초대하면 409
    mvc.perform(as(post("/users"), userBackend, wsHr).content(req)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(false)).andExpect(jsonPath("$.temp_password").value(nullValue()));
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isOk());
    mvc.perform(as(post("/users"), userBackend, wsHr).content(req)).andExpect(status().isConflict());
  }

  @Test
  void inviteRequiresAdminAndValidInput() throws Exception {
    String req = "{\"employee_no\": \"T0009\", \"name\": \"X\"}";
    mvc.perform(as(post("/users"), userBackend, wsSw).content(req)).andExpect(status().isForbidden());   // SW 에서 장문경은 member
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\": \"bad no!\", \"name\": \"X\"}")).andExpect(status().isBadRequest());
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\": \"T0009\", \"name\": \"X\", \"email\": \"not-an-email\"}")).andExpect(status().isBadRequest());
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"employee_no\": \"T0009\", \"name\": \"X\", \"role\": \"owner\"}")).andExpect(status().isBadRequest());
  }

  @Test
  void membersListMasksEmailAndShowsCurrentWorkspaceOnly() throws Exception {
    mvc.perform(as(get("/users"), userBackend, wsHr)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].employee_no").value(empBackend)).andExpect(jsonPath("$[0].email_masked").value("b***@test.local"))
        .andExpect(jsonPath("$[0].role").value("admin")).andExpect(jsonPath("$[0].email").doesNotExist());
    mvc.perform(as(get("/users"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)));
  }
}
