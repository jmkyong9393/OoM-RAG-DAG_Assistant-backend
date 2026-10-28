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

/** 이메일+비밀번호 로그인 → 세션 쿠키(OOMSESSION, Spring Session JDBC) → /me → 로그아웃 → 비밀번호 변경 → 관리자 초대. */
class AuthApiTest extends AbstractApiTest {

  static final String COOKIE = "OOMSESSION";

  private MvcResult login(String email, String password) throws Exception {
    return mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON)
        .content("{\"email\": \"" + email + "\", \"password\": \"" + password + "\"}")).andReturn();
  }

  /** Spring Session 은 Set-Cookie 헤더를 직접 쓴다(response.addCookie 가 아님) → 헤더에서 파싱. 속성(HttpOnly·Max-Age)도 여기서 본다. */
  private String setCookieHeader(MvcResult r) {
    return r.getResponse().getHeaders("Set-Cookie").stream().filter(h -> h.startsWith(COOKIE + "=")).findFirst().orElse(null);
  }

  private Cookie sessionCookie(MvcResult r) {
    String h = setCookieHeader(r);
    assertThat(h).as("session cookie").isNotNull();
    String value = h.substring(COOKIE.length() + 1, h.indexOf(';') < 0 ? h.length() : h.indexOf(';'));
    assertThat(value).isNotBlank();
    return new Cookie(COOKIE, value);
  }

  @Test
  void loginSetsHttpOnlySessionCookieAndReturnsMe() throws Exception {
    MvcResult r = login(emailBackend, PASSWORD);
    assertThat(r.getResponse().getStatus()).isEqualTo(200);
    Cookie c = sessionCookie(r);
    assertThat(setCookieHeader(r)).contains("HttpOnly").contains("SameSite=Lax").doesNotContain("Secure");
    // 72 시간 유지 (spring.session.timeout)
    assertThat(jdbc.sql("SELECT max(max_inactive_interval) FROM spring_session").query(Integer.class).single()).isEqualTo(72 * 3600);
    String body = r.getResponse().getContentAsString(StandardCharsets.UTF_8);
    assertThat((int) JsonPath.read(body, "$.id")).isEqualTo((int) userBackend);
    assertThat((String) JsonPath.read(body, "$.email")).isEqualTo(emailBackend);
    assertThat((int) JsonPath.read(body, "$.workspace_id")).isEqualTo((int) wsHr);   // admin 멤버십이 홈
    assertThat((java.util.List<?>) JsonPath.read(body, "$.workspaces")).hasSize(2);
    assertThat((boolean) JsonPath.read(body, "$.must_change_password")).isFalse();
    // 세션이 Postgres 에 저장되고 last_login_at 이 찍힌다
    assertThat(jdbc.sql("SELECT count(*) FROM spring_session").query(Long.class).single()).isGreaterThanOrEqualTo(1L);
    assertThat(jdbc.sql("SELECT last_login_at IS NOT NULL FROM users WHERE id = ?").param(userBackend).query(Boolean.class).single()).isTrue();
    // 같은 쿠키로 /me (워크스페이스 헤더 없이)
    mvc.perform(get("/me").cookie(c)).andExpect(status().isOk()).andExpect(jsonPath("$.name").value("장문경"));
    // 쿠키 + 헤더로 보호된 엔드포인트
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isOk());
  }

  @Test
  void wrongPasswordUnknownEmailAndInactiveUserAre401WithoutRevealingWhich() throws Exception {
    assertThat(login(emailBackend, "nope").getResponse().getStatus()).isEqualTo(401);
    MvcResult unknown = login("ghost@test.local", PASSWORD);
    assertThat(unknown.getResponse().getStatus()).isEqualTo(401);
    assertThat(unknown.getResponse().getContentAsString(StandardCharsets.UTF_8)).contains("\"code\":\"UNAUTHORIZED\"").doesNotContain("ghost");
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userLead).update();
    assertThat(login(emailLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
    // 비밀번호가 없는 계정(초대 전·SSO 전용)도 로그인 불가
    jdbc.sql("UPDATE users SET password_hash = NULL, status = 'active' WHERE id = ?").param(userLead).update();
    assertThat(login(emailLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
  }

  @Test
  void emailIsCaseInsensitive() throws Exception {
    assertThat(login(emailBackend.toUpperCase(), PASSWORD).getResponse().getStatus()).isEqualTo(200);
  }

  @Test
  void unauthenticatedRequestsGet401Json() throws Exception {
    mvc.perform(get("/me")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("UNAUTHORIZED"));
    mvc.perform(get("/logs").header(TenantContextFilter.WORKSPACE_HEADER, wsSw)).andExpect(status().isUnauthorized());
    mvc.perform(get("/me").cookie(new Cookie(COOKIE, "forged-session-id"))).andExpect(status().isUnauthorized());
  }

  @Test
  void logoutInvalidatesSessionServerSide() throws Exception {
    Cookie c = sessionCookie(login(emailLead, PASSWORD));
    mvc.perform(get("/me").cookie(c)).andExpect(status().isOk());
    MvcResult out = mvc.perform(post("/auth/logout").cookie(c)).andExpect(status().isNoContent()).andReturn();
    assertThat(setCookieHeader(out)).as("expired cookie").contains("Max-Age=0");
    mvc.perform(get("/me").cookie(c)).andExpect(status().isUnauthorized());   // 재사용 불가 (서버 세션 삭제)
  }

  @Test
  void changePasswordRequiresCurrentAndTakesEffectImmediately() throws Exception {
    Cookie c = sessionCookie(login(emailLead, PASSWORD));
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON)
            .content("{\"current_password\": \"wrong\", \"new_password\": \"new-secret-99\"}"))
        .andExpect(status().isForbidden());
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON)
            .content("{\"current_password\": \"" + PASSWORD + "\", \"new_password\": \"short\"}"))
        .andExpect(status().isBadRequest());
    mvc.perform(patch("/auth/password").cookie(c).contentType(MediaType.APPLICATION_JSON)
            .content("{\"current_password\": \"" + PASSWORD + "\", \"new_password\": \"new-secret-99\"}"))
        .andExpect(status().isNoContent());
    assertThat(login(emailLead, PASSWORD).getResponse().getStatus()).isEqualTo(401);
    assertThat(login(emailLead, "new-secret-99").getResponse().getStatus()).isEqualTo(200);
  }

  @Test
  void adminInvitesNewUserWithTempPasswordAndMembership() throws Exception {
    String body = "{\"email\": \"New.Person@test.local\", \"name\": \"김하늘\", \"title\": \"상담사\", \"role\": \"member\"}";
    MvcResult r = mvc.perform(as(post("/users"), userLead, wsSw).content(body)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(true)).andExpect(jsonPath("$.email").value("new.person@test.local"))
        .andExpect(jsonPath("$.workspace_id").value(wsSw)).andReturn();
    String json = r.getResponse().getContentAsString(StandardCharsets.UTF_8);
    String temp = JsonPath.read(json, "$.temp_password");
    int newId = JsonPath.read(json, "$.id");
    assertThat(temp).hasSize(14);
    // 임시 비밀번호로 로그인되고 must_change_password 가 true
    MvcResult login = login("new.person@test.local", temp);
    assertThat(login.getResponse().getStatus()).isEqualTo(200);
    assertThat((boolean) JsonPath.read(login.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.must_change_password")).isTrue();
    // 초대된 워크스페이스에만 소속 → HR 은 401(존재 숨김)
    Cookie c = sessionCookie(login);
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsSw)).andExpect(status().isOk());
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isUnauthorized());
    // 감사 로그
    assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE table_name = 'users' AND row_id = ? AND action = 'invite'").param(String.valueOf(newId))
        .query(Long.class).single()).isEqualTo(1L);
    // 같은 이메일을 HR 로 다시 초대하면 계정은 재사용, 멤버십만 추가 (임시 비밀번호 없음)
    mvc.perform(as(post("/users"), userBackend, wsHr).content(body)).andExpect(status().isCreated())
        .andExpect(jsonPath("$.created").value(false)).andExpect(jsonPath("$.temp_password").value(nullValue()));
    mvc.perform(get("/logs").cookie(c).header(TenantContextFilter.WORKSPACE_HEADER, wsHr)).andExpect(status().isOk());
    // 또 초대하면 409
    mvc.perform(as(post("/users"), userBackend, wsHr).content(body)).andExpect(status().isConflict());
  }

  @Test
  void inviteRequiresAdminAndValidInput() throws Exception {
    String body = "{\"email\": \"x@test.local\", \"name\": \"X\"}";
    mvc.perform(as(post("/users"), userBackend, wsSw).content(body)).andExpect(status().isForbidden());   // SW 에서 장문경은 member
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"email\": \"not-an-email\", \"name\": \"X\"}")).andExpect(status().isBadRequest());
    mvc.perform(as(post("/users"), userLead, wsSw).content("{\"email\": \"x@test.local\", \"name\": \"X\", \"role\": \"owner\"}")).andExpect(status().isBadRequest());
  }

  @Test
  void membersListShowsCurrentWorkspaceOnly() throws Exception {
    mvc.perform(as(get("/users"), userBackend, wsHr)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1)))
        .andExpect(jsonPath("$[0].email").value(emailBackend)).andExpect(jsonPath("$[0].role").value("admin"));
    mvc.perform(as(get("/users"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)));
  }
}
