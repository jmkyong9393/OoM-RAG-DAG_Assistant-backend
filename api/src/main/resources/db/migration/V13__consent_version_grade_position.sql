-- 처리방침 판 단위 동의 기록, 직함을 직급 · 직책으로 나눔 (처리방침 v2).
-- 동의는 "어느 판에" 동의했는지 남긴다. users.consented_at 은 첫 동의 시각으로 그대로 둔다.
CREATE TABLE user_consents (
  user_id        bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  policy_version text   NOT NULL,
  consented_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, policy_version)
);
GRANT SELECT, INSERT ON user_consents TO oom_api;

-- 지금까지의 동의는 직함을 수집하던 v1 문안에 대한 것이다
INSERT INTO user_consents (user_id, policy_version, consented_at)
SELECT id, 'v1', consented_at FROM users WHERE consented_at IS NOT NULL;

ALTER TABLE users ADD COLUMN grade text, ADD COLUMN duty text;   -- duty = 직책 (position 은 PostgreSQL 함수 이름과 겹친다)

-- 직함 한 칸을 직급 · 직책으로 나누는 규칙(이전 · 초대 · 시드가 같은 규칙을 쓴다). 직책 목록에 없으면 직급으로 본다
CREATE FUNCTION title_parts(p_title text, OUT grade text, OUT duty text) LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN t IS NULL OR t = ANY (ARRAY['팀장','파트장','본부장','실장','센터장','그룹장','셀장','지점장','점장','부서장','사업부장','리더']) THEN NULL ELSE t END,
         CASE WHEN t = ANY (ARRAY['팀장','파트장','본부장','실장','센터장','그룹장','셀장','지점장','점장','부서장','사업부장','리더']) THEN t END
  FROM (SELECT nullif(btrim(p_title), '') AS t) s
$$;

UPDATE users SET (grade, duty) = (SELECT p.grade, p.duty FROM title_parts(title) p) WHERE title IS NOT NULL;
