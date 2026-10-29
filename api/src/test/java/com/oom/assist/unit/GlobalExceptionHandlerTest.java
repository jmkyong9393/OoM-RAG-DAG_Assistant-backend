package com.oom.assist.unit;

import static org.assertj.core.api.Assertions.assertThat;

import com.oom.assist.common.error.GlobalExceptionHandler;
import org.junit.jupiter.api.Test;

/** DB 트리거 메시지 → API 코드 매핑. */
class GlobalExceptionHandlerTest {

  @Test
  void mapsTriggerMessagesToCodes() {
    assertThat(GlobalExceptionHandler.codeOf("ERROR: EDGE_PARENT_TYPE\n  Where: PL/pgSQL")).isEqualTo("EDGE_PARENT_TYPE");
    assertThat(GlobalExceptionHandler.codeOf("ERROR: WORKSPACE_CROSS_TENANT")).isEqualTo("WORKSPACE_CROSS_TENANT");
    assertThat(GlobalExceptionHandler.codeOf("ERROR: NOT_RETIRED")).isEqualTo("NOT_RETIRED");
  }

  @Test
  void mapsDuplicateAndUnknown() {
    assertThat(GlobalExceptionHandler.codeOf("ERROR: duplicate key value violates unique constraint")).isEqualTo("DUPLICATE");
    assertThat(GlobalExceptionHandler.codeOf("ERROR: new row violates check constraint")).isEqualTo("DB_CONSTRAINT");
  }
}
