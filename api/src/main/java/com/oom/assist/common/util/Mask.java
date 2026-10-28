package com.oom.assist.common.util;

/** 개인정보 표시 제한(보호조치 기준 §10). 시스템 전체가 같은 방식으로 가린다. */
public final class Mask {
  private Mask() {}

  /** jang@oom.local → j***@oom.local, ab@x.y → a***@x.y */
  public static String email(String email) {
    if (email == null) return null;
    int at = email.indexOf('@');
    if (at <= 0) return "***";
    return email.charAt(0) + "***" + email.substring(at);
  }

  /** OOM0002 → OOM00** (뒤 2자리) */
  public static String employeeNo(String no) {
    if (no == null) return null;
    if (no.length() <= 2) return "**";
    return no.substring(0, no.length() - 2) + "**";
  }
}
