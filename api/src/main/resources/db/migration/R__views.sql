-- 읽기 모델 뷰. 파생 필드(작성자명·루트·깊이·경로·청크 수·피드백)를 DB 한 곳에서 계산한다 (33 §5-1).
-- 무거운 컬럼(tokens·tsv·embedding)은 뷰에서 뺀다.
-- 다시 적용할 때마다 지우고 만든다: d.* · s.* 는 테이블에 컬럼이 늘면 순서가 바뀌어 CREATE OR REPLACE 가 실패한다.
-- 권한은 V2 의 기본 권한(ALTER DEFAULT PRIVILEGES)으로 다시 붙는다.
DROP VIEW IF EXISTS v_log_nodes, v_policy_documents, v_search_sessions;

-- 깊이 = 루트에서 가장 짧은 거리(단계 동기 BFS, bfs_levels 와 같은 방식 · 깊이 상한 없음).
-- 루트 · 경로는 노드마다 한 단계 위 부모 가운데 id 가 가장 작은 것 하나를 따라 정한다 — 경로를 모두 펼치지 않는다.
CREATE OR REPLACE VIEW v_log_nodes AS
WITH RECURSIVE lv(depth, frontier, seen) AS (
  SELECT 0, r.a, r.a FROM (SELECT array_agg(n.id) AS a FROM log_nodes n
                           WHERE NOT EXISTS (SELECT 1 FROM log_edges e WHERE e.child_id = n.id)) r
  WHERE r.a IS NOT NULL
  UNION ALL
  SELECT lv.depth + 1, nx.f, lv.seen || nx.f FROM lv,
    LATERAL (SELECT array_agg(c) AS f FROM (
      SELECT e.child_id AS c FROM log_edges e WHERE e.parent_id = ANY (lv.frontier)
      EXCEPT
      SELECT unnest(lv.seen)) s) nx
  WHERE nx.f IS NOT NULL
),
lvl AS (SELECT x AS id, lv.depth FROM lv, unnest(lv.frontier) AS x),
up AS (
  SELECT DISTINCT ON (c.id) c.id, e.parent_id
  FROM lvl c JOIN log_edges e ON e.child_id = c.id JOIN lvl p ON p.id = e.parent_id AND p.depth = c.depth - 1
  ORDER BY c.id, e.parent_id
),
first_path AS (
  SELECT l.id, l.id AS root_id, 0 AS depth, 'root.n' || l.id::text AS path_label FROM lvl l WHERE l.depth = 0
  UNION ALL
  SELECT u.id, f.root_id, f.depth + 1, f.path_label || '.n' || u.id::text FROM up u JOIN first_path f ON u.parent_id = f.id
)
SELECT n.id, n.workspace_id, n.type, n.status, n.title, n.body, n.author_id, u.name AS author_name,
       n.category, n.tags, n.attributes,
       n.error_code, n.keywords, n.resolution_summary, n.embedding_model,
       n.refine_status, n.refine_attempts, n.refine_error,
       n.last_confirmed_at, n.deprecated_at, n.deprecated_via, n.deprecate_reason,
       n.resolved_at, n.resolve_note, n.source, n.source_ref, n.compose_ms,
       n.created_at, n.updated_at,
       f.root_id, f.depth, f.path_label,
       -- 장애 건의 원인: 원인으로 표시했고 지금 상위가 있는 기록 (연결이 끊겨 루트가 되면 원인이 아니다)
       coalesce(n.category = 'cause' AND f.depth > 0, false) AS is_cause
FROM log_nodes n
JOIN users u ON u.id = n.author_id
LEFT JOIN first_path f ON f.id = n.id;

CREATE OR REPLACE VIEW v_policy_documents AS
SELECT d.*,
       (SELECT count(*) FROM policy_chunks c WHERE c.doc_id = d.id)::int AS chunk_count,
       (d.file_path IS NOT NULL) AS has_file
FROM policy_documents d;

CREATE OR REPLACE VIEW v_search_sessions AS
SELECT s.*, u.name AS user_name, f.score
FROM search_sessions s
LEFT JOIN users u ON u.id = s.user_id
LEFT JOIN answer_feedback f ON f.session_id = s.id;
