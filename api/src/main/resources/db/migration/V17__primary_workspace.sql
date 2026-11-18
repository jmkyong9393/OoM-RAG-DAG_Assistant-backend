-- 메인 부서 지정(REQ-7) — 여러 부서에 속한 사람이 로그인 뒤 처음 열릴 부서를 고른다(PATCH /me 의 primary_workspace_id).
-- GET /me 의 workspace_id 는 이 값이 지금도 본인이 속한 부서일 때만 따르고, 아니면 계산값(관리자인 부서 먼저, 그다음 번호순)이다.
-- 그래서 부서에서 제외돼도 이 칸을 지울 필요가 없다. 부서가 지워지면 비운다.
-- 권한: users 는 V2 에서 oom_api 에 테이블 단위 UPDATE 를 주었으므로 새 칸에도 적용된다.
ALTER TABLE users ADD COLUMN primary_workspace_id bigint REFERENCES workspaces(id) ON DELETE SET NULL;
