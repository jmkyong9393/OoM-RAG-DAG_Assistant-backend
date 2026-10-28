package com.oom.assist.unit;

import static org.assertj.core.api.Assertions.assertThat;

import com.oom.assist.common.config.AppProps;
import com.oom.assist.session.SessionCookie;
import jakarta.servlet.http.Cookie;
import java.time.Instant;
import java.util.List;
import org.junit.jupiter.api.Test;

/** 서명 쿠키 단위 테스트. Spring 컨텍스트·DB 없이 돈다. */
class SessionCookieTest {

  private SessionCookie cookie(String secret, boolean secure) {
    return new SessionCookie(new AppProps(new AppProps.Ai("http://ai"), new AppProps.Cors(List.of("*")), new AppProps.Session(secret, 1, secure)));
  }

  @Test
  void issueThenVerifyRoundTrips() {
    SessionCookie c = cookie("s3cret", false);
    assertThat(c.verify(c.issue(42L))).contains(42L);
  }

  @Test
  void tamperedPayloadOrSignatureIsRejected() {
    SessionCookie c = cookie("s3cret", false);
    String v = c.issue(42L);
    String payload = v.substring(0, v.lastIndexOf('.'));
    String sig = v.substring(v.lastIndexOf('.') + 1);
    assertThat(c.verify(payload + "x." + sig)).isEmpty();
    assertThat(c.verify(payload + "." + sig.substring(1) + "A")).isEmpty();
    assertThat(c.verify("garbage")).isEmpty();
    assertThat(c.verify(null)).isEmpty();
  }

  @Test
  void differentSecretCannotVerify() {
    assertThat(cookie("b", false).verify(cookie("a", false).issue(1L))).isEmpty();
  }

  @Test
  void expiredCookieIsRejected() {
    SessionCookie c = cookie("s3cret", false);
    assertThat(c.verify(c.issue(7L, Instant.now().getEpochSecond() - 1))).isEmpty();
  }

  @Test
  void cookieAttributesFollowSecureFlag() {
    Cookie local = cookie("s", false).toCookie("v", false);
    assertThat(local.isHttpOnly()).isTrue();
    assertThat(local.getSecure()).isFalse();
    assertThat(local.getAttribute("SameSite")).isEqualTo("Lax");
    assertThat(local.getMaxAge()).isEqualTo(3600);
    Cookie prod = cookie("s", true).toCookie("v", false);
    assertThat(prod.getSecure()).isTrue();
    assertThat(prod.getAttribute("SameSite")).isEqualTo("None");
    assertThat(cookie("s", false).toCookie("", true).getMaxAge()).isZero();
  }
}
