package com.oom.assist.chat;

import com.oom.assist.common.config.AppProps;
import com.oom.assist.common.context.TenantContext;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.stream.Stream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import tools.jackson.databind.ObjectMapper;

/**
 * POST /chat 릴레이 (03 안건 E). 가상 스레드에서 Python /ai/chat 을 줄 단위로 읽어 SseEmitter 로 그대로 흘린다.
 * 1) search_sessions INSERT → `session` 송출  2) Python 이벤트 바이트 통과  3) `done` 가로채 세션 UPDATE
 * 4) 예외·Python 오류 → `error` 송출 + 세션 status=error.  리액티브 스택 없음.
 */
@Service
public class ChatRelayService {

  private static final Logger log = LoggerFactory.getLogger(ChatRelayService.class);
  private static final long EMITTER_TIMEOUT_MS = 180_000L;

  private final AppProps props;
  private final SessionRecorder recorder;
  private final JdbcClient jdbc;
  private final ObjectMapper json;
  // HTTP/1.1 고정: 기본값(HTTP/2)은 h2c 업그레이드 헤더를 붙이는데 uvicorn 이 이를 거부해 본문이 비어 422 가 난다
  private final HttpClient http = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1)
      .connectTimeout(Duration.ofSeconds(5)).build();
  private final ExecutorService relayThreads = Executors.newVirtualThreadPerTaskExecutor();

  public ChatRelayService(AppProps props, SessionRecorder recorder, JdbcClient jdbc, ObjectMapper json) {
    this.props = props;
    this.recorder = recorder;
    this.jdbc = jdbc;
    this.json = json;
  }

  public SseEmitter stream(TenantContext ctx, String query) {
    boolean policyEnabled = jdbc.sql("SELECT policy_enabled FROM workspaces WHERE id = ?").param(ctx.workspaceId()).query(Boolean.class).single();
    long sessionId = recorder.start(ctx, query, policyEnabled);
    SseEmitter emitter = new SseEmitter(EMITTER_TIMEOUT_MS);
    relayThreads.submit(() -> relay(emitter, ctx, sessionId, query, policyEnabled));
    return emitter;
  }

  private void relay(SseEmitter emitter, TenantContext ctx, long sessionId, String query, boolean policyEnabled) {
    StringBuilder answer = new StringBuilder();
    String[] policyJson = {null};
    String[] chainJson = {null};
    boolean finished = false;
    try {
      send(emitter, "session", json.writeValueAsString(Map.of("session_id", sessionId)));

      String body = json.writeValueAsString(Map.of(
          "workspace_id", ctx.workspaceId(), "policy_enabled", policyEnabled, "session_id", sessionId, "query", query));
      HttpRequest req = HttpRequest.newBuilder(URI.create(props.ai().baseUrl() + "/ai/chat"))
          .timeout(Duration.ofMillis(EMITTER_TIMEOUT_MS))
          .header("Content-Type", "application/json").header("Accept", "text/event-stream")
          .POST(HttpRequest.BodyPublishers.ofString(body)).build();
      HttpResponse<Stream<String>> res = http.send(req, HttpResponse.BodyHandlers.ofLines());
      if (res.statusCode() / 100 != 2) {
        String detail;
        try (Stream<String> lines = res.body()) { detail = lines.limit(3).reduce("", (a, b) -> a + b); }
        throw new IOException("ai service responded " + res.statusCode() + " " + detail);
      }

      SseBlock.Parser parser = new SseBlock.Parser();
      try (Stream<String> lines = res.body()) {
        for (String line : (Iterable<String>) lines::iterator) {
          SseBlock block = parser.feed(line.endsWith("\r") ? line.substring(0, line.length() - 1) : line);
          if (block == null) continue;
          finished |= handle(emitter, sessionId, block, answer, policyJson, chainJson);
        }
      }
      SseBlock rest = parser.flush();
      if (rest != null) finished |= handle(emitter, sessionId, rest, answer, policyJson, chainJson);
      if (!finished) {
        recorder.error(sessionId, answer.toString());
        send(emitter, "error", json.writeValueAsString(Map.of("message", "stream ended without done")));
      }
      emitter.complete();
    } catch (Exception e) {
      log.warn("chat relay failed session={} : {}", sessionId, e.toString());
      recorder.error(sessionId, answer.toString());
      try {
        send(emitter, "error", json.writeValueAsString(Map.of("message", "ai service unavailable")));
        emitter.complete();
      } catch (Exception ignored) {
        emitter.completeWithError(e);
      }
    }
  }

  /** 블록을 그대로 전달하고, 세션 기록에 필요한 것만 들여다본다. done 이면 true. */
  @SuppressWarnings("unchecked")
  private boolean handle(SseEmitter emitter, long sessionId, SseBlock block, StringBuilder answer, String[] policyJson, String[] chainJson) throws IOException {
    switch (block.event()) {
      case "token" -> {
        Map<String, Object> m = json.readValue(block.data(), Map.class);
        Object t = m.get("text");
        if (t != null) answer.append(t);
      }
      case "policy" -> policyJson[0] = block.data();
      case "chain" -> chainJson[0] = block.data();
      case "done" -> {
        Map<String, Object> done = json.readValue(block.data(), Map.class);
        recorder.done(sessionId, done, answer.toString(), policyJson[0], chainJson[0]);
        send(emitter, block.event(), block.data());
        return true;
      }
      case "error" -> {
        recorder.error(sessionId, answer.toString());
        send(emitter, block.event(), block.data());
        return true;
      }
      default -> { }
    }
    send(emitter, block.event(), block.data());
    return false;
  }

  private static void send(SseEmitter emitter, String event, String data) throws IOException {
    emitter.send(SseEmitter.event().name(event).data(data));
  }
}
