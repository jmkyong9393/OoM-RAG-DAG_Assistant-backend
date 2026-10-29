-- OoM Assist 스키마 V1. 근거: 개발문서 02 v3 §1-3, 33 §1~2, 34 §7.
-- 격리 키는 workspace_id 하나. 테넌트는 워크스페이스의 상위 그룹.
-- 열거형은 ENUM 대신 text + CHECK (Flyway 트랜잭션 안에서 값 추가 가능).

CREATE EXTENSION IF NOT EXISTS vector;

-- ── 조직 ──────────────────────────────────────────────────────────────────
CREATE TABLE tenants (
  id          bigserial PRIMARY KEY,
  slug        text NOT NULL UNIQUE,
  name        text NOT NULL,
  settings    jsonb NOT NULL DEFAULT '{}'::jsonb,   -- llm_profile 등
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE workspaces (
  id             bigserial PRIMARY KEY,
  tenant_id      bigint NOT NULL REFERENCES tenants(id),
  parent_id      bigint REFERENCES workspaces(id),       -- 조직 계층. 규정 검색 범위 = 자기 + 조상
  kind           text NOT NULL DEFAULT 'dept' CHECK (kind IN ('company', 'division', 'dept')),
  name           text NOT NULL,
  policy_enabled boolean NOT NULL DEFAULT false,         -- true: 규정 RAG + DAG / false: DAG만
  settings       jsonb NOT NULL DEFAULT '{}'::jsonb,     -- labels, language, llm_profile
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            bigserial PRIMARY KEY,
  tenant_id     bigint NOT NULL REFERENCES tenants(id),
  name          text NOT NULL,
  title         text,
  email         text,
  external_id   text,                                     -- SSO 매핑 자리 (v1.1)
  auth_provider text,
  status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, email),                              -- NULL 이메일은 중복 허용 (데모 계정)
  UNIQUE (auth_provider, external_id)
);

-- 조직 계층 무결성: 부모는 같은 테넌트, 자기 자신 금지
CREATE FUNCTION workspaces_validate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.parent_id IS NOT NULL THEN
    IF NEW.parent_id = NEW.id THEN RAISE EXCEPTION 'WORKSPACE_SELF_PARENT' USING ERRCODE = 'check_violation'; END IF;
    IF (SELECT tenant_id FROM workspaces WHERE id = NEW.parent_id) <> NEW.tenant_id THEN
      RAISE EXCEPTION 'WORKSPACE_CROSS_TENANT' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_workspaces_validate BEFORE INSERT OR UPDATE ON workspaces
  FOR EACH ROW EXECUTE FUNCTION workspaces_validate();

-- updated_at 자동 갱신 (log_nodes, answer_feedback)
CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$;

CREATE TABLE workspace_members (
  workspace_id bigint NOT NULL REFERENCES workspaces(id),
  user_id      bigint NOT NULL REFERENCES users(id),
  role         text NOT NULL DEFAULT 'member' CHECK (role IN ('viewer', 'member', 'admin')),
  joined_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

-- ── 업무 로그 (DAG 정점) ───────────────────────────────────────────────────
CREATE TABLE log_nodes (
  id                 bigserial PRIMARY KEY,
  workspace_id       bigint NOT NULL REFERENCES workspaces(id),
  type               text NOT NULL CHECK (type IN ('fail', 'success', 'ref')),
  status             text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deprecated')),
  title              text NOT NULL,
  body               text NOT NULL,
  author_id          bigint NOT NULL REFERENCES users(id),
  category           text,
  tags               text[] NOT NULL DEFAULT '{}',
  attributes         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- 부서별 고유 필드
  -- 정제 결과 (Python 소유)
  error_code         text,                                 -- 식별 코드: 에러·민원·조항 번호
  keywords           text[] NOT NULL DEFAULT '{}',
  resolution_summary text,
  tokens             text,                                 -- kiwi 형태소, 공백 연결
  tsv                tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(tokens, ''))) STORED,
  embedding          vector(1536),
  embedding_model    text,
  refine_status      text NOT NULL DEFAULT 'pending' CHECK (refine_status IN ('pending', 'processing', 'done', 'failed')),
  refine_attempts    int NOT NULL DEFAULT 0,
  refine_error       text,
  -- 수명·상태 (Spring 소유)
  last_confirmed_at  timestamptz,
  deprecated_at      timestamptz,
  deprecated_via     bigint REFERENCES log_nodes(id),      -- 상위 연쇄 폐기 시 그 상위 id
  deprecate_reason   text,
  resolved_at        timestamptz,                          -- 루트(장애 건)에만 의미
  resolve_note       text,                                 -- 활성 success 없이 완료할 때 사유
  source             text NOT NULL DEFAULT 'ui' CHECK (source IN ('ui', 'bulk', 'synthetic')),
  source_ref         text,
  compose_ms         int,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_log_nodes_touch BEFORE UPDATE ON log_nodes
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE log_edges (
  workspace_id bigint NOT NULL REFERENCES workspaces(id),
  parent_id    bigint NOT NULL REFERENCES log_nodes(id),   -- 선행: 실패·원인
  child_id     bigint NOT NULL REFERENCES log_nodes(id),   -- 후속: 원인·해결
  created_by   bigint REFERENCES users(id),
  origin       text NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'recommended')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (parent_id, child_id),
  CHECK (parent_id <> child_id)
);

-- 간선 무결성: 같은 워크스페이스, success의 부모는 fail/ref만. 사이클은 would_create_cycle()로 서비스가 검사.
CREATE FUNCTION log_edges_validate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p log_nodes%ROWTYPE; c log_nodes%ROWTYPE;
BEGIN
  SELECT * INTO p FROM log_nodes WHERE id = NEW.parent_id;
  SELECT * INTO c FROM log_nodes WHERE id = NEW.child_id;
  IF p.workspace_id <> c.workspace_id OR p.workspace_id <> NEW.workspace_id THEN
    RAISE EXCEPTION 'EDGE_CROSS_WORKSPACE' USING ERRCODE = 'check_violation';
  END IF;
  IF c.type = 'success' AND p.type NOT IN ('fail', 'ref') THEN
    RAISE EXCEPTION 'EDGE_PARENT_TYPE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_log_edges_validate BEFORE INSERT OR UPDATE ON log_edges
  FOR EACH ROW EXECUTE FUNCTION log_edges_validate();

-- ── 규정 문서 (RAG) ───────────────────────────────────────────────────────
CREATE TABLE policy_documents (
  id              bigserial PRIMARY KEY,
  workspace_id    bigint NOT NULL REFERENCES workspaces(id),
  slug            text NOT NULL,                           -- 청크 ID 접두어
  title           text NOT NULL,
  version         text NOT NULL,
  doc_type        text,                                    -- 규정·지침·FAQ·매뉴얼·약관
  tags            text[] NOT NULL DEFAULT '{}',
  source_type     text NOT NULL CHECK (source_type IN ('pdf', 'hwp', 'markdown', 'json')),
  file_path       text,                                    -- NULL = 원문 파일 없음
  source_hash     text,                                    -- 동일 파일 재적재 스킵
  effective_from  date,
  effective_to    date,
  meta            jsonb NOT NULL DEFAULT '{}'::jsonb,
  status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
  retired_at      timestamptz,
  retire_reason   text,
  retired_by      text,                                    -- 사람 이름 또는 'script'
  uploaded_by     text NOT NULL DEFAULT 'script',
  uploaded_at     timestamptz NOT NULL DEFAULT now(),
  embedding_model text,
  indexed_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (slug)        -- 청크 ID가 '{slug}-…'로 전역 PK이므로 slug도 전역 유일. SaaS에서는 인덱싱 스크립트가 '{tenant}-{doc}' 형식으로 생성
);

CREATE TABLE policy_chunks (
  id            text PRIMARY KEY,                          -- '{slug}-a{조항}-{seq}', 결정적
  doc_id        bigint NOT NULL REFERENCES policy_documents(id) ON DELETE CASCADE,
  workspace_id  bigint NOT NULL REFERENCES workspaces(id), -- 비정규화 (RLS·검색 범위)
  article_label text NOT NULL,                             -- '제12조', 'Q18', '3.2절'
  article_title text,
  page          int,
  bboxes        jsonb,                                     -- 사각형 목록. 원문 하이라이트용
  text          text NOT NULL,
  tokens        text,
  tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(tokens, ''))) STORED,
  embedding     vector(1536),
  meta          jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- 비정규화한 workspace_id는 문서의 것과 같아야 한다. NULL이면 문서에서 채운다.
CREATE FUNCTION policy_chunks_validate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc_ws bigint;
BEGIN
  SELECT workspace_id INTO doc_ws FROM policy_documents WHERE id = NEW.doc_id;
  IF NEW.workspace_id IS NULL THEN NEW.workspace_id := doc_ws;
  ELSIF NEW.workspace_id <> doc_ws THEN
    RAISE EXCEPTION 'CHUNK_WORKSPACE_MISMATCH' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER trg_policy_chunks_validate BEFORE INSERT OR UPDATE ON policy_chunks
  FOR EACH ROW EXECUTE FUNCTION policy_chunks_validate();

-- ── 어시스턴트 세션 ────────────────────────────────────────────────────────
CREATE TABLE search_sessions (
  id                bigserial PRIMARY KEY,
  workspace_id      bigint NOT NULL REFERENCES workspaces(id),
  user_id           bigint REFERENCES users(id),
  query             text NOT NULL,
  intent            text CHECK (intent IN ('policy', 'troubleshoot', 'both')),   -- dag_only 모드는 'troubleshoot' 고정
  mode              text NOT NULL CHECK (mode IN ('policy', 'dag_only')),
  status            text NOT NULL DEFAULT 'streaming' CHECK (status IN ('streaming', 'done', 'error')),  -- 중단된 세션을 "근거 없음"과 구분
  answer_text       text,                                  -- token 누적. done 시 UPDATE
  policy_refs       jsonb,                                 -- policy 이벤트 payload
  chain_ref         jsonb,                                 -- chain 이벤트 payload
  retrieved_ids     text[] NOT NULL DEFAULT '{}',          -- 'c:{chunk_id}' / 'n:{node_id}'
  verified_ids      text[] NOT NULL DEFAULT '{}',
  unverified_ids    text[] NOT NULL DEFAULT '{}',
  retrieval_ms      int,
  first_token_ms    int,
  total_ms          int,
  model             text,
  prompt_tokens     int,
  completion_tokens int,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answer_feedback (
  id         bigserial PRIMARY KEY,
  session_id bigint NOT NULL UNIQUE REFERENCES search_sessions(id),
  score      smallint NOT NULL CHECK (score IN (1, -1)),
  comment    text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_answer_feedback_touch BEFORE UPDATE ON answer_feedback
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ── 부속 ──────────────────────────────────────────────────────────────────
CREATE TABLE workspace_terms (                             -- kiwi 사용자 사전
  workspace_id bigint NOT NULL REFERENCES workspaces(id),
  term         text NOT NULL,
  source       text NOT NULL DEFAULT 'auto' CHECK (source IN ('auto', 'manual')),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, term)
);

CREATE TABLE eval_runs (                                    -- 오프라인 평가 결과
  id                bigserial PRIMARY KEY,
  workspace_id      bigint REFERENCES workspaces(id),
  run_at            timestamptz NOT NULL DEFAULT now(),
  dataset_version   text NOT NULL,
  mode              text,
  is_sample         boolean NOT NULL DEFAULT false,
  faithfulness      double precision,
  answer_relevance  double precision,
  context_precision double precision,
  context_recall    double precision,
  hit_at_5          double precision,
  n_samples         int,
  meta              jsonb NOT NULL DEFAULT '{}'::jsonb      -- search_time 등 실험값
);

CREATE TABLE audit_log (
  id           bigserial PRIMARY KEY,
  tenant_id    bigint REFERENCES tenants(id),
  workspace_id bigint REFERENCES workspaces(id),
  table_name   text NOT NULL,                              -- log_nodes | policy_documents | log_edges
  row_id       text NOT NULL,
  action       text NOT NULL,                              -- uploaded|indexed|superseded|retired|restored|deleted|deprecated|resolved|unresolved
  actor        text NOT NULL,                              -- 사용자 이름 또는 'script'
  source       text NOT NULL DEFAULT 'ui' CHECK (source IN ('ui', 'script')),
  reason       text,
  label        text,                                       -- 비정규화 표시명 ('제목 v2.0')
  before       jsonb,
  at           timestamptz NOT NULL DEFAULT now()
);
