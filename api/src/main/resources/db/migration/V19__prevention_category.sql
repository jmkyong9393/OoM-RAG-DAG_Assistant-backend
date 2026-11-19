-- 재발 방지 · 후속 조치 표시: category 에 'prevention' 을 더한다. success 기록에만 붙는다(원인 표시 'cause' 는 fail 기록에만).
-- 해결 기록 지정은 log_resolutions 에 따로 있어, 한 기록이 해결이면서 재발 방지일 수 있다.
ALTER TABLE log_nodes DROP CONSTRAINT log_nodes_category_chk;
ALTER TABLE log_nodes ADD CONSTRAINT log_nodes_category_chk
  CHECK (category IS NULL OR (category = 'cause' AND type = 'fail') OR (category = 'prevention' AND type = 'success'));
