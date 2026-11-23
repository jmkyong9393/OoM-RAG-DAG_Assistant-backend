package com.oom.assist.common.util;

import java.io.IOException;
import java.net.ConnectException;
import java.net.http.HttpClient;
import java.net.http.HttpConnectTimeoutException;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;

/**
 * AI 서비스 호출. 연결 단계에서 실패하면(요청은 아직 가지 않았다) 1초 뒤 다시 건다 —
 * 롤링 배포로 ai 컨테이너가 바뀌는 동안 이름 'ai' 가 뜨는 중(준비 전) · 내려가는 중인 컨테이너도 가리킨다.
 * 이름 풀이 캐시가 1초라(api Dockerfile) 다시 걸 때마다 주소를 새로 풀어 준비된 쪽으로 붙는다.
 */
public final class AiHttp {
  static final int CONNECT_RETRIES = 15;
  static long retryDelayMs = 1_000L;

  private AiHttp() {}

  public static <T> HttpResponse<T> send(HttpClient http, HttpRequest req, HttpResponse.BodyHandler<T> handler)
      throws IOException, InterruptedException {
    for (int attempt = 0; ; attempt++) {
      try {
        return http.send(req, handler);
      } catch (ConnectException | HttpConnectTimeoutException e) {
        if (attempt >= CONNECT_RETRIES) throw e;
        Thread.sleep(retryDelayMs);
      }
    }
  }
}
