-- 장애 보고서(템플릿 R1) — 장애 건(루트) 하나를 보고서 모양(content)으로 저장한다. 공개 범위마다 최신 한 건 — 새 보고서가 done 이 되면 같은 장애 건 · 공개 범위의 이전 행을 지운다.
-- 사실 칸(경과 · 원인 · 조치 · 규정 · 근거 기록)은 Spring 이 DB 에서 조립해 넣고, AI 칸(한눈에 보기 · 영향 · 원인 설명 · 재발 방지 · 남은 확인)은
-- AI 워커가 generating 행을 SKIP LOCKED 로 집어 채운다(규정 업로드와 같은 방식). 실패해도 사실 칸은 남는다.
CREATE TABLE incident_reports (
  id                bigserial PRIMARY KEY,
  workspace_id      bigint NOT NULL REFERENCES workspaces(id),
  root_id           bigint NOT NULL REFERENCES log_nodes(id) ON DELETE CASCADE,
  audience          text NOT NULL CHECK (audience IN ('internal', 'external')),   -- external = 작성자 · 보고자를 뺀 공개판(부서 관리자만 생성)
  status            text NOT NULL DEFAULT 'generating' CHECK (status IN ('generating', 'done', 'failed')),
  template_version  text NOT NULL DEFAULT 'R1',
  sig               text NOT NULL,             -- 사실 칸의 지문 — 지금 장애 건으로 다시 조립한 지문과 다르면 stale(기록이 바뀌었다)
  content           jsonb NOT NULL,            -- 프론트 lib/types/report.ts 의 ReportContent
  prompt_version    text,                      -- AI 칸을 만든 지시문 판 (AI 칸을 만들지 않았으면 NULL)
  model             text,
  prompt_tokens     int,
  completion_tokens int,
  error             text,                      -- failed 일 때 원문(확인용)
  requested_by      bigint NOT NULL REFERENCES users(id),
  created_at        timestamptz NOT NULL DEFAULT now(),
  generated_at      timestamptz                -- done · failed 가 된 시각
);
CREATE INDEX idx_incident_reports_root ON incident_reports (root_id, audience, id DESC);
CREATE INDEX idx_incident_reports_queue ON incident_reports (id) WHERE status = 'generating';

GRANT INSERT, DELETE ON incident_reports TO oom_api;   -- DELETE = AI 를 끈 설정에서 바로 done 으로 넣을 때 이전 행 정리
GRANT UPDATE, DELETE ON incident_reports TO oom_ai;    -- DELETE = done 으로 바꿀 때 이전 행 정리
