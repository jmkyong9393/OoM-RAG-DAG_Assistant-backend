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

-- 규정 하이브리드 검색: 어휘(IDF 가중, 제목 0.1) 상위 20 + 벡터 상위 20 → RRF(k=60) → 상위 p_k. active 문서, 자기+조상 워크스페이스.
CREATE OR REPLACE FUNCTION hybrid_search_chunks(p_ws bigint, p_tokens text, p_vec vector(1536), p_k int DEFAULT 5)
RETURNS TABLE(chunk_id text, score double precision) LANGUAGE sql STABLE AS $$
  WITH cand AS (
    SELECT c.id, c.tsv, c.embedding, length(c.text) AS len
    FROM policy_chunks c JOIN policy_documents d ON d.id = c.doc_id
    WHERE d.status = 'active' AND c.workspace_id IN (SELECT workspace_ancestors(p_ws))
  ),
  -- 질의 단어별 IDF(후보 집합 안에서 그 단어가 든 청크 수). Postgres FTS 는 IDF 가 없어 '소비자·기준·환급'처럼
  -- 거의 모든 청크에 있는 단어가 '개봉·신선식품'과 같은 무게가 되는 것을 막는다. 본문·라벨(A) 적중 1.0, 문서 제목(D)만 적중 0.1.
  terms AS (
    SELECT DISTINCT t, plainto_tsquery('simple', t) AS tq
    FROM regexp_split_to_table(coalesce(p_tokens, ''), '\s+') AS t
    WHERE t <> '' AND numnode(plainto_tsquery('simple', t)) > 0
  ),
  tf AS (
    SELECT t.t, t.tq, ln(((SELECT count(*) FROM cand) + 1)::float / (count(c.id) + 1)) AS idf
    FROM terms t LEFT JOIN cand c ON c.tsv @@ t.tq GROUP BY t.t, t.tq
  ),
  -- 동점이면 짧은 청크(더 집중된 조문) 우선, 그다음 id — 순위가 실행마다 바뀌지 않게
  lex AS (
    SELECT id, r FROM (
      SELECT s.id, row_number() OVER (ORDER BY s.s DESC, s.len, s.id) AS r
      FROM (SELECT c.id, c.len, SUM(tf.idf * CASE WHEN ts_filter(c.tsv, '{a}') @@ tf.tq THEN 1.0 ELSE 0.1 END) AS s
            FROM cand c JOIN tf ON c.tsv @@ tf.tq GROUP BY c.id, c.len) s
    ) t WHERE r <= 20
  ),
  vec AS (
    SELECT id, r FROM (
      SELECT id, row_number() OVER (ORDER BY embedding <=> p_vec) AS r
      FROM cand WHERE p_vec IS NOT NULL AND embedding IS NOT NULL
    ) t WHERE r <= 20
  )
  -- RRF 는 한쪽에서만 1위인 조문을 양쪽에서 중간 순위인 조문들 뒤로 민다. 벡터 상위 2건과 어휘 1위는 앞에 둔다
  SELECT id, SUM(1.0 / (60 + r))::double precision AS score
  FROM (SELECT id, r, r <= 1 AS lead FROM lex UNION ALL SELECT id, r, r <= 2 FROM vec) u
  GROUP BY id ORDER BY bool_or(lead) DESC, score DESC, id LIMIT p_k
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

-- 어시스턴트의 사례 진입점 검색: error_code 정확 일치(+1.0) + 어휘(IDF 가중) 상위 20 + 벡터 상위 20(가중 1.5) RRF. active 노드, 자기 워크스페이스만.
-- 관련성 판단용 근거(exact · 일치한 질의 단어 수 · 벡터 거리)를 함께 돌려준다. 등록 화면의 상위 로그 추천은 hybrid_search_nodes(키워드 가산)를 쓴다.
CREATE OR REPLACE FUNCTION search_case_entry(p_ws bigint, p_tokens text, p_vec vector(1536), p_error_code text, p_k int DEFAULT 3)
RETURNS TABLE(node_id bigint, score double precision, exact boolean, lex_terms int, distance double precision) LANGUAGE sql STABLE AS $$
  WITH cand AS (
    SELECT id, tsv, embedding, error_code, length(coalesce(tokens, '')) AS len
    FROM log_nodes WHERE workspace_id = p_ws AND status = 'active'
  ),
  terms AS (
    SELECT DISTINCT t, plainto_tsquery('simple', t) AS tq
    FROM regexp_split_to_table(coalesce(p_tokens, ''), '\s+') AS t
    WHERE t <> '' AND numnode(plainto_tsquery('simple', t)) > 0
  ),
  -- 질의 단어별 IDF: '오류·문제'처럼 대부분의 로그에 있는 단어가 '서킷브레이커'와 같은 무게가 되지 않게 한다
  tf AS (
    SELECT t.t, t.tq, ln(((SELECT count(*) FROM cand) + 1)::float / (count(c.id) + 1)) AS idf
    FROM terms t LEFT JOIN cand c ON c.tsv @@ t.tq GROUP BY t.t, t.tq
  ),
  ls AS (
    SELECT c.id, c.len, SUM(tf.idf) AS s, count(*)::int AS m FROM cand c JOIN tf ON c.tsv @@ tf.tq GROUP BY c.id, c.len
  ),
  lex AS (
    SELECT id, r FROM (SELECT id, row_number() OVER (ORDER BY s DESC, len, id) AS r FROM ls) t WHERE r <= 20
  ),
  vec AS (
    SELECT id, r FROM (
      SELECT id, row_number() OVER (ORDER BY embedding <=> p_vec) AS r
      FROM cand WHERE p_vec IS NOT NULL AND embedding IS NOT NULL
    ) t WHERE r <= 20
  ),
  u AS (
    SELECT id, 1.0 / (60 + r) AS s FROM lex
    UNION ALL SELECT id, 1.5 / (60 + r) FROM vec
    UNION ALL SELECT id, 1.0 FROM cand WHERE coalesce(p_error_code, '') <> '' AND error_code = p_error_code
  ),
  top AS (
    SELECT id, SUM(s)::double precision AS score FROM u GROUP BY id ORDER BY score DESC, id LIMIT p_k
  )
  SELECT t.id, t.score, (coalesce(p_error_code, '') <> '' AND c.error_code = p_error_code) IS TRUE, coalesce(ls.m, 0),
         CASE WHEN p_vec IS NOT NULL AND c.embedding IS NOT NULL THEN (c.embedding <=> p_vec)::double precision END
  FROM top t JOIN cand c ON c.id = t.id LEFT JOIN ls ON ls.id = t.id
  ORDER BY t.score DESC, t.id
$$;

-- 단계 동기 BFS: 시작 노드들에서 하향(p_down) 또는 상향으로 닿는 노드와 최소 거리(시작=0).
-- 한 단계를 한 행(frontier · seen 배열)으로 들고 가 노드마다 한 번만 펼친다 — 재귀 CTE 는 이전 단계 행만 보므로 경로별 전개가 지수로 는다.
CREATE OR REPLACE FUNCTION bfs_levels(p_ws bigint, p_start bigint[], p_down boolean, p_max_depth int DEFAULT NULL)
RETURNS TABLE(node_id bigint, depth int) LANGUAGE sql STABLE AS $$
  WITH RECURSIVE lv(depth, frontier, seen) AS (
    SELECT 0, s.a, s.a FROM (SELECT array_agg(DISTINCT n.id) AS a FROM log_nodes n WHERE n.id = ANY (p_start) AND n.workspace_id = p_ws) s
    WHERE s.a IS NOT NULL
    UNION ALL
    SELECT lv.depth + 1, nx.f, lv.seen || nx.f FROM lv,
      LATERAL (SELECT array_agg(c) AS f FROM (
        SELECT e.child_id AS c FROM log_edges e WHERE p_down AND e.parent_id = ANY (lv.frontier)
        UNION
        SELECT e.parent_id FROM log_edges e WHERE NOT p_down AND e.child_id = ANY (lv.frontier)
        EXCEPT
        SELECT unnest(lv.seen)) s) nx
    WHERE nx.f IS NOT NULL AND (p_max_depth IS NULL OR lv.depth < p_max_depth)
  )
  SELECT x, lv.depth FROM lv, unnest(lv.frontier) AS x
$$;

-- 진입 노드 기준 상향(음수 depth)·하향(양수 depth) 체인. 고정 깊이 버전.
CREATE OR REPLACE FUNCTION chain_at_depth(p_entry bigint, p_depth int)
RETURNS TABLE(node_id bigint, depth int) LANGUAGE sql STABLE AS $$
  SELECT b.node_id, -b.depth FROM log_nodes n, bfs_levels(n.workspace_id, ARRAY[p_entry], false, p_depth) b WHERE n.id = p_entry
  UNION ALL
  SELECT b.node_id, b.depth FROM log_nodes n, bfs_levels(n.workspace_id, ARRAY[p_entry], true, p_depth) b WHERE n.id = p_entry AND b.depth > 0
$$;

-- 체인 노드 고르기. 후보 = 진입의 모든 조상(음수 depth)과 자손(양수). 상한을 넘으면 깊이를 깎지 않고 순서대로 채운다:
-- ① 초점(검색으로 찾은 기록)과 그 위 경로 ② 장애 건을 해결한 기록까지의 경로 ③ 원인까지의 경로 ④ 나머지를 가까운 순(p_max_depth 안).
-- ②③ 은 경로 단위로 통째로 넣고, 다 못 들어가는 경로는 건너뛴다. total = 후보 수(잘렸는지 판정용).
CREATE OR REPLACE FUNCTION get_chain(p_ws bigint, p_entry bigint, p_max_depth int, p_max_nodes int, p_focus bigint)
RETURNS TABLE(node_id bigint, depth int, total int) LANGUAGE plpgsql STABLE AS $$
DECLARE
  cap int := greatest(p_max_nodes, 1); ids bigint[]; deps int[]; tot int; sel bigint[]; grp bigint[]; a bigint;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM log_nodes WHERE id = p_entry AND workspace_id = p_ws) THEN RETURN; END IF;
  SELECT array_agg(c.node_id ORDER BY abs(c.depth), c.node_id), array_agg(c.depth ORDER BY abs(c.depth), c.node_id) INTO ids, deps
  FROM (SELECT b.node_id, -b.depth AS depth FROM bfs_levels(p_ws, ARRAY[p_entry], false) b WHERE b.depth > 0
        UNION ALL SELECT b.node_id, b.depth FROM bfs_levels(p_ws, ARRAY[p_entry], true) b) c;
  tot := cardinality(ids);
  IF tot <= cap AND p_max_depth IS NULL THEN
    RETURN QUERY SELECT x, d, tot FROM unnest(ids, deps) AS t(x, d);
    RETURN;
  END IF;
  -- ① 초점과 그 위 경로(후보 안) — 초점에서 가까운 순
  SELECT array_agg(u.node_id ORDER BY u.depth, u.node_id) INTO sel
  FROM bfs_levels(p_ws, ARRAY[coalesce(p_focus, p_entry)], false) u WHERE u.node_id = ANY (ids);
  sel := coalesce(sel[1:cap], ARRAY[p_entry]);
  IF NOT p_entry = ANY (sel) THEN sel := sel[1:cap - 1] || p_entry; END IF;
  -- ②③ 해결 기록, 원인 — 가까운 순으로 경로째
  FOR a IN
    SELECT x FROM unnest(ids, deps) WITH ORDINALITY AS t(x, d, o)
    WHERE x <> ALL (sel) AND (
      x IN (SELECT r FROM unnest(ids) AS rt(root), resolved_nodes(p_ws, rt.root) AS r
            WHERE NOT EXISTS (SELECT 1 FROM log_edges e WHERE e.child_id = rt.root))
      OR x IN (SELECT n.id FROM log_nodes n WHERE n.id = ANY (ids) AND n.category = 'cause'
               AND EXISTS (SELECT 1 FROM log_edges e WHERE e.child_id = n.id)))
    ORDER BY (x IN (SELECT n.id FROM log_nodes n WHERE n.id = ANY (ids) AND n.category = 'cause')), o
  LOOP
    CONTINUE WHEN a = ANY (sel);
    SELECT array_agg(u.node_id) INTO grp FROM bfs_levels(p_ws, ARRAY[a], false) u WHERE u.node_id = ANY (ids) AND u.node_id <> ALL (sel);
    IF cardinality(sel) + cardinality(grp) <= cap THEN sel := sel || grp; END IF;
  END LOOP;
  -- ④ 나머지를 가까운 순으로
  sel := sel || coalesce((SELECT array_agg(x ORDER BY o) FROM unnest(ids, deps) WITH ORDINALITY AS t(x, d, o)
                          WHERE x <> ALL (sel) AND (p_max_depth IS NULL OR abs(d) <= p_max_depth)), '{}');
  sel := sel[1:cap];
  RETURN QUERY SELECT x, d, tot FROM unnest(ids, deps) AS t(x, d) WHERE x = ANY (sel);
END $$;

-- 진입이 곧 초점인 체인 (화면 · 이전 호출 호환).
CREATE OR REPLACE FUNCTION get_chain(p_ws bigint, p_entry bigint, p_max_depth int DEFAULT 3, p_max_nodes int DEFAULT 30)
RETURNS TABLE(node_id bigint, depth int) LANGUAGE sql STABLE AS $$
  SELECT g.node_id, g.depth FROM get_chain(p_ws, p_entry, p_max_depth, p_max_nodes, p_entry) g
$$;

-- 간선 (p → c)를 넣으면 사이클이 생기는가: c = p 이거나 c에서 하향으로 p에 닿으면 true. 깊이 상한 없음.
CREATE OR REPLACE FUNCTION would_create_cycle(p_parent bigint, p_child bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT p_parent = p_child OR EXISTS (
    SELECT 1 FROM log_nodes c, bfs_levels(c.workspace_id, ARRAY[p_child], true) b WHERE c.id = p_child AND b.node_id = p_parent
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
-- 장애 건을 해결한 기록 가운데 지금 유효한 것: 활성이고 그 루트의 자손인 success (log_resolutions, V10).
-- 폐기되거나 연결이 끊긴 기록은 빠지고, 복원·재연결되면 다시 든다.
CREATE OR REPLACE FUNCTION resolved_nodes(p_ws bigint, p_root bigint)
RETURNS SETOF bigint LANGUAGE sql STABLE AS $$
  SELECT r.node_id FROM log_resolutions r JOIN log_nodes n ON n.id = r.node_id
  WHERE r.workspace_id = p_ws AND r.root_id = p_root AND n.status = 'active'
    AND r.node_id IN (SELECT b.node_id FROM bfs_levels(p_ws, ARRAY[p_root], true) b WHERE b.depth > 0)
  ORDER BY r.node_id
$$;

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

-- ── 규정 개정과 사례의 근거 조문 ────────────────────────────────────────────

-- 개정 사슬의 끝(지금 유효한 문서). 대체된 적이 없으면 자기 자신.
CREATE OR REPLACE FUNCTION policy_successor(p_doc bigint) RETURNS bigint LANGUAGE sql STABLE AS $$
  WITH RECURSIVE s(id, nxt, depth) AS (
    SELECT id, superseded_by, 0 FROM policy_documents WHERE id = p_doc
    UNION ALL
    SELECT d.id, d.superseded_by, s.depth + 1 FROM s JOIN policy_documents d ON d.id = s.nxt WHERE s.depth < 20
  )
  SELECT id FROM s ORDER BY depth DESC LIMIT 1
$$;

-- 청크 라벨('제12조 (접근 토큰)', '[별표 2] 점검표')이 그 조문('제12조', '별표 2')인가. '제1조' 가 '제10조' 에 걸리지 않게 한다.
CREATE OR REPLACE FUNCTION is_article(p_label text, p_article text) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
  SELECT left(h, length(p_article)) = p_article AND substr(h, length(p_article) + 1, 1) !~ '[0-9]'
  FROM (SELECT regexp_replace(btrim(coalesce(p_label, '')), '^\[', '') AS h) x
$$;

-- 사례가 적어 둔 근거 조문(문서 slug + 조문)을 지금 유효한 조문으로 푼다. 문서가 개정됐으면 개정 사슬을 따라간다.
--   current  : 그 문서가 유효하다 (개정 없음)
--   same     : 개정됐지만 그 조문은 제목·내용이 그대로다
--   amended  : 개정으로 그 조문의 내용이 바뀌었다 (제목은 같다)
--   replaced : 개정판에 같은 번호·같은 제목의 조문이 없다 — 대응 조문을 사람이 정해야 한다. suggested_label 은 내용이 가장 가까운 새 조문
--   retired  : 문서가 대체 없이 폐기됐거나 이 부서에서 보이지 않는다
-- current·same·amended 만 chunk 를 돌려준다. 조문이 원래 문서에 없으면 행이 없다.
CREATE OR REPLACE FUNCTION resolve_policy_ref(p_ws bigint, p_slug text, p_article text)
RETURNS TABLE(status text, chunk_id text, doc_id bigint, slug text, doc_title text, version text, doc_type text, article_label text, page int, text text,
              from_title text, from_version text, from_label text, suggested_label text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  d0 policy_documents%ROWTYPE;
  cur policy_documents%ROWTYPE;
  old_c policy_chunks%ROWTYPE;
  new_c policy_chunks%ROWTYPE;
  old_text text;
  new_text text;
  visible boolean;
BEGIN
  SELECT * INTO d0 FROM policy_documents d WHERE d.slug = p_slug;
  IF NOT FOUND THEN RETURN; END IF;
  SELECT * INTO old_c FROM policy_chunks c WHERE c.doc_id = d0.id AND is_article(c.article_label, p_article) ORDER BY c.id LIMIT 1;
  IF NOT FOUND THEN RETURN; END IF;
  SELECT * INTO cur FROM policy_documents d WHERE d.id = policy_successor(d0.id);
  visible := cur.status = 'active' AND cur.workspace_id IN (SELECT workspace_ancestors(p_ws));
  IF NOT visible THEN
    RETURN QUERY SELECT 'retired'::text, NULL::text, NULL::bigint, NULL::text, NULL::text, NULL::text, NULL::text, NULL::text, NULL::int, NULL::text,
                        d0.title, d0.version, old_c.article_label, NULL::text;
    RETURN;
  END IF;
  IF cur.id = d0.id THEN
    RETURN QUERY SELECT 'current'::text, old_c.id, cur.id, cur.slug, cur.title, cur.version, cur.doc_type, old_c.article_label, old_c.page, old_c.text,
                        NULL::text, NULL::text, NULL::text, NULL::text;
    RETURN;
  END IF;
  SELECT * INTO new_c FROM policy_chunks c WHERE c.doc_id = cur.id AND is_article(c.article_label, p_article) ORDER BY c.id LIMIT 1;
  IF FOUND AND regexp_replace(new_c.article_label, '\s+', '', 'g') = regexp_replace(old_c.article_label, '\s+', '', 'g') THEN
    SELECT string_agg(regexp_replace(c.text, '\s+', ' ', 'g'), ' ' ORDER BY c.id) INTO old_text FROM policy_chunks c WHERE c.doc_id = d0.id AND is_article(c.article_label, p_article);
    SELECT string_agg(regexp_replace(c.text, '\s+', ' ', 'g'), ' ' ORDER BY c.id) INTO new_text FROM policy_chunks c WHERE c.doc_id = cur.id AND is_article(c.article_label, p_article);
    RETURN QUERY SELECT CASE WHEN old_text = new_text THEN 'same' ELSE 'amended' END, new_c.id, cur.id, cur.slug, cur.title, cur.version, cur.doc_type,
                        new_c.article_label, new_c.page, new_c.text, d0.title, d0.version, old_c.article_label, NULL::text;
    RETURN;
  END IF;
  RETURN QUERY SELECT 'replaced'::text, NULL::text, cur.id, cur.slug, cur.title, cur.version, cur.doc_type, NULL::text, NULL::int, NULL::text,
                      d0.title, d0.version, old_c.article_label,
                      (SELECT c.article_label FROM policy_chunks c
                        WHERE c.doc_id = cur.id AND c.embedding IS NOT NULL AND old_c.embedding IS NOT NULL
                        ORDER BY c.embedding <=> old_c.embedding LIMIT 1);
END $$;
