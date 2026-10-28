-- 공유 SQL 함수. Java(JdbcClient)·Python(psycopg)이 같은 함수를 호출한다 (02 v3 §1-3, 33 §3).
-- 반복 마이그레이션: 파일이 바뀌면 CREATE OR REPLACE가 다시 실행된다.

-- 워크스페이스 + 조상 (규정 검색 범위)
CREATE OR REPLACE FUNCTION workspace_ancestors(p_ws bigint)
RETURNS SETOF bigint LANGUAGE sql STABLE AS $$
  WITH RECURSIVE a AS (
    SELECT id, parent_id, 0 AS d FROM workspaces WHERE id = p_ws
    UNION ALL
    SELECT w.id, w.parent_id, a.d + 1 FROM workspaces w JOIN a ON w.id = a.parent_id WHERE a.d < 16
  )
  SELECT id FROM a
$$;

-- 질의 토큰을 OR 질의로. plainto_tsquery는 AND라 토큰이 많으면 0건이 된다. 순위는 ts_rank_cd가 매긴다.
CREATE OR REPLACE FUNCTION or_tsquery(p_tokens text)
RETURNS tsquery LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN coalesce(p_tokens, '') = '' THEN NULL::tsquery
         ELSE (SELECT string_agg(plainto_tsquery('simple', t)::text, ' | ')
               FROM regexp_split_to_table(p_tokens, '\s+') AS t
               WHERE t <> '' AND numnode(plainto_tsquery('simple', t)) > 0)::tsquery END
$$;

-- 규정 하이브리드 검색: 어휘(tsvector) 상위 20 + 벡터 상위 20 → RRF(k=60) → 상위 p_k. active 문서, 자기+조상 워크스페이스.
CREATE OR REPLACE FUNCTION hybrid_search_chunks(p_ws bigint, p_tokens text, p_vec vector(1536), p_k int DEFAULT 5)
RETURNS TABLE(chunk_id text, score double precision) LANGUAGE sql STABLE AS $$
  WITH cand AS (
    SELECT c.id, c.tsv, c.embedding, length(c.text) AS len
    FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
    WHERE d.status = 'active' AND c.workspace_id IN (SELECT workspace_ancestors(p_ws))
  ),
  q AS (SELECT or_tsquery(p_tokens) AS tq),
  -- 어휘 점수 동점이면 짧은 청크(더 집중된 조문) 우선, 그다음 id — 순위가 실행마다 바뀌지 않게
  lex AS (
    SELECT id, r FROM (
      SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tq) DESC, c.len, c.id) AS r
      FROM cand c, q WHERE q.tq IS NOT NULL AND c.tsv @@ q.tq
    ) t WHERE r <= 20
  ),
  vec AS (
    SELECT id, r FROM (
      SELECT id, row_number() OVER (ORDER BY embedding <=> p_vec) AS r
      FROM cand WHERE p_vec IS NOT NULL AND embedding IS NOT NULL
    ) t WHERE r <= 20
  )
  SELECT id, SUM(1.0 / (60 + r))::double precision AS score
  FROM (SELECT * FROM lex UNION ALL SELECT * FROM vec) u
  GROUP BY id ORDER BY score DESC, id LIMIT p_k
$$;

-- 로그 진입 노드 검색: error_code 정확 일치(+1.0) → keywords 겹침(+0.5, 겹친 수에 비례) → 어휘·벡터 RRF. active 노드, 자기 워크스페이스만.
-- keywords 는 정제 결과(LLM 추출 구문)이고 p_tokens 는 질의 형태소라, 대소문자 무시 완전 일치로 겹침을 센다.
CREATE OR REPLACE FUNCTION hybrid_search_nodes(p_ws bigint, p_tokens text, p_vec vector(1536), p_error_code text, p_k int DEFAULT 5)
RETURNS TABLE(node_id bigint, score double precision) LANGUAGE sql STABLE AS $$
  WITH cand AS (
    SELECT id, tsv, embedding, error_code, keywords, length(coalesce(tokens, '')) AS len
    FROM log_nodes WHERE workspace_id = p_ws AND status = 'active'
  ),
  exact AS (
    SELECT id FROM cand WHERE coalesce(p_error_code, '') <> '' AND error_code = p_error_code
  ),
  qtok AS (
    SELECT array_agg(DISTINCT lower(t)) AS toks FROM regexp_split_to_table(coalesce(p_tokens, ''), '\s+') AS t WHERE t <> ''
  ),
  -- 키워드 부스팅: 키워드의 모든 어절이 질의 토큰에 포함되면 +0.5 ('토큰 갱신' 같은 복합어도 형태소 단위 질의와 맞는다)
  kw AS (
    SELECT c.id, 0.5 * (SELECT count(*) FROM unnest(c.keywords) k
                        WHERE regexp_split_to_array(lower(trim(k)), '\s+') <@ q.toks) AS s
    FROM cand c, qtok q
    WHERE q.toks IS NOT NULL AND cardinality(c.keywords) > 0
  ),
  q AS (SELECT or_tsquery(p_tokens) AS tq),
  lex AS (
    SELECT id, r FROM (
      SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tq) DESC, c.len, c.id) AS r
      FROM cand c, q WHERE q.tq IS NOT NULL AND c.tsv @@ q.tq
    ) t WHERE r <= 20
  ),
  vec AS (
    SELECT id, r FROM (
      SELECT id, row_number() OVER (ORDER BY embedding <=> p_vec) AS r
      FROM cand WHERE p_vec IS NOT NULL AND embedding IS NOT NULL
    ) t WHERE r <= 20
  ),
  u AS (
    SELECT id, 1.0 / (60 + r) AS s FROM lex
    UNION ALL SELECT id, 1.0 / (60 + r) FROM vec
    UNION ALL SELECT id, s FROM kw WHERE s > 0
    UNION ALL SELECT id, 1.0 FROM exact
  )
  SELECT id, SUM(s)::double precision AS score FROM u GROUP BY id ORDER BY score DESC, id LIMIT p_k
$$;

-- 진입 노드 기준 상향(음수 depth)·하향(양수 depth) 체인. 고정 깊이 버전.
CREATE OR REPLACE FUNCTION chain_at_depth(p_entry bigint, p_depth int)
RETURNS TABLE(node_id bigint, depth int) LANGUAGE sql STABLE AS $$
  WITH RECURSIVE up AS (
    SELECT p_entry AS id, 0 AS depth
    UNION
    SELECT e.parent_id, u.depth - 1 FROM log_edges e JOIN up u ON e.child_id = u.id WHERE u.depth > -p_depth
  ),
  down AS (
    SELECT p_entry AS id, 0 AS depth
    UNION
    SELECT e.child_id, dn.depth + 1 FROM log_edges e JOIN down dn ON e.parent_id = dn.id WHERE dn.depth < p_depth
  )
  SELECT id, max(depth) FROM up GROUP BY id
  UNION ALL
  SELECT id, min(depth) FROM down WHERE id <> p_entry GROUP BY id
$$;

-- 노드 수 상한을 넘으면 깊이를 1씩 줄여 재시도 (02 v3 §1-3).
CREATE OR REPLACE FUNCTION get_chain(p_ws bigint, p_entry bigint, p_max_depth int DEFAULT 3, p_max_nodes int DEFAULT 30)
RETURNS TABLE(node_id bigint, depth int) LANGUAGE plpgsql STABLE AS $$
DECLARE d int := greatest(p_max_depth, 1); n int;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM log_nodes WHERE id = p_entry AND workspace_id = p_ws) THEN RETURN; END IF;
  LOOP
    SELECT count(*) INTO n FROM chain_at_depth(p_entry, d);
    EXIT WHEN n <= p_max_nodes OR d <= 1;
    d := d - 1;
  END LOOP;
  RETURN QUERY SELECT * FROM chain_at_depth(p_entry, d);
END $$;

-- 간선 (p → c)를 넣으면 사이클이 생기는가: c = p 이거나 c에서 하향으로 p에 닿으면 true.
CREATE OR REPLACE FUNCTION would_create_cycle(p_parent bigint, p_child bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT p_parent = p_child OR EXISTS (
    WITH RECURSIVE dn AS (
      SELECT p_child AS id, 0 AS d
      UNION
      SELECT e.child_id, dn.d + 1 FROM log_edges e JOIN dn ON e.parent_id = dn.id WHERE dn.d < 64
    )
    SELECT 1 FROM dn WHERE id = p_parent
  )
$$;

-- 연쇄 폐기 대상: p_node를 거치지 않고는 활성 조상에 닿을 수 없는 활성 자손 (33 §2 B, 규칙 R5).
-- 고정점 반복: 활성 부모가 전부 {p_node} ∪ 결과집합 안에 있는 자손을 더한다.
CREATE OR REPLACE FUNCTION orphaned_descendants(p_node bigint)
RETURNS SETOF bigint LANGUAGE plpgsql STABLE AS $$
DECLARE o bigint[] := ARRAY[p_node]; added bigint[];
BEGIN
  LOOP
    SELECT array_agg(n.id) INTO added
    FROM log_nodes n
    WHERE n.status = 'active' AND n.id <> ALL (o)
      AND EXISTS (SELECT 1 FROM log_edges e WHERE e.child_id = n.id AND e.parent_id = ANY (o))
      AND NOT EXISTS (
        SELECT 1 FROM log_edges e JOIN log_nodes p ON p.id = e.parent_id
        WHERE e.child_id = n.id AND p.status = 'active' AND p.id <> ALL (o)
      );
    EXIT WHEN added IS NULL;
    o := o || added;
  END LOOP;
  RETURN QUERY SELECT x FROM unnest(o) AS x WHERE x <> p_node;
END $$;

-- 문서 삭제는 이 함수만 (retired 상태 한정). 청크는 CASCADE, 파일은 Spring이 공유 볼륨에서 삭제.
CREATE OR REPLACE FUNCTION purge_document(p_doc bigint, p_actor text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE d policy_documents%ROWTYPE;
BEGIN
  SELECT * INTO d FROM policy_documents WHERE id = p_doc FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'NOT_FOUND' USING ERRCODE = 'no_data_found'; END IF;
  IF d.status <> 'retired' THEN RAISE EXCEPTION 'NOT_RETIRED' USING ERRCODE = 'check_violation'; END IF;
  INSERT INTO audit_log (tenant_id, workspace_id, table_name, row_id, action, actor, source, label, before)
  SELECT w.tenant_id, d.workspace_id, 'policy_documents', p_doc::text, 'deleted', p_actor,
         CASE WHEN p_actor = 'script' THEN 'script' ELSE 'ui' END, d.title || ' ' || d.version, to_jsonb(d)
  FROM workspaces w WHERE w.id = d.workspace_id;
  DELETE FROM policy_documents WHERE id = p_doc;
END $$;
