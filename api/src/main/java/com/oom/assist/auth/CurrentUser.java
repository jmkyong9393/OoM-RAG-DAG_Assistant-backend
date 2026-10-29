package com.oom.assist.auth;

import java.util.Optional;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

/** 현재 요청의 로그인 사용자. 세션(Spring Session JDBC)에서 복원된 SecurityContext 를 읽는다. */
public final class CurrentUser {
  private CurrentUser() {}

  public static Optional<AppPrincipal> get() {
    Authentication a = SecurityContextHolder.getContext().getAuthentication();
    if (a == null || !a.isAuthenticated() || !(a.getPrincipal() instanceof AppPrincipal p)) return Optional.empty();
    return Optional.of(p);
  }

  public static Optional<Long> id() {
    return get().map(AppPrincipal::id);
  }
}
