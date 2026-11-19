package com.oom.assist.auth;

import java.io.Serial;
import java.io.Serializable;
import java.util.Collection;
import java.util.List;
import org.springframework.security.core.CredentialsContainer;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.userdetails.UserDetails;

/**
 * 세션에 저장되는 로그인 주체. 로그인 ID 는 사번(employee_no). 워크스페이스 권한(admin/member)은 여기 없고
 * 요청마다 X-Workspace-Id 로 workspace_members 를 본다(TenantContext). 인증이 끝나면 ProviderManager 가 eraseCredentials() 를 불러
 * 비밀번호 해시는 세션에 남지 않는다. authenticatedHash 는 로그인 요청 안에서만 쓰는 사본이다(transient — 세션에 저장되지 않는다):
 * 로그인의 끝에서 "인증에 쓴 해시가 지금도 그 계정의 해시인가"를 다시 확인하는 데 한 번 쓰고 지운다(AuthController.login).
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
  private transient String authenticatedHash;

  public AppPrincipal(long id, long tenantId, String employeeNo, String name, boolean active, boolean locked, String passwordHash) {
    this.id = id;
    this.tenantId = tenantId;
    this.employeeNo = employeeNo;
    this.name = name;
    this.active = active;
    this.locked = locked;
    this.passwordHash = passwordHash;
    this.authenticatedHash = passwordHash;
  }

  public long id() { return id; }
  public long tenantId() { return tenantId; }
  public String employeeNo() { return employeeNo; }
  public String name() { return name; }

  /** 인증에 쓴 비밀번호 해시를 한 번 꺼내고 지운다. */
  public String takeAuthenticatedHash() {
    String hash = authenticatedHash;
    authenticatedHash = null;
    return hash;
  }

  @Override public String getUsername() { return employeeNo; }
  @Override public String getPassword() { return passwordHash; }
  @Override public Collection<? extends GrantedAuthority> getAuthorities() { return List.of(new SimpleGrantedAuthority("ROLE_USER")); }
  @Override public boolean isEnabled() { return active; }
  @Override public boolean isAccountNonLocked() { return !locked; }
  @Override public void eraseCredentials() { this.passwordHash = null; }

  @Override public String toString() { return "AppPrincipal[" + id + " " + employeeNo + "]"; }
}
