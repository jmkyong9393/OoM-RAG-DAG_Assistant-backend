package com.oom.assist.auth;

import com.oom.assist.common.context.TenantContextFilter;
import jakarta.servlet.http.HttpServletResponse;
import java.nio.charset.StandardCharsets;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.config.annotation.authentication.configuration.AuthenticationConfiguration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.AnonymousAuthenticationFilter;
import org.springframework.security.web.context.HttpSessionSecurityContextRepository;
import org.springframework.security.web.context.SecurityContextRepository;
import org.springframework.session.web.http.CookieSerializer;
import org.springframework.session.web.http.DefaultCookieSerializer;
import com.oom.assist.common.config.AppProps;

/**
 * 세션 쿠키 로그인(Spring Session JDBC). 인증 실패는 401 {code:UNAUTHORIZED}, 권한 부족은 403.
 * CSRF 는 토큰 대신 쿠키의 SameSite=Lax 로 막는다 — 다른 사이트의 POST·PUT·PATCH·DELETE 에는 세션 쿠키가 실리지 않는다
 * (헤더도 JSON 본문도 없는 /auth/logout · /auth/consent 도 이것으로 막힌다). 제약: SESSION_SAME_SITE=None 이면 이 전제가 깨진다.
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

  @Bean
  PasswordEncoder passwordEncoder() {
    return new BCryptPasswordEncoder();
  }

  @Bean
  AuthenticationManager authenticationManager(AuthenticationConfiguration config) throws Exception {
    return config.getAuthenticationManager();
  }

  @Bean
  SecurityContextRepository securityContextRepository() {
    return new HttpSessionSecurityContextRepository();
  }

  /** 서블릿 필터로 자동 등록되지 않게 @Component 대신 여기서 만든다(보안 체인 안에서만 한 번 실행). */
  @Bean
  TenantContextFilter tenantContextFilter(JdbcClient jdbc) {
    return new TenantContextFilter(jdbc);
  }

  /** 세션 쿠키 속성을 명시한다 (Boot 4 에서는 server.servlet.session.cookie.* 가 Spring Session 쿠키에 적용되지 않았다 — 실측). */
  @Bean
  CookieSerializer cookieSerializer(AppProps props) {
    DefaultCookieSerializer s = new DefaultCookieSerializer();
    s.setCookieName(props.session().cookieName());
    s.setUseHttpOnlyCookie(true);
    s.setSameSite(props.session().sameSite());
    s.setUseSecureCookie(props.session().secure());
    s.setCookiePath("/"); // 기본값은 컨텍스트 경로(/api/v1) — 그러면 Next 서버 컴포넌트가 받는 페이지 요청(/w/…)에 쿠키가 안 실려 SSR 포워딩이 401 (F-1 실측)
    return s;
  }

  @Bean
  SecurityFilterChain api(HttpSecurity http, TenantContextFilter tenantContextFilter, SecurityContextRepository repo) throws Exception {
    http
        .csrf(c -> c.disable())
        .cors(c -> {})                                                   // WebConfig 의 CORS 매핑을 쓴다
        .httpBasic(b -> b.disable()).formLogin(f -> f.disable()).logout(l -> l.disable())
        .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.IF_REQUIRED))
        .securityContext(sc -> sc.securityContextRepository(repo))
        .authorizeHttpRequests(a -> a
            .requestMatchers(HttpMethod.POST, "/auth/login").permitAll()
            .requestMatchers(HttpMethod.GET, "/auth/password-policy", "/auth/privacy-policy").permitAll()
            .requestMatchers(HttpMethod.GET, "/workspaces").permitAll()   // 로그인 전 앱 셸(부서 목록)
            .requestMatchers("/actuator/health", "/actuator/health/**", "/actuator/info").permitAll()
            .requestMatchers(HttpMethod.OPTIONS, "/**").permitAll()
            .anyRequest().authenticated())
        .exceptionHandling(e -> e
            .authenticationEntryPoint((req, res, ex) -> json(res, HttpServletResponse.SC_UNAUTHORIZED, "UNAUTHORIZED", "login required"))
            .accessDeniedHandler((req, res, ex) -> json(res, HttpServletResponse.SC_FORBIDDEN, "FORBIDDEN", "forbidden")))
        // 세션에서 사용자가 복원된 뒤(익명 처리 포함) X-Workspace-Id 로 멤버십을 확인한다
        .addFilterAfter(tenantContextFilter, AnonymousAuthenticationFilter.class);
    return http.build();
  }

  static void json(HttpServletResponse res, int status, String code, String message) throws java.io.IOException {
    res.setStatus(status);
    res.setContentType(MediaType.APPLICATION_JSON_VALUE);
    res.setCharacterEncoding(StandardCharsets.UTF_8.name());
    res.getWriter().write("{\"code\":\"" + code + "\",\"message\":\"" + message + "\"}");
  }
}
