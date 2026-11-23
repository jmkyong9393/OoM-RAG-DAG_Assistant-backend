package com.oom.assist.auth;

import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

/**
 * 비밀번호 재사용 제한: 새 비밀번호는 최근에 쓴 RECENT 개(지금 것 포함)와 달라야 한다 — A → B → A 로 되돌리는 것을 막는다.
 * 지금 비밀번호는 users.password_hash 에, 그 전의 것은 user_password_history 에 해시로만 남긴다.
 */
@Service
public class PasswordHistory {

  public static final int RECENT = 3;

  private final JdbcClient jdbc;
  private final PasswordEncoder encoder;

  public PasswordHistory(JdbcClient jdbc, PasswordEncoder encoder) {
    this.jdbc = jdbc;
    this.encoder = encoder;
  }

  /** 지금 비밀번호보다 앞서 쓴 것 가운데 최근 RECENT - 1 개와 같은가. 지금 비밀번호와 같은지는 부르는 쪽이 따로 본다. */
  public boolean reused(long userId, String rawPassword) {
    List<String> hashes = jdbc.sql("SELECT password_hash FROM user_password_history WHERE user_id = ? ORDER BY id DESC LIMIT ?")
        .params(userId, RECENT - 1).query(String.class).list();
    return hashes.stream().anyMatch(h -> encoder.matches(rawPassword, h));
  }

  /** 바꾸기 직전의 해시를 이력에 넣고, 비교에 쓰지 않는 오래된 것은 지운다. */
  public void retire(long userId, String oldHash) {
    if (oldHash == null) return;
    jdbc.sql("INSERT INTO user_password_history (user_id, password_hash) VALUES (?, ?)").params(userId, oldHash).update();
    jdbc.sql("""
        DELETE FROM user_password_history WHERE user_id = ? AND id NOT IN
          (SELECT id FROM user_password_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)""").params(userId, userId, RECENT - 1).update();
  }
}
