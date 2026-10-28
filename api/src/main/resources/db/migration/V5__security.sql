-- 규제 가이드(개인정보 기술적·관리적 보호조치 §4 접근통제·§6 암호화) 반영 — 개발문서 35.
-- 로그인 ID 는 사번(employee_no). 이메일은 연락처 속성으로만 남긴다.

ALTER TABLE users
  ADD COLUMN employee_no         text,                               -- 사번. 회사 코드 접두 + 번호 (예: OOM0002)
  ADD COLUMN failed_attempts     int NOT NULL DEFAULT 0,             -- 연속 로그인 실패 횟수 (성공 시 0)
  ADD COLUMN locked_until        timestamptz,                        -- 5회 실패 시 15분 잠금
  ADD COLUMN password_changed_at timestamptz,                        -- 비밀번호 유효기간(180일) 계산 기준
  ADD COLUMN consented_at        timestamptz,                        -- 개인정보 수집·이용 동의 시각 (첫 로그인)
  ADD CONSTRAINT users_employee_no_tenant_uq UNIQUE (tenant_id, employee_no);

-- 로그인은 사번만으로 하므로 사번은 전역 유일(회사 코드 접두사가 이를 실질적으로 보장). 이메일 전역 유일은 해제.
CREATE UNIQUE INDEX users_employee_no_lower_uidx ON users (lower(employee_no)) WHERE employee_no IS NOT NULL;
DROP INDEX IF EXISTS users_email_lower_uidx;

-- 접속기록: audit_log.table_name = 'auth', action ∈ login_success | login_failed | account_locked | logout | password_changed | consent
-- (별도 테이블 대신 기존 감사 로그 재사용. actor = 사번, reason = 접속 IP)
