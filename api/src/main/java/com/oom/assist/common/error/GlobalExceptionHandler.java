package com.oom.assist.common.error;

import java.util.List;
import java.util.Map;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 에러 응답 계약: {"code": "...", "message": "..."}. DB 트리거의 RAISE 메시지(EDGE_*, WORKSPACE_*)도 코드로 승격한다. */
@RestControllerAdvice
public class GlobalExceptionHandler {

  private static final List<String> DB_CODES = List.of(
      "EDGE_CROSS_WORKSPACE", "EDGE_PARENT_TYPE", "WORKSPACE_SELF_PARENT", "WORKSPACE_CROSS_TENANT",
      "CHUNK_WORKSPACE_MISMATCH", "NOT_RETIRED", "NOT_FOUND");

  @ExceptionHandler(ApiException.class)
  ResponseEntity<Map<String, String>> api(ApiException e) {
    return ResponseEntity.status(e.status()).body(Map.of("code", e.code(), "message", e.getMessage()));
  }

  @ExceptionHandler(MethodArgumentNotValidException.class)
  ResponseEntity<Map<String, String>> invalid(MethodArgumentNotValidException e) {
    String msg = e.getBindingResult().getFieldErrors().stream()
        .map(f -> f.getField() + " " + f.getDefaultMessage()).findFirst().orElse("invalid request");
    return ResponseEntity.badRequest().body(Map.of("code", "VALIDATION", "message", msg));
  }

  @ExceptionHandler(DataIntegrityViolationException.class)
  ResponseEntity<Map<String, String>> integrity(DataIntegrityViolationException e) {
    Throwable root = e.getMostSpecificCause();
    String raw = root == null || root.getMessage() == null ? "constraint violation" : root.getMessage();
    String code = codeOf(raw);
    HttpStatus status = code.equals("DB_CONSTRAINT") ? HttpStatus.BAD_REQUEST : HttpStatus.CONFLICT;
    return ResponseEntity.status(status).body(Map.of("code", code, "message", raw.lines().findFirst().orElse(raw)));
  }

  public static String codeOf(String message) {
    for (String k : DB_CODES) if (message.contains(k)) return k;
    if (message.contains("duplicate key")) return "DUPLICATE";
    return "DB_CONSTRAINT";
  }
}
