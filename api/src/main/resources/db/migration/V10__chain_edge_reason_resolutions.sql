-- 사례 체인 기록 구조 보완 1단계: 간선의 연결 사유, 장애 건을 실제로 해결한 기록.

-- 이 기록을 왜 그 상위에 이어 붙였는지 한 줄 (선택).
ALTER TABLE log_edges ADD COLUMN IF NOT EXISTS reason text;
ALTER TABLE log_edges DROP CONSTRAINT IF EXISTS log_edges_reason_len;
ALTER TABLE log_edges ADD CONSTRAINT log_edges_reason_len CHECK (reason IS NULL OR char_length(reason) <= 300);

-- 장애 건(fail 루트) → 그 건을 해결한 success 기록. 원인마다 해결이 따로 붙는 체인이 많아 여러 개를 둔다.
-- 지금 유효한지(활성 · 그 루트의 자손)는 저장 시점이 아니라 resolved_nodes() 가 읽을 때 판정한다 — 폐기·연결 해제 뒤 복원돼도 다시 맞는다.
CREATE TABLE IF NOT EXISTS log_resolutions (
  workspace_id bigint NOT NULL REFERENCES workspaces(id),
  root_id      bigint NOT NULL REFERENCES log_nodes(id) ON DELETE CASCADE,
  node_id      bigint NOT NULL REFERENCES log_nodes(id) ON DELETE CASCADE,
  created_by   bigint REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (root_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_log_resolutions_node ON log_resolutions (node_id);

CREATE OR REPLACE FUNCTION log_resolutions_validate() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r log_nodes%ROWTYPE; n log_nodes%ROWTYPE;
BEGIN
  SELECT * INTO r FROM log_nodes WHERE id = NEW.root_id;
  SELECT * INTO n FROM log_nodes WHERE id = NEW.node_id;
  IF r.workspace_id <> NEW.workspace_id OR n.workspace_id <> NEW.workspace_id THEN
    RAISE EXCEPTION 'RESOLUTION_CROSS_WORKSPACE' USING ERRCODE = 'check_violation';
  END IF;
  IF r.type <> 'fail' OR n.type <> 'success' THEN
    RAISE EXCEPTION 'RESOLUTION_TYPE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_log_resolutions_validate ON log_resolutions;
CREATE TRIGGER trg_log_resolutions_validate BEFORE INSERT OR UPDATE ON log_resolutions
  FOR EACH ROW EXECUTE FUNCTION log_resolutions_validate();

GRANT SELECT ON log_resolutions TO oom_api, oom_ai;
GRANT INSERT, DELETE ON log_resolutions TO oom_api;
