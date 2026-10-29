-- 규정 청크 어휘 가중. 문서 제목 토큰은 모든 청크에 들어가 동점·잡음을 만든다(제목 4어절이 본문 1어절 적중을 이긴다).
-- 제목은 별도 열에 두고 D 가중(0.1), 조문 라벨+본문은 A(1.0). 워커가 두 열을 채운다.
ALTER TABLE policy_chunks ADD COLUMN title_tokens text;
ALTER TABLE policy_chunks DROP COLUMN tsv;
ALTER TABLE policy_chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
  setweight(to_tsvector('simple', coalesce(tokens, '')), 'A') ||
  setweight(to_tsvector('simple', coalesce(title_tokens, '')), 'D')) STORED;
CREATE INDEX idx_policy_chunks_tsv ON policy_chunks USING gin (tsv);
-- 기존 tokens 에는 제목이 섞여 있으므로 비워서 워커가 다시 채우게 한다 (임베딩은 유지)
UPDATE policy_chunks SET tokens = NULL;
