-- 역할 2개. 쓰기 소유를 DB 권한으로 강제한다 (02 v3 §1-3, 04 §3.4).
-- oom_api = Spring, oom_ai = Python. 비밀번호는 Flyway placeholder로 주입.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oom_api') THEN
    CREATE ROLE oom_api LOGIN PASSWORD '${oom_api_password}';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'oom_ai') THEN
    CREATE ROLE oom_ai LOGIN PASSWORD '${oom_ai_password}';
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO oom_api, oom_ai;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO oom_api, oom_ai;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO oom_api, oom_ai;
-- 이후 생성되는 뷰·함수(R__ 마이그레이션)에도 자동 적용
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO oom_api, oom_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO oom_api, oom_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO oom_api, oom_ai;

-- ── Spring (oom_api): 업무 데이터 쓰기. DELETE는 간선·피드백·멤버십만 ───────
GRANT INSERT, UPDATE ON tenants, workspaces, users, workspace_members, log_edges,
  policy_documents, search_sessions, answer_feedback, workspace_terms, eval_runs, audit_log TO oom_api;
GRANT DELETE ON log_edges, answer_feedback, workspace_members TO oom_api;
GRANT INSERT ON log_nodes TO oom_api;
GRANT UPDATE (status, title, body, category, tags, attributes,
              last_confirmed_at, deprecated_at, deprecated_via, deprecate_reason,
              resolved_at, resolve_note, refine_status, refine_attempts, refine_error, updated_at)
  ON log_nodes TO oom_api;

-- ── Python (oom_ai): 정제 컬럼·인덱싱 결과·사전만 ───────────────────────────
GRANT UPDATE (error_code, keywords, resolution_summary, tokens, embedding, embedding_model,
              refine_status, refine_attempts, refine_error, updated_at)
  ON log_nodes TO oom_ai;
GRANT INSERT, UPDATE ON policy_documents TO oom_ai;
GRANT INSERT, UPDATE, DELETE ON policy_chunks TO oom_ai;      -- 재인덱싱 시 청크 교체
GRANT INSERT ON workspace_terms, audit_log TO oom_ai;
