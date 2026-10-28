package com.oom.assist.common.context;

import com.oom.assist.common.error.ApiException;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.MethodParameter;
import org.springframework.stereotype.Component;
import org.springframework.web.bind.support.WebDataBinderFactory;
import org.springframework.web.context.request.NativeWebRequest;
import org.springframework.web.method.support.HandlerMethodArgumentResolver;
import org.springframework.web.method.support.ModelAndViewContainer;

/** 컨트롤러 메서드의 TenantContext 파라미터를 필터가 채운 요청 속성에서 주입한다. 없으면 401. */
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
    if (ctx == null) throw ApiException.unauthorized();
    return ctx;
  }
}
