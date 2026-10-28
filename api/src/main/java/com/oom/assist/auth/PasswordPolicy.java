package com.oom.assist.auth;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 비밀번호 작성규칙 (개인정보 기술적·관리적 보호조치 기준 §4 ⑧):
 *  - 영대문자·영소문자·숫자·특수문자 중 2종 이상 조합 10자 이상, 또는 3종 이상 조합 8자 이상 (최대 128자)
 *  - 사번(ID)과 같거나 사번을 포함하지 않음
 *  - 같은 문자 3연속, 순차 문자 3연속(abc, 321, 1234 …) 금지
 *  - 유효기간 180일(반기 1회 변경)
 */
public final class PasswordPolicy {
  public static final int MAX_AGE_DAYS = 180;
  public static final String RULE_TEXT = "영문 대/소문자·숫자·특수문자 중 2종 이상을 조합해 10자 이상, 또는 3종 이상을 조합해 8자 이상 (최대 128자). "
      + "사번을 포함하거나 같은 문자·연속된 문자(aaa, abc, 123)를 3자 이상 이어 쓸 수 없습니다.";

  private PasswordPolicy() {}

  /** 위반 사유 목록. 비어 있으면 통과. */
  public static List<String> violations(String password, String employeeNo) {
    List<String> v = new ArrayList<>();
    if (password == null) { v.add("비밀번호가 없습니다"); return v; }
    int len = password.length();
    int classes = 0;
    if (password.chars().anyMatch(Character::isUpperCase)) classes++;
    if (password.chars().anyMatch(Character::isLowerCase)) classes++;
    if (password.chars().anyMatch(Character::isDigit)) classes++;
    if (password.chars().anyMatch(c -> !Character.isLetterOrDigit(c) && !Character.isWhitespace(c))) classes++;
    if (len > 128) v.add("128자를 넘을 수 없습니다");
    if (!((classes >= 2 && len >= 10) || (classes >= 3 && len >= 8))) v.add("2종 조합 10자 이상 또는 3종 조합 8자 이상이어야 합니다");
    if (password.chars().anyMatch(Character::isWhitespace)) v.add("공백을 쓸 수 없습니다");
    if (employeeNo != null && !employeeNo.isBlank() && password.toLowerCase(Locale.ROOT).contains(employeeNo.toLowerCase(Locale.ROOT)))
      v.add("사번을 포함할 수 없습니다");
    if (hasRun(password)) v.add("같은 문자 또는 연속된 문자를 3자 이상 이어 쓸 수 없습니다");
    return v;
  }

  public static boolean isValid(String password, String employeeNo) {
    return violations(password, employeeNo).isEmpty();
  }

  /** aaa / abc / cba / 123 / 321 처럼 같은 문자 또는 ±1 단조 연속이 3자 이상인지. */
  public static boolean hasRun(String s) {
    String t = s.toLowerCase(Locale.ROOT);
    for (int i = 0; i + 2 < t.length(); i++) {
      char a = t.charAt(i), b = t.charAt(i + 1), c = t.charAt(i + 2);
      if (a == b && b == c) return true;
      if (Character.isLetterOrDigit(a) && b - a == 1 && c - b == 1) return true;
      if (Character.isLetterOrDigit(a) && a - b == 1 && b - c == 1) return true;
    }
    return false;
  }
}
