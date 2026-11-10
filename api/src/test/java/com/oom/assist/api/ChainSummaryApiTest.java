package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.sun.net.httpserver.HttpServer;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.ResultActions;

/**
 * POST /chains/{id}/summary — 장애 건 AI 요약. Python 대신 JDK HttpServer 가 /ai/chain-summary 에 답한다.
 * 지문이 같으면 다시 부르지 않음 · 본문으로 요약(AI 정리를 기다리지 않음) · 실패 기록과 대기 · 3회 실패면 멈춤 · 이전 요약 · 폐기 · 생성 중 표시.
 * GET /chains/{id} 는 저장된 요약과 summary_status 만 보여 주고 AI 는 부르지 않는다.
 * 테스트 트랜잭션 안에서는 now() 가 고정이라 '시간이 지남'은 시각 컬럼을 직접 앞당겨 만든다.
 */
class ChainSummaryApiTest extends AbstractApiTest {

  static HttpServer stub;
  static final AtomicReference<String> scenario = new AtomicReference<>("ok");
  static final AtomicInteger calls = new AtomicInteger();
  static final AtomicReference<String> lastBody = new AtomicReference<>("");

  @BeforeAll
  static void startStub() throws Exception {
    stub = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    stub.createContext("/ai/chain-summary", ex -> {
      lastBody.set(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
      int n = calls.incrementAndGet();
      String body = switch (scenario.get()) {
        case "fail" -> "{\"summary\":\"A → B\",\"code\":\"AI_TIMEOUT\"}";
        case "disabled" -> "{\"summary\":\"A → B\",\"code\":\"AI_DISABLED\"}";
        default -> "{\"summary\":\"요약 " + n + "\",\"code\":null}";
      };
      byte[] b = body.getBytes(StandardCharsets.UTF_8);
      ex.getResponseHeaders().add("Content-Type", "application/json");
      ex.sendResponseHeaders(200, b.length);
      try (OutputStream os = ex.getResponseBody()) { os.write(b); }
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

  long root, cause;

  @BeforeEach
  void chain() {
    scenario.set("ok");
    calls.set(0);
    root = node(wsSw, "fail", "VPN 접속 시 E-403 반복", userBackend);
    cause = node(wsSw, "fail", "원인: 토큰 갱신 배치 중단", userBackend);
    edge(wsSw, root, cause);
    jdbc.sql("UPDATE log_nodes SET category = 'cause' WHERE id = ?").param(cause).update();
  }

  private ResultActions summary(long entry) throws Exception {
    return mvc.perform(as(post("/chains/" + entry + "/summary"), userBackend, wsSw)).andExpect(status().isOk());
  }

  private long addChild(long parent, String title) {
    long id = node(wsSw, "success", title, userBackend);
    edge(wsSw, parent, id);
    return id;
  }

  private void failedLongAgo() {
    jdbc.sql("UPDATE chain_summaries SET failed_at = now() - interval '6 minutes' WHERE root_id = ?").param(root).update();
  }

  @Test
  void firstOpenSummarizesWholeCaseThenReusesItWhileUnchanged() throws Exception {
    summary(cause)   // 중간 노드를 열어도 장애 건(루트) 전체를 요약한다
        .andExpect(jsonPath("$.status").value("fresh")).andExpect(jsonPath("$.summary").value("요약 1"))
        .andExpect(jsonPath("$.based_on_records").value(2)).andExpect(jsonPath("$.current_records").value(2))
        .andExpect(jsonPath("$.code").doesNotExist()).andExpect(jsonPath("$.generated_at").exists());
    assertThat(lastBody.get()).contains("\"body\"", "\"is_cause\":true", "\"resolves_root\"", "\"created_at\"", "VPN 접속 시 E-403 반복");
    summary(root).andExpect(jsonPath("$.status").value("fresh")).andExpect(jsonPath("$.summary").value("요약 1"));
    assertThat(calls.get()).isEqualTo(1);
    assertThat(jdbc.sql("SELECT root_id FROM chain_summaries").query(Long.class).list()).containsExactly(root);
  }

  @Test
  void anyChangeInTheCaseRegeneratesIncludingResolvingIt() throws Exception {
    summary(root);
    long fix = addChild(cause, "해결: 배치 재기동");
    summary(root).andExpect(jsonPath("$.summary").value("요약 2")).andExpect(jsonPath("$.current_records").value(3));
    jdbc.sql("UPDATE log_nodes SET category = NULL WHERE id = ?").param(cause).update();   // 기록 수는 같고 원인 표시만 해제
    summary(root).andExpect(jsonPath("$.summary").value("요약 3"));
    jdbc.sql("UPDATE log_nodes SET resolved_at = now() WHERE id = ?").param(root).update();   // 완료 처리만 — 요약은 상태를 쓰지 않으므로 다시 만들지 않는다
    summary(root).andExpect(jsonPath("$.summary").value("요약 3"));
    jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)").params(wsSw, root, fix).update();   // 해결한 기록 지정은 내용
    summary(root).andExpect(jsonPath("$.summary").value("요약 4"));
    assertThat(lastBody.get()).contains("\"resolves_root\":true").doesNotContain("resolved_at");
    assertThat(calls.get()).isEqualTo(4);
  }

  @Test
  void summarizesFromBodiesRightAwayAndRefineResultsDoNotRegenerate() throws Exception {
    long fresh = jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id) VALUES (?, 'success', '방금 등록', '포트 충돌 시 env 에서만 바꾼다', ?) RETURNING id")
        .params(wsSw, userBackend).query(Long.class).single();   // refine_status 기본값 pending — AI 정리를 기다리지 않는다
    edge(wsSw, cause, fresh);
    summary(root).andExpect(jsonPath("$.status").value("fresh")).andExpect(jsonPath("$.summary").value("요약 1"));
    assertThat(lastBody.get()).contains("\"body\":\"포트 충돌 시 env 에서만 바꾼다\"").doesNotContain("resolution_summary");

    jdbc.sql("UPDATE log_nodes SET refine_status = 'done', resolution_summary = 'AI 가 정리한 문장' WHERE id = ?").param(fresh).update();
    summary(root).andExpect(jsonPath("$.summary").value("요약 1"));   // 정리가 끝나도 다시 부르지 않는다
    assertThat(calls.get()).isEqualTo(1);

    jdbc.sql("UPDATE log_nodes SET body = ? WHERE id = ?").params("가".repeat(600), fresh).update();   // 본문은 지문에 들어간다
    summary(root).andExpect(jsonPath("$.summary").value("요약 2"));
    assertThat(lastBody.get()).contains("가".repeat(500) + "\"").doesNotContain("가".repeat(501));   // 앞 500자만
  }

  @Test
  void failureIsRecordedWaitsThenStopsAfterThreeAndResetsWhenTheCaseChanges() throws Exception {
    scenario.set("fail");
    summary(root).andExpect(jsonPath("$.status").value("failed")).andExpect(jsonPath("$.summary").doesNotExist())
        .andExpect(jsonPath("$.code").value("AI_TIMEOUT")).andExpect(jsonPath("$.message").value("AI 서버가 응답하지 않습니다"))
        .andExpect(jsonPath("$.retryable").value(true));
    summary(root).andExpect(jsonPath("$.status").value("failed"));   // 실패 후 대기 — 부르지 않는다
    assertThat(calls.get()).isEqualTo(1);

    failedLongAgo();
    summary(root);
    failedLongAgo();
    summary(root).andExpect(jsonPath("$.retryable").value(false));   // 같은 지문으로 3회
    failedLongAgo();
    summary(root).andExpect(jsonPath("$.status").value("failed")).andExpect(jsonPath("$.retryable").value(false));
    assertThat(calls.get()).isEqualTo(3);

    scenario.set("ok");
    addChild(cause, "해결: 배치 재기동");   // 지문이 바뀌면 실패 횟수를 0 으로 보고 다시 시도
    summary(root).andExpect(jsonPath("$.status").value("fresh")).andExpect(jsonPath("$.summary").value("요약 4"));
    assertThat(jdbc.sql("SELECT fail_count FROM chain_summaries WHERE root_id = ?").param(root).query(Integer.class).single()).isZero();
  }

  @Test
  void failureAfterAChangeShowsThePreviousSummaryAsStale() throws Exception {
    summary(root);
    addChild(cause, "해결: 배치 재기동");
    scenario.set("fail");
    summary(root).andExpect(jsonPath("$.status").value("stale")).andExpect(jsonPath("$.summary").value("요약 1"))
        .andExpect(jsonPath("$.code").value("AI_TIMEOUT")).andExpect(jsonPath("$.based_on_records").value(2))
        .andExpect(jsonPath("$.current_records").value(3));
  }

  @Test
  void deprecatedCaseNeverCallsAi() throws Exception {
    mvc.perform(as(patch("/logs/" + root), userLead, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"중복 기록\"}")).andExpect(status().isOk());
    summary(root).andExpect(jsonPath("$.status").value("deprecated")).andExpect(jsonPath("$.summary").doesNotExist());
    assertThat(calls.get()).isZero();
  }

  @Test
  void anotherRequestGeneratingBlocksUntilItsMarkGoesStale() throws Exception {
    jdbc.sql("INSERT INTO chain_summaries (root_id, sig, generating_since) VALUES (?, 'x', now())").param(root).update();
    summary(root).andExpect(jsonPath("$.status").value("generating"));
    assertThat(calls.get()).isZero();
    jdbc.sql("UPDATE chain_summaries SET generating_since = now() - interval '1 minute' WHERE root_id = ?").param(root).update();   // 요약 도중 죽은 표시
    summary(root).andExpect(jsonPath("$.status").value("fresh"));
    assertThat(jdbc.sql("SELECT generating_since IS NULL FROM chain_summaries WHERE root_id = ?").param(root).query(Boolean.class).single()).isTrue();
  }

  @Test
  void disabledAiIsNotCountedAsAFailure() throws Exception {
    scenario.set("disabled");
    summary(root).andExpect(jsonPath("$.status").value("failed")).andExpect(jsonPath("$.code").value("AI_DISABLED"));
    summary(root);   // 대기 없이 다시 부른다
    assertThat(calls.get()).isEqualTo(2);
    assertThat(jdbc.sql("SELECT fail_count FROM chain_summaries WHERE root_id = ?").param(root).query(Integer.class).single()).isZero();
  }

  @Test
  void bumpingSummaryVersionRegeneratesEvenWithoutAnyRecordChange() throws Exception {
    summary(root);
    jdbc.sql("UPDATE chain_summaries SET sig = 'old:' || split_part(sig, ':', 2), summary_sig = 'old:' || split_part(summary_sig, ':', 2) WHERE root_id = ?")
        .param(root).update();   // 이전 규칙(다른 버전)으로 만든 요약
    chainView(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));
    summary(root).andExpect(jsonPath("$.status").value("fresh")).andExpect(jsonPath("$.summary").value("요약 2"));
    assertThat(calls.get()).isEqualTo(2);
  }

  @Test
  void recordCountExcludesDeprecatedLikeTheTimelineCard() throws Exception {
    long dropped = addChild(root, "클라이언트 재설치");
    mvc.perform(as(patch("/logs/" + dropped), userLead, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"효과 없음\"}")).andExpect(status().isOk());
    summary(root).andExpect(jsonPath("$.based_on_records").value(2)).andExpect(jsonPath("$.current_records").value(2));   // 기록 3건 중 폐기 1건 제외
    assertThat(lastBody.get()).contains("클라이언트 재설치");   // 요약 입력에는 폐기 기록도 들어간다('폐기' 태그)
  }

  private ResultActions chainView(long entry) throws Exception {
    return mvc.perform(as(get("/chains/" + entry), userBackend, wsSw)).andExpect(status().isOk());
  }

  @Test
  void getChainShowsOnlyTheStoredAiSummaryWithItsStateAndNeverCallsAi() throws Exception {
    chainView(cause).andExpect(jsonPath("$.summary").doesNotExist()).andExpect(jsonPath("$.summary_status").value("none"))
        .andExpect(jsonPath("$.overview").value("VPN 접속 시 E-403 반복 장애 건: 기록 2건, 해결 0건, 진행 중"));   // 틀 문장은 overview 로만
    summary(root);
    chainView(cause).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("fresh"))
        .andExpect(jsonPath("$.summary_at").exists());

    long fresh = jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id) VALUES (?, 'success', '방금 등록', 'b', ?) RETURNING id")
        .params(wsSw, userBackend).query(Long.class).single();
    edge(wsSw, cause, fresh);   // AI 정리 전이어도 기다리지 않고 바로 '이전 요약'
    chainView(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));
    jdbc.sql("UPDATE chain_summaries SET generating_since = now() WHERE root_id = ?").param(root).update();
    chainView(root).andExpect(jsonPath("$.summary_status").value("generating"));
    mvc.perform(as(patch("/logs/" + root), userLead, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"중복 기록\"}")).andExpect(status().isOk());
    chainView(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("deprecated"));
    assertThat(calls.get()).isEqualTo(1);   // GET 은 한 번도 부르지 않았다
  }

  @Test
  void otherWorkspacesCaseIsNotFound() throws Exception {
    long other = node(wsHr, "fail", "다른 부서 장애", userBackend);
    mvc.perform(as(post("/chains/" + other + "/summary"), userBackend, wsSw))
        .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("NOT_FOUND"));
  }
}
