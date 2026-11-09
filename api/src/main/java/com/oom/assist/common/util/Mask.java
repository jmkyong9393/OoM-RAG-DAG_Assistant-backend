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

  /** IPv4 는 마지막 자리, IPv6 는 앞 4묶음만 남긴다: 10.0.3.7 → 10.0.3.*, 0:0:0:0:0:0:0:1 → 0:0:0:0:* */
  public static String ip(String ip) {
    if (ip == null || ip.isBlank()) return null;
    String v = ip.strip();
    if (v.matches("\\d{1,3}(\\.\\d{1,3}){3}")) return v.substring(0, v.lastIndexOf('.')) + ".*";
    if (v.contains(":")) {
      String[] g = v.split(":", -1);
      return String.join(":", java.util.Arrays.copyOf(g, Math.min(4, g.length))) + ":*";
    }
    return "***";
  }

  /** OOM0002 → OOM00** (뒤 2자리) */
  public static String employeeNo(String no) {
    if (no == null) return null;
    if (no.length() <= 2) return "**";
    return no.substring(0, no.length() - 2) + "**";
  }
}
