package com.oom.assist.common.config;

import com.oom.assist.common.context.TenantContextArgumentResolver;
import java.util.List;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.http.MediaType;
import org.springframework.web.servlet.config.annotation.ContentNegotiationConfigurer;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/** CORS(Vercel 프로덕션·미리보기 오리진) + 컨트롤러 인자 주입(TenantContext). */
@Configuration
@EnableConfigurationProperties(AppProps.class)
public class WebConfig implements WebMvcConfigurer {

  private final AppProps props;
  private final TenantContextArgumentResolver resolver;

  public WebConfig(AppProps props, TenantContextArgumentResolver resolver) {
    this.props = props;
    this.resolver = resolver;
  }

  @Override
  public void addCorsMappings(CorsRegistry registry) {
    registry.addMapping("/**")
        .allowedOriginPatterns(props.cors().origins().toArray(String[]::new))
        .allowedMethods("GET", "POST", "PATCH", "DELETE", "OPTIONS")
        .allowedHeaders("*")
        .allowCredentials(true) // oom_u 쿠키
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
