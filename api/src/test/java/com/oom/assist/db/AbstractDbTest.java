package com.oom.assist.db;

import com.oom.assist.TestcontainersConfiguration;
import java.util.Arrays;
import java.util.stream.Collectors;
import org.junit.jupiter.api.BeforeEach;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.annotation.Import;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * DB 계층 테스트 공통 기반. 실제 pgvector 컨테이너에 Flyway 전체를 적용한 뒤,
 * 테스트마다 테넌트 1 · 회사 워크스페이스 1 · 부서 워크스페이스 1 · 사용자 1을 심고 트랜잭션으로 되돌린다.
 */
@SpringBootTest
@Import(TestcontainersConfiguration.class)
@Transactional
abstract class AbstractDbTest {

  @Autowired protected JdbcClient jdbc;
  @Autowired protected PlatformTransactionManager txManager;

  protected long tenant;
  protected long company;   // kind=company, policy_enabled=true
  protected long ws;        // kind=dept, parent=company, policy_enabled=false
  protected long user;

  @BeforeEach
  void seedBase() {
    tenant = jdbc.sql("INSERT INTO tenants (slug, name) VALUES ('t', 'Tenant') RETURNING id").query(Long.class).single();
    company = jdbc.sql("INSERT INTO workspaces (tenant_id, kind, name, policy_enabled) VALUES (?, 'company', 'Company', true) RETURNING id")
        .param(tenant).query(Long.class).single();
    ws = jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', 'SW', false) RETURNING id")
        .params(tenant, company).query(Long.class).single();
    user = jdbc.sql("INSERT INTO users (tenant_id, name) VALUES (?, 'tester') RETURNING id").param(tenant).query(Long.class).single();
    jdbc.sql("INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (?, ?, 'member')").params(ws, user).update();
  }

  /**
   * 실패가 기대되는 문장을 세이브포인트(NESTED) 안에서 실행한다. Postgres는 오류 뒤 트랜잭션을 중단 상태로 두므로,
   * 이렇게 감싸지 않으면 같은 테스트의 다음 문장이 전부 25P02 로 실패한다.
   */
  protected void assertFails(Runnable statement, String messageContains) {
    TransactionTemplate nested = new TransactionTemplate(txManager);
    nested.setPropagationBehavior(TransactionDefinition.PROPAGATION_NESTED);
    Throwable caught = null;
    try {
      nested.execute(status -> { statement.run(); return null; });
    } catch (RuntimeException e) {
      caught = e;
    }
    org.assertj.core.api.Assertions.assertThat(caught).as("expected failure containing '%s'", messageContains).isNotNull();
    org.assertj.core.api.Assertions.assertThat(caught).hasMessageContaining(messageContains);
  }

  protected long workspace(long tenantId, Long parent, String name, boolean policy) {
    return jdbc.sql("INSERT INTO workspaces (tenant_id, parent_id, kind, name, policy_enabled) VALUES (?, ?, 'dept', ?, ?) RETURNING id")
        .params(tenantId, parent, name, policy).query(Long.class).single();
  }

  protected long node(long wsId, String type, String title, String code) {
    return jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id, error_code, refine_status) VALUES (?, ?, ?, ?, ?, ?, 'done') RETURNING id")
        .params(wsId, type, title, title, user, code).query(Long.class).single();
  }

  protected long node(String type, String title, String code) {
    return node(ws, type, title, code);
  }

  protected void edge(long parent, long child) {
    long wsOfParent = jdbc.sql("SELECT workspace_id FROM log_nodes WHERE id = ?").param(parent).query(Long.class).single();
    jdbc.sql("INSERT INTO log_edges (workspace_id, parent_id, child_id, created_by) VALUES (?, ?, ?, ?)")
        .params(wsOfParent, parent, child, user).update();
  }

  protected long document(long wsId, String slug, String title, String version, String status) {
    return jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, ?, ?, ?, 'markdown', ?) RETURNING id")
        .params(wsId, slug, title, version, status).query(Long.class).single();
  }

  /** tokens 는 kiwi 결과를 흉내 낸 공백 구분 형태소. embedding 은 null 허용. */
  protected void chunk(long docId, String id, String label, String text, String tokens, String embedding) {
    jdbc.sql("INSERT INTO policy_chunks (id, doc_id, article_label, text, tokens, embedding) VALUES (?, ?, ?, ?, ?, CAST(? AS vector))")
        .params(id, docId, label, text, tokens, embedding).update();
  }

  /** 지정한 축만 1인 1536차원 단위 벡터 문자열. 코사인 거리: 같은 축 0, 다른 축 1. */
  protected static String unitVector(int axis) {
    return Arrays.stream(new int[1536]).mapToObj(i -> "0").collect(Collectors.joining(",", "[", "]"))
        .replaceFirst("^\\[" + "0,".repeat(axis) + "0", "[" + "0,".repeat(axis) + "1");
  }
}
