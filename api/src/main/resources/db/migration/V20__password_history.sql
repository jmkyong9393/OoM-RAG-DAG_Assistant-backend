-- 비밀번호 재사용 제한: 지금 비밀번호보다 앞서 쓴 것의 해시를 남겨, 새 비밀번호가 최근에 쓴 것과 같으면 거절한다(A → B → A 막기).
CREATE TABLE user_password_history (
  id            bigserial PRIMARY KEY,
  user_id       bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  password_hash text   NOT NULL,
  retired_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_user_password_history_user ON user_password_history (user_id, id DESC);

-- 계정을 삭제할 때 함께 지운다(자리 표시 행은 남으므로 CASCADE 가 일하지 않는다)
GRANT SELECT, INSERT, DELETE ON user_password_history TO oom_api;
-- AI 쪽은 볼 일이 없다(V2 의 기본 권한이 새 테이블에 SELECT 를 준다)
REVOKE ALL ON user_password_history FROM oom_ai;
