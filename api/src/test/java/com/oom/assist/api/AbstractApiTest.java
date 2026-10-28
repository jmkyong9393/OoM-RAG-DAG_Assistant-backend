package com.oom.assist.api;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;

import com.oom.assist.TestcontainersConfiguration;
import com.oom.assist.common.context.TenantContextFilter;
import jakarta.servlet.http.Cookie;
import java.util.HashMap;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;
import org.springframework.transaction.annotation.Transactional;

/**
 * API 통합 테스트 공통 기반. 실제 pgvector + Flyway + 전체 Spring 컨텍스트(Security 포함) + MockMvc.
 * 매 테스트마다 테넌트·워크스페이스 2(SW: 전원 member, 배종현 admin / HR: 장문경만)·사용자 2명(비밀번호 "pw-" + 이름)을 심고 롤백한다.
 * 보호된 엔드포인트는 as() 로 인증 주체를 직접 주입한다(로그인 흐름 자체는 AuthApiTest 가 검증).
 */
@SpringBootTest
@AutoConfigureMockMvc
@Import(TestcontainersConfiguration.class)
@Transactional
public abstract class AbstractApiTest {

  public static final String PASSWORD = "pw-secret-1";

  @Autowired protected MockMvc mvc;
  @Autowired protected JdbcClient jdbc;
  @Autowired protected PasswordEncoder encoder;

  protected long tenant, company, wsSw, wsHr, userLead, userBackend;
  protected String emailLead = "lead@test.local", emailBackend = "backend@test.local";

  @BeforeEach
  void seedApiBase() {
    tenant = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('t', 'T') RETURNING id").query(Long.class).single();
    company = jdbc.sql("INSERT INTO workspaces (tenant_id, kind, name, policy_enabled) VALUES (?, 'company', '전사', true) RETURNING id").param(tenant).query(Long.class).single();
    wsSw = jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', 'SW 개발', false) RETURNING id").params(tenant, company).query(Long.class).single();
    wsHr = jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', '인사·총무', true) RETURNING id").params(tenant, company).query(Long.class).single();
    userLead = user("배종현", "팀장", emailLead);
    userBackend = user("장문경", "매니저", emailBackend);
    member(wsSw, userLead, "admin");
    member(wsSw, userBackend, "member");
    member(wsHr, userBackend, "admin");
  }

  protected long user(String name, String title, String email) {
    return jdbc.sql("INSERT INTO users (tenant_id, name, title, email, password_hash) VALUES (?, ?, ?, ?, ?) RETURNING id")
        .params(tenant, name, title, email, encoder.encode(PASSWORD)).query(Long.class).single();
  }

  protected void member(long ws, long user, String role) {
    jdbc.sql("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, ?)").params(ws, user, role).update();
  }

  private final Map<Long, Cookie> sessionCookies = new HashMap<>();

  /**
   * 실제 로그인(POST /auth/login)으로 세션 쿠키를 받아 테스트 안에서 재사용한다.
   * (authentication() 포스트프로세서는 요청마다 새 세션을 만들어 SSE 비동기 디스패치에서 spring_session 중복 삽입이 났다.)
   */
  protected Cookie loginCookie(long userId) {
    return sessionCookies.computeIfAbsent(userId, id -> {
      String email = jdbc.sql("SELECT email FROM users WHERE id = ?").param(id).query(String.class).single();
      try {
        MvcResult r = mvc.perform(post("/auth/login").contentType(MediaType.APPLICATION_JSON)
            .content("{\"email\": \"" + email + "\", \"password\": \"" + PASSWORD + "\"}")).andReturn();
        if (r.getResponse().getStatus() != 200) throw new IllegalStateException("login failed: " + r.getResponse().getStatus());
        String h = r.getResponse().getHeaders("Set-Cookie").stream().filter(x -> x.startsWith("OOMSESSION=")).findFirst().orElseThrow();
        return new Cookie("OOMSESSION", h.substring("OOMSESSION=".length(), h.indexOf(';')));
      } catch (Exception e) {
        throw new IllegalStateException(e);
      }
    });
  }

  /** 세션 쿠키 + X-Workspace-Id + JSON 을 붙인 요청 빌더. */
  protected MockHttpServletRequestBuilder as(MockHttpServletRequestBuilder b, long userId, long ws) {
    return asUser(b, userId).header(TenantContextFilter.WORKSPACE_HEADER, ws)
        .contentType(MediaType.APPLICATION_JSON).accept(MediaType.APPLICATION_JSON);
  }

  /** 세션 쿠키만 (워크스페이스 헤더 없음 — /me, /workspaces 용). */
  protected MockHttpServletRequestBuilder asUser(MockHttpServletRequestBuilder b, long userId) {
    return b.cookie(loginCookie(userId));
  }

  protected long node(long ws, String type, String title, long authorId) {
    return jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id, refine_status) VALUES (?, ?, ?, ?, ?, 'done') RETURNING id")
        .params(ws, type, title, title, authorId).query(Long.class).single();
  }

  protected void edge(long ws, long parent, long child) {
    jdbc.sql("INSERT INTO log_edges (workspace_id, parent_id, child_id) VALUES (?, ?, ?)").params(ws, parent, child).update();
  }
}
