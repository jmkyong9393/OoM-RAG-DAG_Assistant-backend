package com.oom.assist.common.util;

import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;

/** JdbcClient 의 Map 행(listOfRows)은 timestamptz 를 java.sql.Timestamp 로 준다. 응답에는 OffsetDateTime(ISO-8601)으로 통일한다. */
public final class Times {
  private Times() {}

  public static OffsetDateTime offset(Object v) {
    if (v == null) return null;
    if (v instanceof OffsetDateTime o) return o;
    if (v instanceof Timestamp t) return t.toInstant().atOffset(ZoneOffset.UTC);
    if (v instanceof java.time.Instant i) return i.atOffset(ZoneOffset.UTC);
    throw new IllegalArgumentException("unsupported time value: " + v.getClass());
  }
}
