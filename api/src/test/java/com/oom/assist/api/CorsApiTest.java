package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.options;

import java.util.ArrayList;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.web.servlet.mvc.method.annotation.RequestMappingHandlerMapping;

/** 다른 출처(프론트 개발 서버 · 프록시)에서 부르는 모든 엔드포인트가 CORS 사전 요청을 통과한다 — 허용 목록에서 빠진 메서드는 403 이 된다. */
class CorsApiTest extends AbstractApiTest {

  @Autowired @Qualifier("requestMappingHandlerMapping") RequestMappingHandlerMapping mappings;

  @Test
  void everyEndpointPassesPreflightFromAllowedOrigin() throws Exception {
    var failed = new ArrayList<String>();
    int checked = 0;
    for (var info : mappings.getHandlerMethods().keySet()) {
      if (info.getPathPatternsCondition() == null) continue;
      String path = info.getPathPatternsCondition().getPatternValues().iterator().next().replaceAll("\\{[^}]+}", "1");
      for (var m : info.getMethodsCondition().getMethods()) {
        var r = mvc.perform(options(path).header("Origin", "http://localhost:3000").header("Access-Control-Request-Method", m.name())).andReturn().getResponse();
        checked++;
        String allowed = r.getHeader("Access-Control-Allow-Methods");
        if (r.getStatus() != 200 || allowed == null || !allowed.contains(m.name())) failed.add(m + " " + path + " → " + r.getStatus());
      }
    }
    assertThat(checked).isGreaterThan(30);
    assertThat(failed).isEmpty();
  }
}
