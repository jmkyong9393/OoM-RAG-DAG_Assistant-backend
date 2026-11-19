-- 계정 삭제: 개인정보를 지우고 자리 표시 행만 남긴다. 작성한 기록이 작성자를 참조하므로(log_nodes.author_id 등) 행은 지우지 않는다.
ALTER TABLE users DROP CONSTRAINT users_status_check;
ALTER TABLE users ADD CONSTRAINT users_status_check CHECK (status IN ('active', 'inactive', 'deleted'));
ALTER TABLE users ADD COLUMN deleted_at timestamptz;

-- 삭제할 때 동의 이력도 지운다 (V13 은 SELECT · INSERT 만 주었다)
GRANT DELETE ON user_consents TO oom_api;
