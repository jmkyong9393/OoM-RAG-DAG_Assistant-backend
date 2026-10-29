-- 일괄 등록(POST /logs/bulk)으로 만든 간선의 출처 구분. manual(사용자) · recommended(추천 수락) · bulk(일괄 적재)
ALTER TABLE log_edges DROP CONSTRAINT log_edges_origin_check;
ALTER TABLE log_edges ADD CONSTRAINT log_edges_origin_check CHECK (origin IN ('manual', 'recommended', 'bulk'));
