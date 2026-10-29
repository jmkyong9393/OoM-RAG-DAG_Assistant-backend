package com.oom.assist.common.error;

import org.springframework.http.HttpStatus;

/** 프론트가 분기하는 에러 코드(NOT_ROOT, NOTE_REQUIRED …)를 싣는 예외. 응답은 {code, message}. */
public class ApiException extends RuntimeException {
  private final HttpStatus status;
  private final String code;

  public ApiException(HttpStatus status, String code, String message) {
    super(message);
    this.status = status;
    this.code = code;
  }

  public HttpStatus status() { return status; }
  public String code() { return code; }

  public static ApiException notFound(String what) { return new ApiException(HttpStatus.NOT_FOUND, "NOT_FOUND", what + " not found"); }
  public static ApiException badRequest(String code, String message) { return new ApiException(HttpStatus.BAD_REQUEST, code, message); }
  public static ApiException conflict(String code, String message) { return new ApiException(HttpStatus.CONFLICT, code, message); }
  public static ApiException unauthorized() { return new ApiException(HttpStatus.UNAUTHORIZED, "UNAUTHORIZED", "login required"); }
  public static ApiException forbidden(String message) { return new ApiException(HttpStatus.FORBIDDEN, "FORBIDDEN", message); }
}
