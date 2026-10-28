package com.oom.assist.auth;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.core.userdetails.UsernameNotFoundException;
import org.springframework.stereotype.Service;

/** users 테이블에서 사번으로 로그인 주체를 읽는다. password_hash 가 NULL 인 계정(초대 전·SSO 전용)은 로그인 불가. 잠금 상태는 locked_until 로 판단. */
@Service
public class DbUserDetailsService implements UserDetailsService {

  private final JdbcClient jdbc;

  public DbUserDetailsService(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  @Override
  public UserDetails loadUserByUsername(String employeeNo) throws UsernameNotFoundException {
    return jdbc.sql("""
          SELECT id, tenant_id, employee_no, name, status = 'active' AS active,
                 (locked_until IS NOT NULL AND locked_until > now()) AS locked, password_hash
          FROM users WHERE lower(employee_no) = lower(?) AND password_hash IS NOT NULL
          """).param(employeeNo == null ? "" : employeeNo.trim())
        .query((rs, i) -> new AppPrincipal(rs.getLong("id"), rs.getLong("tenant_id"), rs.getString("employee_no"), rs.getString("name"),
            rs.getBoolean("active"), rs.getBoolean("locked"), rs.getString("password_hash")))
        .optional().orElseThrow(() -> new UsernameNotFoundException("no such user"));
  }
}
