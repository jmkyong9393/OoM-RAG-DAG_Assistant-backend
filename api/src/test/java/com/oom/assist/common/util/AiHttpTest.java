package com.oom.assist.common.util;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.sun.net.httpserver.HttpServer;
import java.net.ConnectException;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.concurrent.Executors;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

class AiHttpTest {
  private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(1)).build();
  private final long saved = AiHttp.retryDelayMs;

  @AfterEach
  void restore() { AiHttp.retryDelayMs = saved; }

  @Test
  void retriesWhileServerIsNotListeningYet() throws Exception {
    int port;
    try (ServerSocket s = new ServerSocket(0)) { port = s.getLocalPort(); }   // 잠깐 비워 둔 포트 — 처음 몇 번은 연결 거절
    AiHttp.retryDelayMs = 200;
    HttpServer server = HttpServer.create();
    int p = port;
    Executors.newSingleThreadScheduledExecutor().schedule(() -> {
      try {
        server.bind(new InetSocketAddress("127.0.0.1", p), 0);
        server.createContext("/ai", ex -> { byte[] b = "ok".getBytes(StandardCharsets.UTF_8); ex.sendResponseHeaders(200, b.length); ex.getResponseBody().write(b); ex.close(); });
        server.start();
      } catch (Exception e) { throw new RuntimeException(e); }
    }, 500, java.util.concurrent.TimeUnit.MILLISECONDS);
    try {
      HttpResponse<String> res = AiHttp.send(http, HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + p + "/ai")).build(), HttpResponse.BodyHandlers.ofString());
      assertThat(res.statusCode()).isEqualTo(200);
      assertThat(res.body()).isEqualTo("ok");
    } finally {
      server.stop(0);
    }
  }

  @Test
  void givesUpAfterRetries() throws Exception {
    int port;
    try (ServerSocket s = new ServerSocket(0)) { port = s.getLocalPort(); }
    AiHttp.retryDelayMs = 10;
    HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/ai")).build();
    assertThatThrownBy(() -> AiHttp.send(http, req, HttpResponse.BodyHandlers.ofString())).isInstanceOf(ConnectException.class);
  }
}
