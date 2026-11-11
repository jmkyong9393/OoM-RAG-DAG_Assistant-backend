-- 트러블슈팅 'AI 요약' — 장애 건(루트) 하나의 LLM 요약과 그 지문 · 실패 기록 · 생성 중 표시.
-- GET /chains 때 지문(요약에 영향을 주는 값의 md5)을 다시 계산해 summary_sig 와 같으면 저장된 요약을 쓰고,
-- 다르면 백그라운드로 Python /ai/chain-summary 를 불러 다시 만든다. log_nodes 에 두지 않는 이유: 저장할 때마다 trg_log_nodes_touch 가 루트의 updated_at 을 바꾼다.
-- 루트 행에서 바로 나오는 workspace_id 는 두지 않는다(중복 저장하면 어긋날 수 있다 — 부서 확인은 API 가 루트를 찾을 때 한다).
CREATE TABLE chain_summaries (
  root_id          bigint PRIMARY KEY REFERENCES log_nodes(id) ON DELETE CASCADE,
  sig              text NOT NULL,             -- 지금 지문. 실패 횟수 · 생성 중 표시가 이 지문 기준
  summary          text,                      -- 마지막으로 성공한 요약 (실패만 있으면 NULL)
  summary_sig      text,                      -- 그 요약을 만든 지문 — 지금 지문과 다르면 '이전 요약'
  summary_records  int,                       -- 그 요약을 만들 때의 기록 수
  created_at       timestamptz,               -- 요약을 만든 시각
  model            text,                      -- 그 요약을 만든 모델 · 토큰(비용 확인 · 모델 선택용, search_sessions 와 같은 이름)
  prompt_tokens    int,
  completion_tokens int,
  fail_count       int NOT NULL DEFAULT 0,    -- 지금 지문으로 형식 오류(AI_BAD_OUTPUT)가 난 횟수 (지문이 바뀌면 생성 중 표시를 할 때 0)
  error_kind       text,                      -- AI_TIMEOUT | AI_RATE_LIMIT | AI_BAD_OUTPUT | AI_ERROR
  last_error       text,                      -- 마지막 실패 원문 (확인용, 화면에 안 보인다)
  failed_at        timestamptz,               -- 마지막 실패 시각 — 실패 후 대기 판단
  generating_since timestamptz                -- 생성 중이면 시작 시각 — 같은 장애 건을 두 요청이 동시에 요약하지 않게
);

-- DELETE: 기록에 상위가 생기면(더 이상 루트가 아님) 그 번호의 요약 행을 지운다(LogRepository.insertEdge).
GRANT INSERT, UPDATE, DELETE ON chain_summaries TO oom_api;
