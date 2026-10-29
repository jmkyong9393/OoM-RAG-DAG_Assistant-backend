package com.oom.assist.common.config;

import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

/** application.yml 의 oom.* 설정. */
@ConfigurationProperties(prefix = "oom")
public record AppProps(Ai ai, Cors cors, Session session) {
  public record Ai(String baseUrl) {}
  public record Cors(List<String> origins) {}
  /** 세션 쿠키 속성. 프론트가 다른 사이트(예: *.vercel.app)면 sameSite=None + secure=true 가 필요하다. */
  public record Session(String cookieName, String sameSite, boolean secure) {}
}
