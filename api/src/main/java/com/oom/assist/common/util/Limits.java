package com.oom.assist.common.util;

import com.oom.assist.common.error.ApiException;

/** 입력 길이 상한(자, 앞뒤 공백 제외). 화면의 maxLength 와 같은 값이다 — 바꾸면 계약 문서도 함께 바꾼다. */
public final class Limits {
  public static final int LOG_TITLE = 200;
  public static final int LOG_BODY = 5000;
  public static final int NOTE = 300;        // 사유 · 메모(완료 · 폐기 · 반려 · 비활성화)
  public static final int QUERY = 4000;
  public static final int COMMENT = 1000;
  public static final int NAME = 50;
  public static final int GRADE = 30;        // 직급 · 직책
  public static final int EMAIL = 254;
  public static final int DOC_TITLE = 200;
  public static final int DOC_VERSION = 30;  // 판 · 문서 종류

  private Limits() {}

  /** value 가 max 자를 넘으면 400 VALIDATION. null 은 통과한다. */
  public static void max(String value, int max, String field) {
    if (value != null && value.strip().length() > max)
      throw ApiException.badRequest("VALIDATION", field + " must be at most " + max + " characters");
  }
}
