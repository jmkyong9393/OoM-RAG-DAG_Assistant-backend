package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.ResultActions;

/**
 * 계정 관리 — 임시 비밀번호 재발급(대상이 속한 모든 워크스페이스의 admin 또는 회사 관리자) · 비활성화 · 재활성화 · 비활성 목록(회사 관리자, 부서 헤더 없이).
 * 배종현 = 회사 관리자 + SW admin, 장문경 = SW member + 경영지원의 하나뿐인 admin, 김하늘 = SW member.
 */
class UserAccountApiTest extends AbstractApiTest {

  long userNew;
  String empNew = "T0003";

  @BeforeEach
  void seedAccounts() {
    member(company, userLead, "admin");
    userNew = user("김하늘", "상담사", empNew, "new@test.local");
    member(wsSw, userNew, "member");
  }

  private MvcResult login(String employeeNo, String password) throws Exception {
    return mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON)
        .content("{\"employee_no\": \"" + employeeNo + "\", \"password\": \"" + password + "\"}")).andReturn();
  }

  private long sessionsOf(String employeeNo) {
    return jdbc.sql("SELECT count(*) FROM spring_session WHERE principal_name = ?").param(employeeNo).query(Long.class).single();
  }

  /** 회사 단위 API 는 부서 헤더 없이 부른다. */
  private ResultActions deactivate(long actor, long target, String body) throws Exception {
    return mvc.perform(asUser(post("/users/" + target + "/deactivate"), actor).contentType(MediaType.APPLICATION_JSON).content(body));
  }

  private ResultActions reactivate(long actor, long target) throws Exception {
    return mvc.perform(asUser(post("/users/" + target + "/reactivate"), actor));
  }

  private String statusOf(long uid) {
    return jdbc.sql("SELECT status FROM users WHERE id = ?").param(uid).query(String.class).single();
  }

  private long auditCount(String action, long target) {
    return jdbc.sql("SELECT count(*) FROM audit_log WHERE table_name = 'users' AND action = ? AND row_id = ?").params(action, String.valueOf(target))
        .query(Long.class).single();
  }

  private long otherCompanyUser() {
    long other = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('o', 'O') RETURNING id").query(Long.class).single();
    return jdbc.sql("INSERT INTO users (tenant_id, name, employee_no, password_hash, password_changed_at) VALUES (?, '남의 직원', 'X0001', 'x', now()) RETURNING id")
        .param(other).query(Long.class).single();
  }

  // ── 임시 비밀번호 재발급 ─────────────────────────────────────────────

  @Test
  void resetPasswordReturnsTempPasswordOnceAndUnlocks() throws Exception {
    loginCookie(userNew);   // 재발급 전에 만든 세션 (잠그기 전에 로그인해 둔다)
    jdbc.sql("UPDATE users SET locked_until = now() + interval '1 hour', failed_attempts = 5 WHERE id = ?").param(userNew).update();
    MvcResult r = mvc.perform(as(post("/users/" + userNew + "/reset-password"), userLead, wsSw)).andExpect(status().isOk()).andReturn();
    String temp = JsonPath.read(r.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.temp_password");
    assertThat(temp).hasSize(14);
    assertThat(sessionsOf(empNew)).isZero();                                            // 대상의 기존 세션은 지워진다 (요청한 사람의 세션은 그대로)
    assertThat(sessionsOf(empLead)).isPositive();
    assertThat(login(empNew, PASSWORD).getResponse().getStatus()).isNotEqualTo(200);   // 예전 비밀번호는 더 쓸 수 없다
    MvcResult in = login(empNew, temp);                                                 // 잠금이 풀렸고 임시 비밀번호로 들어간다
    assertThat(in.getResponse().getStatus()).isEqualTo(200);
    assertThat((boolean) JsonPath.read(in.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.must_change_password")).isTrue();
    assertThat(auditCount("password_reset_by_admin", userNew)).isEqualTo(1L);
    assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE reason LIKE ? OR label LIKE ?").params("%" + temp + "%", "%" + temp + "%")
        .query(Long.class).single()).isZero();                                          // 임시 비밀번호는 어디에도 남기지 않는다
  }

  @Test
  void resetPasswordIsForWorkspaceAdmin() throws Exception {
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userBackend, wsSw)).andExpect(status().isForbidden());   // 장문경은 SW 의 member
  }

  @Test
  void resetPasswordOfOwnAccountIs400() throws Exception {
    mvc.perform(as(post("/users/" + userLead + "/reset-password"), userLead, wsSw)).andExpect(status().isBadRequest())
        .andExpect(jsonPath("$.code").value("VALIDATION"));
  }

  @Test
  void resetPasswordOfNonMemberIs404() throws Exception {
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userBackend, wsHr)).andExpect(status().isNotFound());   // 김하늘은 경영지원 소속이 아니다
    mvc.perform(as(post("/users/999999999/reset-password"), userLead, wsSw)).andExpect(status().isNotFound());
  }

  @Test
  void resetPasswordOfInactiveAccountIs409() throws Exception {
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userNew).update();
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userLead, wsSw)).andExpect(status().isConflict())
        .andExpect(jsonPath("$.code").value("ALREADY_INACTIVE"));
  }

  @Test
  void workspaceAdminCannotResetCompanyAdminPassword() throws Exception {
    member(wsHr, userLead, "member");   // 회사 관리자 배종현이 경영지원의 일반 멤버 — 그 부서 admin 은 장문경
    mvc.perform(as(post("/users/" + userLead + "/reset-password"), userBackend, wsHr)).andExpect(status().isForbidden());
    assertThat(login(empLead, PASSWORD).getResponse().getStatus()).isEqualTo(200);      // 비밀번호는 바뀌지 않았다
    member(company, userNew, "admin");   // 회사 관리자끼리는 된다
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userLead, wsSw)).andExpect(status().isOk());
  }

  @Test
  void resetPasswordNeedsAdminOfEveryWorkspaceTheTargetBelongsTo() throws Exception {
    member(wsHr, userNew, "member");   // 김하늘: SW member + 경영지원 member. 장문경은 경영지원 admin 이지만 SW 에서는 member
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userBackend, wsHr)).andExpect(status().isForbidden())
        .andExpect(jsonPath("$.code").value("FORBIDDEN"));                               // 대상이 다른 부서의 일반 멤버여도 — 권한이 아니라 소속을 본다
    assertThat(login(empNew, PASSWORD).getResponse().getStatus()).isEqualTo(200);      // 비밀번호는 바뀌지 않았다
    assertThat(auditCount("password_reset_by_admin", userNew)).isZero();
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userLead, wsSw)).andExpect(status().isOk());   // 회사 관리자는 경영지원의 admin 이 아니어도 된다
  }

  @Test
  void resetPasswordAfterInvitingIntoOwnWorkspaceIs403() throws Exception {
    // 장문경(경영지원 admin)이 SW 소속인 김하늘을 경영지원에 초대한다 — 이미 있는 계정이면 소속만 추가된다
    mvc.perform(as(post("/users"), userBackend, wsHr).content("{\"employee_no\": \"" + empNew + "\", \"name\": \"김하늘\"}")).andExpect(status().isCreated());
    mvc.perform(as(post("/users/" + userNew + "/reset-password"), userBackend, wsHr)).andExpect(status().isForbidden());   // 초대해도 김하늘은 SW 에 그대로 속해 있다
    assertThat(login(empNew, PASSWORD).getResponse().getStatus()).isEqualTo(200);
  }

  @Test
  void workspaceAdminResetsAccountThatBelongsOnlyToThatWorkspace() throws Exception {
    long hrOnly = user("경영지원 멤버", "사원", "T0004", "hr-only@test.local");
    member(wsHr, hrOnly, "member");
    MvcResult r = mvc.perform(as(post("/users/" + hrOnly + "/reset-password"), userBackend, wsHr)).andExpect(status().isOk()).andReturn();   // 장문경은 회사 관리자가 아니다
    String temp = JsonPath.read(r.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.temp_password");
    assertThat(login("T0004", temp).getResponse().getStatus()).isEqualTo(200);
  }

  // ── 비활성화 ────────────────────────────────────────────────────────

  @Test
  void deactivateKeepsMembershipAndStopsTheAccount() throws Exception {
    loginCookie(userNew);   // 비활성화 전에 만든 세션
    assertThat(sessionsOf(empNew)).isPositive();
    deactivate(userLead, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isNoContent());
    assertThat(statusOf(userNew)).isEqualTo("inactive");
    assertThat(jdbc.sql("SELECT count(*) FROM workspace_members WHERE user_id = ?").param(userNew).query(Long.class).single()).isEqualTo(1L);   // 소속은 그대로
    // 기존 세션은 지워진다(다른 사람의 세션은 그대로). 지워진 쿠키로 요청을 보내 401 을 보지는 않는다 — 테스트는 한 트랜잭션 안에서 돌고
    // 롤백하므로 삭제가 커밋되지 않은 채 남고, 세션 저장소(별도 연결)가 그 행을 기다리며 멈춘다. 운영에서는 응답 전에 커밋된다
    assertThat(sessionsOf(empNew)).isZero();
    assertThat(sessionsOf(empLead)).isPositive();
    assertThat(login(empNew, PASSWORD).getResponse().getStatus()).isNotEqualTo(200);    // 새 로그인도 안 된다
    mvc.perform(as(get("/users"), userLead, wsSw)).andExpect(jsonPath("$[?(@.id == " + userNew + ")]").isEmpty());   // 멤버 목록에서 빠진다
  }

  @Test
  void deactivationShowsReasonAndActorInAuditLog() throws Exception {
    deactivate(userLead, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isNoContent());
    mvc.perform(asUser(get("/audit").param("action", "user_deactivated"), userLead)).andExpect(status().isOk())
        .andExpect(jsonPath("$.items.length()").value(1)).andExpect(jsonPath("$.items[0].category").value("member"))
        .andExpect(jsonPath("$.items[0].employee_no").value(empNew)).andExpect(jsonPath("$.items[0].actor").value("배종현"))
        .andExpect(jsonPath("$.items[0].reason").value("퇴사"));
  }

  @Test
  void deactivateNeedsReason() throws Exception {
    deactivate(userLead, userNew, "{}").andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("REASON_REQUIRED"));
    deactivate(userLead, userNew, "{\"reason\": \"  \"}").andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("REASON_REQUIRED"));
    assertThat(statusOf(userNew)).isEqualTo("active");
  }

  @Test
  void deactivateOwnAccountIs409() throws Exception {
    member(company, userNew, "admin");   // 회사 관리자가 둘이어도 자기 자신은 안 된다
    deactivate(userLead, userLead, "{\"reason\": \"테스트\"}").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SELF_DEACTIVATION"));
  }

  @Test
  void deactivateLastCompanyAdminIs409() throws Exception {
    deactivate(userLead, userLead, "{\"reason\": \"테스트\"}").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LAST_COMPANY_ADMIN"));
    assertThat(statusOf(userLead)).isEqualTo("active");
  }

  @Test
  void deactivateAlreadyInactiveIs409() throws Exception {
    deactivate(userLead, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isNoContent());
    deactivate(userLead, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("ALREADY_INACTIVE"));
    assertThat(auditCount("user_deactivated", userNew)).isEqualTo(1L);
  }

  @Test
  void deactivateLastWorkspaceAdminIs409() throws Exception {
    deactivate(userLead, userBackend, "{\"reason\": \"퇴사\"}").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LAST_ADMIN"));   // 경영지원의 하나뿐인 admin
    member(wsHr, userNew, "admin");
    deactivate(userLead, userBackend, "{\"reason\": \"퇴사\"}").andExpect(status().isNoContent());
  }

  @Test
  void companyLevelApisAreForCompanyAdmin() throws Exception {
    deactivate(userBackend, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isForbidden());   // 장문경은 부서 admin 일 뿐이다
    reactivate(userBackend, userNew).andExpect(status().isForbidden());
    mvc.perform(asUser(get("/users/inactive"), userBackend)).andExpect(status().isForbidden());
    mvc.perform(post("/users/" + userNew + "/deactivate").contentType(MediaType.APPLICATION_JSON).content("{\"reason\": \"x\"}"))
        .andExpect(status().isUnauthorized());
    assertThat(statusOf(userNew)).isEqualTo("active");
  }

  @Test
  void otherCompanyOrUnknownUserIs404() throws Exception {
    long stranger = otherCompanyUser();
    deactivate(userLead, stranger, "{\"reason\": \"퇴사\"}").andExpect(status().isNotFound());
    deactivate(userLead, 999_999_999L, "{\"reason\": \"퇴사\"}").andExpect(status().isNotFound());
    reactivate(userLead, stranger).andExpect(status().isNotFound());
    assertThat(statusOf(stranger)).isEqualTo("active");
  }

  // ── 재활성화 · 비활성 목록 · 재초대 ──────────────────────────────────

  @Test
  void reactivateRestoresLoginAndUnlocks() throws Exception {
    deactivate(userLead, userNew, "{\"reason\": \"휴직\"}").andExpect(status().isNoContent());
    jdbc.sql("UPDATE users SET locked_until = now() + interval '1 hour', failed_attempts = 5 WHERE id = ?").param(userNew).update();
    reactivate(userLead, userNew).andExpect(status().isNoContent());
    assertThat(statusOf(userNew)).isEqualTo("active");
    MvcResult in = login(empNew, PASSWORD);                                         // 잠금도 풀렸다
    assertThat(in.getResponse().getStatus()).isEqualTo(200);
    assertThat((boolean) JsonPath.read(in.getResponse().getContentAsString(StandardCharsets.UTF_8), "$.must_change_password")).isTrue();   // 예전 비밀번호로 들어오되 바꿔야 한다
    reactivate(userLead, userNew).andExpect(status().isNoContent());                // 이미 활성이면 그대로 204
    assertThat(auditCount("user_reactivated", userNew)).isEqualTo(1L);
  }

  @Test
  void inactiveListWorksWithoutWorkspaceHeader() throws Exception {
    deactivate(userLead, userNew, "{\"reason\": \"퇴사\"}").andExpect(status().isNoContent());
    otherCompanyUser();
    mvc.perform(asUser(get("/users/inactive"), userLead)).andExpect(status().isOk()).andExpect(jsonPath("$.length()").value(1))
        .andExpect(jsonPath("$[0].id").value(userNew)).andExpect(jsonPath("$[0].employee_no").value(empNew)).andExpect(jsonPath("$[0].name").value("김하늘"))
        .andExpect(jsonPath("$[0].email_masked").exists()).andExpect(jsonPath("$[0].role").doesNotExist()).andExpect(jsonPath("$[0].locked").doesNotExist());
  }

  @Test
  void invitingInactiveAccountIs409() throws Exception {
    jdbc.sql("UPDATE users SET status = 'inactive' WHERE id = ?").param(userNew).update();
    mvc.perform(as(post("/users"), userBackend, wsHr).content("{\"employee_no\": \"" + empNew + "\", \"name\": \"김하늘\"}"))
        .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("ALREADY_INACTIVE"));
    assertThat(jdbc.sql("SELECT count(*) FROM workspace_members WHERE user_id = ? AND workspace_id = ?").params(userNew, wsHr).query(Long.class).single()).isZero();
  }
}
