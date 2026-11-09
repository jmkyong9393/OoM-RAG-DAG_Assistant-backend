-- 사이클을 만드는 간선을 DB 에서도 거부한다(API 를 거치지 않는 적재 · 직접 SQL 포함). would_create_cycle() 은 R__functions.sql.
-- API 와 같은 부서 단위 advisory lock 으로 동시 삽입 두 건이 서로를 못 보고 사이클을 만드는 틈을 닫는다.
CREATE OR REPLACE FUNCTION log_edges_no_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(NEW.workspace_id);
  IF NEW.parent_id <> NEW.child_id AND would_create_cycle(NEW.parent_id, NEW.child_id) THEN   -- 자기 자신은 CHECK 가 막는다
    RAISE EXCEPTION 'EDGE_CYCLE' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS trg_log_edges_no_cycle ON log_edges;
CREATE TRIGGER trg_log_edges_no_cycle BEFORE INSERT OR UPDATE OF parent_id, child_id ON log_edges
  FOR EACH ROW EXECUTE FUNCTION log_edges_no_cycle();
