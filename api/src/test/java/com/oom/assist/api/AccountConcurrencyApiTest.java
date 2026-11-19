package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * 비활성화와 강등 · 비활성화끼리를 실제로 겹친다. 시점을 sleep 으로 맞추지 않고 잠금으로 순서를 만든다 —
 * 테스트가 두 요청이 모두 거치는 잠금(UserAdminController.lockMembers · lockCompany)을 먼저 쥐고, 두 요청이 그 앞에서 기다리는 것을 확인한 뒤 풀어 준다.
 * 먼저 기다린 요청이 먼저 지나가므로(PostgreSQL 은 잠금을 기다린 순서대로 준다) 보내는 순서를 바꿔 두 가지 순서를 모두 본다.
 * 어느 쪽이 먼저 끝나도 관리자가 0명이 되지 않고, 서로 기다리다 멈추지 않는지 본다.
 */
@Transactional(propagation = Propagation.NOT_SUPPORTED) // 요청이 다른 스레드 · 커넥션에서 돌므로 테스트 트랜잭션을 끄고 직접 정리한다 (ChatRelayApiTest 와 같다)
class AccountConcurrencyApiTest extends AbstractApiTest {

  static final long TIMEOUT_SECONDS = 20;

  @Autowired DataSource dataSource;

  @AfterEach
  void cleanup() {
    jdbc.sql("DELETE FROM spring_session").update();
    jdbc.sql("DELETE FROM audit_log WHERE tenant_id = ?").param(tenant).update();
    jdbc.sql("DELETE FROM workspace_members WHERE workspace_id IN (?, ?, ?)").params(wsSw, wsHr, company).update();
    jdbc.sql("DELETE FROM workspaces WHERE id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM workspaces WHERE id = ?").param(company).update();
    jdbc.sql("DELETE FROM users WHERE tenant_id = ?").param(tenant).update();
    jdbc.sql("DELETE FROM tenants WHERE id = ?").param(tenant).update();
  }

  /**
   * 잠금 (key, id) 를 쥔 채 first 를 보내 그 잠금 앞에 선 것을 확인하고, 이어서 second 를 보내 둘 다 기다리면 풀어 준다.
   * first 가 먼저 지나간다. 두 요청의 응답 코드를 보낸 순서대로 돌려준다.
   */
  private List<Integer> overlap(int key, long id, MockHttpServletRequestBuilder first, MockHttpServletRequestBuilder second) throws Exception {
    ExecutorService pool = Executors.newFixedThreadPool(2);
    try (Connection holder = dataSource.getConnection()) {
      holder.setAutoCommit(false);
      try (PreparedStatement ps = holder.prepareStatement("SELECT pg_advisory_xact_lock(?, ?)")) {
        ps.setInt(1, key);
        ps.setInt(2, (int) id);
        ps.execute();
      }
      Future<Integer> a = pool.submit(() -> mvc.perform(first).andReturn().getResponse().getStatus());
      awaitWaiting(key, id, 1, a);     // 시간을 재지 않고 상태를 본다
      Future<Integer> b = pool.submit(() -> mvc.perform(second).andReturn().getResponse().getStatus());
      awaitWaiting(key, id, 2, a, b);
      holder.commit();                 // 풀어 준다 — first, second 순서로 지나간다
      return List.of(a.get(TIMEOUT_SECONDS, TimeUnit.SECONDS), b.get(TIMEOUT_SECONDS, TimeUnit.SECONDS));   // 제한 시간 안에 둘 다 끝난다
    } finally {
      pool.shutdownNow();
    }
  }

  private void awaitWaiting(int key, long id, int count, Future<?>... sent) {
    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(TIMEOUT_SECONDS);
    while (jdbc.sql("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted AND classid::bigint = ? AND objid::bigint = ?")
        .params((long) key, id).query(Long.class).single() < count) {
      assertThat(System.nanoTime()).as("요청이 잠금 앞에 서지 않았다").isLessThan(deadline);
      for (Future<?> f : sent) assertThat(f.isDone()).as("잠금에 닿기 전에 끝난 요청이 있다").isFalse();
      Thread.onSpinWait();
    }
  }

  // ── 부서: 경영지원의 admin 둘(장문경 · 후임) 가운데 하나를 비활성화하면서 다른 하나를 강등한다 ──

  private long third;
  private MockHttpServletRequestBuilder deactivateBackend, demoteThird;

  private void twoAdminsInHr() {
    third = user("후임", "매니저", "T0007", "third@test.local");
    member(wsHr, third, "admin");
    member(company, userLead, "admin");      // 배종현: 회사 관리자
    member(wsHr, userLead, "member");
    deactivateBackend = asUser(post("/users/" + userBackend + "/deactivate"), userLead).contentType(MediaType.APPLICATION_JSON).content("{\"reason\": \"퇴사\"}");
    demoteThird = as(patch("/users/" + third), userLead, wsHr).content("{\"role\": \"member\"}");
  }

  private List<Long> activeAdminsOf(long ws) {
    return jdbc.sql("""
          SELECT m.user_id FROM workspace_members m JOIN users u ON u.id = m.user_id
          WHERE m.workspace_id = ? AND m.role = 'admin' AND u.status = 'active'""").param(ws).query(Long.class).list();
  }

  @Test
  void deactivationFirstThenDemotionIsRejected() throws Exception {
    twoAdminsInHr();
    assertThat(overlap(7, wsHr, deactivateBackend, demoteThird)).containsExactly(204, 409);   // 7 = lockMembers. 뒤쪽은 LAST_ADMIN
    assertThat(activeAdminsOf(wsHr)).containsExactly(third);
  }

  @Test
  void demotionFirstThenDeactivationIsRejected() throws Exception {
    twoAdminsInHr();
    assertThat(overlap(7, wsHr, demoteThird, deactivateBackend)).containsExactly(200, 409);
    assertThat(activeAdminsOf(wsHr)).containsExactly(userBackend);
  }

  // ── 회사: 회사 관리자 둘(배종현 · 둘째)이 서로를 비활성화한다 ──

  private long second;
  private MockHttpServletRequestBuilder leadDeactivatesSecond, secondDeactivatesLead;

  private void twoCompanyAdmins() {
    second = user("둘째", "팀장", "T0007", "second@test.local");
    member(company, userLead, "admin");
    member(company, second, "admin");
    member(wsSw, second, "admin");           // SW 개발의 admin 도 둘 — 부서의 마지막 admin 이라 막히는 경우가 아니게
    leadDeactivatesSecond = asUser(post("/users/" + second + "/deactivate"), userLead).contentType(MediaType.APPLICATION_JSON).content("{\"reason\": \"퇴사\"}");
    secondDeactivatesLead = asUser(post("/users/" + userLead + "/deactivate"), second).contentType(MediaType.APPLICATION_JSON).content("{\"reason\": \"퇴사\"}");
  }

  @Test
  void companyAdminsDeactivatingEachOtherLeavesTheOneWhoWentFirst() throws Exception {
    twoCompanyAdmins();
    assertThat(overlap(8, tenant, leadDeactivatesSecond, secondDeactivatesLead)).containsExactly(204, 409);   // 8 = lockCompany. 뒤쪽은 LAST_COMPANY_ADMIN
    assertThat(activeAdminsOf(company)).containsExactly(userLead);
  }

  @Test
  void companyAdminsDeactivatingEachOtherInTheOtherOrder() throws Exception {
    twoCompanyAdmins();
    assertThat(overlap(8, tenant, secondDeactivatesLead, leadDeactivatesSecond)).containsExactly(204, 409);
    assertThat(activeAdminsOf(company)).containsExactly(second);
  }
}
