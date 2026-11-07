-- 사례 체인 기록 구조 2단계: 장애 건의 원인 표시.
-- category 는 V1 부터 있던 빈 컬럼이다. 지금 값은 'cause'(장애 건의 원인) 하나이고 fail 기록에만 쓴다.
-- 상위가 있어야 원인이라는 조건은 CHECK 로 못 걸어 API 가 등록 때 확인하고, 읽을 때 v_log_nodes.is_cause 가 다시 본다.
ALTER TABLE log_nodes ADD CONSTRAINT log_nodes_category_chk
  CHECK (category IS NULL OR (category = 'cause' AND type = 'fail'));
