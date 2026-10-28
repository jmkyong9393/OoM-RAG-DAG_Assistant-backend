package com.oom.assist.common.config;

import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

/** application.yml 의 oom.* 설정. */
@ConfigurationProperties(prefix = "oom")
public record AppProps(Ai ai, Cors cors, Session session) {
  public record Ai(String baseUrl) {}
  public record Cors(List<String> origins) {}
  public record Session(String secret, int ttlHours, boolean secureCookie) {}
}
