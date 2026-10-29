-- 로그인(03 안건 G): 이메일+비밀번호(BCrypt) + 서버 세션(Spring Session JDBC). 데모용 user_id 선택 로그인 폐기.

ALTER TABLE users
  ADD COLUMN password_hash        text,                                   -- BCrypt. NULL 이면 로그인 불가(초대 전·SSO 전용)
  ADD COLUMN must_change_password boolean NOT NULL DEFAULT false,         -- 관리자 초대 직후 true
  ADD COLUMN last_login_at        timestamptz;

-- 로그인은 이메일만으로 하므로 이메일은 전역 유일 (테넌트별 UNIQUE 는 유지). 로그인 화면에 테넌트 선택이 생기면 완화.
CREATE UNIQUE INDEX users_email_lower_uidx ON users (lower(email)) WHERE email IS NOT NULL;

-- Spring Session JDBC 표준 스키마 (spring-session-jdbc schema-postgresql.sql). initialize-schema=never 로 두고 Flyway 가 소유.
CREATE TABLE spring_session (
  primary_id            char(36) NOT NULL,
  session_id            char(36) NOT NULL,
  creation_time         bigint NOT NULL,
  last_access_time      bigint NOT NULL,
  max_inactive_interval int NOT NULL,
  expiry_time           bigint NOT NULL,
  principal_name        varchar(100),
  CONSTRAINT spring_session_pk PRIMARY KEY (primary_id)
);
CREATE UNIQUE INDEX spring_session_ix1 ON spring_session (session_id);
CREATE INDEX spring_session_ix2 ON spring_session (expiry_time);
CREATE INDEX spring_session_ix3 ON spring_session (principal_name);

CREATE TABLE spring_session_attributes (
  session_primary_id char(36) NOT NULL,
  attribute_name     varchar(200) NOT NULL,
  attribute_bytes    bytea NOT NULL,
  CONSTRAINT spring_session_attributes_pk PRIMARY KEY (session_primary_id, attribute_name),
  CONSTRAINT spring_session_attributes_fk FOREIGN KEY (session_primary_id) REFERENCES spring_session (primary_id) ON DELETE CASCADE
);

GRANT SELECT, INSERT, UPDATE, DELETE ON spring_session, spring_session_attributes TO oom_api;
