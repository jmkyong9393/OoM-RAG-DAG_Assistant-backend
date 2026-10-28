package com.oom.assist.session;

import com.oom.assist.common.config.AppProps;
import jakarta.servlet.http.Cookie;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Base64;
import java.util.Optional;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.stereotype.Component;

/**
 * 데모 세션 쿠키 oom_u = base64url("{user_id}:{exp_epoch}") + "." + base64url(HMAC-SHA256).
 * 비밀번호 없는 데모 로그인용. v1.1 JWT 로 바뀔 때 이 클래스와 TenantContextFilter 만 교체한다.
 */
@Component
public class SessionCookie {
  public static final String NAME = "oom_u";
  private static final String ALG = "HmacSHA256";

  private final AppProps.Session props;

  public SessionCookie(AppProps props) {
    this.props = props.session();
  }

  public String issue(long userId) {
    return issue(userId, Instant.now().plusSeconds(props.ttlHours() * 3600L).getEpochSecond());
  }

  public String issue(long userId, long expEpochSeconds) {
    String payload = b64(userId + ":" + expEpochSeconds);
    return payload + "." + sign(payload);
  }

  public Optional<Long> verify(String value) {
    if (value == null) return Optional.empty();
    int dot = value.lastIndexOf('.');
    if (dot <= 0) return Optional.empty();
    String payload = value.substring(0, dot);
    String sig = value.substring(dot + 1);
    if (!MessageDigest.isEqual(sig.getBytes(StandardCharsets.US_ASCII), sign(payload).getBytes(StandardCharsets.US_ASCII))) {
      return Optional.empty();
    }
    try {
      String[] parts = new String(Base64.getUrlDecoder().decode(payload), StandardCharsets.UTF_8).split(":");
      long userId = Long.parseLong(parts[0]);
      long exp = Long.parseLong(parts[1]);
      if (exp < Instant.now().getEpochSecond()) return Optional.empty();
      return Optional.of(userId);
    } catch (RuntimeException e) {
      return Optional.empty();
    }
  }

  public Cookie toCookie(String value, boolean clear) {
    Cookie c = new Cookie(NAME, clear ? "" : value);
    c.setHttpOnly(true);
    c.setPath("/");
    c.setSecure(props.secureCookie());
    c.setMaxAge(clear ? 0 : props.ttlHours() * 3600);
    // 다른 오리진(Vercel)에서 쿠키를 보내려면 Secure + SameSite=None 이어야 한다. 로컬은 Lax.
    c.setAttribute("SameSite", props.secureCookie() ? "None" : "Lax");
    return c;
  }

  private String sign(String payload) {
    try {
      Mac mac = Mac.getInstance(ALG);
      mac.init(new SecretKeySpec(props.secret().getBytes(StandardCharsets.UTF_8), ALG));
      return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(payload.getBytes(StandardCharsets.UTF_8)));
    } catch (GeneralSecurityException e) {
      throw new IllegalStateException(e);
    }
  }

  private static String b64(String s) {
    return Base64.getUrlEncoder().withoutPadding().encodeToString(s.getBytes(StandardCharsets.UTF_8));
  }
}
