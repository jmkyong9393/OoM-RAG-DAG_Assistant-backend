package com.oom.assist.api;

import static org.assertj.core.api.Assertions.assertThat;
import static org.hamcrest.Matchers.hasSize;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** 사례의 근거 조문 입력: 조문 고르기, 등록·일괄 등록·수정, 검증(관계·조문 존재·부서 범위). */
class LogPolicyRefApiTest extends AbstractApiTest {

  @BeforeEach
  void seedPolicies() {
    long ops = doc(wsSw, "ops-2026", "운영지침", "active");
    chunk(ops, "ops-2026-a9-1", "제9조 (커넥션 풀)");
    chunk(ops, "ops-2026-a9-2", "제9조 (커넥션 풀)");
    chunk(ops, "ops-2026-a10-1", "제10조 (컨테이너 자원)");
    chunk(ops, "ops-2026-s21-1", "[별표 2] 배포 전 점검표");
    long sec = doc(company, "sec-2026", "정보보안 기본규정", "active");
    chunk(sec, "sec-2026-a1-1", "제1조 (목적)");
    long old = doc(wsSw, "ops-2025", "운영지침", "retired");
    chunk(old, "ops-2025-a9-1", "제9조 (커넥션 풀)");
    long hr = doc(wsHr, "pay-2026", "급여 규정", "active");
    chunk(hr, "pay-2026-a3-1", "제3조 (지급일)");
  }

  private long doc(long ws, String slug, String title, String status) {
    return jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, ?, ?, '1', 'markdown', ?) RETURNING id")
        .params(ws, slug, title, status).query(Long.class).single();
  }

  private void chunk(long docId, String id, String label) {
    jdbc.sql("INSERT INTO policy_chunks (id, doc_id, article_label, text, tokens) VALUES (?, ?, ?, '본문', '본문')").params(id, docId, label).update();
  }

  @Test
  void articlePickerListsVisibleActiveArticlesOnce() throws Exception {
    // 자기 부서 + 전사 공통의 활성 문서만, 조문마다 한 줄. 폐기 문서·다른 부서 문서는 나오지 않는다
    mvc.perform(as(get("/logs/policy-articles"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(4)))
        .andExpect(jsonPath("$[?(@.policy == 'ops-2026' && @.article == '제9조')]", hasSize(1)))
        .andExpect(jsonPath("$[?(@.policy == 'ops-2026' && @.article == '별표 2')].article_label").value("[별표 2] 배포 전 점검표"))
        .andExpect(jsonPath("$[?(@.policy == 'sec-2026')].doc_title").value("정보보안 기본규정"))
        .andExpect(jsonPath("$[?(@.policy == 'pay-2026')]", hasSize(0))).andExpect(jsonPath("$[?(@.policy == 'ops-2025')]", hasSize(0)));
    mvc.perform(as(get("/logs/policy-articles").param("q", "운영지침 컨테이너"), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$", hasSize(1))).andExpect(jsonPath("$[0].article").value("제10조"));
  }

  @Test
  void createWithPolicyRefsValidatesBeforeCreating() throws Exception {
    String ok = """
        {"type": "fail", "title": "풀 고갈", "body": "풀 20 으로 조정", "policy_refs": [
          {"relation": "follows", "policy": "ops-2026", "article": "제9조", "note": "풀 20"},
          {"relation": "gap", "topic": "합산 상한"}]}""";
    String body = mvc.perform(as(post("/logs"), userLead, wsSw).content(ok)).andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
    long id = com.jayway.jsonpath.JsonPath.parse(body).read("$.id", Long.class);
    mvc.perform(as(get("/logs/" + id + "/policy-refs"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(2)))
        .andExpect(jsonPath("$[0].relation").value("follows")).andExpect(jsonPath("$[0].doc_title").value("운영지침"))
        .andExpect(jsonPath("$[0].article_label").value("제9조 (커넥션 풀)")).andExpect(jsonPath("$[0].note").value("풀 20"))
        .andExpect(jsonPath("$[1].relation").value("gap")).andExpect(jsonPath("$[1].topic").value("합산 상한"));
    // 체인 조회(타임라인)의 노드에도 같은 근거가 실린다. 근거가 없는 노드는 null
    mvc.perform(as(get("/chains/" + id), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$.nodes[0].policy_refs", hasSize(2))).andExpect(jsonPath("$.nodes[0].policy_refs[0].article_label").value("제9조 (커넥션 풀)"))
        .andExpect(jsonPath("$.nodes[0].policy_refs[1].relation").value("gap"));
    // 저장 형태는 적재 스크립트(load_alignment.py)와 같다
    assertThat(jdbc.sql("SELECT attributes->'policy_refs'->0->>'article' FROM log_nodes WHERE id = ?").param(id).query(String.class).single()).isEqualTo("제9조");

    long before = jdbc.sql("SELECT count(*) FROM log_nodes").query(Long.class).single();
    // 없는 조문·'제1조' 로 '제10조' 를 가리키기·다른 부서 문서·폐기 문서·잘못된 관계·주제 없는 gap → 400, 로그는 만들어지지 않는다
    for (String bad : new String[] {
        "{\"relation\": \"follows\", \"policy\": \"ops-2026\", \"article\": \"제99조\"}",
        "{\"relation\": \"follows\", \"policy\": \"ops-2026\", \"article\": \"제1조\"}",
        "{\"relation\": \"follows\", \"policy\": \"pay-2026\", \"article\": \"제3조\"}",
        "{\"relation\": \"exception\", \"policy\": \"ops-2025\", \"article\": \"제9조\"}",
        "{\"relation\": \"same\", \"policy\": \"ops-2026\", \"article\": \"제9조\"}",
        "{\"relation\": \"gap\"}"}) {
      mvc.perform(as(post("/logs"), userLead, wsSw).content("{\"type\": \"fail\", \"title\": \"t\", \"body\": \"b\", \"policy_refs\": [" + bad + "]}"))
          .andExpect(status().isBadRequest());
    }
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes").query(Long.class).single()).isEqualTo(before);
  }

  @Test
  void revisionKeepsLinksAndListsAffectedCases() throws Exception {
    String body = mvc.perform(as(post("/logs"), userLead, wsSw).content("""
        {"type": "fail", "title": "풀 고갈", "body": "b", "policy_refs": [
          {"relation": "follows", "policy": "ops-2026", "article": "제9조"},
          {"relation": "exception", "policy": "ops-2026", "article": "제10조", "note": "당시 75%"},
          {"relation": "follows", "policy": "ops-2026", "article": "별표 2"}]}""")).andExpect(status().isCreated()).andReturn().getResponse().getContentAsString();
    long id = com.jayway.jsonpath.JsonPath.parse(body).read("$.id", Long.class);
    // 개정판: 제9조는 그대로, 제10조는 내용이 바뀌고, 별표 2 는 없어졌다(점검표가 제11조로 옮겨짐)
    long next = jdbc.sql("INSERT INTO policy_documents (workspace_id, slug, title, version, source_type, status) VALUES (?, 'ops-2027', '운영지침', '2', 'markdown', 'active') RETURNING id")
        .param(wsSw).query(Long.class).single();
    chunk(next, "ops-2027-a9-1", "제9조 (커넥션 풀)");
    chunk(next, "ops-2027-a9-2", "제9조 (커넥션 풀)");
    jdbc.sql("INSERT INTO policy_chunks (id, doc_id, article_label, text, tokens) VALUES ('ops-2027-a10-1', ?, '제10조 (컨테이너 자원)', '바뀐 본문', '본문')").param(next).update();
    chunk(next, "ops-2027-a11-1", "제11조 (배포 전 점검)");
    jdbc.sql("UPDATE policy_documents SET status = 'retired', superseded_by = ? WHERE slug = 'ops-2026'").param(next).update();

    mvc.perform(as(get("/logs/" + id + "/policy-refs"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(3)))
        .andExpect(jsonPath("$[0].article_label").value("제9조 (커넥션 풀)")).andExpect(jsonPath("$[0].revision").isEmpty())
        .andExpect(jsonPath("$[0].from_label").value("운영지침 1 제9조 (커넥션 풀)"))
        .andExpect(jsonPath("$[1].revision").value("amended")).andExpect(jsonPath("$[1].article_label").value("제10조 (컨테이너 자원)"))
        .andExpect(jsonPath("$[2].revision").value("replaced")).andExpect(jsonPath("$[2].article_label").isEmpty())
        .andExpect(jsonPath("$[2].from_label").value("운영지침 1 [별표 2] 배포 전 점검표"));
    mvc.perform(as(get("/policy-revisions/impact"), userLead, wsSw)).andExpect(status().isOk())
        // 요약은 사례 수다 — 한 사례가 여러 조문을 가리키면 가장 나쁜 상태로 한 번만 센다. 목록(items)은 조문마다 한 줄
        .andExpect(jsonPath("$.cases").value(1)).andExpect(jsonPath("$.summary.same").value(0))
        .andExpect(jsonPath("$.summary.amended").value(0)).andExpect(jsonPath("$.summary.replaced").value(1))
        .andExpect(jsonPath("$.items", hasSize(3))).andExpect(jsonPath("$.items[0].status").value("replaced"))
        .andExpect(jsonPath("$.items[0].node_id").value(id)).andExpect(jsonPath("$.items[0].current_policy").value("ops-2027"))
        .andExpect(jsonPath("$.items[1].status").value("amended")).andExpect(jsonPath("$.items[1].current_label").value("제10조 (컨테이너 자원)"))
        .andExpect(jsonPath("$.items[1].note").value("당시 75%"));
    // 다른 부서에는 이 부서의 사례가 보이지 않는다
    mvc.perform(as(get("/policy-revisions/impact"), userBackend, wsHr)).andExpect(status().isOk()).andExpect(jsonPath("$.cases").value(0));
    // 저장돼 있던 근거는 개정 뒤에도 그대로 다시 저장할 수 있다(메모만 고칠 때). 개정 전 문서의 조문을 새로 붙이는 것은 안 된다
    mvc.perform(as(put("/logs/" + id + "/policy-refs"), userLead, wsSw).content("""
        {"policy_refs": [
          {"relation": "follows", "policy": "ops-2026", "article": "제9조", "note": "메모 추가"},
          {"relation": "exception", "policy": "ops-2026", "article": "제10조", "note": "당시 75%"},
          {"relation": "follows", "policy": "ops-2026", "article": "별표 2"}]}"""))
        .andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(3))).andExpect(jsonPath("$[0].note").value("메모 추가"));
    long other = node(wsSw, "fail", "다른 사례", userLead);
    mvc.perform(as(put("/logs/" + other + "/policy-refs"), userLead, wsSw)
            .content("{\"policy_refs\": [{\"relation\": \"follows\", \"policy\": \"ops-2026\", \"article\": \"제9조\"}]}"))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("POLICY_REF_NOT_FOUND"));
    // 다시 연결하면 영향 목록에서 빠진다
    mvc.perform(as(put("/logs/" + id + "/policy-refs"), userLead, wsSw)
            .content("{\"policy_refs\": [{\"relation\": \"follows\", \"policy\": \"ops-2027\", \"article\": \"제11조\"}]}"))
        .andExpect(status().isOk());
    mvc.perform(as(get("/policy-revisions/impact"), userLead, wsSw)).andExpect(status().isOk()).andExpect(jsonPath("$.cases").value(0));
  }

  @Test
  void faqItemsArePickedAndLinkedLikeArticles() throws Exception {
    long faq = doc(wsSw, "faq-2026", "계정·보안 FAQ", "active");
    chunk(faq, "faq-2026-q3-1", "Q3. 인증번호가 오지 않아요");
    chunk(faq, "faq-2026-q30-1", "Q30. 탈퇴하면 기록은 어떻게 되나요");
    mvc.perform(as(get("/logs/policy-articles").param("q", "FAQ"), userLead, wsSw)).andExpect(status().isOk())
        .andExpect(jsonPath("$", hasSize(2))).andExpect(jsonPath("$[?(@.article == 'Q3')]", hasSize(1))).andExpect(jsonPath("$[?(@.article == 'Q30')]", hasSize(1)));
    long n = node(wsSw, "success", "인증번호 재발송", userLead);
    mvc.perform(as(put("/logs/" + n + "/policy-refs"), userLead, wsSw)
            .content("{\"policy_refs\": [{\"relation\": \"follows\", \"policy\": \"faq-2026\", \"article\": \"Q3\"}]}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$[0].article_label").value("Q3. 인증번호가 오지 않아요"));
  }

  @Test
  void putReplacesRefsAndBulkCarriesThem() throws Exception {
    long n = node(wsSw, "fail", "힙 부족", userLead);
    mvc.perform(as(put("/logs/" + n + "/policy-refs"), userBackend, wsSw)
            .content("{\"policy_refs\": [{\"relation\": \"exception\", \"policy\": \"ops-2026\", \"article\": \"제10조\", \"note\": \"당시 75%\"}]}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(1))).andExpect(jsonPath("$[0].relation").value("exception"));
    mvc.perform(as(put("/logs/" + n + "/policy-refs"), userBackend, wsSw).content("{\"policy_refs\": []}"))
        .andExpect(status().isOk()).andExpect(jsonPath("$", hasSize(0)));
    mvc.perform(as(put("/logs/" + n + "/policy-refs"), userBackend, wsSw).content("{}")).andExpect(status().isBadRequest());
    // 다른 부서의 로그는 보이지 않는다
    mvc.perform(as(put("/logs/" + n + "/policy-refs"), userBackend, wsHr).content("{\"policy_refs\": []}")).andExpect(status().isNotFound());
    assertThat(jdbc.sql("SELECT count(*) FROM audit_log WHERE action = 'policy_refs_set' AND row_id = ?").param(String.valueOf(n)).query(Long.class).single()).isEqualTo(2L);

    String bulk = """
        {"items": [
          {"ref": "a", "type": "fail", "title": "풀 고갈", "body": "b", "source_ref": "t-1",
           "policy_refs": [{"relation": "follows", "policy": "sec-2026", "article": "제1조"}]},
          {"ref": "b", "type": "success", "title": "해결", "body": "b", "parent_ref": "a", "source_ref": "t-2"}]}""";
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content(bulk)).andExpect(status().isCreated()).andExpect(jsonPath("$.created").value(2));
    assertThat(jdbc.sql("SELECT attributes->'policy_refs'->0->>'policy' FROM log_nodes WHERE source_ref = 't-1'").query(String.class).single()).isEqualTo("sec-2026");
    assertThat(jdbc.sql("SELECT attributes->'policy_refs' IS NULL FROM log_nodes WHERE source_ref = 't-2'").query(Boolean.class).single()).isTrue();
    // 일괄 등록 중 하나라도 근거 조문이 틀리면 전체가 취소된다
    mvc.perform(as(post("/logs/bulk"), userLead, wsSw).content(bulk.replace("t-1", "t-3").replace("t-2", "t-4").replace("제1조", "제7조")))
        .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("POLICY_REF_NOT_FOUND"));
    assertThat(jdbc.sql("SELECT count(*) FROM log_nodes WHERE source_ref IN ('t-3', 't-4')").query(Long.class).single()).isZero();
  }
}
