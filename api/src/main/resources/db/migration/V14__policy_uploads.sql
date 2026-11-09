-- 규정 파일 업로드 → 자동 변환 → 검수 → 등록. 원본 · 검수한 MD 파일은 저장소(UPLOADS_DIR)에, 상태 · 변환본 · 미리보기는 이 표에.
-- 변환(converting)과 등록(indexing)은 AI 워커가 SKIP LOCKED 로 집어 간다.
CREATE TABLE policy_uploads (
  id             bigserial PRIMARY KEY,
  workspace_id   bigint NOT NULL REFERENCES workspaces(id),          -- 등록될 문서의 워크스페이스(부서 또는 전사 공통)
  status         text NOT NULL DEFAULT 'converting'
                 CHECK (status IN ('converting', 'review', 'indexing', 'approved', 'rejected', 'failed')),
  original_name  text NOT NULL,
  format         text NOT NULL CHECK (format IN ('pdf', 'hwp', 'hwpx', 'docx', 'md', 'txt')),
  size_bytes     bigint NOT NULL,
  sha256         text NOT NULL,
  original_path  text NOT NULL,                                      -- 저장소 기준 상대 경로
  markdown       text,                                               -- 변환본(검수 중 고친 것)
  markdown_path  text,                                               -- 승인 때 보관한 검수본(저장소 기준)
  report         jsonb NOT NULL DEFAULT '{}'::jsonb,                 -- 쪽별 추출 방법 · OCR 쪽 · 조문 미리보기 · 경고
  slug           text NOT NULL,
  title          text NOT NULL,
  version        text NOT NULL,
  doc_type       text,
  supersedes     text,                                               -- 대체할 옛 판 slug
  error          text,
  uploaded_by    bigint NOT NULL REFERENCES users(id),
  reviewed_by    bigint REFERENCES users(id),
  reviewed_at    timestamptz,
  reject_reason  text,
  document_id    bigint REFERENCES policy_documents(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_policy_uploads_ws ON policy_uploads (workspace_id, created_at DESC);
CREATE INDEX idx_policy_uploads_queue ON policy_uploads (status, id) WHERE status IN ('converting', 'indexing');

GRANT INSERT, UPDATE ON policy_uploads TO oom_api;
GRANT UPDATE ON policy_uploads TO oom_ai;

-- 검수 승인자(업로드 경로로 등록된 문서). 스크립트 적재는 null
ALTER TABLE policy_documents ADD COLUMN approved_by text;
