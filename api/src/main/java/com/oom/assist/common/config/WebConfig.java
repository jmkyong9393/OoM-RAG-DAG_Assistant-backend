package com.oom.assist.common.config;

import com.oom.assist.auth.AccountGate;
import com.oom.assist.common.context.TenantContextArgumentResolver;
import java.util.List;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.http.MediaType;
import org.springframework.web.servlet.config.annotation.ContentNegotiationConfigurer;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/** CORS(Vercel 프로덕션·미리보기 오리진) + 컨트롤러 인자 주입(TenantContext) + 계정 상태 확인(AccountGate). */
@Configuration
@EnableConfigurationProperties(AppProps.class)
public class WebConfig implements WebMvcConfigurer {

  private final AppProps props;
  private final TenantContextArgumentResolver resolver;
  private final AccountGate accountGate;

  public WebConfig(AppProps props, TenantContextArgumentResolver resolver, AccountGate accountGate) {
    this.props = props;
    this.resolver = resolver;
    this.accountGate = accountGate;
  }

  /** 로그인한 요청마다 계정 상태(동의 · 비밀번호 변경)를 컨트롤러 전에 확인한다. */
  @Override
  public void addInterceptors(InterceptorRegistry registry) {
    registry.addInterceptor(accountGate);
  }

  @Override
  public void addCorsMappings(CorsRegistry registry) {
    registry.addMapping("/**")
        .allowedOriginPatterns(props.cors().origins().toArray(String[]::new))
        .allowedMethods("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
        .allowedHeaders("*")
        .allowCredentials(true) // 세션 쿠키 OOMSESSION
        .maxAge(3600);
  }

  /**
   * Accept 헤더를 무시하고 JSON 을 기본으로 한다. 프론트는 /chat 에 Accept: text/event-stream 만 보내는데,
   * 그 요청의 검증 오류(JSON)가 406 이 되지 않게 한다. SseEmitter 는 자기 Content-Type 을 직접 쓴다.
   */
  @Override
  public void configureContentNegotiation(ContentNegotiationConfigurer c) {
    c.ignoreAcceptHeader(true).defaultContentType(MediaType.APPLICATION_JSON);
  }

  @Override
  public void addArgumentResolvers(List<HandlerMethodArgumentResolver> resolvers) {
    resolvers.add(resolver);
  }
}
