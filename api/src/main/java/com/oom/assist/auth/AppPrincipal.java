package com.oom.assist.auth;

import java.io.Serial;
import java.io.Serializable;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Collection;
import java.util.HexFormat;
import java.util.List;
import org.springframework.security.core.CredentialsContainer;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.userdetails.UserDetails;

/**
 * 세션에 저장되는 로그인 주체. 로그인 ID 는 사번(employee_no). 워크스페이스 권한(admin/member)은 여기 없고
 * 요청마다 X-Workspace-Id 로 workspace_members 를 본다(TenantContext). 인증이 끝나면 ProviderManager 가 eraseCredentials() 를 불러
 * 비밀번호 해시는 세션에 남지 않는다. 대신 그 해시의 지문(credential)이 남아 세션을 로그인에 쓴 비밀번호에 묶는다 —
 * 비밀번호가 바뀌면(본인 변경 · 재발급) 그 전의 세션은 다음 요청에서 끝난다(AccountGate).
 */
public final class AppPrincipal implements UserDetails, CredentialsContainer, Serializable {
  @Serial private static final long serialVersionUID = 2L;

  private final long id;
  private final long tenantId;
  private final String employeeNo;
  private final String name;
  private final boolean active;
  private final boolean locked;
  private String passwordHash;
  private final String credential;

  public AppPrincipal(long id, long tenantId, String employeeNo, String name, boolean active, boolean locked, String passwordHash) {
    this.id = id;
    this.tenantId = tenantId;
    this.employeeNo = employeeNo;
    this.name = name;
    this.active = active;
    this.locked = locked;
    this.passwordHash = passwordHash;
    this.credential = fingerprint(passwordHash);
  }

  public long id() { return id; }
  public long tenantId() { return tenantId; }
  public String employeeNo() { return employeeNo; }
  public String name() { return name; }

  /** 이 세션이 그 비밀번호 해시로 로그인한 것인가. 지문이 없는 세션(이 칸이 생기기 전의 것)은 거짓이다. */
  public boolean boundTo(String currentHash) {
    return credential != null && credential.equals(fingerprint(currentHash));
  }

  /** 같은 사람을 새 비밀번호 해시에 묶은 주체 — 본인이 비밀번호를 바꾼 세션을 이어 가는 데 쓴다. */
  public AppPrincipal reboundTo(String newHash) {
    AppPrincipal p = new AppPrincipal(id, tenantId, employeeNo, name, active, locked, newHash);
    p.eraseCredentials();
    return p;
  }

  private static String fingerprint(String hash) {
    if (hash == null) return null;
    try {
      return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(hash.getBytes(StandardCharsets.UTF_8)));
    } catch (NoSuchAlgorithmException e) {
      throw new IllegalStateException(e);
    }
  }

  @Override public String getUsername() { return employeeNo; }
  @Override public String getPassword() { return passwordHash; }
  @Override public Collection<? extends GrantedAuthority> getAuthorities() { return List.of(new SimpleGrantedAuthority("ROLE_USER")); }
  @Override public boolean isEnabled() { return active; }
  @Override public boolean isAccountNonLocked() { return !locked; }
  @Override public void eraseCredentials() { this.passwordHash = null; }

  @Override public String toString() { return "AppPrincipal[" + id + " " + employeeNo + "]"; }
}
