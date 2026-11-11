package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.patch;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.oom.assist.chain.ChainSummaryService;
import com.oom.assist.common.config.AppProps;
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
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.convention.TestBean;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.ObjectMapper;

/**
 * 장애 건 AI 요약 — GET /chains/{id} 가 저장된 요약을 보여 주고, 없거나 오래됐으면 백그라운드로 만든다. Python 대신 JDK HttpServer 가 /ai/chain-summary 에 답한다.
 * 백그라운드 실행기를 같은 스레드로 바꿔(테스트 트랜잭션의 데이터를 보게) GET 한 번이 끝나면 생성도 끝나 있다 — 결과는 다음 GET 과 chain_summaries 행으로 확인한다.
 * 지문이 같으면 다시 부르지 않음 · 본문으로 요약(AI 정리를 기다리지 않음) · 실패 기록과 대기 · 형식 오류 3회면 멈춤(서버 꺼짐 등은 안 셈) · 이전 요약 · 폐기 · 생성 중 표시
 * · 입력 40건 상한 · 루트가 아니게 되면 요약 행 삭제 · 모델/토큰 저장.
 * 테스트 트랜잭션 안에서는 now() 가 고정이라 '시간이 지남'은 시각 컬럼을 직접 앞당겨 만든다.
 */
class ChainSummaryApiTest extends AbstractApiTest {

  static final String TEMPLATE = "VPN 접속 시 E-403 반복 장애 건: 기록 2건, 해결 0건, 진행 중";   // 요약이 없을 때 보이는 틀 문장

  static HttpServer stub;
  static final AtomicReference<String> scenario = new AtomicReference<>("ok");
  static final AtomicInteger calls = new AtomicInteger();
  static final AtomicReference<String> lastBody = new AtomicReference<>("");

  @Autowired AppProps props;
  @Autowired ObjectMapper json;

  @TestBean(name = "chainSummaryBackground", methodName = "inline")
  ChainSummaryService.Background background;

  static ChainSummaryService.Background inline() {
    return Runnable::run;
  }

  @BeforeAll
  static void startStub() throws Exception {
    stub = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
    stub.createContext("/ai/chain-summary", ex -> {
      lastBody.set(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
      int n = calls.incrementAndGet();
      String body = switch (scenario.get()) {
        case "fail" -> "{\"summary\":\"A → B\",\"code\":\"AI_BAD_OUTPUT\"}";   // LLM 이 답했지만 형식 오류 — 비용이 들어 센다
        case "down" -> "{\"summary\":\"A → B\",\"code\":\"AI_TIMEOUT\"}";       // 서버 꺼짐 · 시간 초과 — 세지 않는다
        case "disabled" -> "{\"summary\":\"A → B\",\"code\":\"AI_DISABLED\"}";
        default -> "{\"summary\":\"요약 " + n + "\",\"code\":null,\"model\":\"gpt-test\",\"prompt_tokens\":120,\"completion_tokens\":30}";
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
    r.add("oom.chain-summary.auto-on-read", () -> "true");   // 다른 API 테스트는 꺼 둔다(AbstractApiTest)
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

  /** GET — 요약이 없거나 오래됐으면 이 조회가 생성을 시작하고(테스트는 같은 스레드라 여기서 끝난다) 응답은 그 전 상태를 보여 준다. */
  private ResultActions open(long entry) throws Exception {
    return mvc.perform(as(get("/chains/" + entry), userBackend, wsSw)).andExpect(status().isOk());
  }

  /** 생성을 시작하는 조회 + 그 결과를 보는 조회. */
  private ResultActions openTwice(long entry) throws Exception {
    open(entry);
    return open(entry);
  }

  private Object row(String column) {
    return jdbc.sql("SELECT " + column + " FROM chain_summaries WHERE root_id = ?").param(root).query().singleValue();
  }

  private long addChild(long parent, String title) {
    long id = node(wsSw, "success", title, userBackend);
    edge(wsSw, parent, id);
    return id;
  }

  private void failedLongAgo() {
    jdbc.sql("UPDATE chain_summaries SET failed_at = now() - interval '6 minutes' WHERE root_id = ?").param(root).update();
  }

  private void deprecate(long id) throws Exception {
    mvc.perform(as(patch("/logs/" + id), userLead, wsSw).content("{\"status\":\"deprecated\",\"reason\":\"중복 기록\"}")).andExpect(status().isOk());
  }

  @Test
  void firstOpenShowsTheTemplateThenTheWholeCaseSummaryAndReusesIt() throws Exception {
    open(cause).andExpect(jsonPath("$.summary").value(TEMPLATE)).andExpect(jsonPath("$.summary_status").value("none"));   // 처음엔 틀 문장
    assertThat(lastBody.get()).contains("\"body\"", "\"is_cause\":true", "\"resolves_root\"", "\"created_at\"", "VPN 접속 시 E-403 반복");   // 중간 노드를 열어도 장애 건 전체
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("fresh"))
        .andExpect(jsonPath("$.summary_at").exists());
    open(cause);
    assertThat(calls.get()).isEqualTo(1);   // 내용이 같으면 다시 부르지 않는다
    assertThat(jdbc.sql("SELECT root_id FROM chain_summaries").query(Long.class).list()).containsExactly(root);
    assertThat(row("summary_records")).isEqualTo(2);
    assertThat(row("model || ':' || prompt_tokens || ':' || completion_tokens")).isEqualTo("gpt-test:120:30");   // 비용 확인용
  }

  @Test
  void anyChangeShowsThePreviousSummaryThenRegeneratesButCompletingAloneDoesNot() throws Exception {
    open(root);
    long fix = addChild(cause, "해결: 배치 재기동");
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));   // 이전 요약을 보여 주고
    open(root).andExpect(jsonPath("$.summary").value("요약 2")).andExpect(jsonPath("$.summary_status").value("fresh"));   // 다음 조회에 새 요약
    assertThat(row("summary_records")).isEqualTo(3);
    jdbc.sql("UPDATE log_nodes SET category = NULL WHERE id = ?").param(cause).update();   // 기록 수는 같고 원인 표시만 해제
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 3"));
    jdbc.sql("UPDATE log_nodes SET resolved_at = now() WHERE id = ?").param(root).update();   // 완료 처리만 — 요약은 상태를 쓰지 않으므로 다시 만들지 않는다
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 3")).andExpect(jsonPath("$.summary_status").value("fresh"));
    jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)").params(wsSw, root, fix).update();   // 해결한 기록 지정은 내용
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 4"));
    assertThat(lastBody.get()).contains("\"resolves_root\":true").doesNotContain("resolved_at");
    assertThat(calls.get()).isEqualTo(4);
  }

  @Test
  void summarizesFromBodiesRightAwayAndRefineResultsDoNotRegenerate() throws Exception {
    long fresh = jdbc.sql("INSERT INTO log_nodes (workspace_id, type, title, body, author_id) VALUES (?, 'success', '방금 등록', '포트 충돌 시 env 에서만 바꾼다', ?) RETURNING id")
        .params(wsSw, userBackend).query(Long.class).single();   // refine_status 기본값 pending — AI 정리를 기다리지 않는다
    edge(wsSw, cause, fresh);
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 1"));
    assertThat(lastBody.get()).contains("\"body\":\"포트 충돌 시 env 에서만 바꾼다\"").doesNotContain("resolution_summary");

    jdbc.sql("UPDATE log_nodes SET refine_status = 'done', resolution_summary = 'AI 가 정리한 문장' WHERE id = ?").param(fresh).update();
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 1"));   // 정리가 끝나도 다시 부르지 않는다
    assertThat(calls.get()).isEqualTo(1);

    jdbc.sql("UPDATE log_nodes SET body = ? WHERE id = ?").params("가".repeat(600), fresh).update();   // 본문은 지문에 들어간다
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 2"));
    assertThat(lastBody.get()).contains("가".repeat(500) + "\"").doesNotContain("가".repeat(501));   // 앞 500자만
  }

  @Test
  void badOutputIsRecordedWaitsThenStopsAfterThreeAndResetsWhenTheCaseChanges() throws Exception {
    scenario.set("fail");
    open(root);   // 생성 시도 → 형식 오류
    assertThat(row("error_kind")).isEqualTo("AI_BAD_OUTPUT");
    assertThat(row("fail_count")).isEqualTo(1);
    open(root).andExpect(jsonPath("$.summary").value(TEMPLATE)).andExpect(jsonPath("$.summary_status").value("none"));   // 실패 후 대기 — 부르지 않는다
    assertThat(calls.get()).isEqualTo(1);

    failedLongAgo();
    open(root);   // 2회
    failedLongAgo();
    open(root);   // 3회
    assertThat(row("fail_count")).isEqualTo(3);
    failedLongAgo();
    open(root);   // 같은 내용으로 3회 — 기록이 바뀔 때까지 멈춤
    assertThat(calls.get()).isEqualTo(3);

    scenario.set("ok");
    addChild(cause, "해결: 배치 재기동");   // 지문이 바뀌면 실패 횟수를 0 으로 보고 다시 시도
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 4")).andExpect(jsonPath("$.summary_status").value("fresh"));
    assertThat(row("fail_count")).isEqualTo(0);
  }

  @Test
  void serverDownOrRateLimitWaitsButNeverStopsTheSummary() throws Exception {
    scenario.set("down");
    for (int i = 0; i < 4; i++) {
      open(root);
      assertThat(row("error_kind")).isEqualTo("AI_TIMEOUT");
      failedLongAgo();
    }
    assertThat(calls.get()).isEqualTo(4);   // 비용이 안 드는 실패는 세지 않아 3회에서 멈추지 않는다
    assertThat(row("fail_count")).isEqualTo(0);
  }

  @Test
  void failureAfterAChangeKeepsShowingThePreviousSummary() throws Exception {
    open(root);
    addChild(cause, "해결: 배치 재기동");
    scenario.set("fail");
    open(root);
    assertThat(row("error_kind")).isEqualTo("AI_BAD_OUTPUT");
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));
    assertThat(row("summary_records")).isEqualTo(2);
  }

  @Test
  void whileGeneratingOrDeprecatedItKeepsTheLastSummaryWithoutCallingAi() throws Exception {
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 1"));
    addChild(cause, "해결: 배치 재기동");
    jdbc.sql("UPDATE chain_summaries SET generating_since = now() WHERE root_id = ?").param(root).update();   // 다른 요청이 만드는 중
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("generating"));
    deprecate(root);
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("deprecated"));
    assertThat(calls.get()).isEqualTo(1);
  }

  @Test
  void deprecatedCaseWithoutSummaryShowsTheTemplateAndNeverCallsAi() throws Exception {
    deprecate(root);
    openTwice(root).andExpect(jsonPath("$.summary_status").value("deprecated")).andExpect(jsonPath("$.summary_at").doesNotExist());
    assertThat(calls.get()).isZero();
  }

  @Test
  void aStaleGeneratingMarkIsTakenOver() throws Exception {
    jdbc.sql("INSERT INTO chain_summaries (root_id, sig, generating_since) VALUES (?, 'x', now())").param(root).update();
    open(root).andExpect(jsonPath("$.summary_status").value("generating"));
    assertThat(calls.get()).isZero();
    jdbc.sql("UPDATE chain_summaries SET generating_since = now() - interval '1 minute' WHERE root_id = ?").param(root).update();   // 요약 도중 죽은 표시
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("fresh"));
    assertThat(row("generating_since IS NULL")).isEqualTo(true);
  }

  @Test
  void disabledAiIsNotCountedAsAFailure() throws Exception {
    scenario.set("disabled");
    open(root);
    open(root).andExpect(jsonPath("$.summary").value(TEMPLATE)).andExpect(jsonPath("$.summary_status").value("none"));   // 대기 없이 다시 부른다
    assertThat(calls.get()).isEqualTo(2);
    assertThat(row("fail_count")).isEqualTo(0);
  }

  @Test
  void bumpingSummaryVersionRegeneratesEvenWithoutAnyRecordChange() throws Exception {
    open(root);
    jdbc.sql("UPDATE chain_summaries SET sig = 'old:' || split_part(sig, ':', 2), summary_sig = 'old:' || split_part(summary_sig, ':', 2) WHERE root_id = ?")
        .param(root).update();   // 이전 규칙(다른 버전)으로 만든 요약
    open(root).andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));
    open(root).andExpect(jsonPath("$.summary").value("요약 2")).andExpect(jsonPath("$.summary_status").value("fresh"));
    assertThat(calls.get()).isEqualTo(2);
  }

  @Test
  void recordCountExcludesDeprecatedLikeTheTimelineCard() throws Exception {
    long dropped = addChild(root, "클라이언트 재설치");
    deprecate(dropped);
    open(root);
    assertThat(row("summary_records")).isEqualTo(2);   // 기록 3건 중 폐기 1건 제외
    assertThat(lastBody.get()).contains("클라이언트 재설치");   // 요약 입력에는 폐기 기록도 들어간다('폐기' 태그)
  }

  @Test
  void largeCasesSendAtMost40RecordsWithTheTotal() throws Exception {
    for (int i = 0; i < 45; i++) addChild(cause, "시도 " + i);
    open(root);
    assertThat(lastBody.get().split("\"title\"", -1).length - 1).isBetween(1, 40);
    assertThat(lastBody.get()).contains("\"total\":47");   // 루트 + 원인 + 45건
  }

  @Test
  void linkingACaseUnderAnotherRecordDropsItsStoredSummary() throws Exception {
    open(root);
    long upper = node(wsSw, "fail", "상위 장애", userBackend);
    mvc.perform(as(post("/edges"), userBackend, wsSw).content("{\"parent_id\":" + upper + ",\"child_id\":" + root + "}"))
        .andExpect(status().isCreated());
    assertThat(jdbc.sql("SELECT count(*) FROM chain_summaries WHERE root_id = ?").param(root).query(Integer.class).single())
        .isZero();   // 더 이상 장애 건(루트)이 아니어서 아무도 읽지 않는 행
  }

  @Test
  void summarizeFalseReadsTheStoredSummaryButNeverStartsOne() throws Exception {
    ResultActions preview = mvc.perform(as(get("/chains/" + root + "?summarize=false"), userBackend, wsSw)).andExpect(status().isOk());
    preview.andExpect(jsonPath("$.summary").value(TEMPLATE)).andExpect(jsonPath("$.summary_status").value("none"));
    assertThat(calls.get()).isZero();   // 미리보기는 만들지 않는다
    openTwice(root);
    addChild(cause, "해결: 배치 재기동");
    mvc.perform(as(get("/chains/" + root + "?summarize=false"), userBackend, wsSw))
        .andExpect(jsonPath("$.summary").value("요약 1")).andExpect(jsonPath("$.summary_status").value("stale"));   // 저장된 이전 요약은 보여 준다
    assertThat(calls.get()).isEqualTo(1);
    openTwice(root).andExpect(jsonPath("$.summary").value("요약 2"));   // 타임라인 조회는 그대로 다시 만든다
  }

  @Test
  void autoOnReadOffNeverStartsASummary() {
    AtomicInteger started = new AtomicInteger();
    ChainSummaryService off = new ChainSummaryService(jdbc, props, json, task -> started.incrementAndGet(), false);
    assertThat(off.view(wsSw, root, r -> null).status()).isEqualTo("none");
    assertThat(started.get()).isZero();
    ChainSummaryService on = new ChainSummaryService(jdbc, props, json, task -> started.incrementAndGet(), true);
    on.view(wsSw, root, r -> null);
    assertThat(started.get()).isEqualTo(1);
  }
}
