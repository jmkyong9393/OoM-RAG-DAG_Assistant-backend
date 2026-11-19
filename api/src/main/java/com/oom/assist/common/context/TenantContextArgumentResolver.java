package com.oom.assist.common.context;

import com.oom.assist.auth.CurrentUser;
import com.oom.assist.common.error.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.MethodParameter;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.support.WebDataBinderFactory;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.method.support.ModelAndViewContainer;

/**
 * 컨트롤러 메서드의 TenantContext 파라미터를 필터가 채운 요청 속성에서 주입한다.
 * 없으면 — 로그인하지 않았으면 401, 로그인했는데 부서 정보를 만들 수 없으면(소속이 아닌 부서 · 없는 부서 · 잘못된 헤더 · 헤더 없음) 403.
 * 프론트는 401 을 로그인이 끊긴 것으로 보고 로그인 화면으로 보내므로, 부서를 잘못 연 것까지 401 로 주면 로그아웃된다.
 * 규칙 R1: 없는 부서와 소속이 아닌 부서는 같은 403 — 부서가 있는지 알 수 없게 한다.
 */
@Component
public class TenantContextArgumentResolver implements HandlerMethodArgumentResolver {
  public static final String ATTR = TenantContext.class.getName();

  @Override
  public boolean supportsParameter(MethodParameter parameter) {
    return TenantContext.class.equals(parameter.getParameterType());
  }

  @Override
  public Object resolveArgument(MethodParameter parameter, ModelAndViewContainer mav, NativeWebRequest webRequest, WebDataBinderFactory binderFactory) {
    HttpServletRequest req = webRequest.getNativeRequest(HttpServletRequest.class);
    Object ctx = req == null ? null : req.getAttribute(ATTR);
    if (ctx == null) throw CurrentUser.id().isPresent() ? ApiException.forbidden("no access to this workspace") : ApiException.unauthorized();
    return ctx;
  }
}
