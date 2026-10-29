-- 인덱스 (02 v3 §1-3, 04 §3.5). HNSW는 시연 규모에서 필수는 아니나 비용이 작아 미리 둔다.

CREATE INDEX idx_workspaces_tenant        ON workspaces (tenant_id);
CREATE INDEX idx_workspaces_parent        ON workspaces (parent_id);
CREATE INDEX idx_members_user             ON workspace_members (user_id);

CREATE INDEX idx_log_nodes_ws_created     ON log_nodes (workspace_id, created_at DESC);
CREATE INDEX idx_log_nodes_ws_type        ON log_nodes (workspace_id, type);
CREATE INDEX idx_log_nodes_ws_status      ON log_nodes (workspace_id, status);
CREATE INDEX idx_log_nodes_ws_error_code  ON log_nodes (workspace_id, error_code);
CREATE INDEX idx_log_nodes_keywords       ON log_nodes USING gin (keywords);
CREATE INDEX idx_log_nodes_tsv            ON log_nodes USING gin (tsv);
CREATE INDEX idx_log_nodes_embedding      ON log_nodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_log_nodes_refine_pending ON log_nodes (created_at) WHERE refine_status = 'pending';

CREATE INDEX idx_log_edges_child          ON log_edges (child_id);
CREATE INDEX idx_log_edges_ws             ON log_edges (workspace_id);

CREATE INDEX idx_policy_docs_ws_status    ON policy_documents (workspace_id, status);
CREATE INDEX idx_policy_docs_ws_title     ON policy_documents (workspace_id, title);   -- 개정 자동 폐기(R8) 조회
CREATE INDEX idx_sessions_ws_status       ON search_sessions (workspace_id, status);
CREATE INDEX idx_policy_chunks_doc        ON policy_chunks (doc_id);
CREATE INDEX idx_policy_chunks_ws         ON policy_chunks (workspace_id);
CREATE INDEX idx_policy_chunks_tsv        ON policy_chunks USING gin (tsv);
CREATE INDEX idx_policy_chunks_embedding  ON policy_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX idx_sessions_ws_created      ON search_sessions (workspace_id, created_at DESC);
CREATE INDEX idx_audit_table_row          ON audit_log (table_name, row_id);
CREATE INDEX idx_audit_ws_at              ON audit_log (workspace_id, at DESC);
