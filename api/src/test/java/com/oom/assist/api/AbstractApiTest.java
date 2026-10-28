package com.oom.assist.api;

import com.oom.assist.TestcontainersConfiguration;
import com.oom.assist.common.context.TenantContextFilter;
import com.oom.assist.session.SessionCookie;
import jakarta.servlet.http.Cookie;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.transaction.annotation.Transactional;

/**
 * API 통합 테스트 공통 기반. 실제 pgvector + Flyway + 전체 Spring 컨텍스트 + MockMvc.
 * 매 테스트마다 테넌트·워크스페이스 2(SW: 전원 member, 배종현 admin / HR: 장문경만)·사용자 2명을 심고 롤백한다.
 * 인증은 서명 쿠키를 직접 발급한다(비밀번호 비의존).
 */
@SpringBootTest
@AutoConfigureMockMvc
@Import(TestcontainersConfiguration.class)
@Transactional
public abstract class AbstractApiTest {

  @Autowired protected MockMvc mvc;
  @Autowired protected JdbcClient jdbc;
  @Autowired protected SessionCookie sessionCookie;

  protected long tenant, company, wsSw, wsHr, userLead, userBackend;

  @BeforeEach
  void seedApiBase() {
    tenant = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('t', 'T') RETURNING id").query(Long.class).single();
    company = jdbc.sql("INSERT INTO workspaces (tenant_id, kind, name, policy_enabled) VALUES (?, 'company', '전사', true) RETURNING id").param(tenant).query(Long.class).single();
    wsSw = jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', 'SW 개발', false) RETURNING id").params(tenant, company).query(Long.class).single();
    wsHr = jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', '인사·총무', true) RETURNING id").params(tenant, company).query(Long.class).single();
    userLead = jdbc.sql("INSERT INTO users (tenant_id, name, title) VALUES (?, '배종현', '팀장') RETURNING id").param(tenant).query(Long.class).single();
    userBackend = jdbc.sql("INSERT INTO users (tenant_id, name, title) VALUES (?, '장문경', '매니저') RETURNING id").param(tenant).query(Long.class).single();
    member(wsSw, userLead, "admin");
    member(wsSw, userBackend, "member");
    member(wsHr, userBackend, "admin");
  }

  protected void member(long ws, long user, String role) {
    jdbc.sql("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)").params(ws, user, role).update();
  }

  protected Cookie loginAs(long userId) {
    return new Cookie(SessionCookie.NAME, sessionCookie.issue(userId));
  }

  /** 쿠키 + X-Workspace-Id + JSON 을 붙인 요청 빌더. */
  protected MockHttpServletRequestBuilder as(MockHttpServletRequestBuilder b, long userId, long ws) {
    return b.cookie(loginAs(userId)).header(TenantContextFilter.WORKSPACE_HEADER, ws)
        .contentType(MediaType.APPLICATION_JSON).accept(MediaType.APPLICATION_JSON);
  }

  protected long node(long ws, String type, String title, long authorId) {
    return jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id, refine_status) VALUES (?, ?, ?, ?, ?, 'done') RETURNING id")
        .params(ws, type, title, title, authorId).query(Long.class).single();
  }

  protected void edge(long ws, long parent, long child) {
    jdbc.sql("INSERT INTO log_edges (workspace_id, parent_id, child_id) VALUES (?, ?, ?)").params(ws, parent, child).update();
  }
}
