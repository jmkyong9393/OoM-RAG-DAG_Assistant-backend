package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.asyncDispatch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.request;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.sun.net.httpserver.HttpServer;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * Spring 릴레이 통합 테스트 (E1 스파이크 1번의 자동화 부분).
 * Python 대신 JDK 내장 HttpServer 가 SSE 를 흘린다 — 의존성 없이 "바이트 통과 + session 선송출 + done 가로채기 + 오류 경로"를 검증.
 */
@Transactional(propagation = Propagation.NOT_SUPPORTED) // 릴레이는 다른 스레드·커넥션에서 쓰므로 테스트 트랜잭션을 끄고 직접 정리한다
class ChatRelayApiTest extends AbstractApiTest {

  @AfterEach
  void cleanup() {
    jdbc.sql("DELETE FROM answer_feedback WHERE session_id IN (SELECT id FROM search_sessions WHERE workspace_id IN (?, ?))").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM search_sessions WHERE workspace_id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM audit_log WHERE tenant_id = ?").param(tenant).update();
    jdbc.sql("DELETE FROM log_edges WHERE workspace_id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM log_nodes WHERE workspace_id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM workspace_members WHERE workspace_id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM workspaces WHERE id IN (?, ?)").params(wsSw, wsHr).update();
    jdbc.sql("DELETE FROM workspaces WHERE id = ?").param(company).update();
    jdbc.sql("DELETE FROM users WHERE tenant_id = ?").param(tenant).update();
    jdbc.sql("DELETE FROM tenants WHERE id = ?").param(tenant).update();
  }

  static HttpServer stub;
  static final AtomicReference<String> scenario = new AtomicReference<>("ok");
  static final AtomicReference<String> lastRequestBody = new AtomicReference<>("");

  static final String OK_STREAM = String.join("\n",
      "event: stage", "data: {\"node\":\"supervisor\",\"status\":\"start\"}", "",
      "event: intent", "data: {\"intent\":\"troubleshoot\",\"mode\":\"dag_only\"}", "",
      "event: chain", "data: {\"entry_id\":101,\"nodes\":[],\"edges\":[]}", "",
      "event: token", "data: {\"text\":\"E-403은 \"}", "",
      "event: token", "data: {\"text\":\"배치 중단 [N1]\"}", "",
      "event: citations", "data: {\"verified\":[\"N1\"],\"unverified\":[\"N2\"]}", "",
      "event: done", "data: {\"intent\":\"troubleshoot\",\"retrieved_ids\":[\"n:101\",\"n:106\"],\"verified_ids\":[\"n:101\"],\"unverified_ids\":[\"n:106\"],"
          + "\"latency\":{\"retrieval_ms\":12,\"first_token_ms\":34,\"total_ms\":56},\"model\":\"dummy\",\"prompt_tokens\":10,\"completion_tokens\":5}", "", "");

  @BeforeAll
  static void startStub() throws Exception {
    stub = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    stub.createContext("/ai/chat", ex -> {
      lastRequestBody.set(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
      // uvicorn 은 h2c 업그레이드 요청의 본문을 버린다(실측 422) — 릴레이가 HTTP/1.1 로 보내는지 스텁에서 강제
      if (ex.getRequestHeaders().containsKey("Upgrade")) { ex.sendResponseHeaders(422, -1); ex.close(); return; }
      switch (scenario.get()) {
        case "http500" -> { ex.sendResponseHeaders(500, -1); ex.close(); }
        case "truncated" -> {
          byte[] b = "event: token\ndata: {\"text\":\"partial\"}\n\n".getBytes(StandardCharsets.UTF_8);
          ex.getResponseHeaders().add("Content-Type", "text/event-stream");
          ex.sendResponseHeaders(200, b.length);
          try (OutputStream os = ex.getResponseBody()) { os.write(b); }
        }
        default -> {
          byte[] b = OK_STREAM.getBytes(StandardCharsets.UTF_8);
          ex.getResponseHeaders().add("Content-Type", "text/event-stream");
          ex.sendResponseHeaders(200, b.length);
          try (OutputStream os = ex.getResponseBody()) { os.write(b); }
        }
      }
    });
    stub.start();
  }

  @AfterAll
  static void stopStub() {
    stub.stop(0);
  }

  @DynamicPropertySource
  static void aiBaseUrl(DynamicPropertyRegistry r) {
    r.add("oom.ai.base-url", () -> "http://127.0.0.1:" + stub.getAddress().getPort());
  }

  private String perform(String query) throws Exception {
    MvcResult started = mvc.perform(as(post("/chat"), userBackend, wsSw).content("{\"query\":\"" + query + "\"}"))
        .andExpect(request().asyncStarted()).andReturn();
    started.getAsyncResult(10_000);
    return mvc.perform(asyncDispatch(started)).andExpect(status().isOk()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
  }

  @Test
  void relaysEventsVerbatimAndRecordsSession() throws Exception {
    scenario.set("ok");
    String body = perform("E-403 어떻게 해결했어?");
    // 순서: session(Spring) → 나머지(Python 그대로) → done
    List<String> events = body.lines().filter(l -> l.startsWith("event:")).map(l -> l.substring(6).trim()).toList();
    assertThat(events).containsExactly("session", "stage", "intent", "chain", "token", "token", "citations", "done");
    assertThat(body).contains("data:{\"session_id\":").contains("배치 중단 [N1]");
    // Python 에 넘긴 요청: 워크스페이스·모드·세션 id·질의
    assertThat(lastRequestBody.get()).contains("\"workspace_id\":" + wsSw).contains("\"policy_enabled\":false").contains("\"session_id\":").contains("E-403");
    // 세션 기록
    Map<String, Object> s = jdbc.sql("SELECT status, mode, intent, answer_text, verified_ids, unverified_ids, retrieved_ids, total_ms, model, user_id, chain_ref IS NOT NULL AS has_chain, policy_refs IS NULL AS no_policy FROM search_sessions WHERE workspace_id = ? ORDER BY id DESC LIMIT 1")
        .param(wsSw).query().singleRow();
    assertThat(s.get("status")).isEqualTo("done");
    assertThat(s.get("mode")).isEqualTo("dag_only");
    assertThat(s.get("intent")).isEqualTo("troubleshoot");
    assertThat(s.get("answer_text")).isEqualTo("E-403은 배치 중단 [N1]");
    assertThat(((Number) s.get("total_ms")).intValue()).isEqualTo(56);
    assertThat(s.get("model")).isEqualTo("dummy");
    assertThat(((Number) s.get("user_id")).longValue()).isEqualTo(userBackend);
    assertThat(s.get("has_chain")).isEqualTo(true);
    assertThat(s.get("no_policy")).isEqualTo(true);
    assertThat(pgArray(s.get("verified_ids"))).containsExactly("n:101");
    assertThat(pgArray(s.get("unverified_ids"))).containsExactly("n:106");
    assertThat(pgArray(s.get("retrieved_ids"))).containsExactly("n:101", "n:106");
  }

  @Test
  void aiHttpErrorEmitsErrorEventAndMarksSessionError() throws Exception {
    scenario.set("http500");
    String body = perform("q");
    assertThat(body.lines().filter(l -> l.startsWith("event:")).map(String::trim).toList()).containsExactly("event:session", "event:error");
    assertThat(jdbc.sql("SELECT status FROM search_sessions WHERE workspace_id = ? ORDER BY id DESC LIMIT 1").param(wsSw).query(String.class).single()).isEqualTo("error");
  }

  @Test
  void truncatedStreamWithoutDoneIsReportedAsError() throws Exception {
    scenario.set("truncated");
    String body = perform("q");
    assertThat(body).contains("event:token").contains("event:error").contains("stream ended without done");
    Map<String, Object> s = jdbc.sql("SELECT status, answer_text FROM search_sessions WHERE workspace_id = ? ORDER BY id DESC LIMIT 1").param(wsSw).query().singleRow();
    assertThat(s.get("status")).isEqualTo("error");
    assertThat(s.get("answer_text")).isEqualTo("partial");
  }

  @Test
  void blankQueryIs400AndNoSessionRow() throws Exception {
    long before = jdbc.sql("SELECT count(*) FROM search_sessions").query(Long.class).single();
    mvc.perform(as(post("/chat"), userBackend, wsSw).content("{\"query\":\"  \"}")).andExpect(status().isBadRequest());
    assertThat(jdbc.sql("SELECT count(*) FROM search_sessions").query(Long.class).single()).isEqualTo(before);
  }

  private static List<String> pgArray(Object o) throws Exception {
    if (o instanceof java.sql.Array a) return java.util.Arrays.stream((Object[]) a.getArray()).map(String::valueOf).toList();
    if (o instanceof Object[] arr) return java.util.Arrays.stream(arr).map(String::valueOf).toList();
    return List.of();
  }
}
