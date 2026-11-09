package com.oom.assist.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/** 공유 SQL 함수·뷰의 동작을 기능 단위로 검증한다. Python Worker 와 Spring 이 같은 결과를 보는 계약. */
class DbFunctionsTest extends AbstractDbTest {

  // ── workspace_ancestors ─────────────────────────────────────────────────

  @Test
  void ancestorsIncludeSelfAndAllParents() {
    long division = workspace(tenant, company, "Division", true);
    long dept = workspace(tenant, division, "Dept", false);
    List<Long> scope = jdbc.sql("SELECT workspace_ancestors(?)").param(dept).query(Long.class).list();
    assertThat(scope).containsExactlyInAnyOrder(dept, division, company);
  }

  // ── or_tsquery ──────────────────────────────────────────────────────────

  @Test
  void orTsqueryJoinsTokensWithOrAndHandlesEmpty() {
    String q = jdbc.sql("SELECT or_tsquery('접속 신청')::text").query(String.class).single();
    assertThat(q).isEqualTo("'접속' | '신청'");
    assertThat(jdbc.sql("SELECT or_tsquery('') IS NULL").query(Boolean.class).single()).isTrue();
    assertThat(jdbc.sql("SELECT or_tsquery(NULL) IS NULL").query(Boolean.class).single()).isTrue();
  }

  // ── hybrid_search_chunks ───────────────────────────────────────────────

  @Test
  void chunkSearchLexicalUsesOrSemanticsAndRanksOverlap() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "d1-a1-0", "제1조", "VPN 접속 신청", "VPN 접속 신청 기준", null);
    chunk(doc, "d1-a2-0", "제2조", "출장 정산", "출장 정산 기준", null);
    chunk(doc, "d1-a3-0", "제3조", "VPN", "VPN 장비 반납", null);
    List<String> hits = jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, 'VPN 신청', NULL, 5)").param(ws).query(String.class).list();
    // AND 였다면 'VPN 신청' 둘 다 가진 1건만 나온다. OR 이라 두 건, 겹침이 많은 쪽이 먼저
    assertThat(hits).containsExactly("d1-a1-0", "d1-a3-0");
  }

  @Test
  void chunkSearchVectorOrdersByCosineDistance() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "d1-a1-0", "제1조", "a", null, unitVector(0));
    chunk(doc, "d1-a2-0", "제2조", "b", null, unitVector(1));
    List<String> hits = jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, NULL, CAST(? AS vector), 5)").params(ws, unitVector(0)).query(String.class).list();
    assertThat(hits).containsExactly("d1-a1-0", "d1-a2-0");
  }

  @Test
  void chunkSearchFusesLexicalAndVectorWithRrf() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "both", "1", "x", "VPN 신청", unitVector(0));   // 어휘 1위 + 벡터 1위
    chunk(doc, "lexOnly", "2", "x", "VPN", unitVector(5));
    chunk(doc, "vecOnly", "3", "x", "정산", unitVector(1));
    List<Map<String, Object>> rows = jdbc.sql("SELECT chunk_id, score FROM hybrid_search_chunks(?, 'VPN 신청', CAST(? AS vector), 5)")
        .params(ws, unitVector(0)).query().listOfRows();
    assertThat(rows.get(0).get("chunk_id")).isEqualTo("both");
    double top = (Double) rows.get(0).get("score");
    assertThat(top).isCloseTo(2.0 / 61, org.assertj.core.data.Offset.offset(1e-9));
  }

  @Test
  void chunkSearchKeepsSingleListLeadersAheadOfOverlap() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "vecTop", "1", "x", "정산", unitVector(0));              // 벡터 1위, 어휘에는 없음
    chunk(doc, "vecSecond", "2", "x", "출장", mixedVector(0, 1));       // 벡터 2위, 어휘에는 없음
    chunk(doc, "lexTop", "3", "x", "VPN 신청", unitVector(5));          // 어휘 1위
    chunk(doc, "overlap", "4", "x", "VPN", unitVector(6));              // 어휘 2위 + 벡터 3~4위 — RRF 점수는 벡터 1위보다 높다
    List<String> hits = jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, 'VPN 신청', CAST(? AS vector), 5)")
        .params(ws, unitVector(0)).query(String.class).list();
    assertThat(hits).containsExactly("lexTop", "vecTop", "vecSecond", "overlap");
  }

  @Test
  void chunkSearchScopeIsSelfPlusAncestorsAndActiveOnly() {
    long sibling = workspace(tenant, company, "HR", true);
    long companyDoc = document(company, "c", "C", "v1", "active");
    long siblingDoc = document(sibling, "s", "S", "v1", "active");
    long retiredDoc = document(ws, "r", "R", "v1", "retired");
    long ownDoc = document(ws, "o", "O", "v1", "active");
    chunk(companyDoc, "c-a1-0", "1", "x", "VPN", null);
    chunk(siblingDoc, "s-a1-0", "1", "x", "VPN", null);
    chunk(retiredDoc, "r-a1-0", "1", "x", "VPN", null);
    chunk(ownDoc, "o-a1-0", "1", "x", "VPN", null);
    List<String> hits = jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, 'VPN', NULL, 10)").param(ws).query(String.class).list();
    assertThat(hits).containsExactlyInAnyOrder("c-a1-0", "o-a1-0");
  }

  @Test
  void chunkSearchReturnsNothingWithoutQuery() {
    long doc = document(ws, "d1", "D", "v1", "active");
    chunk(doc, "d1-a1-0", "1", "x", "VPN", null);
    assertThat(jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, NULL, NULL, 5)").param(ws).query(String.class).list()).isEmpty();
    assertThat(jdbc.sql("SELECT chunk_id FROM hybrid_search_chunks(?, '', NULL, 5)").param(ws).query(String.class).list()).isEmpty();
  }

  // ── resolve_policy_ref ─────────────────────────────────────────────────

  @Test
  void policyRefFollowsRevisionChain() {
    long old = document(ws, "ops-1", "운영지침", "v1", "retired");
    chunk(old, "ops-1-a9-1", "제9조 (커넥션 풀)", "풀 최대 20", "풀", unitVector(0));
    chunk(old, "ops-1-a10-1", "제10조 (컨테이너 자원)", "힙 70%", "힙", unitVector(1));
    chunk(old, "ops-1-a11-1", "제11조 (로그 보관)", "90일 보관", "로그", unitVector(2));
    long cur = document(ws, "ops-2", "운영지침", "v2", "active");
    chunk(cur, "ops-2-a9-1", "제9조 (커넥션 풀)", "풀  최대 20", "풀", unitVector(0));          // 공백만 다르다 → 그대로
    chunk(cur, "ops-2-a10-1", "제10조 (컨테이너 자원)", "힙 65%", "힙", unitVector(1));         // 내용이 바뀜
    chunk(cur, "ops-2-a11-1", "제11조 (배포 창)", "화~목", "배포", unitVector(5));              // 같은 번호, 다른 조문
    chunk(cur, "ops-2-a12-1", "제12조 (로그 보관 기간)", "180일 보관", "로그", unitVector(2));   // 옛 제11조와 내용이 가장 가깝다
    jdbc.sql("UPDATE policy_documents SET superseded_by = ? WHERE id = ?").params(cur, old).update();

    assertThat(jdbc.sql("SELECT policy_successor(?)").param(old).query(Long.class).single()).isEqualTo(cur);
    assertThat(resolve("ops-2", "제9조")).containsEntry("status", "current").containsEntry("chunk_id", "ops-2-a9-1");
    assertThat(resolve("ops-1", "제9조")).containsEntry("status", "same").containsEntry("chunk_id", "ops-2-a9-1").containsEntry("from_version", "v1");
    assertThat(resolve("ops-1", "제10조")).containsEntry("status", "amended").containsEntry("chunk_id", "ops-2-a10-1").containsEntry("version", "v2");
    Map<String, Object> gone = resolve("ops-1", "제11조");
    assertThat(gone).containsEntry("status", "replaced").containsEntry("from_label", "제11조 (로그 보관)")
        .containsEntry("suggested_label", "제12조 (로그 보관 기간)").containsEntry("slug", "ops-2");
    assertThat(gone.get("chunk_id")).isNull();
    // 원래 문서에 없는 조문은 행이 없다. '제1조' 는 '제10조' 에 걸리지 않는다
    assertThat(jdbc.sql("SELECT count(*) FROM resolve_policy_ref(?, 'ops-1', '제1조')").param(ws).query(Long.class).single()).isZero();
    // 대체 없이 폐기된 문서
    long dead = document(ws, "dead-1", "폐지 지침", "v1", "retired");
    chunk(dead, "dead-1-a1-1", "제1조 (목적)", "x", "x", null);
    assertThat(resolve("dead-1", "제1조")).containsEntry("status", "retired");
    // 다른 부서의 규정은 보이지 않는다
    long sibling = workspace(tenant, company, "HR", true);
    assertThat(jdbc.sql("SELECT status FROM resolve_policy_ref(?, 'ops-1', '제9조')").param(sibling).query(String.class).single()).isEqualTo("retired");
  }

  private Map<String, Object> resolve(String slug, String article) {
    return jdbc.sql("SELECT * FROM resolve_policy_ref(?, ?, ?)").params(ws, slug, article).query().singleRow();
  }

  // ── hybrid_search_nodes ────────────────────────────────────────────────

  @Test
  void nodeSearchExactErrorCodeOutranksEverything() {
    long exact = node("fail", "E-403 오류", "E-403");
    long near = node("fail", "E-404 오류", "E-404");
    jdbc.sql("UPDATE log_nodes SET tokens = 'VPN 인증 오류 토큰' WHERE id = ?").param(near).update();
    jdbc.sql("UPDATE log_nodes SET tokens = '기타' WHERE id = ?").param(exact).update();
    List<Long> hits = jdbc.sql("SELECT node_id FROM hybrid_search_nodes(?, 'VPN 인증 오류', NULL, 'E-403', 5)").param(ws).query(Long.class).list();
    assertThat(hits).containsExactly(exact, near); // 어휘로는 near 가 우세하지만 정확 일치가 이긴다
  }

  @Test
  void nodeSearchKeywordOverlapRanksBetweenExactAndLexical() {
    long exact = node("fail", "정확", "E-1");
    long kw2 = node("fail", "키워드2", null);
    long kw1 = node("fail", "키워드1", null);
    long lex = node("fail", "어휘", null);
    jdbc.sql("UPDATE log_nodes SET keywords = ARRAY['VPN','토큰 갱신'] WHERE id = ?").param(kw2).update();
    jdbc.sql("UPDATE log_nodes SET keywords = ARRAY['vpn'] WHERE id = ?").param(kw1).update();
    jdbc.sql("UPDATE log_nodes SET tokens = 'VPN 접속 오류' WHERE id = ?").param(lex).update();
    List<Long> hits = jdbc.sql("SELECT node_id FROM hybrid_search_nodes(?, 'VPN 토큰 갱신', NULL, 'E-1', 10)").param(ws).query(Long.class).list();
    // 정확 일치(+1.0) ≥ 키워드 겹침(+0.5/개, 대소문자 무시; 동점은 id 순) > 어휘만(1/61)
    // kw2 는 'VPN' + 복합어 '토큰 갱신'(어절 전부가 질의 토큰에 포함) 2개 → +1.0 으로 exact 와 동점, id 순으로 exact 가 먼저
    assertThat(hits).containsExactly(exact, kw2, kw1, lex);
    List<Double> scores = jdbc.sql("SELECT score FROM hybrid_search_nodes(?, 'VPN 토큰 갱신', NULL, 'E-1', 10)").param(ws).query(Double.class).list();
    assertThat(scores.get(0)).isEqualTo(scores.get(1)).isEqualTo(1.0);
    assertThat(scores.get(2)).isEqualTo(0.5);
    // 복합어 일부만 맞으면 부스팅 없음
    assertThat(jdbc.sql("SELECT score FROM hybrid_search_nodes(?, '갱신', NULL, NULL, 10)").param(ws).query(Double.class).list()).isEmpty();
  }

  @Test
  void nodeSearchExcludesDeprecatedAndOtherWorkspaces() {
    long active = node("fail", "A", "E-1");
    long dep = node("fail", "B", "E-1");
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(dep).update();
    long other = workspace(tenant, company, "HR", true);
    node(other, "fail", "C", "E-1");
    List<Long> hits = jdbc.sql("SELECT node_id FROM hybrid_search_nodes(?, NULL, NULL, 'E-1', 5)").param(ws).query(Long.class).list();
    assertThat(hits).containsExactly(active);
  }

  // ── search_case_entry ──────────────────────────────────────────────────

  @Test
  void caseEntryRanksRareWordsAboveCommonOnesAndReturnsEvidence() {
    long common1 = node("fail", "흔한1", null);
    long common2 = node("fail", "흔한2", null);
    long rare = node("fail", "드문", null);
    long exact = node("fail", "코드", "E-9");
    jdbc.sql("UPDATE log_nodes SET tokens = '오류 오류 오류 발생' WHERE id IN (?, ?)").params(common1, common2).update();
    jdbc.sql("UPDATE log_nodes SET tokens = '서킷브레이커 오류' WHERE id = ?").param(rare).update();
    jdbc.sql("UPDATE log_nodes SET tokens = '무관' WHERE id = ?").param(exact).update();
    // '오류'는 셋 다 갖고 '서킷브레이커'는 하나만 — 빈도가 아니라 드문 단어가 순위를 정한다
    var rows = jdbc.sql("SELECT node_id, exact, lex_terms, distance FROM search_case_entry(?, '서킷브레이커 오류', NULL, NULL, 5)").param(ws)
        .query((rs, i) -> new Object[] {rs.getLong(1), rs.getBoolean(2), rs.getInt(3), rs.getObject(4)}).list();
    assertThat(rows).hasSize(3);
    assertThat(rows.get(0)[0]).isEqualTo(rare);
    assertThat(rows.get(0)[2]).isEqualTo(2);
    assertThat(rows.get(1)[2]).isEqualTo(1);
    assertThat(rows.get(0)[1]).isEqualTo(false);
    assertThat(rows.get(0)[3]).as("벡터 없이 부르면 거리는 null").isNull();
    // 에러 코드 정확 일치는 어휘가 안 겹쳐도 맨 위, exact 로 표시
    var withCode = jdbc.sql("SELECT node_id, exact FROM search_case_entry(?, '서킷브레이커 오류', NULL, 'E-9', 5)").param(ws)
        .query((rs, i) -> new Object[] {rs.getLong(1), rs.getBoolean(2)}).list();
    assertThat(withCode.get(0)[0]).isEqualTo(exact);
    assertThat(withCode.get(0)[1]).isEqualTo(true);
    assertThat(withCode.get(1)[1]).isEqualTo(false);
  }

  @Test
  void caseEntryExcludesDeprecatedAndOtherWorkspaces() {
    long active = node("fail", "A", "E-1");
    long dep = node("fail", "B", "E-1");
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(dep).update();
    long other = workspace(tenant, company, "HR", true);
    node(other, "fail", "C", "E-1");
    List<Long> hits = jdbc.sql("SELECT node_id FROM search_case_entry(?, NULL, NULL, 'E-1', 5)").param(ws).query(Long.class).list();
    assertThat(hits).containsExactly(active);
  }

  // ── get_chain / would_create_cycle / orphaned_descendants ──────────────

  @Test
  void chainReturnsAncestorsNegativeAndDescendantsPositive() {
    long fail = node("fail", "E-403", "E-403");
    long cause = node("ref", "원인", null);
    long fix = node("success", "해결", null);
    edge(fail, cause);
    edge(cause, fix);
    List<Map<String, Object>> chain = jdbc.sql("SELECT node_id, depth FROM get_chain(?, ?, 3, 30)").params(ws, cause).query().listOfRows();
    assertThat(chain).hasSize(3);
    assertThat(depthOf(chain, fail)).isEqualTo(-1);
    assertThat(depthOf(chain, cause)).isEqualTo(0);
    assertThat(depthOf(chain, fix)).isEqualTo(1);
  }

  @Test
  void chainFillsNearestFirstWhenNodeCapExceeded() {
    long root = node("fail", "root", null);
    long prev = root;
    for (int i = 0; i < 5; i++) { // root → c1 → c2 → c3 → c4 → c5 직선
      long c = node("ref", "c" + i, null);
      edge(prev, c);
      prev = c;
    }
    List<Map<String, Object>> full = jdbc.sql("SELECT node_id, depth FROM get_chain(?, ?, 5, 30)").params(ws, root).query().listOfRows();
    assertThat(full).hasSize(6);
    List<Map<String, Object>> capped = jdbc.sql("SELECT node_id, depth FROM get_chain(?, ?, 5, 3)").params(ws, root).query().listOfRows();
    assertThat(capped).hasSize(3); // 해결 · 원인 표시가 없으면 가까운 순: root + 2단계
    assertThat(capped.stream().map(r -> ((Number) r.get("depth")).intValue())).containsExactlyInAnyOrder(0, 1, 2);
  }

  @Test
  void deepChainHasNoDepthCap() {
    long root = node("fail", "root", null);
    long[] line = line(root, 40, "fail");
    long bottom = line[39];
    Map<String, Object> row = jdbc.sql("SELECT root_id, depth, path_label FROM v_log_nodes WHERE id = ?").param(bottom).query().singleRow();
    assertThat(row.get("root_id")).isEqualTo(root);
    assertThat(row.get("depth")).isEqualTo(40);
    assertThat(((String) row.get("path_label")).split("\\.")).hasSize(42); // root + 루트 노드 + 40단
    assertThat(jdbc.sql("SELECT count(*) FROM get_chain(?, ?, NULL, 100, ?)").params(ws, root, root).query(Long.class).single()).isEqualTo(41);
    // 40단 아래 해결 기록도 해결로 인식한다
    long fix = node("success", "fix", null);
    edge(bottom, fix);
    jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)").params(ws, root, fix).update();
    assertThat(jdbc.sql("SELECT resolved_nodes(?, ?)").params(ws, root).query(Long.class).list()).containsExactly(fix);
  }

  @Test
  void wideChainOverCapKeepsFocusResolutionAndCausePaths() {
    long root = node("fail", "root", null);
    long[] kids = new long[12];
    long focus = 0, fix = 0, cause = 0;
    for (int i = 0; i < 12; i++) {
      kids[i] = node("fail", "k" + i, null);
      edge(root, kids[i]);
      for (int j = 0; j < 3; j++) {
        long g = node(i == 7 && j == 2 ? "success" : "ref", "g" + i + "-" + j, null);
        edge(kids[i], g);
        if (i == 7 && j == 2) fix = g;
        if (i == 11 && j == 2) focus = g;
      }
    }
    cause = kids[9];
    long causeChild = node("ref", "cause detail", null);
    edge(cause, causeChild);
    jdbc.sql("UPDATE log_nodes SET category = 'cause' WHERE id = ?").param(cause).update();
    jdbc.sql("INSERT INTO log_resolutions (workspace_id, root_id, node_id) VALUES (?, ?, ?)").params(ws, root, fix).update();
    List<Map<String, Object>> rows = jdbc.sql("SELECT node_id, depth, total FROM get_chain(?, ?, NULL, 10, ?)").params(ws, root, focus).query().listOfRows();
    List<Long> ids = rows.stream().map(r -> ((Number) r.get("node_id")).longValue()).toList();
    assertThat(rows).hasSize(10);
    assertThat(((Number) rows.get(0).get("total")).intValue()).isEqualTo(50);
    // 검색으로 찾은 기록과 그 위, 해결 기록과 그 위, 원인 — 깊이를 깎았다면 2단의 focus · fix 가 빠진다
    assertThat(ids).contains(root, kids[11], focus, kids[7], fix, cause);
  }

  @Test
  void stackedDiamondsAreReadWithoutPathExplosion() {
    long top = node("fail", "top", null);
    long cur = top;
    for (int i = 0; i < 16; i++) { // 경로 2^16 = 65,536 개
      long a = node("ref", "a" + i, null), b = node("ref", "b" + i, null), m = node("ref", "m" + i, null);
      edge(cur, a); edge(cur, b); edge(a, m); edge(b, m);
      cur = m;
    }
    long t0 = System.nanoTime();
    Map<String, Object> row = jdbc.sql("SELECT root_id, depth FROM v_log_nodes WHERE id = ?").param(cur).query().singleRow();
    long ms = (System.nanoTime() - t0) / 1_000_000;
    assertThat(row.get("root_id")).isEqualTo(top);
    assertThat(row.get("depth")).isEqualTo(32);
    assertThat(ms).isLessThan(1000); // 경로를 모두 펼치던 이전 뷰는 수 초
    assertThat(jdbc.sql("SELECT count(*) FROM get_chain(?, ?, NULL, 100, ?)").params(ws, top, top).query(Long.class).single()).isEqualTo(49);
  }

  @Test
  void cycleIsRejectedAtAnyDepthAlsoByTrigger() {
    long root = node("fail", "root", null);
    long[] line = line(root, 70, "ref");
    assertThat(cycle(line[69], root)).isTrue(); // 이전 검사는 64단까지만 내려갔다
    assertFails(() -> edge(line[69], root), "EDGE_CYCLE");
    assertFails(() -> edge(line[69], line[10]), "EDGE_CYCLE");
  }

  @Test
  void chainIsEmptyForUnknownOrForeignEntry() {
    long other = workspace(tenant, company, "HR", true);
    long n = node(other, "fail", "X", null);
    assertThat(jdbc.sql("SELECT node_id FROM get_chain(?, ?, 3, 30)").params(ws, n).query(Long.class).list()).isEmpty();
    assertThat(jdbc.sql("SELECT node_id FROM get_chain(?, 999999, 3, 30)").param(ws).query(Long.class).list()).isEmpty();
  }

  @Test
  void cycleDetection() {
    long a = node("fail", "A", null);
    long b = node("ref", "B", null);
    long c = node("ref", "C", null);
    edge(a, b);
    edge(b, c);
    assertThat(cycle(c, a)).isTrue();   // c → a 는 a→b→c→a
    assertThat(cycle(b, a)).isTrue();
    assertThat(cycle(a, a)).isTrue();
    assertThat(cycle(a, c)).isFalse();  // 지름길은 사이클 아님 (다중 경로 허용)
    long d = node("ref", "D", null);
    assertThat(cycle(d, a)).isFalse();
  }

  @Test
  void orphanedDescendantsRespectAlternativeActivePaths() {
    long fail = node("fail", "E", null);
    long c1 = node("ref", "c1", null);
    long c2 = node("ref", "c2", null);
    long onlyViaC1 = node("success", "f1", null);
    long shared = node("success", "f2", null);
    long deep = node("ref", "g", null);
    edge(fail, c1); edge(fail, c2);
    edge(c1, onlyViaC1); edge(c1, shared); edge(c2, shared);
    edge(onlyViaC1, deep);
    List<Long> orphans = jdbc.sql("SELECT orphaned_descendants(?)").param(c1).query(Long.class).list();
    assertThat(orphans).containsExactlyInAnyOrder(onlyViaC1, deep); // shared 는 c2 경로가 살아 있음
    // c2 가 이미 폐기되어 있으면 shared 도 고아가 된다
    jdbc.sql("UPDATE log_nodes SET status = 'deprecated' WHERE id = ?").param(c2).update();
    orphans = jdbc.sql("SELECT orphaned_descendants(?)").param(c1).query(Long.class).list();
    assertThat(orphans).containsExactlyInAnyOrder(onlyViaC1, deep, shared);
  }

  // ── purge_document ─────────────────────────────────────────────────────

  @Test
  void purgeOnlyRetiredAndWritesAudit() {
    long active = document(ws, "a", "A", "v1", "active");
    assertFails(() -> jdbc.sql("SELECT purge_document(?, 'tester')").param(active).query(String.class).single(), "NOT_RETIRED");
    long retired = document(ws, "r", "R", "v2", "retired");
    chunk(retired, "r-a1-0", "1", "x", null, null);
    jdbc.sql("SELECT purge_document(?, 'tester')").param(retired).query(String.class).optional();
    assertThat(jdbc.sql("SELECT count(*) FROM policy_documents WHERE id = ?").param(retired).query(Long.class).single()).isZero();
    assertThat(jdbc.sql("SELECT count(*) FROM policy_chunks WHERE doc_id = ?").param(retired).query(Long.class).single()).isZero();
    Map<String, Object> audit = jdbc.sql("SELECT action, actor, label, workspace_id, tenant_id FROM audit_log WHERE table_name = 'policy_documents' AND row_id = ?")
        .param(String.valueOf(retired)).query().singleRow();
    assertThat(audit.get("action")).isEqualTo("deleted");
    assertThat(audit.get("label")).isEqualTo("R v2");
    assertThat(audit.get("workspace_id")).isEqualTo(ws);
    assertThat(audit.get("tenant_id")).isEqualTo(tenant);
  }

  // ── views ──────────────────────────────────────────────────────────────

  @Test
  void logNodesViewComputesRootDepthPathAndAuthor() {
    long fail = node("fail", "E", null);
    long c1 = node("ref", "c1", null);
    long c2 = node("ref", "c2", null);
    long shared = node("success", "f", null);
    edge(fail, c1); edge(fail, c2); edge(c1, shared); edge(c2, shared);
    Map<String, Object> row = jdbc.sql("SELECT root_id, depth, path_label, author_name FROM v_log_nodes WHERE id = ?").param(shared).query().singleRow();
    assertThat(row.get("root_id")).isEqualTo(fail);
    assertThat(row.get("depth")).isEqualTo(2);
    assertThat(row.get("path_label")).isEqualTo("root.n" + fail + ".n" + c1 + ".n" + shared); // 첫 부모(작은 id) 경로
    assertThat(row.get("author_name")).isEqualTo("tester");
    Map<String, Object> rootRow = jdbc.sql("SELECT root_id, depth, path_label FROM v_log_nodes WHERE id = ?").param(fail).query().singleRow();
    assertThat(rootRow.get("root_id")).isEqualTo(fail);
    assertThat(rootRow.get("depth")).isEqualTo(0);
    assertThat(rootRow.get("path_label")).isEqualTo("root.n" + fail);
  }

  @Test
  void documentAndSessionViewsExposeDerivedFields() {
    long doc = document(ws, "d", "D", "v1", "active");
    chunk(doc, "d-a1-0", "1", "x", null, null);
    chunk(doc, "d-a1-1", "1", "y", null, null);
    Map<String, Object> d = jdbc.sql("SELECT chunk_count, has_file FROM v_policy_documents WHERE id = ?").param(doc).query().singleRow();
    assertThat(d.get("chunk_count")).isEqualTo(2);
    assertThat(d.get("has_file")).isEqualTo(false);

    long s = jdbc.sql("INSERT INTO search_sessions (workspace_id, user_id, query, mode) VALUES (?, ?, 'q', 'dag_only') RETURNING id").params(ws, user).query(Long.class).single();
    jdbc.sql("INSERT INTO answer_feedback (session_id, score) VALUES (?, -1)").param(s).update();
    Map<String, Object> v = jdbc.sql("SELECT user_name, score FROM v_search_sessions WHERE id = ?").param(s).query().singleRow();
    assertThat(v.get("user_name")).isEqualTo("tester");
    assertThat(((Number) v.get("score")).intValue()).isEqualTo(-1);
  }

  private long[] line(long from, int n, String type) {
    long[] out = new long[n];
    long prev = from;
    for (int i = 0; i < n; i++) {
      out[i] = node(type, "l" + i, null);
      edge(prev, out[i]);
      prev = out[i];
    }
    return out;
  }

  private boolean cycle(long parent, long child) {
    return jdbc.sql("SELECT would_create_cycle(?, ?)").params(parent, child).query(Boolean.class).single();
  }

  private static int depthOf(List<Map<String, Object>> rows, long nodeId) {
    return rows.stream().filter(r -> ((Number) r.get("node_id")).longValue() == nodeId).map(r -> ((Number) r.get("depth")).intValue()).findFirst().orElseThrow();
  }
}
