-- 읽기 모델 뷰. 파생 필드(작성자명·루트·깊이·경로·청크 수·피드백)를 DB 한 곳에서 계산한다 (33 §5-1).
-- 무거운 컬럼(tokens·tsv·embedding)은 뷰에서 뺀다.

CREATE OR REPLACE VIEW v_log_nodes AS
WITH RECURSIVE t AS (
  SELECT n.id, n.id AS root_id, 0 AS depth, 'root.n' || n.id::text AS path_label
  FROM log_nodes n
  WHERE NOT EXISTS (SELECT 1 FROM log_edges e WHERE e.child_id = n.id)
  UNION ALL
  SELECT e.child_id, t.root_id, t.depth + 1, t.path_label || '.n' || e.child_id::text
  FROM log_edges e JOIN t ON e.parent_id = t.id
  WHERE t.depth < 32
),
first_path AS (
  SELECT DISTINCT ON (id) id, root_id, depth, path_label FROM t ORDER BY id, depth, path_label
)
SELECT n.id, n.workspace_id, n.type, n.status, n.title, n.body, n.author_id, u.name AS author_name,
       n.category, n.tags, n.attributes,
       n.error_code, n.keywords, n.resolution_summary, n.embedding_model,
       n.refine_status, n.refine_attempts, n.refine_error,
       n.last_confirmed_at, n.deprecated_at, n.deprecated_via, n.deprecate_reason,
       n.resolved_at, n.resolve_note, n.source, n.source_ref, n.compose_ms,
       n.created_at, n.updated_at,
       f.root_id, f.depth, f.path_label
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
